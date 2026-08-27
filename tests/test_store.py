"""스토어 계약 — 테넌트 격리가 최우선이다.

한 번의 스코프 누락이 다른 조직의 프롬프트를 노출시킨다.
그래서 격리 테스트가 파일 맨 앞에 온다.
"""

from __future__ import annotations

import inspect
import sqlite3

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


def test_an_older_build_can_read_a_newer_schema(tmp_path):
    """**전진 호환** — 롤백이 이미지 태그를 되돌리는 것으로 끝나야 한다.

    스키마가 ADD COLUMN 전용이므로, 신버전이 컬럼을 더한 DB 를 구버전이 그대로
    읽을 수 있어야 한다. 여기서는 지금 코드를 "구버전" 으로 두고, 아직 존재하지
    않는 컬럼을 직접 붙여 "신버전이 쓴 DB" 를 흉내 낸다.

    이게 깨지면 업그레이드가 편도가 된다 — 되돌릴 수 없는 업그레이드는 설치처가
    아예 안 하게 된다.
    """
    path = tmp_path / "t.db"
    store = SqliteStore(path)
    store.create_tenant("acme", "Acme", end_user_salt=b"s")
    scope = TenantScope("acme")
    store.create_service(scope, "web", "web")
    job_id = store.create_job(
        scope, service_id="web", role="summarize", lane="interactive",
        prompt_masked="마스킹본",
    )

    # 미래 버전이 컬럼을 더했다고 치자. ADD COLUMN 전용 규칙을 지킨 형태다.
    store._conn.execute("ALTER TABLE jobs ADD COLUMN future_field TEXT")
    store._conn.execute("ALTER TABLE tenants ADD COLUMN future_flag INTEGER DEFAULT 0")
    store._conn.execute(
        "UPDATE jobs SET future_field = ? WHERE id = ?", ("신버전만 아는 값", job_id)
    )
    store._conn.commit()
    store.close()

    # 구버전(지금 코드)이 그 DB 를 그대로 연다.
    older = SqliteStore(path)
    try:
        job = older.get_job(scope, job_id)
        assert job is not None and job.prompt_masked == "마스킹본"
        assert older.get_tenant("acme") is not None

        # 읽기만이 아니라 쓰기도 된다 — 모르는 컬럼이 있다고 INSERT 가 깨지면 안 된다.
        new_id = older.create_job(
            scope, service_id="web", role="summarize", lane="interactive",
            prompt_masked="구버전이 쓴 행",
        )
        assert older.get_job(scope, new_id) is not None
        assert older.update_job(scope, job_id, status="ok")
        assert older.list_jobs(scope)
    finally:
        older.close()


def test_unknown_config_keys_are_a_warning_not_a_rejection(tmp_path):
    """설정도 전진 호환이다 — 신버전이 쓴 설정을 구버전이 거부하면 롤백이 막힌다."""
    import shutil
    from pathlib import Path

    from app.config import load_config, validate_cross_references

    source = Path(__file__).resolve().parent.parent / "config"
    target = tmp_path / "config"
    shutil.copytree(source, target)

    nodes = target / "nodes.yaml"
    nodes.write_text(
        nodes.read_text(encoding="utf-8")
        + "\nfuture-node:\n  provider: mock\n  data_boundary: internal\n",
        encoding="utf-8",
    )
    (target / "thresholds.yaml").write_text(
        (target / "thresholds.yaml").read_text(encoding="utf-8")
        + "\nfuture_threshold_from_a_newer_build: 42\n",
        encoding="utf-8",
    )

    config = load_config(target)          # 모르는 키로 기동이 멈추지 않는다
    validate_cross_references(config)
    assert "future-node" in config.nodes


class _FailingOn:
    """특정 SQL 에서만 터지는 커넥션 프록시.

    `sqlite3.Connection.execute` 는 인스턴스에 덮어쓸 수 없어서 감싼다.
    """

    def __init__(self, conn, prefix: str) -> None:
        self._conn = conn
        self._prefix = prefix

    def execute(self, sql, *args, **kwargs):
        if sql.startswith(self._prefix):
            raise sqlite3.OperationalError("디스크 오류")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ── 감사 M9 — 스토어에 rollback 이 한 곳도 없었다 ────────────────────────────
