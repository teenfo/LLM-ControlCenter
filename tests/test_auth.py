"""인증 · 권한 · 3단 레이트리밋 · 신원 해싱."""

from __future__ import annotations

import pytest

from app.auth import (
    ROLE_PLATFORM_ADMIN,
    ROLE_SERVICE,
    ROLE_TENANT_ADMIN,
    Principal,
    RateLimiter,
    RateLimits,
    authenticate,
    bearer_from_header,
    check_role_allowed,
    issue_token,
    require_platform_admin,
    require_tenant_admin,
    rotate_token,
)
from app.i18n import ApiError
from app.identity import (
    hash_end_user,
    hash_prompt,
    hash_system,
    looks_like_pii,
    new_salt,
)
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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> SqliteStore:
    s = SqliteStore(":memory:", now=clock)
    for tenant in ("acme", "globex"):
        s.create_tenant(tenant, tenant.title(), end_user_salt=new_salt())
        s.create_service(TenantScope(tenant), f"{tenant}-web", "web")
    yield s
    s.close()


# ── 신원 해싱 ────────────────────────────────────────────────────────────────


def test_email_as_end_user_never_lands_in_the_database(store):
    """계약에 "불투명 식별자를 보내라" 고 적는 것만으로는 부족하다.

    적힌 계약은 어겨지고, 어겨진 순간 개인정보가 저장된다. 받는 쪽에서 해싱한다.
    """
    salt = store.get_tenant("acme")["end_user_salt"]
    hashed = hash_end_user("hong@example.com", salt)

    store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        end_user_hash=hashed, prompt_masked="본문",
    )

    dumped = str([dict(r) for r in store._conn.execute("SELECT * FROM jobs")])
    assert "hong@example.com" not in dumped
    assert "hong" not in dumped


def test_same_end_user_hashes_differently_per_tenant(store):
    """한 사람이 두 테넌트를 써도 그것을 대조할 수 없어야 한다."""
    acme_salt = store.get_tenant("acme")["end_user_salt"]
    globex_salt = store.get_tenant("globex")["end_user_salt"]

    assert hash_end_user("u-1", acme_salt) != hash_end_user("u-1", globex_salt)


def test_end_user_hash_is_stable_within_a_tenant():
    salt = new_salt()
    assert hash_end_user("u-1", salt) == hash_end_user("  u-1  ", salt)


def test_blank_end_user_is_none():
    assert hash_end_user(None, new_salt()) is None
    assert hash_end_user("   ", new_salt()) is None


def test_prompt_hash_is_salted_per_tenant():
    """탐색 공간이 좁은 값은 솔트 없이 해싱하면 전수조사로 역산된다."""
    a, b = new_salt(), new_salt()
    masked = "주민등록번호는 [주민등록번호] 입니다"

    assert hash_prompt(masked, a) != hash_prompt(masked, b)


def test_system_hash_is_not_salted_so_it_compares_across_tenants():
    """프롬프트 전략은 저엔트로피 개인정보가 아니고, 테넌트를 넘어 비교돼야 한다.

    프롬프트 변경과 품질 변화의 상관을 재려면 이 비교가 필요하다.
    """
    assert hash_system("같은 프롬프트") == hash_system("같은 프롬프트")
    assert hash_system("프롬프트 A") != hash_system("프롬프트 B")
    assert hash_system(None) is None


@pytest.mark.parametrize(
    "value,expected",
    [("hong@example.com", "email"), ("010-1234-5678", "phone_or_id"), ("u_8f3a91", None)],
)
def test_pii_shaped_identifiers_are_flagged_not_blocked(value, expected):
    """차단하지 않는다 — 이미 해싱돼 저장되므로 거부할 실익이 없고,
    거부하면 소비자가 우회로를 만든다."""
    assert looks_like_pii(value) == expected


# ── 토큰 ────────────────────────────────────────────────────────────────────


def test_raw_token_is_never_stored(store):
    _, raw = issue_token(store, ACME, "acme-web")

    dumped = str([dict(r) for r in store._conn.execute("SELECT * FROM tokens")])
    assert raw not in dumped, "원값이 저장됐다 — 발급 시 1회 표시가 전부여야 한다"


def test_token_listing_does_not_expose_the_secret(store):
    _, raw = issue_token(store, ACME, "acme-web")
    listed = store.list_tokens(ACME)[0]

    assert raw not in str(dict(listed))
    assert "token_hash" not in listed.keys()


def test_authenticate_round_trip(store):
    token_id, raw = issue_token(store, ACME, "acme-web")
    principal = authenticate(store, raw)

    assert principal.tenant_id == "acme"
    assert principal.service_id == "acme-web"
    assert principal.token_id == token_id
    assert principal.role == ROLE_SERVICE


