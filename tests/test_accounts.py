"""계정 로그인 — 토큰을 발급하는 앞문 (feature-spec AUTH-9).

계정은 새 권한 모델이 아니다. 비밀번호가 맞으면 만료가 있는 관리자 토큰(세션)을
발급하고, 그 뒤는 지금까지의 토큰과 같은 `authenticate` 를 지난다. 그래서 여기
테스트의 절반은 "세션이 곧 토큰이다" 를 확인한다 — 로그아웃은 폐기, 비밀번호 변경은
그 계정의 다른 토큰 폐기, 정지는 전부 폐기.
"""

from __future__ import annotations

import pytest

from app.auth import (
    LOGIN_LOCK_AFTER,
    LOGIN_LOCK_SECONDS,
    MIN_PASSWORD_LENGTH,
    ROLE_TENANT_ADMIN,
    ROLE_USER,
    SESSION_TTL_SECONDS,
    account_session,
    authenticate,
    change_password,
    create_account,
    hash_password,
    issue_token,
    login,
    logout,
    reset_password,
    set_account_enabled,
    validate_username,
    verify_password,
)
from app.i18n import ApiError
from app.identity import new_salt
from app.store import TenantScope
from tests.conftest import auth

ACME = TenantScope("acme")
PASSWORD = "correct horse battery"
OTHER = "another good passphrase"
WRONG = "wrong wrong wrong"


@pytest.fixture
def acme_store(store, vault):
    store.create_tenant(
        "acme", "Acme", locale="ko-KR", end_user_salt=new_salt(), dek_wrapped=vault.create_dek(),
    )
    store.create_service(ACME, "acme-web", "acme-web", allow_roles=["*"])
    return store


def make_account(
    store, username: str = "ops", *, password: str = PASSWORD, role: str = ROLE_TENANT_ADMIN,
    tenant: str = "acme", service: str = "acme-web",
) -> str:
    return create_account(
        store, username, password, role=role, tenant_id=tenant, service_id=service, actor="test",
    )


def sign_in(store, clock, username: str = "ops", password=PASSWORD):
    return login(store, username, password, now=clock)


def error_code(response) -> str | None:
    body = response.json()
    return (body.get("error") or {}).get("code") or body.get("code")


# ── 비밀번호 ────────────────────────────────────────────────────────────────


def test_password_hashes_are_salted_scrypt():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second, "솔트가 없다 — 같은 비밀번호가 같은 해시면 표를 훔친 사람이 두 계정을 한 번에 연다"
    assert first.startswith("scrypt$")
    assert verify_password(PASSWORD, first) and verify_password(PASSWORD, second)
    assert not verify_password(PASSWORD + "!", first)
    assert not verify_password(PASSWORD, "not-a-hash")


def test_the_plaintext_password_is_never_stored(acme_store, clock):
    """해시만 남는다 — 계정 표에도, 감사 상세에도, 토큰 note 에도."""
    make_account(acme_store)
    _, raw, _, _ = sign_in(acme_store, clock)
    with pytest.raises(ApiError):
        sign_in(acme_store, clock, password=WRONG)
    principal = authenticate(acme_store, raw, now=clock)
    change_password(acme_store, principal, PASSWORD, OTHER)
    reset_password(acme_store, "ops", PASSWORD, actor="test")

    dumped = "\n".join(acme_store._conn.iterdump())
    for secret in (PASSWORD, OTHER, WRONG):
        assert secret not in dumped, "비밀번호가 DB 어딘가에 평문으로 남았다"


@pytest.mark.parametrize("bad", ["short", "a" * (MIN_PASSWORD_LENGTH - 1), "a" * 257, 1234567890, None])
def test_weak_passwords_are_refused(acme_store, bad):
    with pytest.raises(ApiError) as caught:
        make_account(acme_store, password=bad)
    assert caught.value.code == "weak_password"
    assert caught.value.params == {"min": MIN_PASSWORD_LENGTH}
    assert acme_store.get_account("ops") is None


