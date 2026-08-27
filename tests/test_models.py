"""모델 생애주기 — 탐지 · 승인 · 설치 · 삭제 차단."""

from __future__ import annotations

import pytest

from app.cluster import HEALTHY, Cluster
from app.config import CatalogEntry, Config, GuardSettings, Lane, Node, Pricing, Role, Thresholds
from app.i18n import ApiError
from app.models import (
    BLOCK_EMBEDDING_ROLE,
    BLOCK_INSTALLING,
    BLOCK_QUEUED_JOBS,
    BLOCK_ROLE_IN_USE,
    BLOCK_RUNNING,
    FAILED,
    PENDING,
    READY,
    ModelRegistrar,
)
from app.store import SqliteStore, TenantScope

ACME = TenantScope("acme")


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_config() -> Config:
    return Config(
        nodes={
            "big": Node(name="big", provider="mock", data_boundary="internal",
                        mem_budget_gb=40, tags=("internal",), models=("small",)),
            "small-node": Node(name="small-node", provider="mock", data_boundary="internal",
                               mem_budget_gb=8, tags=("internal",), models=("small",)),
            "cloud": Node(name="cloud", provider="mock", data_boundary="external",
                          tags=("external",), models=("cloud-m",), metered_override=True),
        },
        roles={
            "summarize": Role(name="summarize", model="small", placement=("internal",)),
            "heavy": Role(name="heavy", model="huge", placement=("internal",)),
            "embed": Role(name="embed", model="embed-m", kind="embed", placement=("internal",)),
        },
        lanes={"interactive": Lane("interactive", 2), "batch": Lane("batch", 1)},
        guard_rules=(),
        guard_settings=GuardSettings(),
        pricing=Pricing(table={"mock": {"*": {"input_per_mtok": 0.0, "output_per_mtok": 0.0}}}),
        thresholds=Thresholds(),
        catalog=(
            CatalogEntry(name="small", provider="mock", est_size_gb=5.0),
            CatalogEntry(name="huge", provider="mock", est_size_gb=21.0),
            CatalogEntry(name="embed-m", provider="mock", est_size_gb=1.0),
            CatalogEntry(name="cloud-m", provider="mock", est_size_gb=0.0, note="클라우드"),
        ),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> SqliteStore:
    s = SqliteStore(":memory:", now=clock)
    s.create_tenant("acme", "Acme", end_user_salt=b"s")
    s.create_service(ACME, "acme-web", "web")
    yield s
    s.close()


@pytest.fixture
def notifications() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def registrar(store, clock, notifications) -> ModelRegistrar:
    config = make_config()
    cluster = Cluster(config, store, now=clock)
    for name, state in cluster.nodes.items():
        state.models = frozenset(config.nodes[name].models)
        state.status = HEALTHY
    return ModelRegistrar(
        config, cluster, store, now=clock,
        notify=lambda e, d: notifications.append((e, d)),
    )


# ── 요청 ────────────────────────────────────────────────────────────────────


def test_request_creates_pending_and_notifies(registrar, notifications):
    """사람이 모르면 조용히 멈추는 지점 — 알림의 기준이 정확히 이것이다."""
    request = registrar.request_install("big", "small-v2", requested_by="admin")

    assert request.status == PENDING
    assert ("model_approval_pending", {"node": "big", "model": "small-v2"}) in notifications


def test_size_gate_rejects_before_downloading(registrar):
    """20GB 를 잘 받아 놓고 실행할 때마다 잡이 죽는 것을 막는다."""
    with pytest.raises(ApiError) as exc:
        registrar.request_install("small-node", "huge")   # 21GB vs 예산 8GB

    assert exc.value.code == "oversized_model"
    assert exc.value.status == 409


def test_size_gate_allows_a_node_with_room(registrar):
    request = registrar.request_install("big", "huge")   # 21GB vs 예산 40GB
    assert request.status == PENDING


def test_request_is_per_node_model_pair(registrar):
    """같은 모델을 3대에 얹으려면 요청 3건이다."""
    a = registrar.request_install("big", "new-m")
    b = registrar.request_install("small-node", "new-m")

    assert a.id != b.id
    assert {r["node"] for r in registrar._store.list_model_requests()} == {"big", "small-node"}


def test_duplicate_request_returns_the_existing_one(registrar):
    first = registrar.request_install("big", "new-m")
    second = registrar.request_install("big", "new-m")
    assert first.id == second.id


def test_cloud_node_has_no_install_lifecycle(registrar):
    with pytest.raises(ApiError):
        registrar.request_install("cloud", "anything")


def test_unknown_node_is_rejected(registrar):
    with pytest.raises(ApiError) as exc:
        registrar.request_install("ghost", "m")
    assert exc.value.status == 404


# ── 자동 탐지 ────────────────────────────────────────────────────────────────


def test_detect_missing_creates_requests_without_blocking_lanes(registrar):
    """배치 필터가 그 노드만 스킵하므로 레인은 막히지 않는다. 여기서는 요청만 남긴다."""
    created = registrar.detect_missing()

    models = {r.model for r in created}
    assert "embed-m" in models, "역할이 참조하는데 없는 모델이 탐지되지 않았다"
    assert "huge" in models


def test_detect_missing_skips_nodes_that_cannot_hold_the_model(registrar):
    """크기 게이트에 걸린 노드는 건너뛴다 — 다른 노드가 받을 수 있다."""
    created = registrar.detect_missing()

    huge_nodes = {r.node for r in created if r.model == "huge"}
    assert huge_nodes == {"big"}, "8GB 노드에 21GB 모델을 요청했다"


def test_detect_missing_does_not_request_before_inventory_is_known(store, clock):
    """프로브 전에는 인벤토리를 모른다. 모른다는 이유로 요청하면 안 된다."""
    config = make_config()
    cluster = Cluster(config, store, now=clock)   # models 를 안 채운 상태
    registrar = ModelRegistrar(config, cluster, store, now=clock)

    assert registrar.detect_missing() == []


# ── 승인 · 설치 ──────────────────────────────────────────────────────────────


async def test_approve_then_pull_reaches_ready(registrar, notifications):
    request = registrar.request_install("big", "new-m")
    registrar.approve(request.id, actor="platform-admin")

    results = await registrar.process_approved()

    assert results[0].status == READY
    assert results[0].progress == 100
    assert ("model_ready", {"node": "big", "model": "new-m"}) in notifications


async def test_install_updates_inventory_immediately(registrar):
    """다음 헬스 주기를 기다리면 대기 잡이 그만큼 더 굶는다."""
    request = registrar.request_install("big", "new-m")
    registrar.approve(request.id, actor="admin")
    await registrar.process_approved()

    assert "new-m" in registrar._cluster.nodes["big"].models


async def test_pull_failure_is_recorded_and_notified(registrar, notifications):
    request = registrar.request_install("big", "new-m")
    registrar.approve(request.id, actor="admin")
    registrar._cluster.provider_for("big").kill()

    results = await registrar.process_approved()

    assert results[0].status == FAILED
    assert results[0].error
    assert any(e == "model_failed" for e, _ in notifications)


def test_rejection_gives_waiting_jobs_a_clear_reason(registrar):
    """거부한 모델은 다시 물어보지 않는다 — 기다리던 잡은 무한 대기 대신 명확한 오류로 끝난다."""
    request = registrar.request_install("big", "new-m")
    registrar.reject(request.id, actor="admin", reason="라이선스 미확인")

    assert registrar.dead_request_for("big", "new-m") == "라이선스 미확인"


def test_pending_request_is_not_dead(registrar):
    registrar.request_install("big", "new-m")
    assert registrar.dead_request_for("big", "new-m") is None


def test_rejected_request_can_be_resubmitted(registrar):
    """사람이 다시 올리는 경우 — 되살린다."""
    request = registrar.request_install("big", "new-m")
    registrar.reject(request.id, actor="admin", reason="보류")

    again = registrar.request_install("big", "new-m", requested_by="admin")
    assert again.status == PENDING
    assert again.error is None


def test_approval_and_rejection_are_audited(registrar, store):
    request = registrar.request_install("big", "new-m", requested_by="admin")
    registrar.approve(request.id, actor="platform-admin")

    actions = {a["action"] for a in store._conn.execute("SELECT * FROM admin_audit")}
    assert {"request_model_install", "approve_model_install"} <= actions


# ── 삭제 차단 5종 ────────────────────────────────────────────────────────────


def test_role_in_use_blocks_deletion(registrar):
    """지워도 다음 요청에서 곧바로 재설치 대기 — 역할을 먼저 바꿔야 한다."""
    assert BLOCK_ROLE_IN_USE in registrar.deletion_blockers("big", "small")


def test_embedding_role_blocks_deletion(registrar):
    """임베딩은 동기 경로라 소비자가 즉시 503 을 받는다."""
    blockers = registrar.deletion_blockers("big", "embed-m")
    assert BLOCK_EMBEDDING_ROLE in blockers


def test_queued_jobs_block_deletion(registrar, store):
    store.create_job(ACME, service_id="acme-web", role="summarize", lane="interactive")
    assert BLOCK_QUEUED_JOBS in registrar.deletion_blockers("big", "small")


def test_running_jobs_block_deletion(registrar, store):
    job_id = store.create_job(ACME, service_id="acme-web", role="summarize", lane="interactive")
    store.update_job(ACME, job_id, status="running", node="big", model="small")

    assert BLOCK_RUNNING in registrar.deletion_blockers("big", "small")


def test_in_progress_install_blocks_deletion(registrar):
    """부분 파일이 남는다."""
    request = registrar.request_install("big", "new-m")
    registrar.approve(request.id, actor="admin")

    assert BLOCK_INSTALLING in registrar.deletion_blockers("big", "new-m")


async def test_deletion_succeeds_when_nothing_blocks(registrar):
    request = registrar.request_install("big", "unused-m")
    registrar.approve(request.id, actor="admin")
    await registrar.process_approved()

    await registrar.delete("big", "unused-m", actor="admin")
    assert "unused-m" not in registrar._cluster.nodes["big"].models


async def test_deletion_raises_with_the_blocking_reason(registrar):
    with pytest.raises(ApiError) as exc:
        await registrar.delete("big", "small", actor="admin")

    assert exc.value.code == "model_in_use"
    assert BLOCK_ROLE_IN_USE in exc.value.params["reason"]


async def test_no_force_flag_exists(registrar):
    """force 는 없다 — 다섯 가지가 전부 실제 고장으로 이어진다."""
    import inspect

    params = set(inspect.signature(registrar.delete).parameters)
    assert "force" not in params


async def test_deletion_removes_the_request_row(registrar, store):
    """ready 로 두면 다음 탐지에서 되살아나고, rejected 로 두면 이후 잡이
    "설치가 거부됨" 이라는 거짓 사유로 하드 실패한다."""
    request = registrar.request_install("big", "unused-m")
    registrar.approve(request.id, actor="admin")
    await registrar.process_approved()

    await registrar.delete("big", "unused-m", actor="admin")

    assert store.get_model_request("big", "unused-m") is None
    assert registrar.dead_request_for("big", "unused-m") is None


# ── 카탈로그 ────────────────────────────────────────────────────────────────


def test_catalog_search_is_local_only(registrar):
    """모델 레지스트리를 스크레이핑하지 않는다 — 남의 사이트 개편에 제품이 끌려 죽는다."""
    assert {e.name for e in registrar.catalog_search()} == {
        "small", "huge", "embed-m", "cloud-m"
    }
    assert [e.name for e in registrar.catalog_search("huge")] == ["huge"]
    assert [e.name for e in registrar.catalog_search("클라우드")] == ["cloud-m"]


def test_unknown_model_has_no_size_constraint(registrar):
    """모르는 것을 이유로 잡을 죽이지 않는다."""
    assert registrar.estimated_size_gb("never-heard-of-it") == 0.0
    registrar.request_install("small-node", "never-heard-of-it")  # 크기 게이트를 안 탄다


# ── 관제 ────────────────────────────────────────────────────────────────────


def test_snapshot_shows_why_a_model_cannot_be_deleted(registrar):
    rows = {(r["node"], r["model"]): r for r in registrar.snapshot()}

    assert BLOCK_ROLE_IN_USE in rows[("big", "small")]["deletion_blockers"]
    assert rows[("big", "small")]["est_size_gb"] == 5.0


def test_pending_count_drives_the_approval_card(registrar):
    assert registrar.pending_count() == 0
    registrar.request_install("big", "new-m")
    assert registrar.pending_count() == 1
