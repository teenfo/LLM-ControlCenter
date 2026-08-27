"""관제 UI — 정적 자산 · `/v1/session` · UI 가 의존하는 계약.

UI 자체는 브라우저에서 돌지만, **깨지는 지점은 대부분 서버 쪽 계약이다** —
문자열 키가 사라지거나, 세션이 역할을 안 알려주거나, 화면이 쓰는 필드가
이름을 바꾸거나. 그것들을 여기서 못박는다.

그리고 두 가지 규칙을 파일 자체에 대해 검사한다:
**외부 CDN 을 안 쓴다**(에어갭에서 깨진다) · **`innerHTML` 로 서버 데이터를 안 꽂는다**.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conftest import auth, seed_tenant

STATIC = Path(__file__).resolve().parent.parent / "static"
LOCALES = Path(__file__).resolve().parent.parent / "locales"


# ── 정적 자산 ────────────────────────────────────────────────────────────────


def test_the_ui_is_served_without_a_build_step():
    for name in ("index.html", "app.js", "style.css"):
        assert (STATIC / name).is_file(), name


def test_ui_is_mounted(client):
    response = client.get("/ui/")
    assert response.status_code == 200
    assert "LLM ControlCenter" in response.text


@pytest.mark.parametrize("name", ["index.html", "app.js", "style.css"])
def test_no_external_assets(name):
    """**에어갭에서 화면이 깨지고, 인터넷이 있어도 남의 사이트 개편에 끌려 죽는다.**"""
    text = (STATIC / name).read_text(encoding="utf-8")
    for pattern in ("http://", "https://", "//cdn.", "unpkg", "jsdelivr", "googleapis"):
        assert pattern not in text, f"{name} 이 외부 자산을 참조한다: {pattern}"


def test_no_framework_bundle():
    """의존성 5개를 이식성의 근거로 삼은 제품이 프런트에서 그것을 버릴 이유가 없다."""
    code = strip_comments((STATIC / "app.js").read_text(encoding="utf-8"))
    for marker in ("require(", "import ", "React", "Vue.", "angular"):
        assert marker not in code, marker


def strip_comments(source: str) -> str:
    """주석을 뺀 코드. 주석에 적어 둔 금지어가 검사에 걸리면 안 된다."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//.*$", "", source)


def test_server_data_is_never_written_as_html():
    """`innerHTML` 로 서버 데이터를 꽂으면 테넌트 이름 하나로 XSS 가 열린다."""
    code = strip_comments((STATIC / "app.js").read_text(encoding="utf-8"))
    assert "innerHTML" not in code
    assert "outerHTML" not in code
    assert "document.write" not in code
    assert "insertAdjacentHTML" not in code


def test_the_token_is_kept_in_session_storage_only():
    """공용 PC 에 토큰이 남지 않게 — 탭을 닫으면 지워져야 한다."""
    code = strip_comments((STATIC / "app.js").read_text(encoding="utf-8"))
    assert "sessionStorage" in code
    assert "localStorage" not in code


def test_the_ui_only_opens_the_raw_prompt_one_job_at_a_time():
    """화면은 마스킹본만 본다. 원문은 **단건 API + 감사**다."""
    code = strip_comments((STATIC / "app.js").read_text(encoding="utf-8"))
    assert "/raw'" in code, "단건 원문 경로를 안 쓴다"
    # 목록에 원문을 실어 달라고 부르는 코드가 없어야 한다.
    assert "/admin/jobs/raw" not in code
    assert "raw=1" not in code and "include_raw" not in code
    # 열기 전에 사람이 한 번 확인한다 — 감사에 남는 행위이기 때문이다.
    assert "confirm(t('ui.raw_audited'))" in code


# ── 문자열 카탈로그 ──────────────────────────────────────────────────────────


