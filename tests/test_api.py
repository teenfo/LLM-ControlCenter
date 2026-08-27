"""HTTP API — 요청 순서 · 테넌트 격리 · 폴링 방어 · 다국어 · 관리 경로.

이 파일이 잡는 것은 **계약**이다. 파이프라인 단위 동작은 `test_pipeline.py` 가 본다.
"""

from __future__ import annotations

import asyncio
import json

from app.pipeline import MAX_WAIT_SECONDS
from app.store import TenantScope
from tests.conftest import auth, seed_tenant

# 체크섬을 통과하는 샘플. 오탐 방지를 위해 검증기가 붙어 있으므로 아무 숫자나 안 걸린다.
VALID_CARD = "4111 1111 1111 1111"
VALID_RRN = "990101-1234563"


def drive(harness, lane="interactive", rounds=6):
    """스케줄러를 손으로 돌린다. 테스트는 배경 루프를 켜지 않는다."""

    async def run() -> None:
        for _ in range(rounds):
            await harness.scheduler.tick(lane)
            await asyncio.sleep(0)

    asyncio.run(run())


# ── 인증 ────────────────────────────────────────────────────────────────────


def test_generate_requires_a_token(client):
    response = client.post("/v1/generate", json={"role": "summarize", "prompt": "안녕"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_revoked_token_stops_working(harness, client, acme):
    scope = TenantScope("acme")
    token_id = harness.store.list_tokens(scope)[0]["id"]
    harness.store.revoke_token(scope, token_id)

    # 폐기한 토큰이 어느 것이든 최소 하나는 막혀야 한다.
    codes = {
        client.post(
            "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
            headers=auth(acme[r]),
        ).status_code
        for r in ("service", "tenant_admin", "platform_admin")
    }
    assert 401 in codes


def test_service_token_cannot_reach_admin_routes(client, acme):
    response = client.get("/v1/admin/services", headers=auth(acme["service"]))
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_admin"


def test_tenant_admin_cannot_reach_platform_routes(client, acme):
    response = client.get("/v1/platform/tenants", headers=auth(acme["tenant_admin"]))
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_platform_admin"


# ── 순서가 계약이다 ──────────────────────────────────────────────────────────


def test_blocked_prompt_never_reaches_storage_or_a_node(harness, client, acme):
    """②가 ③보다 먼저다. 차단된 프롬프트는 평문으로 저장되지 않는다."""
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": f"주민번호는 {VALID_RRN} 입니다"},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "guard_blocked"
    assert "kr_rrn" in response.json()["rules"]
    # 원문이 응답에 안 실린다.
    assert VALID_RRN not in response.text

    rows = harness.store._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    assert rows["n"] == 0


def test_blocked_prompt_still_records_the_detection(harness, client, acme):
    """차단은 기록된다 — 다만 **매칭된 값은 어디에도 안 남는다.**"""
    client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": f"주민번호 {VALID_RRN}"},
        headers=auth(acme["service"]),
    )
    events = harness.store.list_filter_events(TenantScope("acme"))
    # 경계마다 한 행 — "안에서는 통과, 밖에서는 차단" 판정이 감사에서 사라지면 안 된다.
    assert {e["rule_id"] for e in events} == {"kr_rrn"}
    assert {e["boundary"] for e in events} == {"internal", "external"}
    assert VALID_RRN not in json.dumps([dict(e) for e in events], ensure_ascii=False)


def test_masked_prompt_is_stored_and_the_raw_is_encrypted(harness, client, acme):
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "연락처 hong@example.com", "wait": 0},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    job = harness.store.get_job(TenantScope("acme"), job_id)
    assert "hong@example.com" not in job.prompt_masked
    assert job.prompt_cipher is not None
    assert b"hong@example.com" not in job.prompt_cipher