#
# 다중 문장 메서드가 중간에 실패하면 앞선 쓰기가 **열린 트랜잭션에 남았다가 다음
# 무관한 commit 에 섞여** 영속화된다. 파기 요청이 절반만 처리되고 그 사실이 감사에도
# 안 남는다는 뜻이다.


def test_a_failed_purge_leaves_nothing_behind(store, clock):
    """**절반만 지워진 파기는 파기가 아니다** — 요청자에게는 파기됐다고 답한 뒤다."""
    scope = TenantScope("acme")
    for _ in range(3):
        store.create_job(
            scope, service_id="acme-web", role="r", lane="interactive",
            prompt_masked="x", end_user_hash="u1",
        )
    store.record_usage(scope, service_id="acme-web", role="r", end_user_hash="u1")

    # usage 삭제에서 터뜨린다 — jobs 는 이미 지워진 뒤다.
    real = store._conn
    store._conn = _FailingOn(real, "DELETE FROM usage")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.purge_end_user(scope, "u1")
    finally:
        store._conn = real

    # 되돌아갔어야 한다. 안 되돌리면 다음 commit 이 이 삭제를 영속화한다.
    assert len(store.list_jobs(scope)) == 3, "실패한 파기가 잡을 지운 채로 남겼다"


def test_an_unrelated_commit_does_not_persist_a_failed_write(store):
    """rollback 이 없으면 **다음 무관한 commit 에 섞여** 조용히 영속화된다."""
    scope = TenantScope("acme")
    job_id = store.create_job(
        scope, service_id="acme-web", role="r", lane="interactive",
        prompt_masked="x", end_user_hash="u1",
    )

    real = store._conn
    store._conn = _FailingOn(real, "DELETE FROM usage")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.purge_end_user(scope, "u1")
    finally:
        store._conn = real

    store.audit("someone", "완전히_무관한_동작")     # 이것이 commit 을 부른다
    assert store.get_job(scope, job_id) is not None


# ── 감사 M10 — IN 절이 SQLite 파라미터 상한을 넘는다 ────────────────────────


def test_purging_an_end_user_with_many_jobs_does_not_blow_the_variable_limit(store):
    """`too many SQL variables` 는 **잡이 많은 엔드유저일수록** 터진다.

    즉 파기가 가장 중요한 경우에 실패한다.
    """
    from app.store import SQL_VARIABLE_LIMIT

    scope = TenantScope("acme")
    count = SQL_VARIABLE_LIMIT * 2 + 7
    for _ in range(count):
        job_id = store.create_job(
            scope, service_id="acme-web", role="r", lane="interactive",
            prompt_masked="x", end_user_hash="u1",
        )
        store.record_filter_event(
            scope, rule_id="card", stage="pattern", action="audit",
            match_count=1, offsets=(), job_id=job_id, service_id="acme-web",
        )

    result = store.purge_end_user(scope, "u1")

    assert result["jobs"] == count
    assert result["filter_events_unlinked"] == count


def test_filter_events_have_an_index_on_job_id():
    """없으면 파기가 풀스캔이다 — 잡이 많을수록 느려진다."""
    from app.store import _SCHEMA

    assert "idx_filter_job" in _SCHEMA


# ── 감사 M11 — 검사와 갱신 사이에 창이 있다 ─────────────────────────────────


def test_update_job_can_require_a_current_status(store):
    """**"취소됨" 을 응답받은 잡이 실행되고 과금까지 가면 안 된다.**"""
    scope = TenantScope("acme")
    job_id = store.create_job(
        scope, service_id="acme-web", role="r", lane="interactive", prompt_masked="x",
    )
    store.update_job(scope, job_id, status="running")

    assert store.update_job(scope, job_id, expect_status="queued", status="cancelled") is False
    assert store.get_job(scope, job_id).status == "running"

    assert store.update_job(scope, job_id, expect_status="running", status="ok") is True
    assert store.get_job(scope, job_id).status == "ok"


def test_the_expected_status_accepts_a_set(store):
    scope = TenantScope("acme")
    job_id = store.create_job(
        scope, service_id="acme-web", role="r", lane="interactive", prompt_masked="x",
    )
    assert store.update_job(
        scope, job_id, expect_status=("queued", "pending"), status="cancelled"
    ) is True


# ── 감사 M12 — 정산이 두 개의 독립 커밋이었다 ───────────────────────────────


