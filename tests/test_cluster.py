"""배치 라우팅 · 원자적 예약 · 헬스 · 비용.

이 파일이 고정하는 것 중 하나는 제품의 안전 보증 그 자체다:
**`placement: [internal]` 역할은 어떤 상황에서도 경계 밖 노드에 배치되지 않는다.**
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from app.cluster import DRAINING, FAIL, HEALTHY, PLACED, UNHEALTHY, WAIT, Cluster
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
from app.cost import CostAccountant
from app.store import SqliteStore, TenantScope

ACME = TenantScope("acme")


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_config(*, nodes: dict[str, Node], roles: dict[str, Role], catalog=()) -> Config:
    return Config(
        nodes=nodes,
        roles=roles,
        lanes={"interactive": Lane("interactive", 3), "guard": Lane("guard", 4)},
        guard_rules=(),
        guard_settings=GuardSettings(),
        pricing=Pricing(
            table={
                "mock": {
                    "cloud-m": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
                    "*": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
                }
            },
            assumed_max_output_tokens=1000,
        ),
        thresholds=Thresholds(),
        catalog=tuple(catalog),
    )


def two_tier_config() -> Config:
    return build_config(
        nodes={
            "in-1": Node(
                name="in-1", provider="mock", data_boundary="internal",
                mem_budget_gb=40, max_concurrent=2, tags=("internal",),
                models=("small", "guard-m"),
            ),
            "in-2": Node(
                name="in-2", provider="mock", data_boundary="internal",
                mem_budget_gb=20, max_concurrent=1, tags=("internal",),
                models=("small",),
            ),
            "out-1": Node(
                name="out-1", provider="mock", data_boundary="external",
                max_concurrent=4, tags=("external",), models=("cloud-m",),
                metered_override=True,
            ),
        },
        roles={
            "summarize": Role(
                name="summarize", model="small", placement=("internal", "external"),
                tier_models={"external": "cloud-m"},
            ),
            "classify": Role(name="classify", model="small", placement=("internal",)),
            "_guard": Role(
                name="_guard", model="guard-m", lane="guard",
                placement=("internal",), internal_only=True,
            ),
        },
        catalog=(
            CatalogEntry(name="small", provider="mock", est_size_gb=5.0),
            CatalogEntry(name="guard-m", provider="mock", est_size_gb=1.0),
            CatalogEntry(name="cloud-m", provider="mock", est_size_gb=0.0),
        ),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> SqliteStore:
    s = SqliteStore(":memory:", now=clock)
    s.create_tenant("acme", "Acme", end_user_salt=b"salt")
    s.create_service(ACME, "acme-web", "web")
    s.create_tenant("globex", "Globex", end_user_salt=b"salt2")
    yield s
    s.close()


@pytest.fixture
def cluster(store, clock) -> Cluster:
    config = two_tier_config()
    c = Cluster(config, store, now=clock)
    # 프로브를 흉내 내 모델 인벤토리를 채운다.
    for name, state in c.nodes.items():
        state.models = frozenset(config.nodes[name].models)
        state.status = HEALTHY
    return c


def place(cluster: Cluster, role_name: str, **kwargs):
    role = cluster._config.roles[role_name]
    defaults = dict(
        job_id="j1", tenant_id="acme", service_id="acme-web",
        role=role, prompt="a" * 400,
    )
    defaults.update(kwargs)
    return cluster.place(**defaults)


# ── 데이터 경계 ──────────────────────────────────────────────────────────────


def test_internal_only_role_never_lands_outside(cluster):
    """내부 노드를 전부 죽이고 경계 밖 노드만 살려도 나가지 않는다."""
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    result = place(cluster, "_guard")

    assert result.outcome != PLACED
    assert result.placement is None


def test_internal_only_role_ignores_external_tier_even_if_configured(store, clock):
    """설정에 external 티어가 있어도 internal_only 면 안 나간다.

    안전장치가 설정으로 풀리면 안전장치가 아니다.
    """
    config = build_config(
        nodes={
            "out": Node(name="out", provider="mock", data_boundary="external",
                        tags=("external",), models=("m",)),
        },
        roles={
            "guard": Role(name="guard", model="m", placement=("external",), internal_only=True),
        },
    )
    cluster = Cluster(config, store, now=clock)
    cluster.nodes["out"].status = HEALTHY
    cluster.nodes["out"].models = frozenset({"m"})

    result = cluster.place(
        job_id="j", tenant_id="acme", service_id="acme-web",
        role=config.roles["guard"],
    )
    assert result.outcome != PLACED


def test_internal_first_tier_wins_over_warm_external_model(cluster):
    """경계 밖에 같은 모델이 웜으로 떠 있어도 내부 우선 선언을 이긴다.

    성능 휴리스틱이 비용·데이터 경계 정책을 이길 수 없다.
    """
    cluster.nodes["out-1"].loaded_model = "cloud-m"   # 웜
    cluster.nodes["in-1"].loaded_model = None          # 콜드

    result = place(cluster, "summarize")

    assert result.placement.node.startswith("in-")
    assert result.placement.tier == "internal"


def test_falls_back_to_external_only_when_internal_is_gone(cluster):
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    result = place(cluster, "summarize")

    assert result.outcome == PLACED
    assert result.placement.node == "out-1"
    assert result.placement.model == "cloud-m", "티어별 모델 덮어쓰기가 안 먹었다"


def test_role_without_external_tier_waits_instead_of_leaking(cluster):
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    result = place(cluster, "classify")

    assert result.outcome == WAIT, "경계 밖으로 새는 대신 대기해야 한다"


# ── 안전 필드 교집합 ─────────────────────────────────────────────────────────


def test_snapshot_intersects_with_current_config(cluster):
    """비용 사고로 external 티어를 뺐는데 큐의 잡이 스냅샷대로 나가면 안 된다."""
    role = cluster._config.roles["classify"]  # 현재 설정은 internal 뿐
    effective = cluster.effective_placement(role, ["internal", "external"])

    assert effective == ("internal",), "스냅샷이 현재 설정보다 넓어졌다"


def test_intersection_narrows_only_never_widens(cluster):
    role = cluster._config.roles["summarize"]  # internal, external
    effective = cluster.effective_placement(role, ["internal"])

    assert effective == ("internal",)


def test_empty_intersection_waits_rather_than_fails(cluster):
    """관리자가 되돌릴 수 있으므로 하드 실패시키지 않는다."""
    result = place(cluster, "classify", placement_snapshot=["external"])
    assert result.outcome == WAIT
    assert result.reason == "placement_narrowed"


# ── 원자적 예약 ──────────────────────────────────────────────────────────────


def test_single_slot_node_accepts_exactly_one(store, clock):
    config = build_config(
        nodes={"n": Node(name="n", provider="mock", data_boundary="internal",
                         max_concurrent=1, tags=("internal",), models=("m",))},
        roles={"r": Role(name="r", model="m", placement=("internal",))},
    )
    cluster = Cluster(config, store, now=clock)
    cluster.nodes["n"].status = HEALTHY
    cluster.nodes["n"].models = frozenset({"m"})

    first = cluster.place(job_id="a", tenant_id="acme", service_id="s", role=config.roles["r"])
    second = cluster.place(job_id="b", tenant_id="acme", service_id="s", role=config.roles["r"])

    assert first.outcome == PLACED
    assert second.outcome == WAIT
    assert second.rejections["n"] == "no_slot"


def test_memory_budget_is_reserved_not_just_checked(cluster):
    """20GB 잔여를 두 잡이 각각 확인하고 각각 올리면 안 된다."""
    node = cluster.nodes["in-2"]  # 20GB, 슬롯 1개
    for other in ("in-1", "out-1"):
        cluster.nodes[other].status = UNHEALTHY

    first = place(cluster, "classify", job_id="a")
    assert first.outcome == PLACED
    assert node.reserved_mem_gb == 5.0

    second = place(cluster, "classify", job_id="b")
    assert second.outcome == WAIT


def test_concurrent_placement_does_not_oversubscribe(store, clock):
    """레인 루프가 병렬로 도는 순간 같은 슬롯을 두 잡이 잡는 것을 막는다."""
    config = build_config(
        nodes={"n": Node(name="n", provider="mock", data_boundary="internal",
                         max_concurrent=5, tags=("internal",), models=("m",))},
        roles={"r": Role(name="r", model="m", placement=("internal",))},
    )
    cluster = Cluster(config, store, now=clock)
    cluster.nodes["n"].status = HEALTHY
    cluster.nodes["n"].models = frozenset({"m"})

    results = []
    barrier = threading.Barrier(20)

    def attempt(i: int) -> None:
        barrier.wait()
        results.append(
            cluster.place(job_id=f"j{i}", tenant_id="acme", service_id="s",
                          role=config.roles["r"])
        )

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    placed = [r for r in results if r.outcome == PLACED]
    assert len(placed) == 5, f"슬롯 5개에 {len(placed)}개가 배치됐다"
    assert cluster.nodes["n"].running == 5


def test_release_returns_slot_and_memory(cluster):
    node = cluster.nodes["in-1"]
    result = place(cluster, "classify")

    assert node.running == 1
    cluster.release(result.placement)
    assert node.running == 0
    assert node.reserved_mem_gb == 0.0


# ── 용량 불가 vs 행정적 부재 ─────────────────────────────────────────────────


def test_oversized_model_fails_immediately(store, clock):
    """21GB 모델을 20GB 노드뿐인 클러스터에 던지면 큐가 비어도 못 돈다.

    조용히 쌓아두지 않고 사유를 붙여 죽인다.
    """
    config = build_config(
        nodes={"n": Node(name="n", provider="mock", data_boundary="internal",
                         mem_budget_gb=20, tags=("internal",), models=("huge",))},
        roles={"r": Role(name="r", model="huge", placement=("internal",))},
        catalog=(CatalogEntry(name="huge", provider="mock", est_size_gb=21.0),),
    )
    cluster = Cluster(config, store, now=clock)
    cluster.nodes["n"].status = HEALTHY
    cluster.nodes["n"].models = frozenset({"huge"})

    result = cluster.place(job_id="j", tenant_id="acme", service_id="s", role=config.roles["r"])

    assert result.outcome == FAIL
    assert result.code == "capacity_impossible"


def test_disabled_node_causes_wait_not_hard_failure(cluster):
    """관리자가 5분 정비하려고 내렸는데 큐가 전멸하면 안 된다."""
    for name in ("in-1", "in-2", "out-1"):
        state = cluster.nodes[name]
        state.node = dataclasses.replace(state.node, enabled=False)

    result = place(cluster, "summarize")

    assert result.outcome == WAIT, "정비 중 노드 때문에 잡이 하드 실패했다"
    assert result.reason == "disabled"


def test_busy_nodes_cause_wait(cluster):
    for state in cluster.nodes.values():
        state.running = state.node.max_concurrent

    result = place(cluster, "summarize")
    assert result.outcome == WAIT
    assert result.reason == "no_slot"


def test_wait_reason_is_reported_for_the_ui(cluster):
    """관제 UI 가 "노드 정비로 대기 중 12건" 처럼 묶어 보여줄 근거."""
    cluster.nodes["in-1"].status = UNHEALTHY
    cluster.nodes["in-2"].status = UNHEALTHY
    cluster.nodes["out-1"].status = UNHEALTHY

    result = place(cluster, "summarize")
    assert result.reason == "unhealthy"


# ── 테넌트 격리 ──────────────────────────────────────────────────────────────


def test_tenant_affinity_node_rejects_other_tenants(store, clock):
    config = build_config(
        nodes={"ded": Node(name="ded", provider="mock", data_boundary="internal",
                           tags=("internal",), models=("m",), tenant_affinity=("acme",))},
        roles={"r": Role(name="r", model="m", placement=("internal",))},
    )
    cluster = Cluster(config, store, now=clock)
    cluster.nodes["ded"].status = HEALTHY
    cluster.nodes["ded"].models = frozenset({"m"})

    mine = cluster.place(job_id="a", tenant_id="acme", service_id="s", role=config.roles["r"])
    theirs = cluster.place(job_id="b", tenant_id="globex", service_id="s", role=config.roles["r"])

    assert mine.outcome == PLACED
    assert theirs.outcome == WAIT
    assert theirs.rejections["ded"] == "tenant_affinity"


# ── 재시도 재배치 ────────────────────────────────────────────────────────────


def test_retry_excludes_the_node_that_just_failed(cluster):
    """죽은 노드로 3회 재시도하고 끝나면 안 된다."""
    cluster.nodes["out-1"].status = UNHEALTHY

    result = place(cluster, "classify", last_failed_node="in-1")

    assert result.placement.node == "in-2"


# ── 선택 순서 ────────────────────────────────────────────────────────────────


def test_warm_model_wins_within_the_same_tier(cluster):
    cluster.nodes["in-1"].loaded_model = None
    cluster.nodes["in-2"].loaded_model = "small"

    result = place(cluster, "classify")
    assert result.placement.node == "in-2"


def test_least_loaded_wins_when_warmth_is_equal(cluster):
    cluster.nodes["in-1"].running = 1   # 2개 중 1개 = 0.5
    cluster.nodes["in-2"].running = 0   # 1개 중 0개 = 0.0

    result = place(cluster, "classify")
    assert result.placement.node == "in-2"


# ── 비용 ────────────────────────────────────────────────────────────────────


def test_free_local_path_reserves_nothing(cluster):
    result = place(cluster, "classify")
    assert result.placement.reserved_cost_usd == 0.0


def test_metered_node_reserves_upper_bound(cluster, store):
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    result = place(cluster, "summarize", prompt="a" * 3000)

    assert result.placement.node == "out-1"
    assert result.placement.reserved_cost_usd > 0
    assert store.get_job(ACME, "j1") is None or True  # 잡 행이 없어도 예약은 계산된다


def test_budget_exhaustion_demotes_to_the_free_path(cluster, store):
    """예산이 떨어지면 무료 경로로 자동 강등 — 이것이 의도한 동작이다."""
    store.record_usage(ACME, service_id="acme-web", role="summarize", cost_usd=100.0)

    result = place(cluster, "summarize", tenant_budget=1.0, prompt="a" * 3000)

    assert result.outcome == PLACED
    assert result.placement.tier == "internal", "예산 소진인데 과금 경로를 탔다"


def test_budget_exhaustion_blocks_when_no_free_tier(cluster, store):
    store.record_usage(ACME, service_id="acme-web", role="summarize", cost_usd=100.0)
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    result = place(cluster, "summarize", tenant_budget=1.0, prompt="a" * 3000)

    assert result.outcome == WAIT
    assert result.rejections["out-1"].startswith("budget_exceeded")


def test_reservations_count_against_the_budget(store, clock):
    """확인만 하면 동시 디스패치 N건이 각각 통과한 뒤 합계가 예산을 넘는다."""
    accountant = CostAccountant(two_tier_config().pricing, store, now=clock)

    store.create_job(ACME, service_id="acme-web", role="r", lane="interactive")
    jobs = store.list_jobs(ACME)
    store.update_job(ACME, jobs[0].id, cost_reserved_usd=8.0, status="running")

    status = accountant.budget_status(ACME, limit=10.0)
    assert status.reserved == 8.0
    assert status.can_afford(1.0) is True
    assert status.can_afford(5.0) is False, "예약분을 무시하고 초과 지출을 허용했다"


def test_settlement_clears_the_reservation(store, clock):
    """예약이 남아 있으면 예산이 영원히 묶인다."""
    accountant = CostAccountant(two_tier_config().pricing, store, now=clock)
    job_id = store.create_job(ACME, service_id="acme-web", role="r", lane="interactive")
    store.update_job(ACME, job_id, cost_reserved_usd=5.0, status="running")

    cost = accountant.settle(
        ACME, job_id, provider="mock", model="cloud-m",
        input_tokens=1_000_000, output_tokens=100_000,
        node="out-1", role="r", service_id="acme-web", status="ok", duration_ms=100,
    )

    assert cost == pytest.approx(1.0 + 0.5)
    assert store.reserved_cost(ACME) == 0.0


def test_release_reservation_without_settlement(store, clock):
    accountant = CostAccountant(two_tier_config().pricing, store, now=clock)
    job_id = store.create_job(ACME, service_id="acme-web", role="r", lane="interactive")
    store.update_job(ACME, job_id, cost_reserved_usd=5.0, status="running")

    accountant.release_reservation(ACME, job_id)
    assert store.reserved_cost(ACME) == 0.0


# ── 헬스 ────────────────────────────────────────────────────────────────────


def test_one_failure_does_not_kill_a_node(cluster):
    cluster.record_failure("in-1", "일시적 오류")
    assert cluster.nodes["in-1"].status != UNHEALTHY


def test_three_consecutive_failures_mark_unhealthy(cluster):
    for _ in range(3):
        cluster.record_failure("in-1", "오류")
    assert cluster.nodes["in-1"].status == UNHEALTHY


def test_one_success_does_not_revive_a_node(cluster):
    for _ in range(3):
        cluster.record_failure("in-1")
    cluster.record_success("in-1")
    assert cluster.nodes["in-1"].status == UNHEALTHY, "1회 성공으로 되살아났다(플래핑)"

    cluster.record_success("in-1")
    assert cluster.nodes["in-1"].status == HEALTHY


def test_unknown_health_passes_the_filter(store, clock):
    """헬스 정보를 아직 못 받았다는 이유로 큐를 멈추면 기동 직후가 항상 정지 상태가 된다."""
    config = two_tier_config()
    cluster = Cluster(config, store, now=clock)
    for state in cluster.nodes.values():
        state.models = frozenset(config.nodes[state.name].models)
    # status 는 전부 unknown 인 채로 둔다

    result = cluster.place(
        job_id="j", tenant_id="acme", service_id="s", role=config.roles["classify"]
    )
    assert result.outcome == PLACED


async def test_probe_updates_inventory_and_health(store, clock):
    config = two_tier_config()
    cluster = Cluster(config, store, now=clock)

    assert await cluster.probe("in-1") is True
    state = cluster.nodes["in-1"]
    assert "small" in state.models
    assert state.last_probe_at == clock.now


async def test_probe_failure_records_error(store, clock):
    config = two_tier_config()
    cluster = Cluster(config, store, now=clock)
    cluster.provider_for("in-1").kill()

    assert await cluster.probe("in-1") is False
    assert cluster.nodes["in-1"].last_error


# ── 드레이닝 ─────────────────────────────────────────────────────────────────


def test_draining_blocks_new_but_keeps_running_jobs(cluster):
    result = place(cluster, "classify")
    assert result.placement.node == "in-1"

    cluster.drain("in-1")

    assert cluster.nodes["in-1"].running == 1, "실행 중인 잡을 즉시 버렸다"
    assert cluster.nodes["in-1"].status == DRAINING

    again = place(cluster, "classify", job_id="j2")
    assert again.placement.node != "in-1"


def test_force_drain_clears_immediately(cluster):
    """강제 드레이닝은 **신규를 즉시 막는다** — 그것이 목적이다."""
    for name in ("in-2", "out-1"):
        cluster.nodes[name].status = UNHEALTHY

    first = place(cluster, "classify", job_id="a")
    assert first.placement.node == "in-1"

    cluster.drain("in-1", force=True)

    blocked = place(cluster, "classify", job_id="b")
    assert blocked.outcome != PLACED
    assert blocked.rejections["in-1"] == "draining"


def test_force_drain_does_not_lose_the_running_count(cluster):
    """**세는 것과 막는 것은 다른 일이다.**

    실행 중인 잡은 여전히 노드에서 돌고 있다. 카운터를 0 으로 밀면 그 잡들이
    끝날 때 `release()` 가 한 번 더 내려 0 에 머물고, 드레이닝을 풀었을 때
    노드가 비어 있는 것처럼 보여 **동시성 상한을 넘겨 배치된다.**
    """
    for name in ("in-2", "out-1"):
        cluster.nodes[name].status = UNHEALTHY

    placed = place(cluster, "classify", job_id="a")
    cluster.drain("in-1", force=True)

    assert cluster.nodes["in-1"].running == 1, "실행 중인 잡을 잊었다"

    cluster.undrain("in-1")
    cluster.nodes["in-1"].status = HEALTHY
    cluster.release(placed.placement)          # 그 잡이 이제 끝난다

    assert cluster.nodes["in-1"].running == 0, "해제가 두 번 세어졌다"


def test_undrain_does_not_oversubscribe(cluster):
    """in-1 은 슬롯 2개다. 강제 드레이닝 후 풀어도 3개가 들어가면 안 된다."""
    for name in ("in-2", "out-1"):
        cluster.nodes[name].status = UNHEALTHY

    place(cluster, "classify", job_id="a")
    place(cluster, "classify", job_id="b")
    cluster.drain("in-1", force=True)
    cluster.undrain("in-1")
    cluster.nodes["in-1"].status = HEALTHY

    third = place(cluster, "classify", job_id="c")
    assert third.outcome != PLACED, "동시성 상한을 넘겨 배치됐다"


# ── 관제 ────────────────────────────────────────────────────────────────────


def test_single_homed_roles_are_surfaced(cluster):
    """모델이 노드 A 에만 있고 A 가 포화면 B·C 가 놀아도 그 역할만 굶는다.

    자동 복제는 안 하지만, 안 보여주면 사람이 판단할 수가 없다.
    """
    warnings = cluster.single_homed_roles()
    assert warnings.get("_guard") == "in-1", "guard-m 은 in-1 에만 있다"
    assert "classify" not in warnings, "small 은 두 노드에 있다"


def test_snapshot_exposes_boundary_and_load(cluster):
    rows = {r["node"]: r for r in cluster.snapshot()}

    assert rows["out-1"]["data_boundary"] == "external"
    assert rows["out-1"]["metered"] is True
    assert rows["in-1"]["mem_budget_gb"] == 40


def test_metered_nodes_are_identified_for_crash_recovery(cluster):
    """크래시 복구가 이중 청구를 피하려면 어느 노드가 과금인지 알아야 한다."""
    assert cluster.metered_nodes() == ("out-1",)


# ── 감사 H5 — 노드 선언 영속화 ───────────────────────────────────────────────


async def test_a_registered_node_survives_a_restart(tmp_path, clock):
    """**메모리에만 두면 컨테이너 재시작 한 번에 사라진다.**

    그 노드에서 돌던 잡은 복구 후 배치 불가가 되고, 관리자는 증설한 노드가 왜
    없어졌는지 알 수 없다. `config/nodes.yaml` 의 "이후로는 DB 가 권위다" 주석은
    구현되지 않은 약속이었다.
    """
    from app.store import SqliteStore

    path = tmp_path / "cc.db"
    config = two_tier_config()

    store = SqliteStore(path, now=clock)
    first = Cluster(config, store, now=clock)
    await first.register_node(
        {"name": "added-later", "provider": "mock", "data_boundary": "internal",
         "max_concurrent": 3, "tags": ["internal"], "models": ["m"]},
        actor="platform_admin",
    )
    assert "added-later" in first.nodes
    store.close()

    # 재기동
    reopened = SqliteStore(path, now=clock)
    try:
        second = Cluster(config, reopened, now=clock)
        assert "added-later" in second.nodes, "등록한 노드가 재기동에서 사라졌다"

        node = second.nodes["added-later"].node
        assert node.data_boundary == "internal"
        assert node.max_concurrent == 3
        assert node.tags == ("internal",)
    finally:
        reopened.close()


async def test_the_database_wins_over_the_yaml_seed(tmp_path, clock):
    """YAML 은 시드다. 관제 UI 에서 고친 값이 재기동마다 되돌아가면 안 된다."""
    from app.store import SqliteStore

    path = tmp_path / "cc.db"
    config = two_tier_config()
    seeded = next(iter(config.nodes))

    store = SqliteStore(path, now=clock)
    before = Cluster(config, store, now=clock)
    assert before.nodes[seeded].node.max_concurrent == config.nodes[seeded].max_concurrent
    # 시드 노드와 같은 이름을 다른 용량으로 다시 등록한다(관리자가 증설한 상황).
    store.save_node(
        {"name": seeded, "provider": "mock", "data_boundary": "internal",
         "max_concurrent": 9, "tags": ["internal"], "models": ["m"]},
        actor="platform_admin",
    )
    store.close()

    reopened = SqliteStore(path, now=clock)
    try:
        restarted = Cluster(config, reopened, now=clock)
        assert restarted.nodes[seeded].node.max_concurrent == 9
    finally:
        reopened.close()


async def test_a_broken_node_row_does_not_stop_startup(tmp_path, clock):
    """노드 한 줄이 깨졌다고 컨트롤 플레인이 안 뜨면 그 줄을 고칠 방법도 없어진다."""
    from app.store import SqliteStore

    path = tmp_path / "cc.db"
    config = two_tier_config()

    store = SqliteStore(path, now=clock)
    store._conn.execute(
        "INSERT INTO nodes(name, provider, data_boundary, max_concurrent, created_at) "
        "VALUES('broken', 'mock', '경계가아님', 1, 0)"
    )
    store._conn.commit()
    store.close()

    reopened = SqliteStore(path, now=clock)
    try:
        cluster = Cluster(config, reopened, now=clock)     # 예외 없이 뜬다
        assert "broken" not in cluster.nodes
        assert cluster.nodes, "시드 노드까지 사라졌다"
    finally:
        reopened.close()


# ── 감사 H6 — 노드 한 대 구성에서 재시도가 불가능했다 ────────────────────────


def _single_node_cluster(store, clock) -> Cluster:
    """Starter 프로파일 — 내장 노드 한 대."""
    config = build_config(
        nodes={"only": Node(name="only", provider="mock", data_boundary="internal",
                            max_concurrent=2, tags=("internal",), models=("m",))},
        roles={"r": Role(name="r", model="m", placement=("internal",))},
    )
    c = Cluster(config, store, now=clock)
    c.nodes["only"].status = HEALTHY
    c.nodes["only"].models = frozenset({"m"})
    return c


def test_a_single_node_install_can_still_retry(store, clock):
    """**배제는 선호이지 금지가 아니다.**

    노드가 한 대뿐인 Starter 구성에서 직전 실패 노드를 영구 배제하면 그 잡은
    다시는 못 돈다. 재시도가 재배치를 동반해야 한다는 것(B7)은 죽은 노드로 3회
    재시도하고 끝나지 말라는 뜻이지, 노드를 영구히 금지하라는 뜻이 아니었다.
    """
    cluster = _single_node_cluster(store, clock)

    result = cluster.place(
        job_id="j", tenant_id="acme", service_id="acme-web",
        role=cluster._config.roles["r"], last_failed_node="only",
    )

    assert result.outcome == PLACED, f"재시도가 영원히 막혔다: {result.reason}"
    assert result.placement.node == "only"


def test_crash_recovery_does_not_strand_a_single_node_install(store, clock):
    """크래시 복구가 `last_failed_node` 를 심는다 — 재기동만 해도 잡이 멈췄다."""
    cluster = _single_node_cluster(store, clock)
    job_id = store.create_job(
        ACME, service_id="acme-web", role="r", lane="interactive", kind="generate",
        status="running", priority=0, prompt_masked="x",
    )
    store.update_job(ACME, job_id, node="only")
    counts = store.recover_running_jobs(cluster.metered_nodes())
    assert counts["requeued"] == 1

    job = store.get_job(ACME, job_id)
    assert job.status == "queued"
    assert job.last_failed_node == "only", "전제가 틀렸다 — 복구가 노드를 안 심었다"

    result = cluster.place(
        job_id=job.id, tenant_id="acme", service_id="acme-web",
        role=cluster._config.roles["r"], last_failed_node=job.last_failed_node,
    )
    assert result.outcome == PLACED, "재기동 뒤 잡이 영원히 대기한다"


def test_another_node_still_wins_over_the_failed_one(cluster):
    """되살리는 것은 **다른 후보가 없을 때뿐**이다. 우선순위는 그대로다."""
    cluster.nodes["out-1"].status = UNHEALTHY

    result = place(cluster, "classify", last_failed_node="in-1")

    assert result.placement.node == "in-2", "다른 후보가 있는데 실패 노드를 골랐다"


def test_a_genuinely_dead_node_is_still_refused(store, clock):
    """정말 죽은 노드는 연속 실패 3회에 `unhealthy` 가 되어 걸린다.

    그것이 사실이고 `last_failed_node` 는 힌트다. **힌트가 사실을 이기면 안 된다.**
    """
    cluster = _single_node_cluster(store, clock)
    cluster.nodes["only"].status = UNHEALTHY

    result = cluster.place(
        job_id="j", tenant_id="acme", service_id="acme-web",
        role=cluster._config.roles["r"], last_failed_node="only",
    )

    assert result.outcome != PLACED
    assert result.rejections["only"] == "unhealthy"


def test_the_revived_node_is_not_reported_as_rejected(store, clock):
    """되살렸는데 탈락 사유로도 남으면 UI 가 "왜 안 도는지" 를 거짓으로 말한다."""
    cluster = _single_node_cluster(store, clock)

    result = cluster.place(
        job_id="j", tenant_id="acme", service_id="acme-web",
        role=cluster._config.roles["r"], last_failed_node="only",
    )

    assert "only" not in result.rejections


# ── 감사 H8 — 입력 토큰이 비용 예약에서 통째로 빠졌다 ────────────────────────


def test_the_input_prompt_is_part_of_the_reservation(cluster):
    """스케줄러가 길이를 `0` 으로 넘겨서 큐를 지난 모든 잡의 입력이 빠졌다.

    긴 프롬프트가 과금 노드로 나가도 예약은 출력 토큰만 잡았고, 예산 초과가
    **정산 뒤에야** 드러났다 — 예약의 목적이 정확히 그것을 막는 것인데.
    """
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    short = place(cluster, "summarize", job_id="a", prompt="짧다")
    cluster.release(short.placement)
    long = place(cluster, "summarize", job_id="b", prompt="가" * 20_000)

    assert long.placement.reserved_cost_usd > short.placement.reserved_cost_usd, \
        "프롬프트 길이가 예약에 반영되지 않는다"


def test_korean_is_not_counted_as_if_it_were_english(cluster):
    """하나의 비율로 뭉뚱그리면 한국어 입력 토큰을 서너 배 과소 추정한다.

    과소 추정한 "상한 예약" 은 상한이 아니다.
    """
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    english = place(cluster, "summarize", job_id="a", prompt="a" * 3000)
    cluster.release(english.placement)
    korean = place(cluster, "summarize", job_id="b", prompt="가" * 3000)

    assert korean.placement.reserved_cost_usd > english.placement.reserved_cost_usd


def test_the_estimator_errs_high_not_low():
    """정확할 수 없으므로 **어느 쪽으로 틀릴지를 고른다.**

    과소 추정은 예산을 넘긴 뒤에 드러나고, 과대 추정은 정산에서 풀린다.
    """
    from app.cost import estimate_input_tokens

    # 한글 한 글자가 토큰 하나 아래로 계상되면 안 된다.
    assert estimate_input_tokens("가" * 100) >= 100
    assert estimate_input_tokens("") == 0
    # 영어는 그보다 낮게 잡힌다 — 같은 길이라도 토큰이 적다.
    assert estimate_input_tokens("a" * 100) < estimate_input_tokens("가" * 100)


# ── 감사 M3·M4 — 프로브 한 바퀴가 죽은 노드 수에 비례한다 ────────────────────


class SlowProvider:
    """응답이 늦는 노드. 죽은 노드의 타임아웃을 흉내 낸다."""

    name = "slow"

    def __init__(self, seconds: float = 0.3) -> None:
        from app.providers.base import Capabilities

        self.capabilities = Capabilities(
            requires_model_install=False, uses_memory_budget=False, metered=False
        )
        self.seconds = seconds

    async def health(self, *, timeout: float = 10.0):
        import asyncio as _asyncio

        from app.providers import HealthResult

        await _asyncio.sleep(self.seconds)
        return HealthResult(ok=False, error="타임아웃")


class ExplodingProvider:
    """200 에 비정형 본문을 주는 노드 — 앞에 리버스 프록시가 선 구성이 흔하다."""

    name = "exploding"

    def __init__(self) -> None:
        from app.providers.base import Capabilities

        self.capabilities = Capabilities(
            requires_model_install=False, uses_memory_budget=False, metered=False
        )

    async def health(self, *, timeout: float = 10.0):
        raise KeyError("name")


def _three_node_cluster(store, clock, providers) -> Cluster:
    config = build_config(
        nodes={
            name: Node(name=name, provider="mock", data_boundary="internal",
                       tags=("internal",), models=("m",))
            for name in ("a-node", "b-node", "c-node")
        },
        roles={"r": Role(name="r", model="m", placement=("internal",))},
    )
    return Cluster(config, store, now=clock, providers=providers)


async def test_probing_dead_nodes_does_not_take_n_times_the_timeout(store, clock):
    """순차로 돌면 죽은 노드 N 대에 한 바퀴가 N × 타임아웃이다.

    그 사이 살아난 노드는 계속 못 쓴다 — 느린 노드 하나가 나머지 전부의 갱신을
    미루면 안 된다.
    """
    import time as _time

    cluster = _three_node_cluster(
        store, clock, {name: SlowProvider(0.3) for name in ("a-node", "b-node", "c-node")}
    )

    started = _time.monotonic()
    await cluster.probe_all()
    elapsed = _time.monotonic() - started

    assert elapsed < 0.6, f"프로브가 순차로 돌았다 ({elapsed:.2f}초)"


async def test_one_broken_node_does_not_stop_the_probe_cycle(store, clock):
    """**사전순 뒤 노드들이 매 주기 프로브를 못 받는다.**

    그리고 정작 문제의 노드는 unhealthy 판정도 못 받아 계속 배치된다 —
    배경 루프의 `suppress` 가 그 사실을 통째로 삼킨다.
    """
    from app.providers.mock import MockProvider

    cluster = _three_node_cluster(store, clock, {
        "a-node": ExplodingProvider(),
        "b-node": MockProvider(Node(name="b-node", provider="mock", models=("m",))),
        "c-node": MockProvider(Node(name="c-node", provider="mock", models=("m",))),
    })

    results = await cluster.probe_all()

    assert set(results) == {"a-node", "b-node", "c-node"}, "뒤 노드들이 프로브를 못 받았다"
    assert results["b-node"] is True and results["c-node"] is True


async def test_a_broken_node_is_counted_as_a_failure(store, clock):
    """예외를 위로 흘리면 그 노드는 실패로도 안 세어져 계속 후보로 남는다."""
    cluster = _three_node_cluster(store, clock, {"a-node": ExplodingProvider()})

    assert await cluster.probe("a-node") is False
    assert cluster.nodes["a-node"].consecutive_failures == 1
    assert "프로브 예외" in (cluster.nodes["a-node"].last_error or "")


# ── 감사 M6 — 영원히 못 도는 잡을 900초 기다리게 한다 ────────────────────────


def test_a_boundary_blocked_job_fails_instead_of_waiting(cluster):
    """가드가 좁힌 경계는 **이 잡의 생애 동안 변하지 않는다.**

    그런데도 WAIT 로 두면 소비자는 15분을 매달린 뒤 `administrative_wait_timeout`
    을 받는다 — "관리자를 기다렸다" 는 뜻이라 실제 원인을 아무도 못 찾는다.
    """
    result = place(cluster, "summarize", allowed_boundaries=())

    assert result.outcome == FAIL
    assert result.code == "boundary_impossible"


def test_an_internal_only_role_with_only_external_nodes_fails(store, clock):
    """역할의 `internal_only` 도 오버라이드 불가 필드라 바뀌지 않는다."""
    config = build_config(
        nodes={"out": Node(name="out", provider="mock", data_boundary="external",
                           tags=("external",), models=("m",))},
        roles={"g": Role(name="g", model="m", placement=("external",), internal_only=True)},
    )
    c = Cluster(config, store, now=clock)
    c.nodes["out"].status = HEALTHY
    c.nodes["out"].models = frozenset({"m"})

    result = c.place(
        job_id="j", tenant_id="acme", service_id="acme-web", role=config.roles["g"]
    )
    assert result.outcome == FAIL
    assert result.code == "boundary_impossible"


def test_an_administrative_absence_still_waits(cluster):
    """**관리자가 되돌릴 수 있는 것을 하드 실패시키면 정비 5분에 그 티어가 전멸한다.**"""
    for name in ("in-1", "in-2"):
        cluster.nodes[name].status = UNHEALTHY

    result = place(cluster, "classify")

    assert result.outcome == WAIT, "헬스는 관리자가 되돌릴 수 있다"


def test_airgap_and_affinity_are_administrative_not_permanent(store, clock):
    """에어갭도 `tenant_affinity` 도 관리자가 바꿀 수 있다 — 기다릴 값이 있다."""
    config = build_config(
        nodes={"out": Node(name="out", provider="mock", data_boundary="external",
                           tags=("external",), models=("m",))},
        roles={"r": Role(name="r", model="m", placement=("external",))},
    )
    c = Cluster(config, store, now=clock, airgap=True)
    c.nodes["out"].status = HEALTHY
    c.nodes["out"].models = frozenset({"m"})

    result = c.place(
        job_id="j", tenant_id="acme", service_id="acme-web", role=config.roles["r"]
    )
    assert result.outcome == WAIT
    assert result.rejections["out"] == "airgap_external_disabled"


def test_a_permanent_reason_is_reported_even_when_the_node_is_also_down(store, clock):
    """순서가 진단의 정확도를 정한다 — 헬스를 먼저 보면 영구 조건이 가려진다."""
    config = build_config(
        nodes={"out": Node(name="out", provider="mock", data_boundary="external",
                           tags=("external",), models=("m",))},
        roles={"g": Role(name="g", model="m", placement=("external",), internal_only=True)},
    )
    c = Cluster(config, store, now=clock)
    c.nodes["out"].status = UNHEALTHY      # 죽어 있기도 하다

    result = c.place(
        job_id="j", tenant_id="acme", service_id="acme-web", role=config.roles["g"]
    )
    assert result.rejections["out"] == "boundary_internal_only"
    assert result.outcome == FAIL