@pytest.mark.parametrize("bad", [None, "", "garbage", "lcc_deadbeef_nope"])
def test_bad_tokens_are_rejected(store, bad):
    with pytest.raises(ApiError) as exc:
        authenticate(store, bad)
    assert exc.value.code == "unauthorized"
    assert exc.value.status == 401


def test_revoked_token_stops_working(store):
    token_id, raw = issue_token(store, ACME, "acme-web")
    store.revoke_token(ACME, token_id)

    with pytest.raises(ApiError):
        authenticate(store, raw)


def test_expired_token_stops_working(store, clock):
    _, raw = issue_token(store, ACME, "acme-web", expires_at=clock.now + 10)

    authenticate(store, raw, now=clock)  # 아직 유효
    clock.advance(20)
    with pytest.raises(ApiError):
        authenticate(store, raw, now=clock)


def test_token_of_suspended_tenant_is_rejected(store):
    """정지된 테넌트의 토큰은 존재하지 않는 것과 같다."""
    _, raw = issue_token(store, ACME, "acme-web")
    store._conn.execute("UPDATE tenants SET status='suspended' WHERE id='acme'")

    with pytest.raises(ApiError):
        authenticate(store, raw)


def test_rotation_issues_new_and_kills_old(store):
    old_id, old_raw = issue_token(store, ACME, "acme-web")
    _, new_raw = rotate_token(store, ACME, old_id)

    assert authenticate(store, new_raw).tenant_id == "acme"
    with pytest.raises(ApiError):
        authenticate(store, old_raw)


def test_rotation_grace_window_keeps_old_alive(store, clock):
    """소비자가 배포하는 동안 끊기지 않게 하는 창."""
    old_id, old_raw = issue_token(store, ACME, "acme-web")
    rotate_token(store, ACME, old_id, grace_seconds=300, now=clock)

    authenticate(store, old_raw, now=clock)  # 유예 중에는 산다
    clock.advance(301)
    with pytest.raises(ApiError):
        authenticate(store, old_raw, now=clock)


def test_token_lifecycle_is_audited(store):
    token_id, _ = issue_token(store, ACME, "acme-web", actor="admin@acme")
    rotate_token(store, ACME, token_id, actor="admin@acme")

    actions = {a["action"] for a in store.list_audit(ACME)}
    assert {"issue_token", "rotate_token"} <= actions


def test_tokens_do_not_cross_tenants(store):
    _, raw = issue_token(store, ACME, "acme-web")
    assert authenticate(store, raw).tenant_id == "acme"
    assert len(store.list_tokens(GLOBEX)) == 0


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("Basic abc", None),
        ("Bearer ", None),
        (None, None),
    ],
)
def test_bearer_header_parsing(header, expected):
    assert bearer_from_header(header) == expected


# ── 권한 ────────────────────────────────────────────────────────────────────


def principal(role: str) -> Principal:
    return Principal(tenant_id="acme", service_id="s", token_id="t", role=role)


def test_platform_admin_can_do_tenant_admin_things():
    require_tenant_admin(principal(ROLE_PLATFORM_ADMIN))
    require_platform_admin(principal(ROLE_PLATFORM_ADMIN))


def test_tenant_admin_cannot_do_platform_things():
    require_tenant_admin(principal(ROLE_TENANT_ADMIN))
    with pytest.raises(ApiError) as exc:
        require_platform_admin(principal(ROLE_TENANT_ADMIN))
    assert exc.value.code == "forbidden_platform_admin"


def test_service_token_is_not_an_admin():
    for check in (require_tenant_admin, require_platform_admin):
        with pytest.raises(ApiError):
            check(principal(ROLE_SERVICE))


def test_allow_roles_gate():
    check_role_allowed(["summarize", "classify"], "summarize")
    check_role_allowed(["*"], "anything")
    check_role_allowed('["summarize"]', "summarize")  # DB 는 JSON 문자열로 준다

    with pytest.raises(ApiError) as exc:
        check_role_allowed(["summarize"], "analyze")
    assert exc.value.code == "forbidden_role"
    assert exc.value.params["role"] == "analyze"


# ── 3단 레이트리밋 ───────────────────────────────────────────────────────────


@pytest.fixture
def limiter(store, clock) -> RateLimiter:
    return RateLimiter(store, now=clock)


def test_service_limit_blocks_at_threshold(limiter):
    p = principal(ROLE_SERVICE)
    limits = RateLimits(service=3)

    for _ in range(3):
        limiter.check_and_consume(p, limits)

    with pytest.raises(ApiError) as exc:
        limiter.check_and_consume(p, limits)
    assert exc.value.status == 429
    assert exc.value.retryable is True