def ui_keys_used() -> set[str]:
    """화면이 실제로 쓰는 키.

    **키를 문자열로 조립하면 여기서 못 잡는다** — 그래서 UI 쪽에서 조립을 금지하고
    조회표(`BOUNDARY_LABEL` 등)로 쓰며, 아래 검사가 조립 흔적을 잡아낸다.
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"['\"]ui\.[a-z0-9_]*['\"]\s*\+", js), "번역 키를 조립하고 있다"
    return set(re.findall(r"['\"](ui\.[a-z0-9_]+)['\"]", js + html))


def test_every_ui_string_the_screen_uses_exists_in_every_locale():
    """**키를 추가하고 번역을 안 달면 화면에 키 문자열이 그대로 뜬다.**"""
    used = ui_keys_used()
    assert len(used) > 30, "UI 문자열을 못 찾았다 — 추출 정규식을 확인할 것"

    for path in LOCALES.glob("*.json"):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        missing = sorted(used - set(catalog))
        assert not missing, f"{path.name} 에 없는 키: {missing}"


def test_locale_catalogs_stay_in_parity():
    catalogs = {
        path.name: set(json.loads(path.read_text(encoding="utf-8")))
        for path in LOCALES.glob("*.json")
    }
    reference = next(iter(catalogs.values()))
    for name, keys in catalogs.items():
        assert keys == reference, f"{name} 의 키가 다르다: {sorted(keys ^ reference)}"


def test_no_dead_ui_strings():
    """반대 방향도 본다 — 화면에서 안 쓰는 문자열이 카탈로그에 쌓이면 번역 비용만 는다."""
    catalog = json.loads((LOCALES / "ko-KR.json").read_text(encoding="utf-8"))
    declared = {k for k in catalog if k.startswith("ui.")}
    assert not sorted(declared - ui_keys_used())


# ── /v1/session ──────────────────────────────────────────────────────────────


def test_session_tells_the_ui_its_role_and_strings(client, acme):
    body = client.get("/v1/session", headers=auth(acme["tenant_admin"])).json()
    assert body["is_tenant_admin"] is True
    assert body["is_platform_admin"] is False
    assert body["tenant"]["id"] == "acme"
    assert body["strings"]["ui.nodes"] == "노드"


def test_session_follows_the_requested_locale(client, acme):
    en = client.get(
        "/v1/session", headers={**auth(acme["tenant_admin"]), "Accept-Language": "en-US"}
    ).json()
    assert en["locale"] == "en-US"
    assert en["strings"]["ui.nodes"] == "Nodes"


def test_session_falls_back_to_the_tenant_locale(client, globex):
    body = client.get("/v1/session", headers=auth(globex["tenant_admin"])).json()
    assert body["locale"] == "en-US"


def test_session_reports_the_conditions_the_ui_must_show(harness, client, acme):
    """**조용한 실패를 시끄럽게 만드는 것이 관제 UI 의 일이다.**"""
    body = client.get("/v1/session", headers=auth(acme["service"])).json()
    assert body["guard_locale_pack"] == "ko_KR"
    assert body["raw_prompt_storage"] is True
    assert "guard_classifier_ready" in body
    assert body["airgap"] is False


def test_session_flags_a_missing_locale_pack(harness, client):
    """팩이 없으면 그 나라 PII 는 안 잡힌다. UI 가 상시 표시할 근거를 준다."""
    tokens = seed_tenant(harness, "nordic", locale="en-US")
    body = client.get("/v1/session", headers=auth(tokens["service"])).json()
    assert body["guard_locale_pack"] == "en_US"


def test_session_never_returns_the_token(client, acme):
    body = client.get("/v1/session", headers=auth(acme["service"])).text
    assert acme["service"] not in body


def test_session_needs_auth(client):
    assert client.get("/v1/session").status_code == 401


def test_session_carries_only_the_negotiated_locale(client, acme):
    """전체 카탈로그를 보내면 쓰지도 않을 번역이 매 요청마다 따라다닌다."""
    body = client.get(
        "/v1/session", headers={**auth(acme["service"]), "Accept-Language": "ko-KR"}
    ).json()
    assert body["strings"]["ui.nodes"] == "노드"
    assert "Nodes" not in json.dumps(body["strings"], ensure_ascii=False)


# ── UI 가 의존하는 응답 필드 ─────────────────────────────────────────────────


def test_platform_overview_has_the_fields_the_first_class_cards_read(client, acme):
    body = client.get("/v1/platform/overview", headers=auth(acme["platform_admin"])).json()
    for key in ("single_homed_roles", "waiting_by_reason", "lanes", "nodes",
                "usage_by_tenant", "model_requests_pending", "tenants"):
        assert key in body, key


def test_node_grid_carries_the_boundary_badge(client, acme):
    """`provider: ollama` 가 로컬이라는 뜻이 아니다 — 배지는 경계를 보여줘야 한다."""
    body = client.get("/v1/platform/nodes", headers=auth(acme["platform_admin"])).json()
    for node in body["nodes"]:
        assert node["data_boundary"] in ("internal", "external")
        assert {"status", "running", "max_concurrent", "metered"} <= set(node)


def test_guard_view_shows_the_effective_merged_rules(client, acme):
    """화면이 보여줘야 하는 것은 베이스라인도 테넌트 규칙도 아니라 **적용된 값**이다."""
    body = client.get("/v1/admin/guard/rules", headers=auth(acme["tenant_admin"])).json()
    assert body["effective"]
    for rule in body["effective"]:
        assert set(rule["action"]) == {"internal", "external"}


def test_usage_view_axes_match_what_the_selector_offers(client, acme):
    """UI 셀렉터에 있는 축이 서버에서 거절되면 화면이 빈다."""
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    offered = re.search(r"\[([^\]]*'service_id'[^\]]*)\]", text).group(1)
    axes = re.findall(r"'([a-z_]+)'", offered)
    assert "service_id" in axes

    for axis in axes:
        response = client.get(
            f"/v1/admin/usage?by={axis}", headers=auth(acme["tenant_admin"])
        )
        assert response.status_code == 200, axis


def test_job_list_gives_the_ui_a_raw_availability_flag(harness, client, acme):
    """버튼을 띄울지 말지를 화면이 추측하면 안 된다."""
    client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    )
    body = client.get("/v1/admin/jobs", headers=auth(acme["tenant_admin"])).json()
    assert body["jobs"][0]["has_raw"] is True
    assert "prompt_cipher" not in body["jobs"][0]


def test_settings_view_can_tell_a_capped_value_from_an_applied_one(client, acme):
    client.put(
        "/v1/admin/settings", json={"raw_prompt_retention_days": 999},
        headers=auth(acme["tenant_admin"]),
    )
    body = client.get("/v1/admin/settings", headers=auth(acme["tenant_admin"])).json()
    assert body["raw_prompt_retention_days_requested"] == 999
    assert body["raw_prompt_retention_days"] < 999


# ── 감사 M23 — 로그인 화면이 항상 원시 i18n 키를 표시한다 ───────────────────


def test_static_strings_do_not_overwrite_the_fallback_when_empty():
    """**모든 설치의 첫 화면이 `"ui.sign_in"` 으로 깨져 보였다.**

    `t()` 는 없는 키를 키 자체로 돌려주는데(누락이 화면을 멈추게 하지 않는다),
    로그인 전에는 카탈로그가 통째로 비어 있다 — 세션 API 로 받아오기 때문이다.
    그래서 `applyStaticStrings()` 가 index.html 의 폴백 텍스트를 키 리터럴로
    덮어썼다.
    """
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function applyStaticStrings"):]
    body = body[:body.index("\n}")]

    assert "t(node.dataset.t)" not in body, "카탈로그가 비어도 폴백을 덮어쓴다"
    assert "if (text)" in body, "빈 값을 거르지 않는다"


def test_the_login_screen_has_real_fallback_text():
    """폴백이 없으면 위 수정이 화면을 비워 버린다 — 둘은 한 벌이다."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    login = html[html.index('id="login"'):]
    login = login[:login.index("</section>")] if "</section>" in login else login

    labelled = re.findall(r'data-t="([^"]+)"[^>]*>([^<]*)<', login)
    assert labelled, "로그인 화면에 data-t 요소가 없다"
    for key, text in labelled:
        assert text.strip(), f"{key} 에 폴백 텍스트가 없다"
        assert text.strip() != key, f"{key} 의 폴백이 키 자체다"