def test_guard_narrowing_removes_the_external_boundary(harness, client, acme):
    """카드번호는 밖으로 나갈 때 `full` 이다 — 등급은 경계별로 다르게 매겨진다."""
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": f"카드 {VALID_CARD}", "wait": 0},
        headers=auth(acme["service"]),
    )
    body = response.json()
    assert body["guard_actions"]["card"] == "audit"

    job = harness.store.get_job(TenantScope("acme"), body["job_id"])
    # 내부는 audit(원본 유지), 외부는 full(치환) — 두 벌이 따로 저장된다.
    assert VALID_CARD in (job.prompt_masked or "")
    assert VALID_CARD not in (job.prompt_external or "")


def test_end_user_is_hashed_not_stored(harness, client, acme):
    """이메일을 넣어도 DB 에 이메일이 남지 않는다."""
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "안녕", "end_user": "hong@example.com", "wait": 0},
        headers=auth(acme["service"]),
    )
    job = harness.store.get_job(TenantScope("acme"), response.json()["job_id"])
    assert job.end_user_hash and "hong@example.com" not in job.end_user_hash

    dump = "".join(
        str(row) for row in harness.store._conn.execute("SELECT * FROM jobs")
    )
    assert "hong@example.com" not in dump


def test_end_user_that_looks_like_pii_is_flagged(harness, client, acme):
    client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "안녕", "end_user": "hong@example.com", "wait": 0},
        headers=auth(acme["service"]),
    )
    audit = harness.store.list_audit(TenantScope("acme"))
    flagged = [a for a in audit if a["action"] == "end_user_looks_like_pii"]
    assert flagged and "hong@example.com" not in flagged[0]["detail_json"]


def test_require_end_user_is_enforced(harness, client):
    tokens = seed_tenant(harness, "acme", require_end_user=True)
    response = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕"},
        headers=auth(tokens["service"]),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "end_user_required"


def test_oversized_prompt_is_rejected_before_the_guard_runs(harness, client, acme):
    harness.config.roles["summarize"].__dict__  # 읽기만
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "가" * 200_001},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert harness.store.count_queued("interactive") == 0


# ── 실행 ────────────────────────────────────────────────────────────────────


def test_job_runs_and_can_be_fetched(harness, client, acme):
    submitted = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()
    assert submitted["status"] == "pending"

    drive(harness)

    done = client.get(f"/v1/jobs/{submitted['job_id']}", headers=auth(acme["service"])).json()
    assert done["status"] == "ok"
    assert done["response"].startswith("[mock:")
    assert done["node"] in ("in-1", "in-2")


def test_embed_is_synchronous_and_returns_one_vector_per_input(client, acme):
    response = client.post(
        "/v1/embed", json={"role": "vec", "input": ["첫째", "둘째", "셋째"]},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["vectors"]) == 3
    assert body["node"] in ("in-1", "in-2")


