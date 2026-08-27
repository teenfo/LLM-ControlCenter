"""스토어 계약 — 테넌트 격리가 최우선이다.

한 번의 스코프 누락이 다른 조직의 프롬프트를 노출시킨다.
그래서 격리 테스트가 파일 맨 앞에 온다.
"""

from __future__ import annotations

import inspect

import pytest

from app.store import (
    PlatformScope,
    ScopeViolation,
    SqliteStore,
    StoreError,
    TenantScope,
)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> SqliteStore:
    s = SqliteStore(":memory:", now=clock)
    for tenant in ("acme", "globex"):
        s.create_tenant(tenant, tenant.title(), end_user_salt=f"salt-{tenant}".encode())
        s.create_service(TenantScope(tenant), f"{tenant}-web", "web")
    yield s
    s.close()


ACME = TenantScope("acme")
GLOBEX = TenantScope("globex")


def make_job(store: SqliteStore, scope: TenantScope, **overrides) -> str:
    fields = {
        "service_id": f"{scope.tenant_id}-web",
        "role": "summarize",
        "lane": "interactive",
        "prompt_masked": "안녕하세요",
    }
    fields.update(overrides)
    return store.create_job(scope, **fields)


# ── 테넌트 격리 ──────────────────────────────────────────────────────────────


def test_cannot_read_another_tenants_job(store):
    job_id = make_job(store, ACME)

    assert store.get_job(ACME, job_id) is not None
    assert store.get_job(GLOBEX, job_id) is None, "다른 테넌트의 잡이 보였다"


def test_cannot_list_another_tenants_jobs(store):
    make_job(store, ACME)
    make_job(store, ACME)
    make_job(store, GLOBEX)

    assert len(store.list_jobs(ACME)) == 2
    assert len(store.list_jobs(GLOBEX)) == 1


def test_cannot_update_another_tenants_job(store):
    job_id = make_job(store, ACME)

    assert store.update_job(GLOBEX, job_id, status="cancelled") is False
    assert store.get_job(ACME, job_id).status == "queued", "다른 테넌트가 잡을 바꿨다"


def test_cannot_move_a_job_between_tenants(store):
    """tenant_id 변경은 격리 그 자체를 무너뜨린다."""
    job_id = make_job(store, ACME)
    with pytest.raises(ScopeViolation):
        store.update_job(ACME, job_id, tenant_id="globex")


def test_usage_and_spend_are_scoped(store):
    store.record_usage(ACME, service_id="acme-web", role="summarize", cost_usd=5.0)
    store.record_usage(GLOBEX, service_id="globex-web", role="summarize", cost_usd=3.0)

    assert store.spend_since(ACME, 0) == 5.0
    assert store.spend_since(GLOBEX, 0) == 3.0


def test_filter_events_are_scoped(store):
    store.record_filter_event(ACME, rule_id="kr_rrn", stage="pattern", action="full")
    assert len(store.list_filter_events(ACME)) == 1
    assert len(store.list_filter_events(GLOBEX)) == 0


def test_role_overrides_are_scoped(store):
    store.set_role_override(ACME, "summarize", {"timeout": 60})

    assert store.get_role_overrides(ACME) == {"summarize": {"timeout": 60}}
    assert store.get_role_overrides(GLOBEX) == {}


def test_audit_is_scoped(store):
    store.audit("admin", "did_thing", tenant_id="acme")
    assert len(store.list_audit(ACME)) == 1
    assert len(store.list_audit(GLOBEX)) == 0


def test_services_and_tokens_are_scoped(store):
    store.create_token(ACME, "acme-web", "hash-a", "pfx")

    assert len(store.list_tokens(ACME)) == 1
    assert len(store.list_tokens(GLOBEX)) == 0
    assert store.get_service(GLOBEX, "acme-web") is None


# ── 스코프 강제 ──────────────────────────────────────────────────────────────


def test_tenant_scope_rejects_empty_id():
    with pytest.raises(ScopeViolation):
        TenantScope("")


@pytest.mark.parametrize("bad", [None, "acme", 42])
def test_scoped_methods_reject_non_scope(store, bad):
    """문자열을 스코프 자리에 넘기는 실수를 타입이 아니라 런타임이 잡는다."""
    with pytest.raises(ScopeViolation):
        store.list_jobs(bad)


def test_no_unscoped_query_path_is_exposed(store):
    """스코프 없는 범용 실행 경로가 공개 API 에 없다.

    있으면 언젠가 누군가 그것을 쓴다.
    """
    public = {n for n in dir(store) if not n.startswith("_")}
    assert not public & {"execute", "executemany", "query", "raw", "conn", "connection"}