# ── 감사 M28 — 모델 화면이 서버의 `missing` 목록을 안 그린다 ────────────────


def test_the_models_view_renders_what_is_missing():
    """`역할이 요구하는데 어느 노드에도 없는 모델` 을 서버가 주는데 안 그렸다.

    그 잡들은 레인을 막지 않고 조용히 대기하므로(§13-6), 화면에 안 보이면
    관리자는 왜 그 역할만 안 도는지 알 방법이 없다.
    """
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    view = source[source.index("async function renderModels"):]
    view = view[:view.index("\nasync function ")]

    assert "m.missing" in view, "missing 목록을 읽지 않는다"
    assert "ui.missing_models" in view, "missing 을 카드로 그리지 않는다"


def test_the_missing_list_offers_the_action_that_fixes_it():
    """보여주기만 하고 고칠 방법을 안 주면 화면을 한 번 더 옮겨 다녀야 한다."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "async function requestInstall" in source
    assert "ui.request_install" in source


def test_the_missing_model_endpoint_actually_returns_it(harness, client, acme):
    """화면을 고쳤는데 서버가 안 주면 소용없다 — 양쪽을 함께 못박는다."""
    # 노드는 살아 있고 인벤토리도 받았는데 **역할이 요구하는 모델만 없다.**
    # 인벤토리가 비어 있으면(프로브 전) 모른다는 이유로 요청하지 않으므로,
    # 그 상태로는 이 경로를 못 지난다.
    for state in harness.cluster.nodes.values():
        state.models = frozenset({"전혀-다른-모델"})

    body = client.get(
        "/v1/platform/models", headers=auth(acme["platform_admin"])
    ).json()
    assert body["missing"], "역할이 요구하는 모델이 없는데 missing 이 비어 있다"
    assert {"node", "model"} <= set(body["missing"][0])