def test_the_password_may_not_be_the_username(acme_store):
    with pytest.raises(ApiError) as caught:
        make_account(acme_store, "operations", password="Operations")
    assert caught.value.code == "weak_password"


@pytest.mark.parametrize("raw, expected", [("Ops", "ops"), ("  admin  ", "admin"), ("a.b-c_d", "a.b-c_d")])
def test_usernames_are_normalized(raw, expected):
    assert validate_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "ab", "-lead", "has space", "x" * 33, "탭\t이름", None, "한글"])
def test_bad_usernames_are_refused(raw):
    with pytest.raises(ApiError) as caught:
        validate_username(raw)
    assert caught.value.code == "invalid_field"
    assert caught.value.params == {"field": "username"}


def test_creating_an_account_needs_a_real_tenant_service_and_role(acme_store):
    """세션 토큰이 거기 걸린다 — 없는 곳에 걸면 로그인은 되는데 아무것도 못 하는 계정이 생긴다."""
    with pytest.raises(ApiError) as caught:
        make_account(acme_store, tenant="nowhere")
    assert caught.value.status == 404
    with pytest.raises(ApiError) as caught:
        make_account(acme_store, service="nowhere")
    assert caught.value.status == 404
    with pytest.raises(ApiError) as caught:
        make_account(acme_store, role="service")
    assert caught.value.code == "invalid_field"
    assert acme_store.list_accounts() == []


def test_usernames_are_unique_after_normalization(acme_store):
    make_account(acme_store)
    with pytest.raises(ApiError) as caught:
        make_account(acme_store, "OPS")
    assert caught.value.code == "already_exists"
    assert caught.value.params == {"id": "ops"}


def test_account_listing_never_carries_the_hash(acme_store):
    make_account(acme_store)
    row = acme_store.list_accounts()[0]
    assert "password_hash" not in row.keys()
    assert row["username"] == "ops"
    assert acme_store.list_accounts("nowhere") == []


# ── 로그인 = 세션 토큰 발급 ─────────────────────────────────────────────────


def test_login_issues_a_session_token_that_authenticates_like_any_other(acme_store, clock):
    make_account(acme_store)
    token_id, raw, expires_at, account = sign_in(acme_store, clock)

    principal = authenticate(acme_store, raw, now=clock)
    assert (principal.tenant_id, principal.service_id, principal.role, principal.token_id) == (
        "acme", "acme-web", ROLE_TENANT_ADMIN, token_id,
    )
    assert account_session(acme_store, principal) == "ops"
    assert expires_at == clock() + SESSION_TTL_SECONDS
    assert account["username"] == "ops"
    assert acme_store.get_account("ops")["last_login_at"] == clock()


def test_a_session_is_a_token_that_expires(acme_store, clock):
    """12시간. 서비스 토큰과 달리 사람의 세션은 끝이 있어야 한다."""
    make_account(acme_store)
    _, raw, _, _ = sign_in(acme_store, clock)
    clock.advance(SESSION_TTL_SECONDS - 1)
    authenticate(acme_store, raw, now=clock)
    clock.advance(2)
    with pytest.raises(ApiError):
        authenticate(acme_store, raw, now=clock)


def test_a_service_token_is_not_an_account_session(acme_store, clock):
    _, raw = issue_token(acme_store, ACME, "acme-web", role=ROLE_TENANT_ADMIN)
    principal = authenticate(acme_store, raw, now=clock)

    assert account_session(acme_store, principal) is None
    assert logout(acme_store, principal) is False
    with pytest.raises(ApiError) as caught:
        change_password(acme_store, principal, PASSWORD, OTHER)
    assert caught.value.code == "account_session_required"
    # 거절이 폐기로 이어지지 않는다 — 서비스 토큰은 그대로 산다.
    authenticate(acme_store, raw, now=clock)


