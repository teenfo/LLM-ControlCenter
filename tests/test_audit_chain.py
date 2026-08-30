"""감사 해시 체인 — **조작하면 드러난다. 조작을 막지는 못한다.**

그 구분이 이 파일의 절반이다. D10 의 판정은 이랬다: *"해시 체인은 '조작할 수 없다' 를
만드는 장치지, '조작하면 드러난다' 이상은 못 한다 — DB 를 쓸 수 있는 공격자는 체인
전체를 다시 계산할 수 있다. 진짜 무결성은 밖으로 밀어내는 것에서 나오므로, 체인만
넣고 컴플라이언스를 주장하면 과장이다."*

그래서 여기서는 체인이 잡는 것뿐 아니라 **체인이 못 잡는 것**도 테스트한다. 못 잡는
것을 테스트로 박아 두지 않으면 다음 사람이 이 기능을 실제보다 세게 설명한다.

나머지 절반은 정상 운영이 사고로 보이지 않게 하는 것이다. 보존 정리와 합치기는 둘 다
행을 지우거나 고치는 정상 동작인데, 순진하게 만들면 그 둘이 매번 "변조" 로 신고된다 —
**정상 운영을 사고로 신고하는 검증은 곧 꺼진다.**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.store import (
    AUDIT_GENESIS,
    AUDIT_RETENTION_DAYS,
    SqliteStore,
    TenantScope,
    audit_row_hash,
)
from tests.conftest import FakeClock

ACME = TenantScope("acme")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock) -> SqliteStore:
    store = SqliteStore(tmp_path / "audit.db", now=clock)
    yield store
    store.close()


def rows(store: SqliteStore) -> list:
    return list(store._conn.execute("SELECT * FROM admin_audit ORDER BY id"))


# ── 체인이 만들어지는가 ─────────────────────────────────────────────────────


def test_each_row_links_to_the_one_before_it(store):
    for n in range(3):
        store.audit("admin", "purge_tenant", tenant_id="acme", target=f"t{n}")

    chain = rows(store)
    assert chain[0]["prev_hash"] == AUDIT_GENESIS
    assert chain[1]["prev_hash"] == chain[0]["row_hash"]
    assert chain[2]["prev_hash"] == chain[1]["row_hash"]
    assert store.verify_audit_chain()["ok"]


def test_the_first_row_is_not_null_linked(store):
    """**NULL 은 서로 다르다** — 첫 행을 NULL 로 두면 유일 인덱스가 포크를 못 막는다."""
    store.audit("admin", "login")

    assert rows(store)[0]["prev_hash"] == AUDIT_GENESIS


# ── 조작이 드러나는가 ───────────────────────────────────────────────────────


def test_editing_a_row_is_detected(store):
    """**이 파일에서 가장 중요한 단언이다.**"""
    for n in range(3):
        store.audit("admin", "purge_tenant", target=f"t{n}")

    store._conn.execute("UPDATE admin_audit SET actor = '다른사람' WHERE id = 2")
    store._conn.commit()

    verdict = store.verify_audit_chain()
    assert not verdict["ok"]
    assert verdict["broken_at"]["id"] == 2
    assert "해시와 다릅니다" in verdict["reason"]


def test_deleting_a_row_is_detected(store):
    """지우는 것이 가장 흔한 조작이다 — 불리한 한 줄을 없앤다."""
    for n in range(3):
        store.audit("admin", "purge_tenant", target=f"t{n}")

    store._conn.execute("DELETE FROM admin_audit WHERE id = 2")
    store._conn.commit()

    verdict = store.verify_audit_chain()
    assert not verdict["ok"]
    assert verdict["broken_at"]["id"] == 3
    assert "앞 고리가 끊겼습니다" in verdict["reason"]


def test_changing_the_detail_is_detected(store):
    """행위는 남기고 내용만 바꾸는 것 — 로그를 읽는 사람에게 가장 안 보이는 조작이다."""
    store.audit("admin", "export_tenant", detail={"rows": 10})
    store._conn.execute(
        "UPDATE admin_audit SET detail_json = ? WHERE id = 1", (json.dumps({"rows": 1}),)
    )
    store._conn.commit()

    assert not store.verify_audit_chain()["ok"]


def test_the_verdict_names_the_spot_not_the_count(store):
    """"체인이 깨졌습니다" 만으로는 아무도 다음 행동을 못 정한다."""
    for n in range(4):
        store.audit(f"admin{n}", "purge_tenant")
    store._conn.execute("UPDATE admin_audit SET outcome = 'denied' WHERE id = 3")
    store._conn.commit()

    spot = store.verify_audit_chain()["broken_at"]

    assert spot["id"] == 3
    assert spot["actor"] == "admin2"
    assert spot["action"] == "purge_tenant"


# ── 체인이 **못** 잡는 것 ───────────────────────────────────────────────────


def test_a_full_recomputation_passes_verification(store):
    """**이것을 테스트로 박아 두지 않으면 다음 사람이 기능을 과장한다.**

    DB 에 쓸 수 있는 공격자는 고친 행부터 끝까지 다시 계산해 검증을 통과시킨다.
    체인 검증은 이것을 원리적으로 못 잡는다 — 그래서 내보내기가 한 묶음이다.
    """
    for n in range(3):
        store.audit("admin", "purge_tenant", target=f"t{n}")

    # 공격자: 2번 행을 고치고 그 뒤를 전부 다시 계산한다.
    store._conn.execute("UPDATE admin_audit SET actor = '다른사람' WHERE id = 2")
    expected = AUDIT_GENESIS
    for row in rows(store):
        fresh = audit_row_hash(
            expected, ts=row["ts"], tenant_id=row["tenant_id"], actor=row["actor"],
            action=row["action"], target=row["target"],
            detail_json=row["detail_json"], outcome=row["outcome"],
        )
        store._conn.execute(
            "UPDATE admin_audit SET prev_hash = ?, row_hash = ? WHERE id = ?",
            (expected, fresh, row["id"]),
        )
        expected = fresh
    store._conn.commit()

    assert store.verify_audit_chain()["ok"], "재계산은 체인 검증을 통과한다"


def test_the_exported_tip_catches_what_the_chain_cannot(store):
    """**내보내기가 체인과 한 묶음인 이유 그 자체다.**

    밖에 사본이 있으면 재계산은 "내보낸 팁이 지금 체인에 없다" 로 드러난다.
    """
    for n in range(3):
        store.audit("admin", "purge_tenant", target=f"t{n}")
    exported = store.export_audit_chain()
    store.record_audit_export(tip=exported[-1]["row_hash"], last_id=exported[-1]["id"])

    assert store.audit_export_still_agrees() is True

    # 같은 재계산 공격.
    store._conn.execute("UPDATE admin_audit SET actor = '다른사람' WHERE id = 2")
    expected = AUDIT_GENESIS
    for row in rows(store):
        fresh = audit_row_hash(
            expected, ts=row["ts"], tenant_id=row["tenant_id"], actor=row["actor"],
            action=row["action"], target=row["target"],
            detail_json=row["detail_json"], outcome=row["outcome"],
        )
        store._conn.execute(
            "UPDATE admin_audit SET prev_hash = ?, row_hash = ? WHERE id = ?",
            (expected, fresh, row["id"]),
        )
        expected = fresh
    store._conn.commit()

    assert store.verify_audit_chain()["ok"], "체인은 통과한다 — 그것이 한계다"
    assert store.audit_export_still_agrees() is False, "내보낸 팁이 이것을 잡아야 한다"


def test_never_exported_is_unknown_not_ok(store):
    """내보낸 적이 없으면 **판정 불가**다. `False` 로 두면 정상을 사고로 신고한다."""
    store.audit("admin", "login")

    assert store.audit_export_still_agrees() is None


# ── 정상 운영이 사고로 보이면 안 된다 ───────────────────────────────────────


def test_retention_purge_does_not_look_like_tampering(store, clock):
    """**매년 보존 경계에서 "변조됨" 이 뜨면 그 검증은 꺼진다.**

    정리는 옛 행을 지우고, 지우면 살아남은 첫 행의 앞 고리가 사라진다. 끊긴 자리의
    해시를 앵커로 남겨 검증이 거기서부터 잇게 한다.
    """
    for n in range(3):
        store.audit("admin", "old_event", target=f"t{n}")
    clock.advance((AUDIT_RETENTION_DAYS + 1) * 86400)
    for n in range(2):
        store.audit("admin", "recent_event", target=f"r{n}")

    store.purge_expired(job_retention_days=30, raw_prompt_retention_days=7)

    survivors = rows(store)
    assert len(survivors) == 2, "옛 행이 정리됐어야 한다"
    verdict = store.verify_audit_chain()
    assert verdict["ok"], f"보존 정리가 변조로 보인다: {verdict}"


def test_tampering_after_a_purge_is_still_detected(store, clock):
    """앵커가 검증을 **약화시키면** 안 된다 — 정리 뒤에도 조작은 드러나야 한다."""
    store.audit("admin", "old_event")
    clock.advance((AUDIT_RETENTION_DAYS + 1) * 86400)
    for n in range(3):
        store.audit("admin", "recent_event", target=f"r{n}")
    store.purge_expired(job_retention_days=30, raw_prompt_retention_days=7)
    assert store.verify_audit_chain()["ok"]

    store._conn.execute("UPDATE admin_audit SET actor = '다른사람' WHERE id = 3")
    store._conn.commit()

    assert not store.verify_audit_chain()["ok"]


def test_coalescing_does_not_break_the_chain(store):
    """대시보드 폴링을 합치는 것은 정상 동작이다."""
    for _ in range(4):
        store.audit("admin", "view_overview", coalesce_seconds=60.0)

    chain = rows(store)
    assert len(chain) == 1, "합치기가 안 됐다"
    assert json.loads(chain[0]["detail_json"])["repeats"] == 4
    assert store.verify_audit_chain()["ok"], "합치기가 체인을 깨뜨렸다"


def test_coalescing_only_merges_into_the_tip(store):
    """**끝이 아닌 행을 고치면 뒤따르는 해시가 전부 어긋난다.**

    그 사이에 다른 사건이 기록됐다면 합치지 않고 새 행을 쓴다 — 그 편이 정직하다.
    """
    store.audit("admin", "view_overview", coalesce_seconds=60.0)
    store.audit("other", "purge_tenant")          # 사이에 낀 진짜 사건
    store.audit("admin", "view_overview", coalesce_seconds=60.0)

    chain = rows(store)
    assert len(chain) == 3, "끝이 아닌 행에 합쳤다"
    assert store.verify_audit_chain()["ok"]


def test_rows_from_before_the_chain_are_not_called_tampering(store):
    """업그레이드하자마자 "변조됨" 을 보면 그 검증을 아무도 안 믿는다."""
    store._conn.execute(
        "INSERT INTO admin_audit(ts, tenant_id, actor, action, target, detail_json, "
        "outcome) VALUES(?,?,?,?,?,?,?)",
        (1.0, "acme", "옛관리자", "login", None, "{}", "ok"),
    )
    store._conn.commit()
    store.audit("admin", "login")

    verdict = store.verify_audit_chain()

    assert verdict["ok"]
    assert verdict["unchained"] == 1
    assert verdict["checked"] == 1


# ── 내보내기 ────────────────────────────────────────────────────────────────


def test_the_export_is_incremental(store):
    """1년치를 매번 내보내야 하면 아무도 안 돌리고, 안 돌리는 절차는 없는 절차다."""
    for n in range(3):
        store.audit("admin", "event", target=f"t{n}")
    first = store.export_audit_chain()
    store.record_audit_export(tip=first[-1]["row_hash"], last_id=first[-1]["id"])

    store.audit("admin", "event", target="t3")
    second = store.export_audit_chain(since_id=store.last_audit_export()["last_id"])

    assert len(first) == 3
    assert len(second) == 1
    assert second[0]["target"] == "t3"


def test_the_export_carries_the_hashes(store):
    """해시 없이 내보내면 밖에서 독립적으로 검증할 수 없다 — 그냥 로그 사본이다."""
    store.audit("admin", "event")

    row = store.export_audit_chain()[0]

    assert row["prev_hash"] == AUDIT_GENESIS
    assert row["row_hash"]
    assert audit_row_hash(
        row["prev_hash"], ts=row["ts"], tenant_id=row["tenant_id"],
        actor=row["actor"], action=row["action"], target=row["target"],
        detail_json=row["detail_json"], outcome=row["outcome"],
    ) == row["row_hash"], "밖에서 다시 계산한 값이 안 맞는다"


# ── 해시 자체의 성질 ────────────────────────────────────────────────────────


def test_fields_cannot_be_shifted_across_the_boundary(store):
    """**구분자가 없으면 필드 경계를 옮기면서 같은 해시를 유지할 수 있다.**

    `actor="a", action="bc"` 와 `actor="ab", action="c"` 가 같은 해시를 내면,
    공격자는 행위자를 바꾸면서 검증을 통과한다.
    """
    common = dict(
        ts=1.0, tenant_id=None, target=None, detail_json="{}", outcome="ok"
    )
    assert audit_row_hash(AUDIT_GENESIS, actor="a", action="bc", **common) != (
        audit_row_hash(AUDIT_GENESIS, actor="ab", action="c", **common)
    )


def test_the_same_content_at_a_different_position_hashes_differently(store):
    """앞 고리가 들어가므로 같은 내용이라도 자리가 다르면 해시가 다르다 —
    행을 통째로 복사해 옮기는 조작을 막는다."""
    common = dict(
        ts=1.0, tenant_id=None, actor="a", action="b", target=None,
        detail_json="{}", outcome="ok",
    )
    assert audit_row_hash(AUDIT_GENESIS, **common) != audit_row_hash("다른고리", **common)


# ── 문서가 없는 것을 가리키거나 과장하지 않는가 ─────────────────────────────


ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "runbook-audit-integrity.md"


def test_the_runbook_states_the_limit_not_just_the_feature():
    """**과장하지 않는 것이 이 기능의 요구사항이다.**

    D10 의 판정은 "체인만 넣고 컴플라이언스를 주장하면 과장이다" 였다. 런북이
    한계를 안 적으면 그 판정이 구현에서 사라진다.
    """
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "조작하면 드러난다" in text
    assert "조작할 수 없다" in text, "못 하는 것을 안 적었다"
    assert "다시 계산" in text, "재계산 한계가 없다"
    assert "위변조 불가" in text, "쓰면 안 되는 문장을 경고하지 않는다"


def test_the_runbook_names_commands_and_files_that_exist():
    from app.cli import build_parser

    text = RUNBOOK.read_text(encoding="utf-8")
    known = set(build_parser()._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    named = {
        line.split("lcc ")[1].split()[0]
        for line in text.splitlines()
        if "lcc " in line and not line.lstrip().startswith("|")
    }

    assert not named - known, f"런북이 없는 명령을 가리킨다: {sorted(named - known)}"
    for path in ("app/store.py", "tests/test_audit_chain.py", "tests/test_multiprocess.py"):
        assert path in text and (ROOT / path).exists()


def test_the_debt_table_does_not_overclaim():
    """부채 표가 "변조 증적이 없다" 로 남아 있으면 옛말이고, "위변조 불가" 로
    바뀌면 과장이다. 둘 다 아니어야 한다."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    row = next((ln for ln in readme.splitlines() if "감사 조작을" in ln), "")

    assert row, "부채 표에 감사 무결성 항목이 없다"
    assert "audit-export" in row, "내보내기가 필수라는 것이 안 적혀 있다"
    assert "위변조 불가" not in readme