def test_embed_goes_through_the_same_guard(harness, client, acme):
    """큐만 우회하고 가드는 우회하지 않는다."""
    response = client.post(
        "/v1/embed", json={"role": "vec", "input": [f"주민번호 {VALID_RRN}"]},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "guard_blocked"


def test_embed_rejects_a_generate_role(client, acme):
    response = client.post(
        "/v1/embed", json={"role": "summarize", "input": "안녕"},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "wrong_kind"


def test_generate_rejects_an_embed_role(client, acme):
    """큐에 넣으면 소비자가 영원히 폴링한다."""
    response = client.post(
        "/v1/generate", json={"role": "vec", "prompt": "안녕"},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "wrong_kind"


def test_cancel_releases_the_job_but_not_a_running_one(harness, client, acme):
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    cancelled = client.delete(f"/v1/jobs/{job_id}", headers=auth(acme["service"]))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    scope = TenantScope("acme")
    harness.store.update_job(scope, job_id, status="running")
    again = client.delete(f"/v1/jobs/{job_id}", headers=auth(acme["service"]))
    assert again.status_code == 409
    assert again.json()["code"] == "job_running"


# ── 폴링 방어 ────────────────────────────────────────────────────────────────


def test_pending_response_carries_an_adaptive_retry_after(harness, client, acme):
    for _ in range(8):
        client.post(
            "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
            headers=auth(acme["service"]),
        )
    last = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    body = last.json()
    assert body["status"] == "pending"
    assert body["retry_after"] >= 2.0
    assert last.headers["Retry-After"] == str(int(body["retry_after"]))


def test_retry_after_grows_with_the_queue(harness, client, acme):
    def submit() -> float:
        return client.post(
            "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
            headers=auth(acme["service"]),
        ).json()["retry_after"]

    first = submit()
    for _ in range(40):
        submit()
    assert submit() > first


def test_queue_position_is_tenant_scoped(harness, client, acme, globex):
    """전역 깊이를 돌려주면 남의 테넌트가 얼마나 넣었는지가 새어 나간다."""
    for _ in range(5):
        client.post(
            "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
            headers=auth(globex["service"]),
        )
    body = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()
    assert body["queue_position"] == 0


def test_status_polling_has_its_own_limit(harness, client, acme):
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    from app.main import POLL_LIMIT_PER_MIN

    codes = set()
    for _ in range(POLL_LIMIT_PER_MIN + 2):
        codes.add(client.get(f"/v1/jobs/{job_id}", headers=auth(acme["service"])).status_code)
    assert codes == {200, 429}


def test_wait_is_capped(harness, client, acme):
    """무한 대기를 요청해도 상한을 넘지 않는다 — 연결이 영원히 묶이면 안 된다."""
    meta = client.get("/v1/meta", headers=auth(acme["service"])).json()
    assert meta["wait"]["max_seconds"] == MAX_WAIT_SECONDS


# ── 테넌트 격리 ──────────────────────────────────────────────────────────────


def test_one_tenant_cannot_read_another_tenants_job(harness, client, acme, globex):
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "비밀", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    stolen = client.get(f"/v1/jobs/{job_id}", headers=auth(globex["service"]))
    assert stolen.status_code == 404
    assert stolen.json()["code"] == "job_not_found"


def test_one_tenant_cannot_cancel_another_tenants_job(client, acme, globex):
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "비밀", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]
    assert client.delete(f"/v1/jobs/{job_id}", headers=auth(globex["service"])).status_code == 404


def test_admin_job_list_never_crosses_tenants(client, acme, globex):
    client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "에크미 비밀", "wait": 0},
        headers=auth(acme["service"]),
    )
    body = client.get("/v1/admin/jobs", headers=auth(globex["tenant_admin"])).json()
    assert body["jobs"] == []


def test_admin_audit_and_usage_never_cross_tenants(client, acme, globex):
    client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    audit = client.get("/v1/admin/audit", headers=auth(globex["tenant_admin"])).json()
    assert all(row["tenant_id"] == "globex" for row in audit["audit"])

    usage = client.get("/v1/admin/usage", headers=auth(globex["tenant_admin"])).json()
    assert usage["rows"] == []


def test_guard_rules_are_per_tenant(client, acme, globex):
    client.put(
        "/v1/admin/guard/rules",
        json={"id": "acme_secret", "action": "block", "pattern": r"ACME-\d{4}"},
        headers=auth(acme["tenant_admin"]),
    )
    other = client.get("/v1/admin/guard/rules", headers=auth(globex["tenant_admin"])).json()
    assert other["tenant_rules"] == []


# ── 테넌트는 조일 수만 있다 ──────────────────────────────────────────────────


def test_tenant_can_tighten_a_baseline_rule(harness, client, acme):
    """`email` 은 베이스라인이 `partial` 이다. `block` 으로 올리는 것은 허용된다."""
    response = client.put(
        "/v1/admin/guard/rules",
        json={"id": "email", "action": "block", "pattern": r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 201

    blocked = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "메일 a@b.com"},
        headers=auth(acme["service"]),
    )
    assert blocked.status_code == 422


