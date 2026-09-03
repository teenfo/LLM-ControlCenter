"""계약 자기 서빙 — 재고 자동 생성 · 토큰별 계약 · 오류 계약.

여기서 가장 중요한 테스트는 `test_every_route_has_a_summary` 다.
§13-8("손으로 관리하는 표는 반드시 어긋난다 — 실제로 어긋났다")의 **장치**이며,
원칙만 인용하고 장치를 안 만들면 같은 실수를 반복한다.
"""

from __future__ import annotations

import json

from app import meta as meta_mod
from tests.conftest import auth, seed_tenant


# ── 재고 자동 생성 ───────────────────────────────────────────────────────────


def test_every_route_has_a_summary(harness):
    """**라우트를 추가하고 요약을 안 달면 이 테스트가 실패한다.**"""
    missing = meta_mod.missing_summaries(harness.app.routes)
    assert not missing, f"요약이 없는 라우트: {missing}"


def test_no_orphan_summaries(harness):
    """반대 방향도 막는다 — 라우트를 지웠는데 요약만 남는 것."""
    assert not meta_mod.orphan_summaries(harness.app.routes)


def test_inventory_is_derived_not_handwritten(harness):
    """재고는 앱의 라우트 테이블에서 나온다. 손으로 적은 목록이 아니다."""
    live_paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert {r.path for r in meta_mod.inventory(harness.app.routes)} == live_paths


def test_adding_a_route_without_a_summary_is_caught():
    """장치가 실제로 작동하는지 — 요약 없는 라우트를 넣어 확인한다."""
    from starlette.routing import Route

    async def handler(request):  # pragma: no cover - 호출되지 않는다
        raise AssertionError

    fake = [Route("/v1/brand-new", handler, name="brand_new")]
    assert meta_mod.missing_summaries(fake) == ("/v1/brand-new (brand_new)",)


# ── 토큰별 계약 ──────────────────────────────────────────────────────────────


def test_roles_endpoint_lists_only_allowed_roles(harness, client):
    tokens = seed_tenant(harness, "acme", allow_roles=["summarize"])
    body = client.get("/v1/roles", headers=auth(tokens["service"])).json()
    assert [r["name"] for r in body["roles"]] == ["summarize"]


def test_internal_roles_are_invisible_even_with_wildcard(harness, client, acme):
    """`*` 를 줘도 `_guard_classify` 는 안 보인다. 소비자에게는 존재하지 않는다."""
    body = client.get("/v1/roles", headers=auth(acme["service"])).json()
    assert all(not r["name"].startswith("_") for r in body["roles"])