def test_login_failure_does_not_say_why(acme_store, clock):
    """없는 아이디·틀린 비밀번호·정지 계정·정지 테넌트가 전부 같은 401 이다.

    가르면 그것이 곧 계정 목록을 알아내는 방법이 된다.
    """
    make_account(acme_store)
    make_account(acme_store, "frozen")
    set_account_enabled(acme_store, "frozen", False, actor="test")

    attempts = [("nobody", PASSWORD), ("ops", WRONG), ("frozen", PASSWORD), ("", ""), ("ops", None)]
    seen = []
    for username, password in attempts:
        with pytest.raises(ApiError) as caught:
            login(acme_store, username, password, now=clock)
        seen.append((caught.value.code, caught.value.status, dict(caught.value.params)))

    acme_store._conn.execute("UPDATE tenants SET status='suspended' WHERE id='acme'")
    with pytest.raises(ApiError) as caught:
        sign_in(acme_store, clock)
    seen.append((caught.value.code, caught.value.status, dict(caught.value.params)))

    assert seen == [("invalid_credentials", 401, {})] * len(seen)


def test_login_locks_after_five_failures_and_reopens_after_the_window(acme_store, clock):
    make_account(acme_store)
    for _ in range(LOGIN_LOCK_AFTER):
        with pytest.raises(ApiError) as caught:
            sign_in(acme_store, clock, password=WRONG)
        assert caught.value.code == "invalid_credentials"

    # 맞는 비밀번호도 잠금 동안은 안 열린다 — 열리면 잠금은 5회마다 한 번의 무료 시도다.
    with pytest.raises(ApiError) as caught:
        sign_in(acme_store, clock)
    assert (caught.value.code, caught.value.status) == ("login_locked", 429)
    assert caught.value.params == {"minutes": LOGIN_LOCK_SECONDS // 60}
    assert caught.value.retryable is True

    clock.advance(LOGIN_LOCK_SECONDS + 1)
    sign_in(acme_store, clock)


def test_the_lock_counts_unknown_usernames_too(acme_store, clock):
    """잠금 여부로 계정의 존재를 흘리지 않는다 — 그러나 감사에는 실재하는 계정만 남는다."""
    for _ in range(LOGIN_LOCK_AFTER):
        with pytest.raises(ApiError):
            login(acme_store, "ghost", PASSWORD, now=clock)
    with pytest.raises(ApiError) as caught:
        login(acme_store, "ghost", PASSWORD, now=clock)
    assert caught.value.code == "login_locked"

    failed = acme_store._conn.execute(
        "SELECT COUNT(*) AS n FROM admin_audit WHERE action = 'login_failed'"
    ).fetchone()["n"]
    assert failed == 0, "아무 문자열이나 감사 사슬에 쌓이면 그것이 곧 사슬을 잡음으로 채우는 방법이다"


def test_failed_logins_on_real_accounts_are_audited(acme_store, clock):
    make_account(acme_store)
    with pytest.raises(ApiError):
        sign_in(acme_store, clock, password=WRONG)
    actions = [(row["actor"], row["action"]) for row in acme_store.list_audit(ACME, limit=50)]
    assert ("account:ops", "login_failed") in actions


# ── 세션을 끊는 세 가지 ────────────────────────────────────────────────────


def test_logout_revokes_only_this_session(acme_store, clock):
    make_account(acme_store)
    _, first, _, _ = sign_in(acme_store, clock)
    _, second, _, _ = sign_in(acme_store, clock)

    assert logout(acme_store, authenticate(acme_store, first, now=clock)) is True
    with pytest.raises(ApiError):
        authenticate(acme_store, first, now=clock)
    authenticate(acme_store, second, now=clock)


def test_changing_the_password_ends_the_other_sessions(acme_store, clock):
    """바꾸는 이유의 절반은 "누가 알아낸 것 같아서" 다. 그때 다른 세션이 살아 있으면 바꾼 의미가 없다."""
    make_account(acme_store)
    _, mine, _, _ = sign_in(acme_store, clock)
    _, other, _, _ = sign_in(acme_store, clock)
    principal = authenticate(acme_store, mine, now=clock)

    assert change_password(acme_store, principal, PASSWORD, OTHER) == 1
    authenticate(acme_store, mine, now=clock)
    with pytest.raises(ApiError):
        authenticate(acme_store, other, now=clock)
    with pytest.raises(ApiError):
        sign_in(acme_store, clock, password=PASSWORD)
    sign_in(acme_store, clock, password=OTHER)


def test_changing_the_password_needs_the_current_one(acme_store, clock):
    make_account(acme_store)
    _, mine, _, _ = sign_in(acme_store, clock)
    principal = authenticate(acme_store, mine, now=clock)

    with pytest.raises(ApiError) as caught:
        change_password(acme_store, principal, "not the current one", OTHER)
    assert caught.value.code == "invalid_credentials"
    with pytest.raises(ApiError) as caught:
        change_password(acme_store, principal, PASSWORD, "short")
    assert caught.value.code == "weak_password"
    sign_in(acme_store, clock)  # 아무것도 안 바뀌었다


def test_an_admin_reset_ends_every_session(acme_store, clock):
    make_account(acme_store)
    _, first, _, _ = sign_in(acme_store, clock)
    _, second, _, _ = sign_in(acme_store, clock)

    assert reset_password(acme_store, "ops", OTHER, actor="test") == 2
    for raw in (first, second):
        with pytest.raises(ApiError):
            authenticate(acme_store, raw, now=clock)
    sign_in(acme_store, clock, password=OTHER)

    with pytest.raises(ApiError) as caught:
        reset_password(acme_store, "nobody", OTHER, actor="test")
    assert caught.value.status == 404


def test_disabling_an_account_ends_its_sessions_and_blocks_login(acme_store, clock):
    make_account(acme_store)
    _, raw, _, _ = sign_in(acme_store, clock)

    assert set_account_enabled(acme_store, "ops", False, actor="test") == 1
    with pytest.raises(ApiError):
        authenticate(acme_store, raw, now=clock)
    with pytest.raises(ApiError) as caught:
        sign_in(acme_store, clock)
    assert caught.value.code == "invalid_credentials"

    set_account_enabled(acme_store, "ops", True, actor="test")
    sign_in(acme_store, clock)


def test_sessions_do_not_cross_accounts(acme_store, clock):
    """비밀번호 변경이 **같은 테넌트의 다른 계정** 세션을 끊지 않는다."""
    make_account(acme_store, "ops")
    make_account(acme_store, "ops2")
    _, other, _, _ = sign_in(acme_store, clock, "ops2")
    _, mine, _, _ = sign_in(acme_store, clock)

    change_password(acme_store, authenticate(acme_store, mine, now=clock), PASSWORD, OTHER)
    authenticate(acme_store, other, now=clock)


# ── HTTP ───────────────────────────────────────────────────────────────────


def create_via_api(client, acme, username: str = "ops", **extra):
    body = {
        "username": username, "password": PASSWORD,
        "tenant_id": "acme", "service_id": acme["service_id"], **extra,
    }
    return client.post("/v1/platform/accounts", json=body, headers=auth(acme["platform_admin"]))


def login_via_api(client, username: str = "ops", password: str = PASSWORD):
    return client.post("/v1/login", json={"username": username, "password": password})


def test_login_route_needs_no_token_and_returns_one(client, acme):
    assert create_via_api(client, acme).status_code == 201

    response = login_via_api(client, "Ops")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["username"], body["role"], body["tenant"]) == ("ops", ROLE_TENANT_ADMIN, "acme")
    assert body["token"].startswith("lcc_")

    session = client.get("/v1/session", headers=auth(body["token"])).json()
    assert session["account"] == "ops"
    assert session["role"] == ROLE_TENANT_ADMIN
    # 서비스 토큰으로 연 세션은 계정이 아니다 — 화면이 이것으로 계정 탭을 가른다.
    assert client.get("/v1/session", headers=auth(acme["tenant_admin"])).json()["account"] is None


