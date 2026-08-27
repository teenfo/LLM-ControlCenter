"""삭제·내보내기 — crypto-shredding · 엔드유저 파기 · 보관 기간 · 내보내기.

멀티테넌트 + 개인정보 필터 제품에서 삭제는 선택이 아니다. 그리고 **되돌릴 수
없으므로** 잘못 지우는 것과 안 지워지는 것이 둘 다 사고다.
"""

from __future__ import annotations

import json

import pytest

from app.crypto import KeyDestroyed, Sealed
from app.store import TenantScope
from tests.conftest import auth

VALID_CARD = "4111 1111 1111 1111"


def submit(client, tokens, prompt="비밀 메모", end_user=None):
    body = {"role": "summarize", "prompt": prompt, "wait": 0}
    if end_user:
        body["end_user"] = end_user
    return client.post("/v1/generate", json=body, headers=auth(tokens["service"])).json()


def end_user_hash_of(harness, tenant, job_id):
    return harness.store.get_job(TenantScope(tenant), job_id).end_user_hash


# ── 엔드유저 파기 ────────────────────────────────────────────────────────────


def test_purging_one_end_user_leaves_the_others(harness, client, acme):
    keep = submit(client, acme, end_user="u_keep")
    drop = submit(client, acme, end_user="u_drop")
    scope = TenantScope("acme")
    victim = end_user_hash_of(harness, "acme", drop["job_id"])

    response = client.delete(
        f"/v1/admin/end-users/{victim}?confirm={victim}",
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 200
    assert response.json()["purged"]["jobs"] == 1

    assert harness.store.get_job(scope, drop["job_id"]) is None
    assert harness.store.get_job(scope, keep["job_id"]) is not None


def test_purging_an_end_user_never_touches_another_tenant(harness, client, acme, globex):
    """해시가 우연히 같아도 남의 테넌트 데이터는 안 지워진다."""
    theirs = submit(client, globex, end_user="u_same")
    mine = submit(client, acme, end_user="u_same")
    victim = end_user_hash_of(harness, "acme", mine["job_id"])

    client.delete(
        f"/v1/admin/end-users/{victim}?confirm={victim}",
        headers=auth(acme["tenant_admin"]),
    )
    assert harness.store.get_job(TenantScope("globex"), theirs["job_id"]) is not None


def test_purge_requires_an_exact_confirmation(client, acme, harness):
    job = submit(client, acme, end_user="u_x")
    victim = end_user_hash_of(harness, "acme", job["job_id"])

    response = client.delete(
        f"/v1/admin/end-users/{victim}?confirm=아무거나",
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "confirmation_required"
    assert harness.store.get_job(TenantScope("acme"), job["job_id"]) is not None


def test_purge_audit_records_who_and_how_many_not_what(harness, client, acme):
    """**감사가 새 유출 경로가 되면 안 된다.**"""
    job = submit(client, acme, prompt=f"카드 {VALID_CARD}", end_user="u_x")
    victim = end_user_hash_of(harness, "acme", job["job_id"])
    client.delete(
        f"/v1/admin/end-users/{victim}?confirm={victim}",
        headers=auth(acme["tenant_admin"]),
    )

    rows = [
        a for a in harness.store.list_audit(TenantScope("acme"))
        if a["action"] == "purge_end_user"
    ]
    assert len(rows) == 1                       # 라우터와 스토어가 각각 남기지 않는다
    detail = json.loads(rows[0]["detail_json"])
    assert detail == {"jobs": 1, "usage": 0}
    assert VALID_CARD not in rows[0]["detail_json"]


def test_purge_unlinks_the_guard_events_but_keeps_the_counts(harness, client, acme):
    """이벤트를 지우면 오탐률이 파기 요청 한 건에 왜곡되고, 그게 필터 정책을 왜곡한다.

    다만 지워진 잡을 가리키는 식별자는 끊는다 — 남기면 그 사람의 요청을 다시 묶을 수 있다.
    """
    job = submit(client, acme, prompt=f"카드 {VALID_CARD}", end_user="u_x")
    scope = TenantScope("acme")
    victim = end_user_hash_of(harness, "acme", job["job_id"])

    before = len(harness.store.list_filter_events(scope))
    assert before > 0

    client.delete(
        f"/v1/admin/end-users/{victim}?confirm={victim}",
        headers=auth(acme["tenant_admin"]),
    )
    after = harness.store.list_filter_events(scope)
    assert len(after) == before
    assert all(e["job_id"] is None for e in after)


# ── 테넌트 파기 = crypto-shredding ───────────────────────────────────────────


def test_destroying_the_dek_makes_existing_ciphertext_unreadable(harness, client, acme):
    """**가장 강한 삭제다** — 백업에 암호문이 남아 있어도 못 연다.

    7일 뒤 암호문을 지워도 30일 전 백업을 복원하면 되살아나는 문제를 구조적으로
    푸는 유일한 수단이 이것이다.
    """
    job = submit(client, acme, prompt="복원되면 안 되는 원문")
    scope = TenantScope("acme")
    row = harness.store.get_job(scope, job["job_id"])

    # 백업에 이 암호문이 그대로 있다고 치자.
    backup = Sealed(nonce=row.prompt_nonce, ciphertext=row.prompt_cipher)
    wrapped = harness.store.get_tenant("acme")["dek_wrapped"]
    assert harness.vault.open(wrapped, backup) == "복원되면 안 되는 원문"

    response = client.delete(
        "/v1/platform/tenants/acme?confirm=acme&reason=계약 종료",
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 200
    assert response.json()["dek_destroyed"] is True

    # 래핑된 DEK 가 실제로 사라졌는지 행에서 확인한다 — 응답의 플래그만 믿지 않는다.
    purged = harness.store._conn.execute(
        "SELECT dek_wrapped, status, purged_at FROM tenants WHERE id='acme'"
    ).fetchone()
    assert purged["dek_wrapped"] is None
    assert purged["status"] == "purged" and purged["purged_at"] is not None

    # 백업에 남은 그 암호문을, 같은 마스터 KEK 를 가진 금고로도 더는 열 수 없다.
    with pytest.raises(KeyDestroyed):
        harness.vault.open(purged["dek_wrapped"], backup)


def test_tenant_purge_requires_confirmation_and_a_reason(client, acme, harness):
    response = client.delete(
        "/v1/platform/tenants/acme",
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 400
    assert harness.store.get_tenant("acme") is not None


def test_tenant_purge_removes_every_scoped_table(harness, client, acme):
    submit(client, acme, prompt=f"카드 {VALID_CARD}", end_user="u_x")
    client.put(
        "/v1/admin/guard/rules",
        json={"id": "mine", "action": "audit", "pattern": "X-\\d+"},
        headers=auth(acme["tenant_admin"]),
    )
    client.put(
        "/v1/admin/settings", json={"raw_prompt_retention_days": 3},
        headers=auth(acme["tenant_admin"]),
    )

    client.delete(
        "/v1/platform/tenants/acme?confirm=acme&reason=테스트",
        headers=auth(acme["platform_admin"]),
    )

    for table in ("jobs", "usage", "filter_events", "tenant_guard_rules",
                  "tenant_settings", "tokens", "services"):
        left = harness.store._conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = 'acme'"
        ).fetchone()["n"]
        assert left == 0, table


def test_a_purged_tenants_tokens_stop_authenticating(client, acme):
    client.delete(
        "/v1/platform/tenants/acme?confirm=acme&reason=테스트",
        headers=auth(acme["platform_admin"]),
    )
    response = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 401


def test_tenant_purge_is_audited_with_the_reason(harness, client, acme):
    client.delete(
        "/v1/platform/tenants/acme?confirm=acme&reason=계약 종료",
        headers=auth(acme["platform_admin"]),
    )
    rows = harness.store._conn.execute(
        "SELECT * FROM admin_audit WHERE action='purge_tenant'"
    ).fetchall()
    assert rows and "계약 종료" in rows[-1]["detail_json"]


def test_one_tenants_dek_never_opens_anothers_ciphertext(harness, client, acme, globex):
    job = submit(client, acme, prompt="에크미 원문")
    row = harness.store.get_job(TenantScope("acme"), job["job_id"])
    other_dek = harness.store.get_tenant("globex")["dek_wrapped"]

    with pytest.raises(Exception):
        harness.vault.open(
            other_dek, Sealed(nonce=row.prompt_nonce, ciphertext=row.prompt_cipher)
        )


# ── 보관 기간 ────────────────────────────────────────────────────────────────


def test_retention_erases_the_ciphertext_but_keeps_the_masked_copy(harness, client, acme):
    """마스킹본은 프롬프트 개선의 재료다. 암호문만 지운다."""
    job = submit(client, acme, prompt="원문")
    scope = TenantScope("acme")

    harness.clock.advance(8 * 86400)
    harness.scheduler.run_retention()

    row = harness.store.get_job(scope, job["job_id"])
    assert row.prompt_cipher is None and row.prompt_nonce is None
    assert row.prompt_masked == "원문"


def test_tenant_can_shorten_retention_and_it_actually_applies(harness, client, acme, globex):
    """**설정 화면의 값이 아무것도 안 하면 설정이 없는 것보다 나쁘다.**"""
    client.put(
        "/v1/admin/settings", json={"raw_prompt_retention_days": 1},
        headers=auth(acme["tenant_admin"]),
    )
    fast = submit(client, acme, prompt="빨리 지워야 함")
    slow = submit(client, globex, prompt="기본 주기")

    harness.clock.advance(2 * 86400)
    harness.scheduler.run_retention()

    assert harness.store.get_job(TenantScope("acme"), fast["job_id"]).prompt_cipher is None
    assert harness.store.get_job(TenantScope("globex"), slow["job_id"]).prompt_cipher is not None


def test_tenant_cannot_lengthen_retention_past_the_platform_max(harness, client, acme):
    """테넌트는 짧게만 정할 수 있다 — 가드 규칙과 같은 방향이다."""
    client.put(
        "/v1/admin/settings", json={"raw_prompt_retention_days": 3650},
        headers=auth(acme["tenant_admin"]),
    )
    body = client.get("/v1/admin/settings", headers=auth(acme["tenant_admin"])).json()

    assert body["raw_prompt_retention_days_requested"] == 3650
    assert body["raw_prompt_retention_days_platform_max"] == 7
    # 조용히 자르지 않고 실제 적용값을 함께 보여준다.
    assert body["raw_prompt_retention_days"] == 7

    job = submit(client, acme, prompt="원문")
    harness.clock.advance(8 * 86400)
    harness.scheduler.run_retention()
    assert harness.store.get_job(TenantScope("acme"), job["job_id"]).prompt_cipher is None


def test_zero_retention_means_no_raw_storage_at_all(harness, client, acme):
    client.put(
        "/v1/admin/settings", json={"raw_prompt_retention_days": 0},
        headers=auth(acme["tenant_admin"]),
    )
    job = submit(client, acme, prompt="원문")
    harness.clock.advance(1)
    harness.scheduler.run_retention()
    assert harness.store.get_job(TenantScope("acme"), job["job_id"]).prompt_cipher is None


def test_negative_retention_is_rejected(client, acme):
    response = client.put(
        "/v1/admin/settings", json={"raw_prompt_retention_days": -1},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 400


def test_raw_read_fails_cleanly_after_retention(harness, client, acme):
    job = submit(client, acme, prompt="원문")
    harness.clock.advance(8 * 86400)
    harness.scheduler.run_retention()

    response = client.get(
        f"/v1/admin/jobs/{job['job_id']}/raw", headers=auth(acme["tenant_admin"])
    )
    assert response.status_code == 404
    assert response.json()["code"] == "raw_prompt_unavailable"


# ── 내보내기 ─────────────────────────────────────────────────────────────────


def test_export_carries_the_masked_copy_never_the_ciphertext(harness, client, acme):
    """내보내기 파일이 원문을 나르면 보관 기간과 열람 감사가 파일 밖에서 무력화된다."""
    submit(client, acme, prompt="메일 hong@example.com", end_user="u_x")

    body = client.get("/v1/admin/export", headers=auth(acme["tenant_admin"])).json()
    dumped = json.dumps(body, ensure_ascii=False)

    assert body["jobs"]
    assert "prompt_cipher" not in dumped
    assert "prompt_nonce" not in dumped
    assert "hong@example.com" not in dumped
    assert "[EMAIL" in dumped or "*" in dumped   # 마스킹본은 들어 있다


def test_export_never_crosses_tenants(client, acme, globex):
    submit(client, acme, prompt="에크미 비밀")
    body = client.get("/v1/admin/export", headers=auth(globex["tenant_admin"])).json()
    assert body["tenant"]["id"] == "globex"
    assert "에크미 비밀" not in json.dumps(body, ensure_ascii=False)


def test_export_includes_the_settings_needed_to_rebuild(client, acme):
    client.put(
        "/v1/admin/guard/rules",
        json={"id": "mine", "action": "block", "pattern": "X-\\d+"},
        headers=auth(acme["tenant_admin"]),
    )
    client.put(
        "/v1/admin/overrides", json={"role": "summarize", "fields": {"timeout": 30}},
        headers=auth(acme["tenant_admin"]),
    )
    body = client.get("/v1/admin/export", headers=auth(acme["tenant_admin"])).json()

    assert [r["id"] for r in body["guard_rules"]] == ["mine"]
    assert body["role_overrides"]["summarize"] == {"timeout": 30}
    assert body["services"]


def test_export_is_audited(harness, client, acme):
    client.get("/v1/admin/export", headers=auth(acme["tenant_admin"]))
    actions = {a["action"] for a in harness.store.list_audit(TenantScope("acme"))}
    assert "export_tenant" in actions