def test_tenant_data_methods_require_scope_first(store):
    """테넌트 데이터를 만지는 메서드는 첫 인자가 scope 다 — 빠뜨릴 수 없게."""
    tenant_scoped = [
        "create_job", "get_job", "list_jobs", "update_job", "record_usage",
        "spend_since", "record_filter_event", "list_filter_events",
        "set_role_override", "get_role_overrides", "clear_role_override",
        "list_audit", "create_service", "get_service", "list_services",
        "create_token", "list_tokens", "revoke_token", "purge_end_user",
    ]
    for name in tenant_scoped:
        params = list(inspect.signature(getattr(store, name)).parameters)
        assert params and params[0] == "scope", f"{name} 의 첫 인자가 scope 가 아니다"


def test_cross_tenant_query_requires_platform_scope(store):
    with pytest.raises(ScopeViolation):
        store.usage_across_tenants(ACME, since=0)  # type: ignore[arg-type]


def test_platform_scope_requires_a_reason():
    """왜 경계를 넘는지 적지 않고는 넘을 수 없다."""
    with pytest.raises(ScopeViolation):
        PlatformScope(actor="admin", reason="")


def test_cross_tenant_query_is_audited(store):
    store.record_usage(ACME, service_id="acme-web", role="r", cost_usd=1.0)
    store.record_usage(GLOBEX, service_id="globex-web", role="r", cost_usd=2.0)

    rows = store.usage_across_tenants(
        PlatformScope(actor="platform-admin", reason="월간 정산"), since=0
    )
    assert {r["tenant_id"] for r in rows} == {"acme", "globex"}

    audit = store._conn.execute(
        "SELECT * FROM admin_audit WHERE action='usage_across_tenants'"
    ).fetchall()
    assert len(audit) == 1, "경계를 넘은 사실이 감사에 안 남았다"
    assert "월간 정산" in audit[0]["detail_json"]


# ── 크래시 복구 ──────────────────────────────────────────────────────────────


def test_crash_recovery_requeues_free_node_jobs(store):
    job_id = make_job(store, ACME)
    store.update_job(ACME, job_id, status="running", node="free-node")

    result = store.recover_running_jobs(metered_nodes=[])

    job = store.get_job(ACME, job_id)
    assert result["requeued"] == 1
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.last_failed_node == "free-node", "재시도가 같은 노드를 다시 고를 수 있다"
    assert job.node is None


def test_crash_recovery_does_not_requeue_metered_jobs(store):
    """과금 노드에서 돌던 잡을 자동 재큐하면 같은 작업이 두 번 청구된다.

    노드에 취소 의미론이 없으므로 막지는 못하고 드러내기만 한다.
    """
    job_id = make_job(store, ACME)
    store.update_job(ACME, job_id, status="running", node="cloud-node")

    result = store.recover_running_jobs(metered_nodes=["cloud-node"])

    job = store.get_job(ACME, job_id)
    assert result["needs_review"] == 1
    assert result["requeued"] == 0
    assert job.status == "needs_review"
    assert job.error_code == "possible_double_execution"


# ── 스냅샷과 갱신 ────────────────────────────────────────────────────────────


def test_job_snapshots_policy_at_creation(store):
    job_id = make_job(
        store, ACME,
        placement=["internal", "external"],
        tier_models={"external": "cloud-m"},
        options={"temperature": 0.1},
        timeout_s=90,
    )
    job = store.get_job(ACME, job_id)

    assert job.placement == ("internal", "external")
    assert job.tier_models == {"external": "cloud-m"}
    assert job.options == {"temperature": 0.1}
    assert job.timeout_s == 90


def test_unknown_job_field_is_rejected(store):
    with pytest.raises(StoreError):
        make_job(store, ACME, nonexistent_field="x")

    job_id = make_job(store, ACME)
    with pytest.raises(StoreError):
        store.update_job(ACME, job_id, nonexistent_field="x")


def test_claim_queued_does_not_read_prompt_bodies(store):
    """스케줄러는 전 테넌트를 가로지르므로 프롬프트 본문을 읽지 않는다.

    배치 결정에 필요한 것은 정책과 메타데이터뿐이다.
    """
    make_job(store, ACME, prompt_masked="민감할 수 있는 내용")
    claimed = store.claim_queued("interactive", limit=10)

    assert len(claimed) == 1
    assert claimed[0].prompt_masked is None
    assert claimed[0].prompt_cipher is None


def test_claim_queued_orders_by_priority_then_age(store, clock):
    low = make_job(store, ACME, priority=0)
    clock.advance(10)
    high = make_job(store, ACME, priority=5)

    claimed = store.claim_queued("interactive", limit=10)
    assert [j.id for j in claimed] == [high, low]


# ── 가드 이벤트는 값을 남기지 않는다 ──────────────────────────────────────────


def test_filter_event_api_has_no_value_parameter(store):
    """매칭된 값을 받을 수 있게 두면 언젠가 누군가 넣는다.

    감사 로그가 새 유출 경로가 되면 가드의 나머지 노력이 무의미해진다.
    """
    params = set(inspect.signature(store.record_filter_event).parameters)
    assert not params & {"value", "values", "matched", "matches", "text", "sample"}


