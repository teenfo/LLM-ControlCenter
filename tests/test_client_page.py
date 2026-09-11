"""클라이언트 페이지 — 사람이 LLM 을 쓰는 면 (feature-spec OPS-11).

관제 UI(`test_ui.py`)와 같은 규칙을 이 면에 다시 건다. 두 면이 한 벌의 코드를 못 쓰는 이유:
`test_ui.py` 가 `static/app.js` 를 **파일 단위로** 못박는다(ES 모듈 금지 · 함수 순서 슬라이스 ·
`'/v1/login'` 존재). 그래서 헬퍼는 복제했고, 여기서 두 사본이 **글자 그대로 같은지** 본다 —
복제의 드리프트는 테스트가 막는다.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from app.main import VERSION, asset_version
from tests.test_ui import strip_comments

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
CLIENT = STATIC / "client"
LOCALES = ROOT / "locales"
FILES = ("index.html", "client.js", "client.css")

#: app.js 에서 글자 그대로 복제한 헬퍼. 여기 없는 함수는 페이지 고유다.
SHARED_HELPERS = (
    "function t(key, params) {",
    "function applyStaticStrings(root) {",
    "function applyTitleStrings(root) {",
    "function el(tag, attrs, children) {",
    "async function api(path, options) {",
    "async function login(username, password) {",
)


def block(source: str, head: str) -> str:
    """`head` 로 시작하는 함수 블록(중괄호 매칭)."""
    start = source.index(head)
    depth = 0
    for i in range(source.index("{", start), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(head)


def client_keys_used() -> set[str]:
    text = (CLIENT / "client.js").read_text(encoding="utf-8") + (CLIENT / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"['\"]client\.[a-z0-9_]*['\"]\s*\+", text), "client.* 키를 조립하면 카탈로그 대조가 못 잡는다"
    return set(re.findall(r"['\"](client\.[a-z0-9_]+)['\"]", text))


# ── 정적 규칙 (관제 UI 와 같다) ────────────────────────────────────────────


def test_the_client_page_is_served_without_a_build_step():
    for name in FILES:
        assert (CLIENT / name).is_file(), name


def test_the_client_page_is_mounted(client):
    response = client.get("/client/")
    assert response.status_code == 200
    assert "LLM ControlCenter" in response.text


@pytest.mark.parametrize("name", FILES)
def test_client_has_no_external_assets(name):
    text = (CLIENT / name).read_text(encoding="utf-8")
    for pattern in ("http://", "https://", "//cdn.", "unpkg", "jsdelivr", "googleapis"):
        assert pattern not in text, f"{name} 이 외부 자산을 참조한다: {pattern}"


def test_client_has_no_framework_bundle():
    code = strip_comments((CLIENT / "client.js").read_text(encoding="utf-8"))
    for marker in ("require(", "import ", "React", "Vue.", "angular"):
        assert marker not in code, marker


def test_client_never_writes_server_data_as_html():
    code = strip_comments((CLIENT / "client.js").read_text(encoding="utf-8"))
    for marker in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML"):
        assert marker not in code, marker


def test_client_keeps_the_token_in_session_storage_only():
    """토큰은 sessionStorage, localStorage 에는 테마 키뿐. 토큰 키는 관제 UI 와 **달라야** 한다 —
    같은 탭에서 두 면이 서로의 토큰으로 자동 접속하면 안 된다."""
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    console = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "sessionStorage" in code
    for line in code.splitlines():
        if "localStorage" in line:
            assert "THEME_KEY" in line, line
            assert "TOKEN_KEY" not in line, line
    client_key = re.search(r"const TOKEN_KEY = '([^']+)'", code).group(1)
    console_key = re.search(r"const TOKEN_KEY = '([^']+)'", console).group(1)
    assert client_key != console_key


# ── 문자열 카탈로그 ─────────────────────────────────────────────────────────


def test_every_client_string_the_page_uses_exists_in_every_locale():
    used = client_keys_used()
    assert len(used) > 40
    for path in LOCALES.glob("*.json"):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        missing = sorted(used - set(catalog))
        assert not missing, f"{path.name} 에 없는 키: {missing}"


def test_no_dead_client_strings():
    """`ui.*` 의 죽은 키 검사는 app.js 만 본다 — `client.*` 는 여기서 본다."""
    declared = {k for k in json.loads((LOCALES / "ko-KR.json").read_text(encoding="utf-8")) if k.startswith("client.")}
    assert not sorted(declared - client_keys_used())


def test_the_client_page_does_not_borrow_console_strings():
    """페이지가 `ui.*` 를 쓰면 app.js 만 보는 죽은 키 검사가 거짓말을 한다."""
    # 복제한 헬퍼의 주석이 관제 UI 의 키를 예로 들 수 있으므로 주석은 뺀다.
    text = strip_comments((CLIENT / "client.js").read_text(encoding="utf-8")) + (CLIENT / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"['\"]ui\.[a-z0-9_]+['\"]", text)


def test_client_login_screen_has_real_fallback_text():
    """로그인 전에는 카탈로그가 비어 있다 — 폴백 텍스트가 첫 화면이다."""
    html = (CLIENT / "index.html").read_text(encoding="utf-8")
    login = html[html.index('id="login"'):html.index("</section>")]
    for key, text in re.findall(r'data-t="([^"]+)"[^>]*>([^<]*)<', login):
        assert text.strip() and text.strip() != key, key


def test_client_login_asks_for_an_account_first():
    html = (CLIENT / "index.html").read_text(encoding="utf-8")
    account = re.search(r'<div id="account-fields"[^>]*>', html).group(0)
    token = re.search(r'<div id="token-fields"[^>]*>', html).group(0)
    assert "hidden" not in account and "hidden" in token
    assert 'id="username"' in html and 'id="password"' in html and 'id="end-user"' in html
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    for route in ("'/v1/login'", "'/v1/logout'", "'/v1/session/password'"):
        assert route in code, route


# ── 서빙 · 캐시 ────────────────────────────────────────────────────────────


def test_client_index_without_a_trailing_slash_redirects_to_one(client):
    response = client.get("/client", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"].endswith("/client/")
    response = client.get("/client?x=1", follow_redirects=False)
    assert response.headers["location"].endswith("/client/?x=1")
    page = client.get("/client/").text
    assert 'href="client.css?v=' in page and 'src="client.js?v=' in page
    assert client.get("/client/client.css").status_code == 200


def test_the_served_client_index_substitutes_the_real_version(client):
    text = client.get("/client/").text
    assert "__VERSION__" not in text
    assert re.search(rf'client\.js\?v={re.escape(VERSION)}-[0-9a-f]{{8}}"', text)


def test_client_versioned_assets_are_immutable_and_the_index_is_not(client):
    assert client.get("/client/").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/client/client.js?v=whatever").headers["cache-control"]
    assert client.get("/client/client.js").headers["cache-control"] == "no-cache"


def test_the_asset_key_follows_the_client_files(tmp_path):
    """클라이언트 자산이 바뀌면 캐시 키도 바뀌어야 한다 — 안 그러면 옛 JS 가 새 API 를 때린다."""
    copy = tmp_path / "static"
    shutil.copytree(STATIC, copy)
    before = asset_version(copy, VERSION)
    with (copy / "client" / "client.js").open("a", encoding="utf-8") as handle:
        handle.write("\n// changed\n")
    assert asset_version(copy, VERSION) != before


# ── 관제 UI 와의 동일성 ─────────────────────────────────────────────────────


def test_shared_helpers_are_identical_to_the_console():
    """복제한 헬퍼는 글자 그대로 같아야 한다 — 한쪽만 고치면 두 면이 다르게 실패한다."""
    console = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (CLIENT / "client.js").read_text(encoding="utf-8")
    for head in SHARED_HELPERS:
        assert block(console, head) == block(page, head), head


def test_client_theme_tokens_match_the_console():
    """색 토큰(:root · 다크 두 블록)과 `[hidden]` 규칙은 바이트 단위로 같다 — 두 면이 같은 색이다."""
    console = (STATIC / "style.css").read_text(encoding="utf-8")
    page = (CLIENT / "client.css").read_text(encoding="utf-8")
    start = console.index(":root {")
    end = console.index("\n\n", console.index(':root[data-theme="dark"] {'))
    assert console[start:end] in page
    assert "[hidden] { display: none !important; }" in page


def test_client_hidden_actually_hides():
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    assert re.search(r"\$\('\w[\w-]*'\)\.hidden = ", code)


# ── 이 면 고유의 규칙 ───────────────────────────────────────────────────────


def test_client_polls_with_the_servers_retry_after():
    """고정 간격 폴링은 큐가 길수록 컨트롤 플레인을 때린다 — 서버의 retry_after 를 지킨다."""
    code = strip_comments((CLIENT / "client.js").read_text(encoding="utf-8"))
    assert "retry_after" in code
    assert "setInterval(" not in code
    assert "Idempotency-Key" in code


def test_chat_sends_a_message_array_to_the_chat_route():
    """대화는 서버의 대화 API 로 간다 — 이력을 표식으로 이어 붙이는 합성은 0.4.0 에서 끝났다."""
    code = strip_comments((CLIENT / "client.js").read_text(encoding="utf-8"))
    send = block(code, "async function sendChat(input, role, limit) {")
    assert "'/v1/chat'" in send and "messages:" in send
    assert "prompt:" not in send, "대화 요청에 prompt 를 섞어 보내면 서버가 wrong_kind 로 거절한다"
    assert "USER_MARK" not in code and "ASSISTANT_MARK" not in code and "composePrompt" not in code
    assert "<|im_start|>" not in code and "[INST]" not in code


def test_chat_trims_to_the_role_limit():
    """오래된 턴부터 버려 max_prompt_chars 에 맞춘다 — 남는 기록의 첫 턴은 user 다(서버 계약)."""
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    trim = block(code, "function trimMessages(turns, limit) {")
    assert "dropped" in trim and "limit" in trim
    assert "kept[0].role !== 'user'" in trim
    assert "max_prompt_chars" in code


def test_the_chat_tab_is_keyed_on_role_kind():
    """설치처가 역할 이름을 바꿔도 탭은 남는다 — 이름이 아니라 kind 로 고른다."""
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    assert "r.kind === 'chat'" in block(code, "function chatRole() {")
    assert "r.kind !== 'chat'" in block(code, "function askRoles() {")
    assert "CHAT_ROLE" not in code


def test_history_renders_chat_transcripts_only_from_the_masked_copy():
    """대화 잡의 턴은 서버가 저장한 마스킹본 JSON 을 푼 것이다 — 파싱 대상은 prompt_masked 뿐이다."""
    code = strip_comments((CLIENT / "client.js").read_text(encoding="utf-8"))
    helper = block(code, "function transcriptOf(j) {")
    assert "JSON.parse(j.prompt_masked)" in helper and "j.kind !== 'chat'" in helper
    assert "client.turn_user" in code and "client.turn_assistant" in code


def test_client_reads_files_in_the_browser_only():
    """파일은 브라우저 안에서 읽는다 — 업로드 라우트도 서버 변환도 없다. PDF 는 아직 받지 않는다."""
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    assert "FileReader" in code and "readAsText" in code
    accept = re.search(r"accept: '([^']+)'", code).group(1)
    assert ".pdf" not in accept and "application/pdf" not in accept
    assert "/v1/upload" not in code


def test_client_calls_only_routes_that_exist(harness):
    """API 뒷받침 없는 기능은 그리지 않는다 — 페이지가 부르는 경로는 전부 실재해야 한다."""
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    called = set(re.findall(r"'(/v1/[a-z_/.-]+)", code))
    assert called
    templates = {getattr(r, "path", "") for r in harness.app.routes}
    prefixes = {t.split("{")[0].rstrip("/") for t in templates if t.startswith("/v1")}
    for path in called:
        clean = path.split("?")[0].rstrip("/")
        assert any(clean == p or clean.startswith(p + "/") for p in prefixes), path


def test_the_chat_tab_is_only_offered_when_a_chat_role_exists():
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    pages = code[code.index("const PAGES = ["):code.index("function visiblePages")]
    assert "chatRole()" in pages


def test_history_shows_masked_prompts_only():
    code = (CLIENT / "client.js").read_text(encoding="utf-8")
    assert "prompt_masked" in code
    assert "/raw" not in code