def test_tenant_cannot_loosen_a_baseline_rule(harness, client, acme):
    """**플랫폼이 정한 PII 차단을 테넌트가 끌 수 있으면 제품의 보증이 사라진다.**"""
    client.put(
        "/v1/admin/guard/rules",
        json={"id": "kr_rrn", "action": "off", "pattern": r"\b\d{6}[-\s]?\d{7}\b"},
        headers=auth(acme["tenant_admin"]),
    )
    still_blocked = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": f"주민번호 {VALID_RRN}"},
        headers=auth(acme["service"]),
    )
    assert still_blocked.status_code == 422
    assert still_blocked.json()["code"] == "guard_blocked"


def test_invalid_regex_is_rejected_at_save_time(client, acme):
    """저장 때 안 잡으면 다음 요청에서 가드 전체가 멈춘다."""
    response = client.put(
        "/v1/admin/guard/rules",
        json={"id": "broken", "action": "audit", "pattern": "([unclosed"},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 400
    assert response.json()["field"] == "pattern"


def test_unknown_action_grade_is_rejected(client, acme):
    response = client.put(
        "/v1/admin/guard/rules",
        json={"id": "x", "action": "obliterate", "pattern": "x"},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 400


# ── 오버라이드 ───────────────────────────────────────────────────────────────


def test_role_override_applies_and_is_visible(client, acme):
    response = client.put(
        "/v1/admin/overrides",
        json={"role": "summarize", "fields": {"timeout": 30}},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 201
    body = client.get("/v1/admin/overrides", headers=auth(acme["tenant_admin"])).json()
    assert body["overrides"]["summarize"] == {"timeout": 30}


def test_frozen_role_fields_cannot_be_overridden(client, acme):
    """`kind` 를 embed 로 바꾸면 그 역할이 큐를 우회하는 동기 경로로 넘어간다."""
    for field in ("kind", "system", "internal_only"):
        response = client.put(
            "/v1/admin/overrides",
            json={"role": "summarize", "fields": {field: "embed"}},
            headers=auth(acme["tenant_admin"]),
        )
        assert response.status_code == 400, field
        assert field in response.json()["field"]


def test_internal_roles_cannot_be_overridden(client, acme):
    response = client.put(
        "/v1/admin/overrides",
        json={"role": "_guard_classify", "fields": {"timeout": 5}},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 404


# ── 원문 열람 ────────────────────────────────────────────────────────────────


def test_raw_prompt_read_is_audited(harness, client, acme):
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "고객 메일 hong@example.com", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    response = client.get(f"/v1/admin/jobs/{job_id}/raw", headers=auth(acme["tenant_admin"]))
    assert response.status_code == 200
    assert response.json()["prompt"] == "고객 메일 hong@example.com"

    audit = harness.store.list_audit(TenantScope("acme"))
    reads = [a for a in audit if a["action"] == "read_raw_prompt"]
    assert len(reads) == 1
    assert reads[0]["target"] == job_id
    # 감사에 원문이 남으면 감사가 새 유출 경로가 된다.
    assert "hong@example.com" not in (reads[0]["detail_json"] or "")


def test_raw_prompt_is_not_readable_across_tenants(client, acme, globex):
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "비밀", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]
    stolen = client.get(f"/v1/admin/jobs/{job_id}/raw", headers=auth(globex["tenant_admin"]))
    assert stolen.status_code == 404


def test_admin_job_list_shows_only_the_masked_copy(client, acme):
    client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "메일 hong@example.com", "wait": 0},
        headers=auth(acme["service"]),
    )
    body = client.get("/v1/admin/jobs", headers=auth(acme["tenant_admin"])).json()
    assert "hong@example.com" not in json.dumps(body, ensure_ascii=False)
    assert body["jobs"][0]["has_raw"] is True


# ── 플랫폼 관리 ──────────────────────────────────────────────────────────────


def test_platform_can_create_a_tenant_with_its_locale_pack(client, acme):
    response = client.post(
        "/v1/platform/tenants",
        json={"id": "newco", "name": "NewCo", "locale": "en-US"},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 201
    assert response.json()["guard_locale_pack"] == "en_US"


def test_node_registration_probes_immediately(client, acme):
    """등록 응답이 도달 여부를 바로 말한다.

    `status` 는 아직 `unknown` 이다 — 헬스는 플래핑 방지로 연속 2회 성공을 요구한다.
    등록 화면이 알아야 하는 것은 "연결됐는가" 이지 "안정적인가" 가 아니다.
    """
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "in-3", "provider": "mock", "data_boundary": "internal",
              "max_concurrent": 2, "tags": ["internal"], "models": ["m"]},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["reachable"] is True
    assert body["status"] == "unknown"
    assert body["error"] is None


def test_unreachable_node_is_registered_but_flagged(harness, client, acme):
    """오타와 "아직 안 켬" 을 구분할 수 있어야 한다 — 등록은 남기고 사실만 알린다."""
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "dead", "provider": "mock", "data_boundary": "internal",
              "tags": ["offline"]},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 201
    assert response.json()["reachable"] is False
    assert response.json()["error"]
    assert harness.cluster.state("dead") is not None