def test_bad_credentials_get_a_generic_401(client, acme):
    create_via_api(client, acme)
    for username, password in (("ops", WRONG), ("ghost", PASSWORD)):
        response = login_via_api(client, username, password)
        assert response.status_code == 401
        assert error_code(response) == "invalid_credentials"


def test_a_missing_field_is_a_400_not_a_401(client):
    response = client.post("/v1/login", json={"username": "ops"})
    assert response.status_code == 400
    assert error_code(response) == "missing_field"


def test_too_many_failures_lock_the_route(client, acme, harness):
    create_via_api(client, acme)
    for _ in range(LOGIN_LOCK_AFTER):
        assert login_via_api(client, "ops", WRONG).status_code == 401

    locked = login_via_api(client)
    assert locked.status_code == 429
    assert error_code(locked) == "login_locked"

    harness.clock.advance(LOGIN_LOCK_SECONDS + 1)
    assert login_via_api(client).status_code == 200


def test_logout_route_ends_the_session_and_refuses_service_tokens(client, acme):
    create_via_api(client, acme)
    token = login_via_api(client).json()["token"]

    assert client.post("/v1/logout", headers=auth(token)).status_code == 200
    assert client.get("/v1/session", headers=auth(token)).status_code == 401

    refused = client.post("/v1/logout", headers=auth(acme["tenant_admin"]))
    assert refused.status_code == 403
    assert error_code(refused) == "account_session_required"
    assert client.get("/v1/session", headers=auth(acme["tenant_admin"])).status_code == 200


