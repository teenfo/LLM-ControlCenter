"""다국어 계약.

가장 중요한 것 하나: **로케일을 바꿔도 기계용 코드는 바뀌지 않는다.**
소비자가 한국어 메시지로 분기하던 코드가 영어 환경에서 조용히 실패하면,
다국어를 넣은 것이 계약을 깬 것이 된다.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.i18n import (
    DEFAULT_LOCALE,
    ApiError,
    Translator,
    guard_pack_for,
    negotiate_locale,
)

LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"


@pytest.fixture
def translator() -> Translator:
    return Translator.from_dir(LOCALES_DIR)


# ── 기계용 코드는 번역되지 않는다 ────────────────────────────────────────────


def test_error_code_is_stable_across_locales(translator):
    error = ApiError("rate_limited", status=429, retryable=True, params={"scope": "tenant", "limit": 120})

    ko = translator.render_error(error, "ko-KR")
    en = translator.render_error(error, "en-US")

    assert ko["code"] == en["code"] == "rate_limited"
    assert ko["retryable"] == en["retryable"] is True
    assert ko["scope"] == en["scope"] == "tenant"   # 값도 번역하지 않는다
    assert ko["message"] != en["message"]           # 사람용 메시지만 바뀐다


def test_response_carries_both_code_and_message(translator):
    """분기는 code 로, 표시는 message 로. 둘 다 실려야 그게 가능하다."""
    body = translator.render_error(ApiError("unknown_role", status=404, params={"role": "nope"}))

    assert "code" in body and "message" in body
    assert body["code"] == "unknown_role"
    assert "nope" in body["message"]  # 역할 이름은 번역하지 않고 그대로 끼워 넣는다


def test_rate_limit_error_names_the_scope(translator):
    """어느 단계에서 걸렸는지 알려주지 않으면, 소비자가 한도를 늘려도 안 풀리는 이유를 모른다."""
    for scope in ("tenant", "service", "end_user"):
        body = translator.render_error(
            ApiError("rate_limited", status=429, params={"scope": scope, "limit": 10})
        )
        assert body["scope"] == scope


def test_guard_rule_ids_are_not_translated(translator):
    body = translator.render_error(
        ApiError("guard_blocked", status=422, params={"rules": "kr_rrn,credit_card"})
    )
    assert "kr_rrn" in body["message"]
    assert body["rules"] == "kr_rrn,credit_card"


# ── 카탈로그 ────────────────────────────────────────────────────────────────


def test_all_locales_have_the_same_keys():
    """키가 갈라지면 어떤 로케일에서만 원문 키가 화면에 노출된다."""
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in LOCALES_DIR.glob("*.json")
    }
    assert len(catalogs) >= 2

    reference_name, reference = next(iter(catalogs.items()))
    for name, catalog in catalogs.items():
        missing = set(reference) - set(catalog)
        extra = set(catalog) - set(reference)
        assert not missing, f"{name} 에 없는 키: {sorted(missing)} (기준: {reference_name})"
        assert not extra, f"{name} 에만 있는 키: {sorted(extra)}"


def test_missing_key_falls_back_to_key_not_exception(translator):
    """번역 누락이 예외로 서비스를 멈추게 하지 않는다."""
    assert translator.t("ui.does_not_exist") == "ui.does_not_exist"


def test_template_mismatch_does_not_crash():
    """템플릿과 인자가 안 맞아도 화면은 떠야 한다."""
    t = Translator({"ko-KR": {"greet": "{missing} 님"}}, default="ko-KR")
    assert t.t("greet") == "{missing} 님"


# ── 로케일 협상 ──────────────────────────────────────────────────────────────


def test_user_setting_beats_accept_language(translator):
    chosen = negotiate_locale(
        translator.available, accept_language="en-US", user_locale="ko-KR"
    )
    assert chosen == "ko-KR"


def test_accept_language_respects_quality_order(translator):
    chosen = negotiate_locale(
        translator.available, accept_language="ko;q=0.3,en-US;q=0.9"
    )
    assert chosen == "en-US"


def test_language_prefix_matches_regional_variant(translator):
    """ko 만 와도 ko-KR 로 붙는다."""
    assert negotiate_locale(translator.available, accept_language="ko") == "ko-KR"
    assert negotiate_locale(translator.available, accept_language="en-GB") == "en-US"


def test_tenant_default_applies_when_nothing_else_matches(translator):
    """멀티테넌트이므로 테넌트마다 기본 로케일이 다를 수 있다."""
    chosen = negotiate_locale(
        translator.available, accept_language="fr-FR", tenant_default="en-US"
    )
    assert chosen == "en-US"


def test_falls_back_to_platform_default(translator):
    chosen = negotiate_locale(translator.available, accept_language="fr-FR")
    assert chosen == DEFAULT_LOCALE


# ── 로케일 → 가드 팩 ─────────────────────────────────────────────────────────


def test_locale_maps_to_guard_pack():
    """i18n 의 진짜 영향은 번역이 아니라 이것이다 — PII 의 형태가 나라마다 다르다."""
    assert guard_pack_for("ko-KR") == "ko_KR"
    assert guard_pack_for("en-US") == "en_US"
    assert guard_pack_for("ja-JP") == "ja_JP"


def test_unknown_locale_has_no_guard_pack():
    """대응 팩이 없으면 None 이다. 조용히 다른 나라 팩을 켜지 않는다."""
    assert guard_pack_for("fr-FR") is None


# ── 코드와 카탈로그가 어긋나는 것을 잡는다 ───────────────────────────────────


def test_every_raised_error_code_has_a_message():
    """`ApiError("x")` 를 새로 던져 놓고 문구를 안 넣으면, 소비자 화면에

    `error.x` 라는 **원문 키가 그대로 뜬다.** 위의
    `test_all_locales_have_the_same_keys` 는 로케일끼리만 대조하므로,
    두 로케일에서 똑같이 빠진 키는 잡지 못한다 — 실제로 그렇게
    `plugin_rejected` 와 `unsupported_operation` 이 빠져 있었다.

    자리(placeholder)까지는 보지 않는다. `t()` 가 인자 불일치에도 화면을
    띄우게 만들어 두었으므로 그건 장애가 아니고, 여기서 보려면 호출부의
    `params` 를 정적으로 따라가야 해서 규칙이 주관적이 된다.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in LOCALES_DIR.glob("*.json")
    }

    raised: dict[str, str] = {}   # code → 처음 던진 곳
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ApiError"):
                continue
            first = node.args[0] if node.args else None
            # 코드를 실행 중에 정하는 곳이 하나 있다(배치 결과 코드).
            # 정적으로는 못 보므로 건너뛴다 — 못 보는 것을 본 척하지 않는다.
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                raised.setdefault(first.value, f"{path.relative_to(app_dir.parent)}:{node.lineno}")

    assert raised, "ApiError 호출을 하나도 못 찾았다 — 이 검사가 죽어 있다는 뜻이다"

    for name, catalog in catalogs.items():
        missing = {code: where for code, where in raised.items() if f"error.{code}" not in catalog}
        assert not missing, f"{name} 에 문구가 없는 오류 코드: {missing}"