def test_misspelled_node_field_is_rejected_not_ignored(client, acme):
    """`data_boundry` 오타를 흘려보내면 external 기본값이 조용히 적용된다."""
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "typo", "provider": "mock", "data_boundry": "internal"},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 400
    assert "data_boundry" in response.json()["field"]


def test_duplicate_node_name_is_rejected(client, acme):
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "in-1", "provider": "mock", "data_boundary": "internal"},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 409


def test_external_node_requires_tls_and_auth(client, acme):
    """경계 밖 노드는 공개망을 지난다는 뜻이므로 TLS·인증이 필수다."""
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "rented", "provider": "ollama", "data_boundary": "external",
              "base_url": "http://1.2.3.4:11434"},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "external_node_requires_auth"


def test_node_without_a_boundary_is_treated_as_external(client, acme):
    """미기재는 `external` 이다(fail-safe) — 그래서 TLS·인증 검사에 걸린다."""
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "unspecified", "provider": "ollama", "base_url": "http://x:11434"},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "external_node_requires_auth"


def test_draining_is_not_immediate_termination(harness, client, acme):
    client.post(
        "/v1/platform/nodes/in-1/drain", json={}, headers=auth(acme["platform_admin"])
    )
    assert harness.cluster.state("in-1").status == "draining"

    client.post(
        "/v1/platform/nodes/in-1/drain", json={"undrain": True},
        headers=auth(acme["platform_admin"]),
    )
    assert harness.cluster.state("in-1").status == "unknown"


def test_tenant_admin_cannot_approve_model_installs(harness, client, acme):
    """노드는 테넌트 공유 자원이다 — 남의 테넌트도 쓰는 디스크를 채울 수 없어야 한다."""
    response = client.post(
        "/v1/platform/models/whatever/approve", json={},
        headers=auth(acme["tenant_admin"]),
    )
    assert response.status_code == 403


def test_platform_overview_exposes_the_first_class_cards(client, acme):
    body = client.get("/v1/platform/overview", headers=auth(acme["platform_admin"])).json()
    assert "single_homed_roles" in body
    assert "waiting_by_reason" in body
    assert body["thresholds"]["cost_budget_burn_warn"] == 0.80


def test_platform_overview_is_audited(harness, client, acme):
    client.get(
        "/v1/platform/overview?reason=분기 리뷰", headers=auth(acme["platform_admin"])
    )
    audit = harness.store._conn.execute(
        "SELECT * FROM admin_audit WHERE action='usage_across_tenants'"
    ).fetchall()
    assert audit and "분기 리뷰" in audit[-1]["detail_json"]


def test_guard_baseline_shows_which_locale_packs_are_unused(client, acme):
    """안 켜진 필터는 없는 필터인데, 다국어에서는 켰다고 착각하기가 더 쉽다."""
    body = client.get(
        "/v1/platform/guard/baseline", headers=auth(acme["platform_admin"])
    ).json()
    assert body["packs_in_use"] == {"ko_KR": ["acme"]}
    assert "common" not in body["packs_unused"]