# ── QA V4 — 합치기가 다중 워커에서 체인을 끊는다 ────────────────────────────


class StaleTip:
    """팁 읽기가 낡은 상황을 재현하는 커넥션 프록시.

    실제 경합: A 가 팁을 읽고 → B 가 그 팁 뒤에 새 행을 잇고 → A 가 (이제 팁이
    아닌) 그 행을 합치기 UPDATE 로 고친다. 유일 인덱스는 INSERT 포크만 막지 이
    UPDATE 는 못 막는다. 첫 팁 읽기에만 낡은 값을 돌려주고, 재시도부터는
    실제 값을 준다 — 고친 코드는 재시도에서 회복해야 한다.
    """

    def __init__(self, conn, interleave):
        self._conn = conn
        self._interleave = interleave
        self._armed = True

    def execute(self, sql, *args):
        result = self._conn.execute(sql, *args)
        if self._armed and sql.startswith("SELECT id, detail_json, prev_hash, row_hash"):
            self._armed = False
            stale = result.fetchone()
            self._interleave()          # B 가 이 사이에 새 행을 잇는다
            class _One:
                def fetchone(self_inner):
                    return stale
            return _One()
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_coalescing_a_stale_tip_does_not_break_the_chain(tmp_path):
    """**합치기는 그 행이 아직 팁일 때만 성립한다** — 낡은 팁을 고치면 뒤 행의
    앞 고리가 끊기고, 그것은 변조와 구분되지 않는다(QA V4)."""
    from app.store import SqliteStore

    path = tmp_path / "audit.db"
    a = SqliteStore(path)
    b = SqliteStore(path)
    try:
        a.audit("admin", "poll", coalesce_seconds=300)

        def b_appends():
            b.audit("admin", "delete_tenant", target="acme")

        a._conn = StaleTip(a._conn, b_appends)
        # A 의 합치기 — 낡은 팁(1행)을 고치려 든다.
        a.audit("admin", "poll", coalesce_seconds=300)

        verdict = a.verify_audit_chain()
        assert verdict["ok"], f"체인이 끊겼다: {verdict}"
    finally:
        a.close(); b.close()