def test_the_password_change_route_keeps_this_session_only(client, acme):
    create_via_api(client, acme)
    mine = login_via_api(client).json()["token"]
    other = login_via_api(client).json()["token"]

    response = client.post(
        "/v1/session/password",
        json={"current_password": PASSWORD, "new_password": OTHER}, headers=auth(mine),
    )
    assert response.status_code == 200, response.text
    assert response.json()["sessions_revoked"] == 1
    assert client.get("/v1/session", headers=auth(mine)).status_code == 200
    assert client.get("/v1/session", headers=auth(other)).status_code == 401
    assert login_via_api(client, "ops", OTHER).status_code == 200


def test_account_management_is_platform_only(client, acme):
    calls = (
        ("GET", "/v1/platform/accounts", None),
        ("POST", "/v1/platform/accounts", {"username": "x", "password": PASSWORD, "tenant_id": "acme"}),
        ("POST", "/v1/platform/accounts/ops/password", {"password": OTHER}),
        ("POST", "/v1/platform/accounts/ops/disable", {}),
    )
    for method, path, body in calls:
        response = client.request(method, path, json=body, headers=auth(acme["tenant_admin"]))
        assert response.status_code == 403, (method, path)


def test_account_management_routes(client, acme):
    admin = auth(acme["platform_admin"])
    assert create_via_api(client, acme).status_code == 201
    assert create_via_api(client, acme).status_code == 409

    listed = client.get("/v1/platform/accounts", headers=admin).json()["accounts"]
    assert [row["username"] for row in listed] == ["ops"]
    assert "password_hash" not in listed[0]

    token = login_via_api(client).json()["token"]
    reset = client.post("/v1/platform/accounts/ops/password", json={"password": OTHER}, headers=admin)
    assert reset.status_code == 200 and reset.json()["sessions_revoked"] == 1
    assert client.get("/v1/session", headers=auth(token)).status_code == 401
    assert login_via_api(client, "ops", OTHER).status_code == 200

    assert client.post("/v1/platform/accounts/ops/disable", json={}, headers=admin).status_code == 200
    assert login_via_api(client, "ops", OTHER).status_code == 401
    assert client.get("/v1/platform/accounts", headers=admin).json()["accounts"][0]["disabled_at"]

    enabled = client.post("/v1/platform/accounts/ops/disable", json={"disabled": False}, headers=admin)
    assert enabled.status_code == 200
    assert login_via_api(client, "ops", OTHER).status_code == 200

    assert client.post("/v1/platform/accounts/ghost/disable", json={}, headers=admin).status_code == 404