# ── 다국어 ──────────────────────────────────────────────────────────────────


def test_locale_changes_the_message_but_never_the_code(client, acme):
    payload = {"role": "summarize", "prompt": f"주민 {VALID_RRN}"}
    ko = client.post(
        "/v1/generate", json=payload,
        headers={**auth(acme["service"]), "Accept-Language": "ko-KR"},
    ).json()
    en = client.post(
        "/v1/generate", json=payload,
        headers={**auth(acme["service"]), "Accept-Language": "en-US"},
    ).json()

    assert ko["code"] == en["code"] == "guard_blocked"
    assert ko["retryable"] == en["retryable"] is False
    assert ko["rules"] == en["rules"] == "kr_rrn"   # 규칙 ID 도 안 바뀐다
    assert ko["message"] != en["message"]


def test_response_carries_both_the_code_and_the_message(client, acme):
    body = client.post(
        "/v1/generate", json={"role": "nope", "prompt": "안녕"},
        headers=auth(acme["service"]),
    ).json()
    assert body["code"] == "unknown_role"
    assert body["message"] and body["message"] != "unknown_role"


def test_tenant_default_locale_applies_without_a_header(harness, client, acme, globex):
    ko = client.post(
        "/v1/generate", json={"role": "nope", "prompt": "안녕"},
        headers=auth(acme["service"]),
    )
    en = client.post(
        "/v1/generate", json={"role": "nope", "prompt": "안녕"},
        headers=auth(globex["service"]),
    )
    assert ko.headers["content-language"] == "ko-KR"
    assert en.headers["content-language"] == "en-US"
    assert ko.json()["message"] != en.json()["message"]


def test_rate_limit_names_the_tier_that_tripped(harness, client):
    """자기 서비스 한도를 올려도 안 풀리는 이유가 테넌트 총량인 경우가 있다."""
    tokens = seed_tenant(harness, "acme", rate_limit=2)
    for _ in range(2):
        client.post(
            "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
            headers=auth(tokens["service"]),
        )
    limited = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(tokens["service"]),
    )
    assert limited.status_code == 429
    body = limited.json()
    assert body["code"] == "rate_limited"
    assert body["retryable"] is True
    assert body["scope"] == "tenant"


# ── 잡음 처리 ────────────────────────────────────────────────────────────────