def test_inserting_after_a_stale_tip_does_not_break_the_chain(tmp_path):
    """INSERT 쪽도 같다 — 낡은 팁 해시를 앞 고리로 이으면, 그 사이 합치기로
    바뀐 팁과 어긋난다. 유일 인덱스는 **같은** prev_hash 의 중복만 막는다."""
    from app.store import SqliteStore

    path = tmp_path / "audit2.db"
    a = SqliteStore(path)
    b = SqliteStore(path)
    try:
        a.audit("admin", "poll", coalesce_seconds=300)

        def b_coalesces():
            # B 가 같은 팁을 합치기로 고쳐 팁의 해시가 바뀐다.
            b.audit("admin", "poll", coalesce_seconds=300)

        a._conn = StaleTip(a._conn, b_coalesces)
        a.audit("admin", "delete_tenant", target="acme")   # 낡은 해시에 잇는다

        verdict = a.verify_audit_chain()
        assert verdict["ok"], f"체인이 끊겼다: {verdict}"
    finally:
        a.close(); b.close()


# ── QA V5·V9 — 내보내기 표식과 체인 이전 구간 ──────────────────────────────


def cli_export(data_dir, out, *extra):
    from app.cli import main as cli_main

    return cli_main([
        "--data", str(data_dir), "audit-export", "--out", str(out), *extra,
    ])