def test_settling_writes_the_job_and_the_usage_together(store):
    """그 사이의 크래시가 **예약은 풀고 지출은 잃어** 예산이 영구히 과소 계상된다.

    그 오차는 아무 데도 안 남아서 누구도 발견하지 못한다.
    """
    scope = TenantScope("acme")
    job_id = store.create_job(
        scope, service_id="acme-web", role="r", lane="interactive", prompt_masked="x",
    )
    store.update_job(scope, job_id, cost_reserved_usd=5.0)

    real = store._conn
    store._conn = _FailingOn(real, "INSERT INTO usage")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.settle_job(
                scope, job_id,
                job_fields={"cost_usd": 3.0, "cost_reserved_usd": 0.0},
                usage_fields={"service_id": "acme-web", "role": "r", "cost_usd": 3.0},
            )
    finally:
        store._conn = real

    # 지출을 못 남겼으면 예약도 풀리면 안 된다.
    assert store.get_job(scope, job_id).cost_reserved_usd == 5.0


# ── 감사 M13·M15 — 영원히 안 지워지는 것들 ──────────────────────────────────


def test_needs_review_jobs_are_eventually_cleaned_up(store, clock):
    """보존 정리의 상태 목록에 `needs_review` 가 없어 영원히 쌓이고 있었다."""
    scope = TenantScope("acme")
    job_id = store.create_job(
        scope, service_id="acme-web", role="r", lane="interactive", prompt_masked="x",
    )
    store.update_job(scope, job_id, status="needs_review", finished_at=clock())

    clock.advance(31 * 86400)
    store.purge_expired(job_retention_days=30)

    assert store.get_job(scope, job_id) is None


def test_the_retention_list_is_derived_not_hand_written():
    """**손으로 적은 목록은 어긋난다** — 실제로 어긋나서 이 결함이 났다."""
    from app.store import RETAINABLE_STATUSES, TERMINAL_STATUSES

    assert RETAINABLE_STATUSES is TERMINAL_STATUSES


def test_the_pipeline_and_the_store_agree_on_terminal():
    """두 벌로 두면 갈린다."""
    from app.pipeline import _TERMINAL
    from app.store import TERMINAL_STATUSES

    assert _TERMINAL is TERMINAL_STATUSES


def test_audit_and_eval_runs_are_cleaned_up_too(store, clock):
    """감사와 평가 이력은 어떤 보존 정리에도 없었다."""
    from app.store import AUDIT_RETENTION_DAYS, EVAL_RUN_RETENTION_DAYS

    store.audit("someone", "오래된_동작")
    store.record_eval_run("rule", "card", passed=1, total=1)

    clock.advance((max(AUDIT_RETENTION_DAYS, EVAL_RUN_RETENTION_DAYS) + 1) * 86400)
    result = store.purge_expired()

    assert result["admin_audit"] >= 1
    assert result["eval_runs"] >= 1


def test_a_polling_dashboard_does_not_grow_the_audit_table(store, clock):
    """**대시보드를 열어 둔 시간에 비례해 감사가 자라면 진짜 한 줄을 못 찾는다.**"""
    from app.store import PlatformScope

    scope = PlatformScope(actor="platform_admin", reason="관제")
    for _ in range(50):
        store.list_tenants(scope)
        clock.advance(5)

    rows = [r for r in store._conn.execute(
        "SELECT detail_json FROM admin_audit WHERE action='list_tenants'"
    )]
    assert len(rows) < 5, f"폴링 50회가 감사 {len(rows)}줄을 남겼다"


def test_coalescing_keeps_the_count_not_just_the_last_one(store, clock):
    """경계를 넘은 사실을 지우는 것이 아니다 — 횟수로 적을 뿐이다."""
    import json as _json

    from app.store import PlatformScope

    scope = PlatformScope(actor="platform_admin", reason="관제")
    for _ in range(4):
        store.list_tenants(scope)
        clock.advance(1)

    row = store._conn.execute(
        "SELECT detail_json FROM admin_audit WHERE action='list_tenants' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert _json.loads(row["detail_json"])["repeats"] == 4


def test_mutations_are_never_coalesced(store, clock):
    """**파기 두 번과 파기 한 번은 다른 사건이다.**"""
    for _ in range(3):
        store.audit("admin", "purge_end_user", tenant_id="acme")
        clock.advance(1)

    rows = list(store._conn.execute(
        "SELECT id FROM admin_audit WHERE action='purge_end_user'"
    ))
    assert len(rows) == 3