def test_malformed_json_is_a_clean_400(client, acme):
    response = client.post(
        "/v1/generate", content=b"{not json", headers=auth(acme["service"])
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_json"


def test_missing_field_names_the_field(client, acme):
    response = client.post(
        "/v1/generate", json={"prompt": "안녕"}, headers=auth(acme["service"])
    )
    assert response.status_code == 400
    assert response.json()["field"] == "role"


def test_unknown_path_is_a_structured_error(client):
    response = client.get("/v1/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_wrong_method_is_a_structured_error(client, acme):
    response = client.put("/v1/generate", json={}, headers=auth(acme["service"]))
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


# ── 감사 H3·H4 — 성공 경로 · 적용 여부 ───────────────────────────────────────
#
# 감사가 짚은 두 패턴: **핸들러 성공 경로 미호출**(403 만 테스트) 과
# **저장만 검증하고 적용 미검증**. 둘 다 여기서 막는다.


def test_platform_tenant_list_actually_returns(harness, client, acme, globex):
    """403 만 테스트하면 성공 경로의 500 이 안 드러난다 — 실제로 안 드러났다."""
    response = client.get("/v1/platform/tenants", headers=auth(acme["platform_admin"]))
    assert response.status_code == 200

    tenants = {t["id"]: t for t in response.json()["tenants"]}
    assert {"acme", "globex"} <= set(tenants)
    # 핸들러가 읽는 컬럼이 전부 실려 있어야 한다. 하나만 빠져도 500 이다.
    for tenant in tenants.values():
        assert set(tenant) >= {
            "id", "name", "locale", "status", "budget_usd_per_month",
            "rate_limit_per_min", "has_dek", "created_at",
        }


def test_platform_tenant_list_reports_dek_presence(harness, client, acme):
    body = client.get("/v1/platform/tenants", headers=auth(acme["platform_admin"])).json()
    acme_row = next(t for t in body["tenants"] if t["id"] == "acme")
    assert acme_row["has_dek"] is True


def test_a_role_override_actually_changes_request_handling(harness, client, acme):
    """**저장됐는가가 아니라 다음 요청에 반영되는가를 본다.**

    오버라이드는 저장·감사·조회·내보내기까지 전부 동작하면서 요청 처리에는
    배선되지 않은 죽은 기능이었다. 문서가 있는 죽은 기능이 제일 나쁘다.
    """
    long_prompt = "가" * 100
    ok = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": long_prompt, "wait": 0},
        headers=auth(acme["service"]),
    )
    assert ok.status_code == 200      # 오버라이드 전에는 통과

    client.put(
        "/v1/admin/overrides",
        json={"role": "summarize", "fields": {"max_prompt_chars": 5}},
        headers=auth(acme["tenant_admin"]),
    )

    blocked = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": long_prompt, "wait": 0},
        headers=auth(acme["service"]),
    )
    assert blocked.status_code == 413, "오버라이드가 요청 처리에 반영되지 않았다"
    assert blocked.json()["limit"] == 5


def test_a_role_override_reaches_the_stored_job_snapshot(harness, client, acme):
    """제출 시점 스냅샷도 오버라이드 값을 담아야 한다 — 재현성의 기준이다."""
    client.put(
        "/v1/admin/overrides",
        json={"role": "summarize", "fields": {"timeout": 7, "options": {"temperature": 0.9}}},
        headers=auth(acme["tenant_admin"]),
    )
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    job = harness.store.get_job(TenantScope("acme"), job_id)
    assert job.timeout_s == 7
    assert job.options["temperature"] == 0.9


def test_an_override_narrowing_placement_is_honored_at_dispatch(harness, client, acme):
    """배치 티어는 스냅샷 ∩ **현재 설정**이고, 그 현재에는 오버라이드가 포함된다."""
    client.put(
        "/v1/admin/overrides",
        json={"role": "summarize", "fields": {"placement": ["internal"]}},
        headers=auth(acme["tenant_admin"]),
    )
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    harness.cluster.drain("in-1")
    harness.cluster.drain("in-2")
    drive(harness)

    job = harness.store.get_job(TenantScope("acme"), job_id)
    assert job.node is None, "내부 티어로 좁혔는데 external 로 나갔다"


def test_one_tenants_override_does_not_leak_to_another(harness, client, acme, globex):
    client.put(
        "/v1/admin/overrides",
        json={"role": "summarize", "fields": {"max_prompt_chars": 5}},
        headers=auth(acme["tenant_admin"]),
    )
    theirs = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "가" * 100, "wait": 0},
        headers=auth(globex["service"]),
    )
    assert theirs.status_code == 200, "남의 테넌트 오버라이드가 적용됐다"


def test_a_broken_override_row_does_not_kill_the_tenant(harness, client, acme):
    """**데이터 한 줄 때문에 그 테넌트의 요청이 전부 죽으면 롤백이 더 어려워진다.**"""
    harness.store.set_role_override(
        TenantScope("acme"), "summarize", {"kind": "embed"}, updated_by="tester"
    )
    response = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 200


def test_clearing_an_override_restores_the_configured_value(harness, client, acme):
    client.put(
        "/v1/admin/overrides",
        json={"role": "summarize", "fields": {"max_prompt_chars": 5}},
        headers=auth(acme["tenant_admin"]),
    )
    client.request(
        "DELETE", "/v1/admin/overrides", json={"role": "summarize"},
        headers=auth(acme["tenant_admin"]),
    )
    response = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "가" * 100, "wait": 0},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 200