def test_openapi_role_enum_does_not_leak_other_tenants_roles(harness, client):
    """다른 테넌트의 역할 이름이 OpenAPI 에 새면 그것도 정보 유출이다."""
    limited = seed_tenant(harness, "acme", allow_roles=["summarize"])
    doc = client.get("/v1/openapi.json", headers=auth(limited["service"])).json()
    enum = doc["paths"]["/v1/generate"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["role"]["enum"]
    assert enum == ["summarize"]
    assert "inside" not in json.dumps(doc)


def test_openapi_never_exposes_model_names(harness, client, acme):
    """모델은 정책이다. 노출하면 소비자가 의존하고, 그 순간 정책을 못 바꾼다."""
    doc = client.get("/v1/openapi.json", headers=auth(acme["service"])).json()
    body = json.dumps(doc)
    for model in ("m", "cm", "guard-m"):
        assert f'"{model}"' not in body


def test_error_codes_are_documented_but_not_an_enum(harness, client, acme):
    """엄격한 검증기가 새 코드가 붙은 진짜 응답을 거부하면 안 된다."""
    doc = client.get("/v1/openapi.json", headers=auth(acme["service"])).json()
    error_schema = doc["components"]["schemas"]["Error"]
    assert "enum" not in error_schema["properties"]["code"]

    meta = client.get("/v1/meta", headers=auth(acme["service"])).json()
    codes = {e["code"] for e in meta["error_handling"]["error_codes"]}
    assert {"rate_limited", "guard_blocked", "no_placement"} <= codes
    assert meta["error_handling"]["branch_on"] == ["http_status", "retryable"]


def test_meta_endpoint_is_generated_per_token(harness, client):
    a = seed_tenant(harness, "acme", allow_roles=["summarize"])
    b = seed_tenant(harness, "globex", locale="en-US")

    doc_a = client.get("/v1/meta", headers=auth(a["service"])).json()
    doc_b = client.get("/v1/meta", headers=auth(b["service"])).json()

    assert [r["name"] for r in doc_a["roles"]] == ["summarize"]
    assert len(doc_b["roles"]) > 1
    assert doc_a["i18n"]["tenant_default"] == "ko-KR"
    assert doc_b["i18n"]["tenant_default"] == "en-US"


def test_meta_names_the_guard_locale_pack(harness, client, acme):
    """팩을 안 켜면 그 나라 PII 는 안 잡힌다. 무엇이 켜졌는지 계약에 실린다."""
    doc = client.get("/v1/meta", headers=auth(acme["service"])).json()
    assert doc["i18n"]["guard_locale_pack"] == "ko_KR"


def test_meta_endpoints_exclude_admin_routes(harness, client, acme):
    """소비자 계약에 관리 API 를 싣지 않는다 — 쓸 수 없는 것을 알려줄 이유가 없다."""
    doc = client.get("/v1/meta", headers=auth(acme["service"])).json()
    paths = {e["path"] for e in doc["endpoints"]}
    assert not any("/admin/" in p or "/platform/" in p for p in paths)
    assert "/v1/generate" in paths


# ── 통합 가이드 ──────────────────────────────────────────────────────────────


def test_integration_guide_is_markdown_and_lists_allowed_roles(harness, client):
    tokens = seed_tenant(harness, "acme", allow_roles=["summarize"])
    response = client.get("/v1/integration", headers=auth(tokens["service"]))
    assert response.headers["content-type"].startswith("text/markdown")
    assert "`summarize`" in response.text
    assert "`inside`" not in response.text


def test_integration_guide_warns_against_branching_on_message(harness, client, acme):
    text = client.get("/v1/integration", headers=auth(acme["service"])).text
    assert "`message` 로 분기하지 않는다" in text
    assert "retry_after" in text


def test_integration_guide_follows_the_request_locale(harness, client, acme):
    ko = client.get(
        "/v1/integration", headers={**auth(acme["service"]), "Accept-Language": "ko-KR"}
    )
    en = client.get(
        "/v1/integration", headers={**auth(acme["service"]), "Accept-Language": "en-US"}
    )
    assert ko.headers["content-language"] == "ko-KR"
    assert en.headers["content-language"] == "en-US"
    # 예시 오류 본문만 로케일을 탄다. 코드는 그대로다.
    assert "rate_limited" in ko.text and "rate_limited" in en.text


# ── 무인증 경로 ──────────────────────────────────────────────────────────────


def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_healthz_does_not_touch_the_database(harness, client):
    """DB 가 느릴 때 헬스체크까지 느려지면 오케스트레이터가 멀쩡한 컨테이너를 죽인다."""
    harness.store.close()
    assert client.get("/healthz").status_code == 200


def test_openapi_marks_healthz_as_unauthenticated(harness, client, acme):
    doc = client.get("/v1/openapi.json", headers=auth(acme["service"])).json()
    assert doc["paths"]["/healthz"]["get"]["security"] == []


# ── 클라이언트 번들 ──────────────────────────────────────────────────────────


def test_client_index_lists_bundled_files(harness, client, acme, tmp_path):
    (tmp_path / "client.py").write_text("# 단일 파일 클라이언트\n", encoding="utf-8")
    harness.app.state.ctx.client_dir = tmp_path

    body = client.get("/v1/client", headers=auth(acme["service"])).json()
    assert [f["name"] for f in body["files"]] == ["client.py"]


def test_client_file_refuses_path_traversal(harness, client, acme, tmp_path):
    (tmp_path / "client.py").write_text("ok\n", encoding="utf-8")
    harness.app.state.ctx.client_dir = tmp_path

    assert client.get("/v1/client/client.py", headers=auth(acme["service"])).text == "ok\n"
    escaped = client.get("/v1/client/..%2F..%2Fetc%2Fpasswd", headers=auth(acme["service"]))
    assert escaped.status_code == 404