def test_normal_polling_after_an_export_is_not_an_alarm(tmp_path, capsys):
    """**정상 운영(대시보드 폴링 + 내보내기)이 doctor 경보를 내면 안 된다**(QA V5).

    표식이 아직 합쳐질 수 있는 팁을 가리키면, 다음 폴링의 합치기가 그 해시를
    바꿔 "체인 재계산" 경보가 난다 — 정상을 사고로 신고하는 검증은 곧 꺼진다.
    표식은 얼어붙은 행(뒤에 행이 생긴 행)에 둔다.
    """
    from app.store import SqliteStore

    data = tmp_path / "data"; data.mkdir()
    store = SqliteStore(data / "controlcenter.db")
    store.audit("admin", "delete_tenant", target="old")          # 얼어붙을 행
    store.audit("admin", "poll", coalesce_seconds=300)           # 살아 있는 팁
    store.close()

    assert cli_export(data, tmp_path / "audit.jsonl") == 0

    store = SqliteStore(data / "controlcenter.db")
    try:
        # 대시보드가 계속 폴링한다 — 팁이 합쳐져 해시가 바뀐다.
        store.audit("admin", "poll", coalesce_seconds=300)
        assert store.audit_export_still_agrees() is True, (
            "정상 폴링이 '체인 재계산' 경보가 됐다"
        )
    finally:
        store.close()


def test_export_of_a_pre_chain_database_does_not_crash(tmp_path, capsys):
    """업그레이드 직후 첫 내보내기 — 전부 체인 이전(해시 NULL) 행뿐이어도
    TypeError 없이 나간다(QA V9)."""
    from app.store import SqliteStore

    data = tmp_path / "data"; data.mkdir()
    store = SqliteStore(data / "controlcenter.db")
    store._conn.execute(
        "INSERT INTO admin_audit(ts, actor, action, outcome) VALUES(1, 'a', 'old', 'ok')"
    )
    store._conn.commit()
    store.close()

    assert cli_export(data, tmp_path / "audit.jsonl") == 0
    assert "체인 이전 구간" in capsys.readouterr().out