def test_filter_event_stores_only_metadata(store):
    store.record_filter_event(
        ACME, rule_id="kr_rrn", stage="pattern", action="full",
        match_count=2, offsets=[(10, 24), (40, 54)],
    )
    row = store.list_filter_events(ACME)[0]

    assert row["rule_id"] == "kr_rrn"
    assert row["match_count"] == 2
    assert row["offsets_json"] == "[[10, 24], [40, 54]]"
    assert "901010" not in str(dict(row)), "실제 값이 남았다"


# ── 보존·파기 ────────────────────────────────────────────────────────────────


def test_retention_clears_cipher_before_deleting_jobs(store, clock):
    """원문 암호문과 잡 본체를 다른 주기로 지운다.

    마스킹본은 프롬프트 개선의 재료라 오래 두되, 원문은 짧게 두는 것이 거버넌스의 요구다.
    """
    job_id = make_job(store, ACME, prompt_cipher=b"ciphertext", prompt_nonce=b"nonce")

    clock.advance(8 * 86400)  # 원문 보존(7일)만 지남
    store.purge_expired(job_retention_days=30, raw_prompt_retention_days=7)

    job = store.get_job(ACME, job_id)
    assert job is not None, "잡 본체는 아직 남아야 한다"
    assert job.prompt_cipher is None, "원문 암호문이 안 지워졌다"
    assert job.prompt_masked == "안녕하세요", "마스킹본은 남아야 한다"


def test_retention_deletes_finished_jobs_after_full_period(store, clock):
    job_id = make_job(store, ACME)
    store.update_job(ACME, job_id, status="ok", finished_at=clock.now)

    clock.advance(31 * 86400)
    counts = store.purge_expired(job_retention_days=30)

    assert counts["jobs"] == 1
    assert store.get_job(ACME, job_id) is None


def test_retention_keeps_unfinished_jobs(store, clock):
    """오래됐어도 안 끝난 잡은 지우지 않는다."""
    job_id = make_job(store, ACME)
    clock.advance(365 * 86400)
    store.purge_expired(job_retention_days=30)
    assert store.get_job(ACME, job_id) is not None


def test_purge_end_user_only_touches_that_user(store):
    mine = make_job(store, ACME, end_user_hash="u-1")
    theirs = make_job(store, ACME, end_user_hash="u-2")
    other_tenant = make_job(store, GLOBEX, end_user_hash="u-1")

    store.purge_end_user(ACME, "u-1")

    assert store.get_job(ACME, mine) is None
    assert store.get_job(ACME, theirs) is not None
    assert store.get_job(GLOBEX, other_tenant) is not None, "다른 테넌트의 동명 사용자가 지워졌다"


def test_purge_audit_does_not_record_what_was_deleted(store):
    make_job(store, ACME, end_user_hash="u-1", prompt_masked="지워질 내용")
    store.purge_end_user(ACME, "u-1")

    entry = [a for a in store.list_audit(ACME) if a["action"] == "purge_end_user"][0]
    assert "지워질 내용" not in entry["detail_json"]
    assert "u-1" not in entry["detail_json"], "무엇을 지웠는지가 아니라 얼마나 지웠는지만 남긴다"


def test_purge_tenant_clears_dek(store):
    """DEK 폐기가 가장 강한 삭제다 — 백업에 남은 암호문도 못 연다."""
    make_job(store, ACME)
    store._conn.execute("UPDATE tenants SET dek_wrapped=? WHERE id='acme'", (b"wrapped",))

    store.purge_tenant(PlatformScope(actor="platform-admin", reason="계약 종료"), "acme")

    row = store._conn.execute("SELECT * FROM tenants WHERE id='acme'").fetchone()
    assert row["dek_wrapped"] is None
    assert row["status"] == "purged"
    assert store.get_tenant("acme") is None


def test_purge_tenant_leaves_other_tenants_intact(store):
    survivor = make_job(store, GLOBEX)
    make_job(store, ACME)

    store.purge_tenant(PlatformScope(actor="admin", reason="계약 종료"), "acme")

    assert store.get_job(GLOBEX, survivor) is not None
    assert store.list_jobs(ACME) == []


# ── 스키마 ──────────────────────────────────────────────────────────────────


def test_schema_version_is_recorded(store):
    assert store.schema_version >= 1


def test_reopening_same_db_is_idempotent(tmp_path):
    """마이그레이션이 ADD COLUMN 전용이라 재기동이 안전하다."""
    path = tmp_path / "t.db"
    first = SqliteStore(path)
    first.create_tenant("acme", "Acme", end_user_salt=b"s")
    first.close()

    second = SqliteStore(path)
    assert second.get_tenant("acme") is not None
    second.close()