# ── 감사 H7 — KEK 를 나중에 넣은 설치처 ──────────────────────────────────────
#
# 부트스트랩이 KEK 없이 돌 수 있고(원문 보관 비활성), 그 상태에서 만든 테넌트는
# `dek_wrapped` 가 NULL 이다. 나중에 KEK 를 넣으면 금고만 켜지고 그 테넌트의 키는
# 없어서 **모든 요청이 봉인 단계에서 죽었다.** 설치 순서 하나로 테넌트가 멈춘다.


def _strip_dek(store, tenant_id: str) -> None:
    """KEK 없이 만들어진 테넌트를 흉내 낸다."""
    store.adopt_tenant_dek  # 존재 확인 — 이름이 바뀌면 이 테스트가 먼저 깨져야 한다
    store._conn.execute("UPDATE tenants SET dek_wrapped = NULL WHERE id = ?", (tenant_id,))
    store._conn.commit()


def test_a_tenant_created_before_the_kek_still_works_after_it_is_set(harness, client, acme):
    """**DEK 가 없다고 요청 전체가 죽으면 안 된다.**"""
    _strip_dek(harness.store, "acme")

    response = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    assert response.status_code == 200, response.text


def test_the_backfilled_dek_actually_opens_the_ciphertext(harness, client, acme):
    """붙이기만 하고 봉인에 다른 키를 쓰면 아무도 못 여는 암호문이 쌓인다."""
    _strip_dek(harness.store, "acme")

    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "비밀 원문", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    revealed = client.get(f"/v1/admin/jobs/{job_id}/raw", headers=auth(acme["tenant_admin"]))
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["prompt"] == "비밀 원문"


def test_backfill_never_replaces_an_existing_dek(harness, acme):
    """이미 있는 DEK 를 덮으면 그 테넌트의 **기존 암호문이 통째로 열리지 않는다.**"""
    store = harness.store
    scope = TenantScope("acme")
    before = store.get_tenant("acme")["dek_wrapped"]
    assert before

    returned = store.adopt_tenant_dek(scope, harness.vault.create_dek())
    assert returned == before
    assert store.get_tenant("acme")["dek_wrapped"] == before


def test_concurrent_backfill_agrees_on_one_key(harness, acme):
    """경쟁에서 진 쪽이 자기 키로 봉인하면 그 암호문은 아무도 못 연다."""
    store = harness.store
    scope = TenantScope("acme")
    _strip_dek(store, "acme")

    first = store.adopt_tenant_dek(scope, harness.vault.create_dek())
    second = store.adopt_tenant_dek(scope, harness.vault.create_dek())
    assert first == second, "두 번째 호출이 다른 키를 돌려줬다"

    sealed = harness.vault.seal(second, "원문")
    assert harness.vault.open(first, sealed) == "원문"


def test_backfill_does_not_resurrect_a_purged_tenants_key(harness, acme):
    """파기 이후 새 암호문이 다시 쌓이면 crypto-shredding 이 무의미해진다."""
    from app.store import PlatformScope

    store = harness.store
    store.purge_tenant(PlatformScope(actor="platform_admin", reason="테스트"), "acme")

    assert store.adopt_tenant_dek(TenantScope("acme"), harness.vault.create_dek()) is None
    row = store._conn.execute(
        "SELECT dek_wrapped FROM tenants WHERE id = 'acme'"
    ).fetchone()
    assert row["dek_wrapped"] is None


def test_a_backfill_is_audited(harness, client, acme):
    """테넌트가 키를 갖게 된 시점 앞뒤로 원문 보관 여부가 갈린다 — 남겨야 한다."""
    _strip_dek(harness.store, "acme")
    client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    actions = [
        row["action"]
        for row in harness.store.list_audit(TenantScope("acme"), limit=50)
    ]
    assert "adopt_tenant_dek" in actions
