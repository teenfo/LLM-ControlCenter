"""스케줄러 — 선택 순서 · 재시도 재배치 · 대기 타임아웃 · 스캔 창."""

from __future__ import annotations

import asyncio

import pytest

from app.cluster import HEALTHY, Cluster
from app.config import (
    CatalogEntry,
    Config,
    GuardSettings,
    Lane,
    Node,
    Pricing,
    Role,
    Thresholds,
)
from app.scheduler import Scheduler, _round_robin_by_tenant
from app.store import SqliteStore, TenantScope

ACME = TenantScope("acme")
GLOBEX = TenantScope("globex")


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**overrides) -> Config:
    thresholds = Thresholds(
        max_retries=2,
        retry_backoff_seconds=(2, 4, 8),
        scan_window_per_lane=overrides.pop("scan_window", 50),
        administrative_wait_timeout_seconds=overrides.pop("admin_timeout", 900),
    )
    return Config(
        nodes={
            "in-1": Node(name="in-1", provider="mock", data_boundary="internal",
                         mem_budget_gb=40, max_concurrent=4, tags=("internal",),
                         models=("m",)),
            "in-2": Node(name="in-2", provider="mock", data_boundary="internal",
                         mem_budget_gb=40, max_concurrent=4, tags=("internal",),
                         models=("m",)),
            "out": Node(name="out", provider="mock", data_boundary="external",
                        max_concurrent=4, tags=("external",), models=("cm",),
                        metered_override=True),
        },
        roles={
            "chat": Role(name="chat", model="m", lane="interactive",
                         placement=("internal", "external"), tier_models={"external": "cm"}),
            "inside": Role(name="inside", model="m", lane="interactive",
                           placement=("internal",)),
            "vec": Role(name="vec", model="m", kind="embed", lane="batch",
                        placement=("internal",)),
        },
        lanes={
            "interactive": Lane("interactive", overrides.pop("lane_concurrency", 2),
                                starvation_seconds=overrides.pop("starvation", 300)),
            "batch": Lane("batch", 1, starvation_seconds=600),
        },
        guard_rules=(),
        guard_settings=GuardSettings(),
        pricing=Pricing(
            table={"mock": {"cm": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
                            "*": {"input_per_mtok": 0.0, "output_per_mtok": 0.0}}},
        ),
        thresholds=thresholds,
        catalog=(CatalogEntry(name="m", provider="mock", est_size_gb=5.0),
                 CatalogEntry(name="cm", provider="mock", est_size_gb=0.0)),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> SqliteStore:
    s = SqliteStore(":memory:", now=clock)
    for tenant in ("acme", "globex"):
        s.create_tenant(tenant, tenant.title(), end_user_salt=b"salt")
        s.create_service(TenantScope(tenant), f"{tenant}-web", "web")
    yield s
    s.close()


def build(store, clock, config=None) -> tuple[Cluster, Scheduler]:
    config = config or make_config()
    cluster = Cluster(config, store, now=clock)
    for name, state in cluster.nodes.items():
        state.models = frozenset(config.nodes[name].models)
        state.status = HEALTHY
    return cluster, Scheduler(config, store, cluster, now=clock)


@pytest.fixture
def parts(store, clock):
    return build(store, clock)


def enqueue(store, scope, *, role="chat", lane="interactive", prompt="안녕", **extra):
    extra.setdefault("placement", ["internal", "external"])
    return store.create_job(
        scope, service_id=f"{scope.tenant_id}-web", role=role, lane=lane,
        prompt_masked=prompt, **extra,
    )


async def drain(scheduler, lane="interactive", rounds=6):
    """디스패치된 잡이 끝날 때까지 이벤트 루프를 돌린다."""
    for _ in range(rounds):
        await scheduler.tick(lane)
        await asyncio.sleep(0)


# ── 기본 디스패치 ────────────────────────────────────────────────────────────


async def test_job_runs_end_to_end(parts, store):
    cluster, scheduler = parts
    job_id = enqueue(store, ACME)

    await drain(scheduler)

    job = store.get_job(ACME, job_id)
    assert job.status == "ok"
    assert job.response.startswith("[mock:")
    assert job.node in ("in-1", "in-2")
    assert job.finished_at is not None


async def test_usage_is_recorded_on_success(parts, store):
    cluster, scheduler = parts
    enqueue(store, ACME)
    await drain(scheduler)

    rows = store._conn.execute("SELECT * FROM usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["output_tokens"] > 0


async def test_lane_concurrency_is_respected(store, clock):
    """레인 상한은 클러스터 전체에 걸린 값이다 — 노드가 놀아도 이걸 넘지 않는다."""
    cluster, scheduler = build(store, clock, make_config(lane_concurrency=2))
    for _ in range(5):
        enqueue(store, ACME)

    dispatched = await scheduler.tick("interactive")
    assert dispatched == 2


async def test_embed_role_uses_the_embed_path(parts, store):
    cluster, scheduler = parts
    job_id = enqueue(store, ACME, role="vec", lane="batch", placement=["internal"])

    await drain(scheduler, lane="batch")

    job = store.get_job(ACME, job_id)
    assert job.status == "ok"
    assert job.metrics["vectors"] == 1


# ── 선택 순서 ────────────────────────────────────────────────────────────────


def test_round_robin_interleaves_tenants():
    class J:
        def __init__(self, tenant, idx):
            self.tenant_id, self.idx = tenant, idx

    jobs = [J("a", 1), J("a", 2), J("a", 3), J("b", 1), J("c", 1)]
    order = [(j.tenant_id, j.idx) for j in _round_robin_by_tenant(jobs)]

    assert order[:3] == [("a", 1), ("b", 1), ("c", 1)]
    assert order[3:] == [("a", 2), ("a", 3)]


async def test_one_tenant_cannot_starve_another(store, clock):
    """한 테넌트가 1,000건을 넣어도 다른 테넌트의 첫 잡이 뒤로 밀리지 않는다."""
    cluster, scheduler = build(store, clock, make_config(lane_concurrency=2))

    for _ in range(20):
        enqueue(store, ACME)
    victim = enqueue(store, GLOBEX)

    await scheduler.tick("interactive")

    assert store.get_job(GLOBEX, victim).status != "queued", "옆 테넌트가 굶었다"


async def test_starvation_beats_fairness_and_priority(store, clock):
    """기아 방지는 티어·친화·부하보다 위다."""
    cluster, scheduler = build(store, clock, make_config(lane_concurrency=1, starvation=300))

    old = enqueue(store, ACME, priority=0)
    clock.advance(400)
    fresh = enqueue(store, GLOBEX, priority=99)   # 우선순위가 훨씬 높다

    await scheduler.tick("interactive")

    assert store.get_job(ACME, old).status != "queued", "오래 기다린 잡이 밀렸다"
    assert store.get_job(GLOBEX, fresh).status == "queued"


async def test_priority_orders_within_a_tenant(store, clock):
    cluster, scheduler = build(store, clock, make_config(lane_concurrency=1))

    enqueue(store, ACME, priority=0)
    high = enqueue(store, ACME, priority=10)

    await scheduler.tick("interactive")
    assert store.get_job(ACME, high).status != "queued"


# ── 스캔 창 ─────────────────────────────────────────────────────────────────


async def test_scan_window_truncation_is_surfaced(store, clock):
    """조용히 자르면 "전부 검토했다" 로 읽힌다."""
    cluster, scheduler = build(store, clock, make_config(scan_window=3, lane_concurrency=1))
    for _ in range(10):
        enqueue(store, ACME)

    await scheduler.tick("interactive")

    assert scheduler.snapshot()["interactive"]["scan_truncated"] is True
    assert scheduler.snapshot()["interactive"]["scan_window"] == 3


async def test_no_truncation_flag_when_queue_fits(store, clock):
    cluster, scheduler = build(store, clock, make_config(scan_window=50))
    enqueue(store, ACME)

    await scheduler.tick("interactive")
    assert scheduler.snapshot()["interactive"]["scan_truncated"] is False


# ── 재시도 ──────────────────────────────────────────────────────────────────


async def test_retry_avoids_the_node_that_just_failed(parts, store, clock):
    """죽은 노드로 3회 재시도하고 끝나면 안 된다."""
    cluster, scheduler = parts
    cluster.provider_for("in-1").fail_next = 99
    cluster.provider_for("in-1").fail_retryable = True

    job_id = enqueue(store, ACME, placement=["internal"])
    await drain(scheduler, rounds=2)

    job = store.get_job(ACME, job_id)
    assert job.attempts >= 1
    assert job.last_failed_node is not None

    clock.advance(10)   # 백오프 경과
    await drain(scheduler, rounds=2)

    assert store.get_job(ACME, job_id).node != job.last_failed_node


async def test_backoff_does_not_block_the_lane(parts, store, clock):
    """백오프를 sleep 으로 구현하면 그 레인이 통째로 멈춘다."""
    cluster, scheduler = parts
    cluster.provider_for("in-1").fail_next = 1
    cluster.provider_for("in-2").fail_next = 1

    failing = enqueue(store, ACME, placement=["internal"])
    await drain(scheduler, rounds=2)
    assert store.get_job(ACME, failing).wait_reason == "retry_backoff"

    other = enqueue(store, GLOBEX, placement=["internal"])
    await drain(scheduler, rounds=2)

    assert store.get_job(GLOBEX, other).status == "ok", "백오프 중인 잡이 레인을 막았다"


async def test_backoff_delays_the_retry(parts, store, clock):
    cluster, scheduler = parts
    cluster.provider_for("in-1").fail_next = 99
    cluster.provider_for("in-2").fail_next = 99

    job_id = enqueue(store, ACME, placement=["internal"])
    await drain(scheduler, rounds=2)
    attempts_after_first = store.get_job(ACME, job_id).attempts

    await drain(scheduler, rounds=2)   # 백오프 안 지남
    assert store.get_job(ACME, job_id).attempts == attempts_after_first

    clock.advance(5)
    await drain(scheduler, rounds=2)
    assert store.get_job(ACME, job_id).attempts > attempts_after_first


async def test_non_retryable_failure_stops_immediately(parts, store):
    """컨텍스트 초과처럼 다시 해도 같은 것을 재시도하면 시간과 돈만 쓴다."""
    cluster, scheduler = parts
    for name in ("in-1", "in-2"):
        cluster.provider_for(name).fail_next = 99
        cluster.provider_for(name).fail_retryable = False

    job_id = enqueue(store, ACME, placement=["internal"])
    await drain(scheduler, rounds=3)

    job = store.get_job(ACME, job_id)
    assert job.status == "failed"
    assert job.attempts == 1


async def test_retries_are_capped(parts, store, clock):
    cluster, scheduler = parts
    for name in ("in-1", "in-2"):
        cluster.provider_for(name).fail_next = 99

    job_id = enqueue(store, ACME, placement=["internal"])
    for _ in range(6):
        await drain(scheduler, rounds=2)
        clock.advance(20)

    job = store.get_job(ACME, job_id)
    assert job.status == "failed"
    assert job.attempts <= 3   # 최초 1회 + 재시도 2회


async def test_failed_job_releases_its_cost_reservation(parts, store):
    """예약이 남아 있으면 예산이 영원히 묶인다."""
    cluster, scheduler = parts
    for name in ("in-1", "in-2", "out"):
        cluster.provider_for(name).fail_next = 99
        cluster.provider_for(name).fail_retryable = False

    enqueue(store, ACME)
    await drain(scheduler, rounds=3)

    assert store.reserved_cost(ACME) == 0.0


# ── 대기와 타임아웃 ──────────────────────────────────────────────────────────


async def test_wait_reason_is_recorded_for_the_ui(parts, store):
    cluster, scheduler = parts
    for state in cluster.nodes.values():
        state.status = "unhealthy"

    job_id = enqueue(store, ACME)
    await scheduler.tick("interactive")

    job = store.get_job(ACME, job_id)
    assert job.status == "queued"
    assert job.wait_reason == "unhealthy"
    assert scheduler.snapshot()["interactive"]["wait_reasons"]["unhealthy"] == 1


async def test_administrative_wait_eventually_times_out(store, clock):
    """영원히 기다리게 두면 소비자가 이유를 모른 채 매달린다."""
    cluster, scheduler = build(store, clock, make_config(admin_timeout=60))
    for state in cluster.nodes.values():
        state.status = "unhealthy"

    job_id = enqueue(store, ACME)
    await scheduler.tick("interactive")
    assert store.get_job(ACME, job_id).status == "queued"

    clock.advance(120)
    await scheduler.tick("interactive")

    job = store.get_job(ACME, job_id)
    assert job.status == "failed"
    assert job.error_code == "administrative_wait_timeout"


async def test_capacity_impossible_fails_without_waiting(store, clock):
    """큐가 비어도 못 도는 잡은 조용히 쌓아두지 않는다."""
    config = make_config()
    import dataclasses

    config = dataclasses.replace(
        config,
        roles={**config.roles,
               "huge": Role(name="huge", model="big", lane="interactive", placement=("internal",))},
        catalog=(*config.catalog, CatalogEntry(name="big", provider="mock", est_size_gb=999.0)),
    )
    cluster, scheduler = build(store, clock, config)

    job_id = enqueue(store, ACME, role="huge", placement=["internal"])
    await scheduler.tick("interactive")

    job = store.get_job(ACME, job_id)
    assert job.status == "failed"
    assert job.error_code == "capacity_impossible"


# ── 데이터 경계 ──────────────────────────────────────────────────────────────


async def test_guard_narrowed_boundary_keeps_the_job_inside(parts, store):
    """가드가 external 을 뺐으면 내부 노드가 전멸해도 밖으로 안 나간다."""
    cluster, scheduler = parts
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = "unhealthy"

    job_id = enqueue(store, ACME, allowed_boundaries=["internal"])
    await scheduler.tick("interactive")

    job = store.get_job(ACME, job_id)
    assert job.status == "queued", "경계 밖으로 새어나갔다"
    assert job.node is None


async def test_external_variant_is_sent_when_leaving_the_boundary(parts, store):
    """한 벌만 저장하면 경계별 등급 구분이 디스패치 시점에 사라진다."""
    cluster, scheduler = parts
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = "unhealthy"

    enqueue(
        store, ACME,
        prompt="내부용 원문 900101", prompt_external="외부용 [가림]",
    )
    await drain(scheduler)

    sent = cluster.provider_for("out").call_log
    assert sent, "외부 노드로 안 갔다"
    assert sent[0]["chars"] == len("외부용 [가림]")


async def test_internal_variant_is_sent_inside(parts, store):
    cluster, scheduler = parts
    cluster.nodes["out"].status = "unhealthy"

    enqueue(store, ACME, prompt="내부용 원문", prompt_external="외부용 [가림]")
    await drain(scheduler)

    used = [n for n in ("in-1", "in-2") if cluster.provider_for(n).call_log]
    assert used
    assert cluster.provider_for(used[0]).call_log[0]["chars"] == len("내부용 원문")


# ── 크래시 복구 ──────────────────────────────────────────────────────────────


async def test_start_recovers_running_jobs(parts, store):
    cluster, scheduler = parts
    job_id = enqueue(store, ACME)
    store.update_job(ACME, job_id, status="running", node="in-1")

    await scheduler.start()
    await scheduler.stop()

    assert store.get_job(ACME, job_id).status == "queued"


async def test_start_flags_metered_jobs_for_review(store, clock):
    """과금 노드에서 돌던 잡을 자동 재큐하면 두 번 청구된다."""
    cluster, scheduler = build(store, clock)
    seen: list[tuple[str, dict]] = []
    scheduler._notify = lambda e, d: seen.append((e, d))

    job_id = enqueue(store, ACME)
    store.update_job(ACME, job_id, status="running", node="out")

    await scheduler.start()
    await scheduler.stop()

    assert store.get_job(ACME, job_id).status == "needs_review"
    assert any(e == "crash_recovery_needs_review" for e, _ in seen)


# ── 보존 ────────────────────────────────────────────────────────────────────


def test_retention_runs_both_purges(parts, store, clock):
    cluster, scheduler = parts
    job_id = enqueue(store, ACME, prompt_cipher=b"cipher", prompt_nonce=b"n")
    store.update_job(ACME, job_id, status="ok", finished_at=clock.now)
    store.bump_rate_counter("k", int(clock.now))

    clock.advance(40 * 86400)
    counts = scheduler.run_retention()

    assert counts["jobs"] == 1
    assert counts["rate_counters"] >= 1


# ── 관제 ────────────────────────────────────────────────────────────────────


def test_snapshot_reports_every_lane(parts):
    cluster, scheduler = parts
    snapshot = scheduler.snapshot()

    assert set(snapshot) == {"interactive", "batch"}
    assert snapshot["interactive"]["max_concurrent"] == 2


async def test_starvation_trips_are_counted(store, clock):
    """증설 트리거가 보는 지표 — 배치 경합의 신호다."""
    cluster, scheduler = build(store, clock, make_config(lane_concurrency=1, starvation=100))

    enqueue(store, ACME)
    clock.advance(200)
    await scheduler.tick("interactive")

    assert scheduler.snapshot()["interactive"]["starvation_trips"] >= 1


# ── 감사 M5 — 태스크 참조를 버리고, 종료가 실행 중인 잡을 버린다 ─────────────


async def test_the_execute_task_is_held(parts, store):
    """**`create_task` 의 반환을 버리면 GC 가 실행 도중에 가져갈 수 있다.**

    그러면 잡은 `running` 인 채 남고 `finally` 가 안 돌아 슬롯·메모리·비용 예약이
    영영 안 풀린다 — 부하가 없는데 큐가 쌓이는 그 증상이다.
    """
    cluster, scheduler = parts
    enqueue(store, ACME)

    await scheduler.tick("interactive")
    assert scheduler._inflight, "실행 태스크를 붙잡고 있지 않다"

    await drain(scheduler)
    assert not scheduler._inflight, "끝난 태스크가 안 치워진다"


async def test_stop_waits_for_running_jobs(store, clock):
    """루프만 취소하고 나가면 in-flight 실행이 미정리 상태로 파괴된다.

    그 잡들은 DB 에 `running` 으로 남아 다음 기동의 크래시 복구 경로를 탄다 —
    **정상 종료가 크래시처럼 보이고**, 배포할 때마다 그렇게 된다.
    """
    cluster, scheduler = build(store, clock)
    job_id = enqueue(store, ACME)

    await scheduler.tick("interactive")
    assert store.get_job(ACME, job_id).status == "running"

    await scheduler.stop()

    assert store.get_job(ACME, job_id).status == "ok", "종료가 실행 중인 잡을 버렸다"


async def test_stop_does_not_hang_forever_on_a_stuck_job(store, clock):
    """기다리는 데 상한이 없으면 오케스트레이터가 SIGKILL 을 보낸다."""
    import time as _time

    cluster, scheduler = build(store, clock)

    async def never_ends(*args, **kwargs):
        await asyncio.sleep(60)

    scheduler._inflight.add(asyncio.create_task(never_ends()))

    started = _time.monotonic()
    await scheduler.stop(drain_seconds=0.1)
    elapsed = _time.monotonic() - started

    assert elapsed < 1.0, f"종료가 {elapsed:.1f}초 매달렸다"
    assert not scheduler._inflight


# ── 감사 M7 — 모델 미설치가 재배치 없이 잡을 죽인다 ──────────────────────────


async def test_a_missing_model_is_retried_on_another_node(store, clock):
    """**"이 노드에서 안 된다" 와 "어디서도 안 된다" 는 다르다.**

    기동 직후에는 인벤토리가 비어 있어 배치 필터가 노드를 통과시킨다(모른다는
    이유로 막으면 전부 대기한다). 그렇게 잘못 간 잡이 재배치 없이 죽으면,
    그 모델을 가진 노드가 멀쩡히 놀고 있는데도 요청이 실패한다.
    """
    from app.providers.base import ModelNotFound

    cluster, scheduler = build(store, clock)
    job_id = enqueue(store, ACME)

    # in-1 만 그 모델이 없다고 답한다.
    original = cluster.nodes["in-1"].provider.generate

    async def refuse(*args, **kwargs):
        raise ModelNotFound(kwargs.get("model", "?"), "in-1")

    cluster.nodes["in-1"].provider.generate = refuse
    try:
        await drain(scheduler, rounds=10)
    finally:
        cluster.nodes["in-1"].provider.generate = original

    job = store.get_job(ACME, job_id)
    assert job.status in ("ok", "queued"), f"재배치 없이 죽었다: {job.error_code}"
    if job.status == "ok":
        assert job.node != "in-1"


async def test_a_missing_model_still_gives_up_eventually(store, clock):
    """모든 노드에 없으면 결국 끝난다 — 무한 재시도가 되면 안 된다."""
    from app.providers.base import ModelNotFound

    cluster, scheduler = build(store, clock)
    job_id = enqueue(store, ACME)

    for state in cluster.nodes.values():
        async def refuse(*args, **kwargs):
            raise ModelNotFound(kwargs.get("model", "?"), "any")

        state.provider.generate = refuse

    for _ in range(20):
        await drain(scheduler, rounds=2)
        clock.advance(30)          # 재시도 백오프를 넘긴다
        if store.get_job(ACME, job_id).status == "failed":
            break

    job = store.get_job(ACME, job_id)
    assert job.status == "failed"
    assert job.error_code == "model_not_installed"


# ── 감사 LOW — 재시도가 전부 같은 시각에 몰린다 ─────────────────────────────


def test_retry_backoff_is_jittered_per_job():
    """**노드 하나가 죽으면 그 노드에 있던 잡이 전부 같은 시각에 재시도한다.**

    다음 노드가 그 순간 몰린 요청을 받고 같이 죽는 경로다.
    """
    from app.scheduler import _jitter

    delays = {_jitter(f"job-{n}", 4.0) for n in range(50)}
    assert len(delays) > 40, "지연이 흩어지지 않는다"
    assert all(0 <= d <= 1.0 for d in delays), "지터가 백오프를 크게 넘는다"


def test_the_jitter_is_stable_for_one_job():
    """난수를 쓰면 같은 잡의 준비 여부가 틱마다 달라진다 — 됐다가 안 됐다가 한다."""
    from app.scheduler import _jitter

    assert _jitter("job-a", 4.0) == _jitter("job-a", 4.0)


def test_a_zero_backoff_gets_no_jitter():
    from app.scheduler import _jitter

    assert _jitter("job-a", 0.0) == 0.0


def test_the_backoff_still_ends(store, clock):
    """지터가 재시도를 영원히 미루면 안 된다."""
    cluster, scheduler = build(store, clock)
    job_id = enqueue(store, ACME)
    store.update_job(
        ACME, job_id, attempts=1, wait_reason="retry_backoff", wait_since=clock(),
    )

    job = store.get_job(ACME, job_id)
    assert scheduler._retry_ready(job, clock()) is False

    clock.advance(10)      # 백오프 2초 + 지터 최대 0.5초를 넉넉히 넘긴다
    assert scheduler._retry_ready(job, clock()) is True
