"""다중 프로세스 경합 — **승격한 구성의 미검증분.**

`architecture.md` §11 은 "API 워커 N 개 + 스케줄러 싱글턴" 을 지원 구성으로 선언한다.
그 계약(CAS · `_tx()` · 스코프)은 지금까지 **단일 프로세스 테스트로만** 검증됐고,
그 사실 자체가 부채 표에 있었다. 이 파일이 그것을 해소한다.

**스레드가 아니라 진짜 프로세스를 쓴다.** 요지가 "프로세스 안의 락은 프로세스를 넘지
못한다" 이므로, 스레드로 재면 재는 대상이 사라진다.

여기서 확인하는 것 셋:

1. 잡 상태 전이가 **정확히 한 번만** 이긴다(CAS)
2. `_tx()` 의 롤백이 **다른 프로세스에서도** 성립한다
3. `cluster.place()` 의 슬롯 장부가 **프로세스 로컬이다** — 문서가 한계로 적어 둔
   그 사실을 실행 가능한 형태로 못박는다. 언젠가 예약을 DB 로 옮기면 3번이
   실패하고, 그때 부채 표를 함께 고치라는 신호가 된다.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3

import pytest

from app.store import SqliteStore, TenantScope

ACME = TenantScope("acme")

#: 경합에 쓸 프로세스 수. 늘려도 결론은 같고 CI 시간만 는다.
WORKERS = 4
#: 워커가 안 끝나면 테스트를 매달아 두지 않는다.
JOIN_TIMEOUT = 30.0


# ── 워커 (spawn 이 임포트할 수 있게 모듈 최상단에 둔다) ──────────────────────


def _cas_worker(db_path: str, job_id: str, barrier, results) -> None:
    """같은 잡을 `queued → running` 으로 옮기려고 다툰다."""
    store = SqliteStore(db_path)
    try:
        barrier.wait()          # 진짜로 겹치게 만든다
        won = store.update_job(
            ACME, job_id, expect_status="queued", status="running",
        )
        results.put(("ok", bool(won)))
    except sqlite3.Error as exc:
        # 잠금 오류가 난다면 그것 자체가 다중 워커 구성의 결론이다.
        results.put(("sqlite_error", f"{type(exc).__name__}: {exc}"))
    except Exception as exc:                       # pragma: no cover - 진단용
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        store.close()


def _tx_rollback_worker(db_path: str, results) -> None:
    """`_tx()` 안에서 중간에 터진다. 앞선 쓰기가 남으면 안 된다."""
    store = SqliteStore(db_path)
    try:
        try:
            with store._tx():
                store._conn.execute(
                    "INSERT INTO services(tenant_id, id, name, status, allow_roles_json,"
                    " require_end_user, created_at) VALUES(?,?,?,?,?,?,?)",
                    ("acme", "half-written", "half", "active", "[]", 0, 0.0),
                )
                raise RuntimeError("트랜잭션 중간 실패")
        except RuntimeError:
            pass
        results.put(("done", True))
    except Exception as exc:                       # pragma: no cover - 진단용
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        store.close()


def _place_worker(db_path: str, barrier, results) -> None:
    """각 프로세스가 자기 클러스터를 만들어 **같은 노드의 슬롯 하나**를 잡는다."""
    from app.cluster import HEALTHY, PLACED, Cluster
    from app.config import (
        CatalogEntry, Config, GuardSettings, Lane, Node, Pricing, Role, Thresholds,
    )

    config = Config(
        nodes={
            "solo": Node(
                name="solo", provider="mock", data_boundary="internal",
                mem_budget_gb=40, max_concurrent=1, tags=("internal",), models=("m",),
            )
        },
        roles={"r": Role(name="r", model="m", placement=("internal",))},
        lanes={"interactive": Lane("interactive", 4)},
        guard_rules=(),
        guard_settings=GuardSettings(),
        pricing=Pricing(table={"mock": {"*": {"input_per_mtok": 0.0, "output_per_mtok": 0.0}}}),
        thresholds=Thresholds(),
        catalog=(CatalogEntry(name="m", provider="mock", est_size_gb=1.0),),
    )
    store = SqliteStore(db_path)
    try:
        cluster = Cluster(config, store)
        for state in cluster.nodes.values():
            state.models = frozenset(("m",))
            state.status = HEALTHY

        barrier.wait()
        result = cluster.place(
            job_id=f"j-{mp.current_process().name}",
            tenant_id="acme", service_id="acme-web",
            role=config.roles["r"], placement_snapshot=("internal",),
        )
        results.put(("ok", result.outcome == PLACED))
    except Exception as exc:                       # pragma: no cover - 진단용
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        store.close()


def _idempotency_worker(db_path: str, key: str, barrier, results) -> None:
    """같은 멱등성 키로 잡을 만들려고 다툰다. **하나만 살아야 한다.**"""
    store = SqliteStore(db_path)
    try:
        barrier.wait()
        try:
            job_id = store.create_job(
                ACME, service_id="acme-web", role="r", lane="interactive",
                kind="generate", status="queued", priority=0, prompt_masked="x",
                idempotency_key=key,
            )
            results.put(("created", job_id))
        except sqlite3.IntegrityError:
            # **정상 경로다.** 조회를 나란히 통과한 뒤 삽입에서 갈린다.
            results.put(("rejected", None))
    except Exception as exc:                       # pragma: no cover - 진단용
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        store.close()


def run_workers(target, db_path, *extra, count: int = WORKERS) -> list:
    """`spawn` 으로 워커를 띄우고 결과를 모은다.

    `fork` 를 안 쓰는 이유: 부모의 sqlite 커넥션과 pytest 상태가 자식에 복제되면
    측정하려는 것이 아니라 그 복제본의 동작을 재게 된다.
    """
    context = mp.get_context("spawn")
    barrier = context.Barrier(count)
    results = context.Queue()

    processes = [
        context.Process(target=target, args=(db_path, *extra, barrier, results))
        for _ in range(count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(JOIN_TIMEOUT)
        assert process.exitcode == 0, f"워커가 비정상 종료했다: {process.exitcode}"

    return [results.get() for _ in range(count)]


@pytest.fixture
def shared_db(tmp_path):
    """실제 파일. `:memory:` 는 프로세스를 넘지 못해 이 파일의 전제가 성립하지 않는다."""
    path = tmp_path / "contention.db"
    store = SqliteStore(path)
    store.create_tenant("acme", "Acme", end_user_salt=b"salt")
    store.create_service(ACME, "acme-web", "web")
    store.close()
    return str(path)


# ── 1. CAS 는 정확히 한 번 이긴다 ───────────────────────────────────────────


def test_only_one_process_wins_the_state_transition(shared_db):
    """**이것이 다중 워커 계약의 핵심이다.**

    검사와 갱신이 분리돼 있으면 "취소됨" 을 응답받은 잡이 실행되고 과금까지 간다.
    조건을 UPDATE 문 안에 넣으면 그 창이 없어지는데, 그 주장이 프로세스를 넘어
    성립하는지는 지금까지 검증된 적이 없었다.
    """
    store = SqliteStore(shared_db)
    job_id = store.create_job(
        ACME, service_id="acme-web", role="r", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="x",
    )
    store.close()

    outcomes = run_workers(_cas_worker, shared_db, job_id)

    failures = [o for kind, o in outcomes if kind != "ok"]
    assert not failures, f"워커가 오류를 냈다: {failures}"

    wins = [won for _, won in outcomes if won]
    assert len(wins) == 1, f"{WORKERS}개 프로세스 중 {len(wins)}개가 이겼다"

    verify = SqliteStore(shared_db)
    assert verify.job_status(ACME, job_id) == "running"
    verify.close()


def test_the_losers_do_not_corrupt_the_row(shared_db):
    """진 쪽이 조용히 덮어쓰면 이긴 쪽의 결정이 사라진다."""
    store = SqliteStore(shared_db)
    job_id = store.create_job(
        ACME, service_id="acme-web", role="r", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="x",
    )
    store.close()

    run_workers(_cas_worker, shared_db, job_id)

    verify = SqliteStore(shared_db)
    job = verify.get_job(ACME, job_id)
    verify.close()
    assert job.status == "running"
    assert job.tenant_id == "acme", "격리가 깨졌다"


# ── 2. 트랜잭션 롤백이 프로세스를 넘어 성립한다 ─────────────────────────────


def test_a_failed_transaction_leaves_nothing_behind(shared_db):
    """중간에 터진 다중 문장 쓰기가 **다른 프로세스에서도** 안 보여야 한다.

    이 저장소에 `rollback` 이 한 곳도 없던 시절에는, 실패한 쓰기가 열린 트랜잭션에
    남았다가 다음 무관한 commit 에 섞여 영속화됐다 — 파기 요청이 절반만 처리되고
    그 사실이 감사에도 안 남는다는 뜻이다.
    """
    context = mp.get_context("spawn")
    results = context.Queue()
    process = context.Process(target=_tx_rollback_worker, args=(shared_db, results))
    process.start()
    process.join(JOIN_TIMEOUT)
    assert process.exitcode == 0
    assert results.get() == ("done", True)

    verify = SqliteStore(shared_db)
    found = verify.get_service(ACME, "half-written")
    verify.close()
    assert found is None, "롤백됐어야 할 쓰기가 다른 프로세스에서 보인다"


# ── 3. 알려진 한계 — 슬롯 장부는 프로세스 로컬이다 ──────────────────────────


def test_the_slot_ledger_does_not_cross_processes(shared_db):
    """**부채 표의 그 항목을 실행 가능한 형태로 못박는다.**

    `cluster.place()` 는 `threading.Lock` 아래서 확인-후-차감을 원자화하는데, 그
    장부는 프로세스 메모리다. 비동기 경로는 스케줄러가 싱글턴이라 안전하지만
    **동기 경로 두 곳**(`/v1/embed` · 가드 2단 분류)은 API 워커에서 `place()` 를
    부르므로, 워커 N 개면 `max_concurrent=1` 노드에 N 건이 동시에 올라간다.

    그래서 이 테스트는 "고쳐야 할 버그" 가 아니라 **문서가 주장하는 한계가 실제로
    그러한지**를 잰다. 언젠가 예약을 DB 로 옮기면 여기가 실패하고, 그때 부채 표를
    함께 고치라는 신호가 된다.
    """
    outcomes = run_workers(_place_worker, shared_db)

    failures = [o for kind, o in outcomes if kind != "ok"]
    assert not failures, f"워커가 오류를 냈다: {failures}"

    placed = [ok for _, ok in outcomes if ok]
    assert len(placed) == WORKERS, (
        "슬롯 예약이 프로세스를 넘어 성립한다 — 예약이 DB 로 옮겨졌다면 "
        "architecture.md §11 과 README 부채 표를 함께 고쳐야 한다"
    )


def test_the_debt_table_still_admits_the_process_local_ledger():
    """위 테스트가 재는 사실이 **문서에도 적혀 있어야 한다.**

    코드의 한계와 문서의 한계가 갈리면 둘 중 하나는 거짓말이고, 설치처는 문서를
    읽는다.
    """
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    assert "프로세스 로컬" in readme, "슬롯 장부의 한계가 부채 표에서 사라졌다"


# ── 4. 멱등성은 DB 가 지킨다 ────────────────────────────────────────────────


def test_only_one_process_creates_the_job_for_a_key(shared_db):
    """**"먼저 조회하고 없으면 삽입" 은 다중 워커에서 반드시 진다.**

    두 워커가 조회를 나란히 통과하는 창이 실재하고, 애플리케이션 락으로 막으려 해도
    락이 프로세스를 넘지 못한다. 유일성은 DB 가 지켜야 하고, 진 쪽은 삽입 실패를
    **정상 경로로** 다뤄 이긴 쪽의 잡을 찾아간다.
    """
    outcomes = run_workers(_idempotency_worker, shared_db, "retry-1")

    errors = [o for kind, o in outcomes if kind == "error"]
    assert not errors, f"워커가 오류를 냈다: {errors}"

    created = [job for kind, job in outcomes if kind == "created"]
    assert len(created) == 1, f"{WORKERS}개 프로세스 중 {len(created)}개가 잡을 만들었다"

    verify = SqliteStore(shared_db)
    count = verify.get_job(ACME, created[0])
    rows = verify._conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE idempotency_key = ?", ("retry-1",)
    ).fetchone()["n"]
    verify.close()
    assert count is not None
    assert rows == 1, f"같은 키의 잡이 {rows}건 남았다"