def test_error_names_which_tier_tripped(limiter):
    """어느 단계에서 걸렸는지 알려주지 않으면, 소비자가 서비스 한도를 늘려도
    안 풀리는 이유를 알 수 없다(테넌트 총량에 걸린 경우)."""
    p = principal(ROLE_SERVICE)
    limits = RateLimits(tenant=2, service=100)

    limiter.check_and_consume(p, limits)
    limiter.check_and_consume(p, limits)

    with pytest.raises(ApiError) as exc:
        limiter.check_and_consume(p, limits)
    assert exc.value.params["scope"] == "tenant"
    assert exc.value.params["limit"] == 2


def test_tenant_ceiling_cannot_be_bypassed_with_more_services(limiter):
    """예산이 3단인데 레이트리밋만 2단이면, 서비스를 여러 개 만들어 입구를 독점할 수 있다."""
    limits = RateLimits(tenant=4, service=100)
    svc_a = Principal("acme", "svc-a", "t1", ROLE_SERVICE)
    svc_b = Principal("acme", "svc-b", "t2", ROLE_SERVICE)

    for _ in range(2):
        limiter.check_and_consume(svc_a, limits)
    for _ in range(2):
        limiter.check_and_consume(svc_b, limits)

    with pytest.raises(ApiError) as exc:
        limiter.check_and_consume(svc_b, limits)
    assert exc.value.params["scope"] == "tenant", "서비스를 갈아타 테넌트 총량을 우회했다"


def test_end_user_limit_is_per_user(limiter):
    p = principal(ROLE_SERVICE)
    limits = RateLimits(end_user=2)

    for _ in range(2):
        limiter.check_and_consume(p, limits, end_user_hash="u-1")

    with pytest.raises(ApiError) as exc:
        limiter.check_and_consume(p, limits, end_user_hash="u-1")
    assert exc.value.params["scope"] == "end_user"

    limiter.check_and_consume(p, limits, end_user_hash="u-2")  # 다른 사용자는 영향 없다


def test_tenants_do_not_share_a_counter(limiter):
    limits = RateLimits(tenant=2)
    acme = Principal("acme", "s", "t", ROLE_SERVICE)
    globex = Principal("globex", "s", "t", ROLE_SERVICE)

    for _ in range(2):
        limiter.check_and_consume(acme, limits)

    limiter.check_and_consume(globex, limits)  # 옆 테넌트가 소진시키지 못한다


def test_window_slides(limiter, clock):
    p = principal(ROLE_SERVICE)
    limits = RateLimits(service=2)

    limiter.check_and_consume(p, limits)
    limiter.check_and_consume(p, limits)
    with pytest.raises(ApiError):
        limiter.check_and_consume(p, limits)

    clock.advance(61)
    limiter.check_and_consume(p, limits)  # 윈도를 벗어나면 다시 열린다


def test_rejected_request_does_not_consume_quota(limiter, clock):
    """세 단계를 통과해야만 증가시킨다. 거부된 요청이 다른 단계의 할당량을 갉아먹으면 안 된다."""
    p = principal(ROLE_SERVICE)
    limits = RateLimits(tenant=1, service=10)

    limiter.check_and_consume(p, limits)
    for _ in range(5):
        with pytest.raises(ApiError):
            limiter.check_and_consume(p, limits)

    clock.advance(61)
    for _ in range(10):
        limiter.check_and_consume(p, limits.__class__(tenant=100, service=10))


def test_no_limit_means_no_check(limiter):
    p = principal(ROLE_SERVICE)
    for _ in range(50):
        limiter.check_and_consume(p, RateLimits())


def test_prune_drops_old_buckets(limiter, clock, store):
    p = principal(ROLE_SERVICE)
    limiter.check_and_consume(p, RateLimits(service=10))

    clock.advance(300)
    assert limiter.prune() > 0
    assert store.rate_count("s:acme:s", 0) == 0


def test_counter_lives_in_the_store_not_process_memory(limiter, store):
    """워커를 N개 띄우면 프로세스 메모리 카운터는 실효 한도를 N배로 만든다.

    한도가 조용히 곱해지는 것은 설정한 사람의 의도를 배반한다.
    """
    p = principal(ROLE_SERVICE)
    limiter.check_and_consume(p, RateLimits(service=10))

    other_worker = RateLimiter(store, now=limiter._now)
    assert store.rate_count(f"s:{p.tenant_id}:{p.service_id}", 0) == 1

    for _ in range(9):
        other_worker.check_and_consume(p, RateLimits(service=10))
    with pytest.raises(ApiError):
        other_worker.check_and_consume(p, RateLimits(service=10))