def test_creating_a_tenant_admin_account_needs_a_tenant(client, acme):
    response = client.post(
        "/v1/platform/accounts", json={"username": "ops", "password": PASSWORD},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 400
    assert error_code(response) == "missing_field"


def test_account_actions_leave_an_audit_trail(client, acme, harness):
    create_via_api(client, acme)
    token = login_via_api(client).json()["token"]
    client.post("/v1/logout", headers=auth(token))

    actions = [row["action"] for row in harness.store.list_audit(ACME, limit=50)]
    for expected in ("create_account", "login", "logout"):
        assert expected in actions, actions


# ── 사용자 계정(user) — 클라이언트 페이지의 앞문 (feature-spec AUTH-10) ──────────


def login_user(client, harness, tokens, username: str, tenant: str = "acme") -> str:
    create_account(
        harness.store, username, PASSWORD, role=ROLE_USER, tenant_id=tenant,
        service_id=tokens["service_id"], actor="test",
    )
    body = client.post("/v1/login", json={"username": username, "password": PASSWORD}).json()
    return body["token"]


def test_a_user_account_logs_in_as_a_service_session_bound_to_its_tenant_and_service(
    client, acme, harness,
):
    """`user` 는 토큰 역할이 아니다 — 세션은 자기 서비스의 `service` 토큰이고, 사람임은 계정 정보로 안다."""
    token = login_user(client, harness, acme, "alice")
    session = client.get("/v1/session", headers=auth(token)).json()
    assert session["role"] == "service"
    assert session["account"] == "alice"
    assert session["account_role"] == "user"
    assert session["tenant"]["id"] == "acme"
    assert session["service"]["id"] == acme["service_id"]
    assert not session["is_tenant_admin"] and not session["is_platform_admin"]


def test_a_user_session_cannot_reach_admin_or_platform_routes(client, acme, harness):
    token = login_user(client, harness, acme, "alice")
    assert client.get("/v1/admin/services", headers=auth(token)).status_code == 403
    assert client.get("/v1/admin/accounts", headers=auth(token)).status_code == 403
    assert client.get("/v1/platform/tenants", headers=auth(token)).status_code == 403


def test_tenant_admin_manages_only_user_accounts_of_its_own_tenant(client, acme, globex, harness):
    """자기 테넌트의 `user` 만 — 관리자 계정도, 남의 테넌트 사용자도 이 라우트에는 없다(404)."""
    headers = auth(acme["tenant_admin"])
    # 같은 테넌트의 관리자 계정은 목록에 나오지 않는다 — 플랫폼 소관이다.
    make_account(harness.store, "ops", tenant="acme", service=acme["service_id"])

    created = client.post(
        "/v1/admin/accounts",
        json={"username": "alice", "password": PASSWORD, "service_id": acme["service_id"]},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json() == {
        "username": "alice", "role": "user", "tenant_id": "acme", "service_id": acme["service_id"],
    }

    # 남의 테넌트 서비스에 묶을 수 없다 — 존재를 말하지 않고 404.
    foreign = client.post(
        "/v1/admin/accounts",
        json={"username": "bob", "password": PASSWORD, "service_id": globex["service_id"]},
        headers=headers,
    )
    assert foreign.status_code == 404

    listed = client.get("/v1/admin/accounts", headers=headers).json()["accounts"]
    assert [row["username"] for row in listed] == ["alice"]
    assert all("password_hash" not in row for row in listed)

    # 남의 테넌트 사용자·자기 테넌트 관리자에게는 손댈 수 없다.
    create_account(
        harness.store, "gus", PASSWORD, role=ROLE_USER, tenant_id="globex",
        service_id=globex["service_id"], actor="test",
    )
    for username in ("gus", "ops", "nobody"):
        assert client.post(
            f"/v1/admin/accounts/{username}/password", json={"password": OTHER}, headers=headers,
        ).status_code == 404
        assert client.post(
            f"/v1/admin/accounts/{username}/disable", json={}, headers=headers,
        ).status_code == 404

    reset = client.post("/v1/admin/accounts/alice/password", json={"password": OTHER}, headers=headers)
    assert reset.status_code == 200
    disabled = client.post("/v1/admin/accounts/alice/disable", json={}, headers=headers)
    assert disabled.status_code == 200 and disabled.json()["disabled"] is True
    assert client.post("/v1/login", json={"username": "alice", "password": OTHER}).status_code == 401
    enabled = client.post(
        "/v1/admin/accounts/alice/disable", json={"disabled": False}, headers=headers,
    )
    assert enabled.status_code == 200
    assert client.post("/v1/login", json={"username": "alice", "password": OTHER}).status_code == 200


def test_a_user_account_needs_a_service_id_and_a_real_service(client, acme):
    headers = auth(acme["tenant_admin"])
    missing = client.post(
        "/v1/admin/accounts", json={"username": "alice", "password": PASSWORD}, headers=headers,
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "missing_field"
    unknown = client.post(
        "/v1/admin/accounts",
        json={"username": "alice", "password": PASSWORD, "service_id": "nope"},
        headers=headers,
    )
    assert unknown.status_code == 404


def test_user_account_management_is_tenant_admin_only(client, acme, harness):
    token = login_user(client, harness, acme, "alice")
    for who in (acme["service"], token):
        assert client.get("/v1/admin/accounts", headers=auth(who)).status_code == 403
        assert client.post(
            "/v1/admin/accounts",
            json={"username": "x1", "password": PASSWORD, "service_id": acme["service_id"]},
            headers=auth(who),
        ).status_code == 403


def test_platform_can_create_a_user_account_with_tenant_and_service(client, acme):
    created = client.post(
        "/v1/platform/accounts",
        json={
            "username": "pam", "password": PASSWORD, "role": "user",
            "tenant_id": "acme", "service_id": acme["service_id"],
        },
        headers=auth(acme["platform_admin"]),
    )
    assert created.status_code == 201
    assert created.json()["role"] == "user"
    logged = client.post("/v1/login", json={"username": "pam", "password": PASSWORD})
    assert logged.status_code == 200
    assert logged.json()["role"] == "user"


def test_the_cli_offers_the_user_role():
    from app.cli import build_parser

    args = build_parser().parse_args(
        ["account", "create", "alice", "--role", "user", "--tenant", "acme", "--service", "acme-web"],
    )
    assert args.role == "user" and args.tenant == "acme" and args.service == "acme-web"


def test_user_account_actions_leave_an_audit_trail(client, acme, harness):
    headers = auth(acme["tenant_admin"])
    client.post(
        "/v1/admin/accounts",
        json={"username": "alice", "password": PASSWORD, "service_id": acme["service_id"]},
        headers=headers,
    )
    client.post("/v1/admin/accounts/alice/password", json={"password": OTHER}, headers=headers)
    client.post("/v1/admin/accounts/alice/disable", json={}, headers=headers)
    actions = {
        row["action"]
        for row in harness.store._conn.execute(
            "SELECT action FROM admin_audit WHERE target = 'alice' AND tenant_id = 'acme'"
        )
    }
    assert {"create_account", "reset_password", "disable_account"} <= actions
