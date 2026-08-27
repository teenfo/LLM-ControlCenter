"""가드 — 체크섬 · 마스킹 · 경계 축소 · 계층 · 암호화."""

from __future__ import annotations

import base64
import re

import pytest

from app.config import (
    EXTERNAL,
    INTERNAL,
    Config,
    GuardRule,
    GuardSettings,
    Lane,
    Node,
    Pricing,
    Role,
    Thresholds,
    load_config,
)
from app.crypto import (
    CryptoError,
    KeyDestroyed,
    KeyVault,
    Sealed,
    generate_master_key,
    load_master_key,
)
from app.guard import (
    CHECKSUMS,
    Detection,
    Guard,
    _apply,
    iban_mod97,
    jp_mynumber,
    kr_biz,
    kr_rrn,
    luhn,
)

REPO_CONFIG = "config"


# ── 체크섬 ──────────────────────────────────────────────────────────────────
#
# 체크섬이 없으면 숫자 나열이 전부 PII 가 되고, 오탐이 쏟아지면 관리자가 규칙을 꺼버린다.


@pytest.mark.parametrize("value", ["4111111111111111", "4111-1111-1111-1111", "5500005555555559"])
def test_luhn_accepts_valid_cards(value):
    assert luhn(value) is True


@pytest.mark.parametrize("value", ["4111111111111112", "1234567890123456", "123"])
def test_luhn_rejects_invalid(value):
    assert luhn(value) is False


def test_kr_rrn_checksum():
    assert kr_rrn("900101-1234568") is True
    assert kr_rrn("900101-1234567") is False, "체크섬이 틀린데 통과했다"
    assert kr_rrn("900101123456") is False, "12자리는 주민번호가 아니다"


def test_check_digit_rejects_most_but_not_all_random_numbers():
    """검증부호는 1자리라 **무작위 값의 약 90%만** 걸러낸다.

    `1234567890123` 은 우연히 유효한 검증부호를 갖는다. 이건 알고리즘의 한계이지
    구현 버그가 아니다 — 그래서 가드 품질을 정답셋으로 **측정**해야 하고,
    새 규칙을 바로 block 으로 켜지 않는다.
    """
    assert kr_rrn("1234567890123") is True   # 우연히 통과한다

    passing = sum(1 for n in range(900101_1000000, 900101_1000200) if kr_rrn(str(n)))
    assert 10 <= passing <= 30, f"200개 중 {passing}개 통과 — 대략 1/10 이 예상값"


def test_kr_biz_checksum():
    assert kr_biz("123-45-67891") is True
    assert kr_biz("123-45-67890") is False


def test_jp_mynumber_checksum():
    assert jp_mynumber("123456789018") is True
    assert jp_mynumber("123456789012") is False


def test_iban_checksum():
    assert iban_mod97("GB82 WEST 1234 5698 7654 32") is True
    assert iban_mod97("GB82WEST12345698765433") is False


def test_every_configured_checksum_has_an_implementation():
    """설정이 참조하는 검증기가 없으면 그 규칙은 조용히 패턴만으로 동작한다."""
    cfg = load_config(REPO_CONFIG)
    referenced = {r.checksum for r in cfg.guard_rules if r.checksum}
    assert referenced <= set(CHECKSUMS), f"구현 없는 검증기: {referenced - set(CHECKSUMS)}"


def test_checksum_suppresses_false_positives_in_real_config():
    """13자리 숫자가 전부 주민번호로 잡히면 안 된다."""
    cfg = load_config(REPO_CONFIG)
    rule = next(r for r in cfg.guard_rules if r.id == "kr_rrn")
    text = "주문번호 9001011234567 을 확인해 주세요"

    assert re.search(rule.pattern, text), "패턴 자체는 걸린다"
    assert kr_rrn("9001011234567") is False, "체크섬이 걸러야 한다"


# ── 가드 구성 ────────────────────────────────────────────────────────────────


def make_config(rules: tuple[GuardRule, ...], settings: GuardSettings | None = None) -> Config:
    return Config(
        nodes={"n": Node(name="n", provider="mock", data_boundary="internal", tags=("internal",))},
        roles={"r": Role(name="r", model="m", placement=("internal",))},
        lanes={"interactive": Lane("interactive", 1)},
        guard_rules=rules,
        guard_settings=settings or GuardSettings(),
        pricing=Pricing(table={}),
        thresholds=Thresholds(),
        catalog=(),
    )


RRN_RULE = GuardRule(
    id="kr_rrn", kind="pattern", action="full", label="[주민등록번호]",
    pattern=r"\b\d{6}[-\s]?[1-4]\d{6}\b", checksum="kr_rrn", locale_pack="ko_KR",
)
PHONE_RULE = GuardRule(
    id="phone", kind="pattern", action="partial", keep_tail=4, label="[휴대폰]",
    pattern=r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b", locale_pack="ko_KR",
)
EMAIL_RULE = GuardRule(
    id="email", kind="pattern", action="audit", label="[이메일]",
    pattern=r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", locale_pack="common",
)
TIERED_RULE = GuardRule(
    id="finance", kind="llm", action={INTERNAL: "audit", EXTERNAL: "block"},
    label="[미공개재무]", description="공시 전 재무 수치", locale_pack="common",
)


# ── 마스킹 ──────────────────────────────────────────────────────────────────


async def test_full_masking_replaces_the_value():
    guard = Guard(make_config((RRN_RULE,)))
    result = await guard.inspect("제 번호는 900101-1234568 입니다", locales=["ko_KR"])

    assert "900101" not in result.storable_prompt
    assert "[주민등록번호]" in result.storable_prompt


async def test_partial_masking_keeps_the_tail():
    guard = Guard(make_config((PHONE_RULE,)))
    result = await guard.inspect("연락처 010-1234-5678", locales=["ko_KR"])

    assert "[휴대폰]5678" in result.storable_prompt
    assert "010-1234" not in result.storable_prompt


async def test_audit_grade_passes_through_but_records():
    """audit 은 통과시키되 탐지 사실만 기록한다 — 승격 전 오탐률 측정용."""
    guard = Guard(make_config((EMAIL_RULE,)))
    result = await guard.inspect("hong@example.com 로 보내주세요")

    assert "hong@example.com" in result.storable_prompt, "audit 인데 마스킹됐다"
    assert result.detections[0].rule_id == "email"


async def test_multiple_matches_are_all_masked():
    guard = Guard(make_config((RRN_RULE,)))
    result = await guard.inspect(
        "A는 900101-1234568, B는 900101-1234568", locales=["ko_KR"]
    )

    assert result.storable_prompt.count("[주민등록번호]") == 2
    assert result.detections[0].match_count == 2


async def test_sequential_matches_do_not_corrupt_the_text():
    """뒤에서 앞으로 치환하지 않으면 오프셋이 밀려 다음 스팬이 어긋난다.

    **이건 겹치지 않는 매치다.** 진짜 겹침은 아래 `_coalesce` 테스트들이 본다 —
    예전 이름이 `overlapping` 이었는데 실제로는 이 경우만 검증하고 있었고,
    그래서 겹침 결함이 753개 테스트를 전부 통과한 채 살아 있었다.
    """
    guard = Guard(make_config((RRN_RULE, PHONE_RULE)))
    result = await guard.inspect(
        "번호 900101-1234568 이고 폰은 010-1234-5678 이다", locales=["ko_KR"]
    )

    assert "[주민등록번호]" in result.storable_prompt
    assert "[휴대폰]5678" in result.storable_prompt
    assert "900101" not in result.storable_prompt


async def test_system_prompt_is_masked_too():
    guard = Guard(make_config((RRN_RULE,)))
    result = await guard.inspect(
        "질문", system="담당자 900101-1234568", locales=["ko_KR"]
    )
    assert "900101" not in result.system_for(INTERNAL)


async def test_checksum_failure_leaves_text_untouched():
    guard = Guard(make_config((RRN_RULE,)))
    result = await guard.inspect("주문 9001011234567 확인", locales=["ko_KR"])

    assert "9001011234567" in result.storable_prompt
    assert result.detections == ()


# ── 경계별 축소 ──────────────────────────────────────────────────────────────


async def test_tiered_rule_narrows_the_allowed_boundaries():
    """안에서는 보되 밖으로는 안 내보낸다 — 차단 등급에 걸린 경계만 뺀다."""
    async def classifier(text, rules):
        return {"finance"}

    guard = Guard(make_config((TIERED_RULE,)), classifier=classifier)
    result = await guard.inspect("실적 추정치입니다")

    assert result.allowed_boundaries == {INTERNAL}
    assert result.blocked is False, "내부로는 갈 수 있어야 한다"
    assert "finance" in result.blocked_rules


async def test_block_on_every_boundary_blocks_the_request():
    rule = GuardRule(id="hard", kind="pattern", action="block", pattern=r"SECRET", label="[X]")
    guard = Guard(make_config((rule,)))
    result = await guard.inspect("this is SECRET")

    assert result.blocked is True
    assert result.allowed_boundaries == frozenset()


async def test_boundary_specific_masking_produces_two_variants():
    rule = GuardRule(
        id="tiered", kind="pattern", action={INTERNAL: "audit", EXTERNAL: "full"},
        pattern=r"\b\d{6}-\d{7}\b", label="[가림]",
    )
    guard = Guard(make_config((rule,)))
    result = await guard.inspect("값 900101-1234568 임")

    assert "900101-1234568" in result.prompt_for(INTERNAL), "내부는 audit 이라 통과"
    assert "[가림]" in result.prompt_for(EXTERNAL), "외부는 full 이라 마스킹"


async def test_candidate_boundaries_restrict_the_result():
    """역할이 내부만 쓰면 외부 등급은 판정에 영향을 주지 않는다."""
    async def classifier(text, rules):
        return {"finance"}

    guard = Guard(make_config((TIERED_RULE,)), classifier=classifier)
    result = await guard.inspect("실적", candidate_boundaries=[INTERNAL])

    assert result.allowed_boundaries == {INTERNAL}
    assert result.blocked is False


# ── 2단 분류기 ───────────────────────────────────────────────────────────────


async def test_classifier_receives_masked_text_not_raw():
    """분류기에도 원문을 주지 않는다."""
    seen: list[str] = []

    async def classifier(text, rules):
        seen.append(text)
        return set()

    guard = Guard(make_config((RRN_RULE, TIERED_RULE)), classifier=classifier)
    await guard.inspect("주민번호 900101-1234568 포함", locales=["ko_KR"])

    assert "900101" not in seen[0]
    assert "[주민등록번호]" in seen[0]


async def test_classifier_failure_is_not_a_verdict():
    """분류 실패는 "민감하지 않음" 판정이 아니다."""
    async def failing(text, rules):
        raise RuntimeError("내부 노드가 없다")

    guard = Guard(
        make_config((TIERED_RULE,), GuardSettings(on_classifier_error="mask")),
        classifier=failing,
    )
    result = await guard.inspect("무언가")

    assert result.classifier_failed is True
    assert EXTERNAL not in result.allowed_boundaries, "판정 못 했는데 밖으로 내보냈다"
    assert INTERNAL in result.allowed_boundaries


async def test_classifier_failure_block_policy():
    async def failing(text, rules):
        raise RuntimeError("실패")

    guard = Guard(
        make_config((TIERED_RULE,), GuardSettings(on_classifier_error="block")),
        classifier=failing,
    )
    result = await guard.inspect("무언가")
    assert result.blocked is True


async def test_classifier_failure_allow_policy():
    async def failing(text, rules):
        raise RuntimeError("실패")

    guard = Guard(
        make_config((TIERED_RULE,), GuardSettings(on_classifier_error="allow")),
        classifier=failing,
    )
    result = await guard.inspect("무언가")
    assert result.allowed_boundaries == {INTERNAL, EXTERNAL}


async def test_missing_classifier_counts_as_failure():
    guard = Guard(make_config((TIERED_RULE,)))   # classifier=None
    result = await guard.inspect("무언가")
    assert result.classifier_failed is True


async def test_no_context_rules_means_no_classifier_call():
    """맥락 규칙이 없으면 분류기를 부르지 않는다 — 불필요한 추론 1회를 아낀다."""
    calls = []

    async def classifier(text, rules):
        calls.append(text)
        return set()

    guard = Guard(make_config((RRN_RULE,)), classifier=classifier)
    await guard.inspect("평범한 문장", locales=["ko_KR"])
    assert calls == []


# ── 로케일 팩 ────────────────────────────────────────────────────────────────


async def test_locale_pack_off_means_that_countrys_pii_is_not_caught():
    """안 켜진 필터는 없는 필터다 — 다국어에서는 켰다고 착각하기가 더 쉽다."""
    guard = Guard(make_config((RRN_RULE, EMAIL_RULE)))

    without = await guard.inspect("900101-1234568", locales=["en_US"])
    assert without.detections == (), "로케일 팩을 안 켰는데 잡혔다"

    with_pack = await guard.inspect("900101-1234568", locales=["ko_KR"])
    assert with_pack.detections


async def test_common_pack_is_always_on():
    guard = Guard(make_config((RRN_RULE, EMAIL_RULE)))
    result = await guard.inspect("a@b.com", locales=["en_US"])
    assert result.detections[0].rule_id == "email"


# ── 테넌트 계층 ──────────────────────────────────────────────────────────────


def test_tenant_can_tighten_a_baseline_rule():
    guard = Guard(make_config((EMAIL_RULE,)))
    tenant = GuardRule(id="email", kind="pattern", action="block", pattern=EMAIL_RULE.pattern)

    merged = {r.id: r for r in guard.rules_for(tenant_rules=[tenant])}
    assert merged["email"].action_for_boundary(INTERNAL) == "block"


def test_tenant_cannot_loosen_a_baseline_rule():
    """플랫폼이 정한 PII 차단을 테넌트가 끌 수 있으면 제품의 보증이 사라진다."""
    guard = Guard(make_config((RRN_RULE,)))   # baseline: full
    tenant = GuardRule(id="kr_rrn", kind="pattern", action="off", pattern=RRN_RULE.pattern)

    merged = {r.id: r for r in guard.rules_for(locales=["ko_KR"], tenant_rules=[tenant])}
    assert merged["kr_rrn"].action_for_boundary(INTERNAL) == "full", "테넌트가 규칙을 껐다"


def test_tenant_can_add_a_new_rule():
    guard = Guard(make_config((EMAIL_RULE,)))
    tenant = GuardRule(id="internal_code", kind="pattern", action="full", pattern=r"ACME-\d+")

    merged = {r.id: r for r in guard.rules_for(tenant_rules=[tenant])}
    assert "internal_code" in merged


async def test_tenant_added_rule_actually_masks():
    guard = Guard(make_config((EMAIL_RULE,)))
    tenant = GuardRule(
        id="code", kind="pattern", action="full", pattern=r"ACME-\d+", label="[사내코드]"
    )
    result = await guard.inspect("문서 ACME-4421 참조", tenant_rules=[tenant])

    assert "[사내코드]" in result.storable_prompt


def test_tiered_tightening_is_per_boundary():
    guard = Guard(make_config((TIERED_RULE,)))   # internal: audit, external: block
    tenant = GuardRule(id="finance", kind="llm", action="full", description="x")

    merged = {r.id: r for r in guard.rules_for(tenant_rules=[tenant])}
    rule = merged["finance"]
    assert rule.action_for_boundary(INTERNAL) == "full", "내부는 audit -> full 로 올라야 한다"
    assert rule.action_for_boundary(EXTERNAL) == "block", "외부는 block 이 유지돼야 한다"


# ── 큰 프롬프트 ──────────────────────────────────────────────────────────────


async def test_large_prompt_is_offloaded_and_still_correct():
    """200KB × 20패턴을 이벤트 루프 위에서 돌리면 다른 모든 요청이 멈춘다."""
    guard = Guard(
        make_config((RRN_RULE,), GuardSettings(stage1_threadpool_threshold_bytes=100))
    )
    big = "가" * 50_000 + " 900101-1234568 " + "나" * 50_000
    result = await guard.inspect(big, locales=["ko_KR"])

    assert "900101" not in result.storable_prompt
    assert "[주민등록번호]" in result.storable_prompt


# ── 암호화 ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault() -> KeyVault:
    return KeyVault(base64.b64decode(generate_master_key()))


def test_vault_disabled_without_a_master_key():
    """키 설정을 깜빡한 채 원문이 평문으로 쌓이는 사고를 구조적으로 막는다."""
    disabled = KeyVault(None)

    assert disabled.enabled is False
    assert disabled.seal(None, "민감한 원문") is None, "키가 없는데 무언가를 돌려줬다"


def test_no_plaintext_fallback_path_exists():
    """'키가 없으면 평문으로라도 저장' 은 가장 나쁜 기본값이다."""
    disabled = KeyVault(None)
    sealed = disabled.seal(None, "원문")
    assert sealed is None
    # 평문을 담은 대체 객체를 돌려주지 않는다.
    assert not isinstance(sealed, (str, bytes, Sealed))


def test_seal_and_open_round_trip(vault):
    dek = vault.create_dek()
    sealed = vault.seal(dek, "주민번호 900101-1234568")

    assert b"900101" not in sealed.ciphertext
    assert vault.open(dek, sealed) == "주민번호 900101-1234568"


def test_each_record_gets_a_fresh_nonce(vault):
    dek = vault.create_dek()
    first = vault.seal(dek, "같은 내용")
    second = vault.seal(dek, "같은 내용")

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext, "논스 재사용은 AES-GCM 을 깨뜨린다"


def test_one_tenants_dek_cannot_open_anothers(vault):
    """단일 키였다면 격리 버그 하나가 전체 유출이다."""
    acme, globex = vault.create_dek(), vault.create_dek()
    sealed = vault.seal(acme, "acme 의 원문")

    with pytest.raises(CryptoError):
        vault.open(globex, sealed)


def test_destroying_the_dek_makes_ciphertext_unreadable(vault):
    """crypto-shredding — 백업에 남아 있어도 못 연다."""
    dek = vault.create_dek()
    sealed = vault.seal(dek, "파기될 원문")

    with pytest.raises(KeyDestroyed):
        vault.open(None, sealed)   # DEK 를 지운 뒤


def test_wrong_master_key_cannot_unwrap(vault):
    dek = vault.create_dek()
    other = KeyVault(base64.b64decode(generate_master_key()))

    with pytest.raises(CryptoError):
        other.open(dek, Sealed(b"0" * 12, b"x" * 32))


def test_tampered_ciphertext_is_rejected(vault):
    """AES-GCM 의 인증 태그가 변조를 잡는다."""
    dek = vault.create_dek()
    sealed = vault.seal(dek, "원문")
    tampered = Sealed(sealed.nonce, sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1]))

    with pytest.raises(CryptoError):
        vault.open(dek, tampered)


def test_master_key_must_be_correct_length():
    with pytest.raises(CryptoError):
        KeyVault(b"too-short")


def test_load_master_key_validates_encoding(monkeypatch):
    monkeypatch.setenv("LCC_PROMPT_KEY", "not-base64!!")
    with pytest.raises(CryptoError):
        load_master_key()


def test_load_master_key_absent_is_none(monkeypatch):
    monkeypatch.delenv("LCC_PROMPT_KEY", raising=False)
    assert load_master_key() is None


def test_generated_key_round_trips_through_env(monkeypatch):
    monkeypatch.setenv("LCC_PROMPT_KEY", generate_master_key())
    vault = KeyVault.from_env()

    assert vault.enabled is True
    dek = vault.create_dek()
    assert vault.open(dek, vault.seal(dek, "왕복")) == "왕복"


async def test_system_prompt_does_not_bypass_the_filter():
    """필드마다 따로 훑지 않으면 한쪽이 필터를 통째로 우회한다.

    소비자가 system 에 개인정보를 넣으면 마스킹 없이 나가던 결함의 회귀 테스트다.
    """
    guard = Guard(make_config((RRN_RULE,)))
    result = await guard.inspect(
        "평범한 질문", system="담당자 주민번호 900101-1234568", locales=["ko_KR"]
    )

    assert "900101" not in result.system_for(INTERNAL)
    assert "[주민등록번호]" in result.system_for(INTERNAL)
    assert result.detections[0].match_count == 1


async def test_both_fields_are_masked_independently():
    guard = Guard(make_config((RRN_RULE,)))
    result = await guard.inspect(
        "본문 900101-1234568", system="시스템 900101-1234568", locales=["ko_KR"]
    )

    assert "900101" not in result.storable_prompt
    assert "900101" not in result.system_for(INTERNAL)
    detection = result.detections[0]
    assert len(detection.spans) == 1 and len(detection.system_spans) == 1


# ── 겹치는 탐지 (H1) ─────────────────────────────────────────────────────────
#
# 역순 치환이 오프셋을 지킨다는 것은 **스팬이 안 겹칠 때만** 참이다.
# 겹치면 안쪽을 먼저 치환한 뒤 바깥 스팬이 이미 바뀐 텍스트를 가리킨다.


def _det(rule_id, action, spans, label, keep_tail=0):
    return Detection(
        rule_id=rule_id, stage="pattern", actions={INTERNAL: action, EXTERNAL: action},
        spans=tuple(spans), label=label, keep_tail=keep_tail,
    )


def test_overlapping_spans_do_not_leak_the_covered_text():
    """겹친 두 규칙 중 하나가 감춘 구간이 다른 쪽 치환으로 되살아나면 안 된다."""
    text = "AAAA 1234-5678-9012-3456 BBBB"
    out = _apply(
        text,
        (_det("card", "full", [(5, 24)], "[CARD]"),
         _det("inner", "full", [(10, 19)], "[INNER]")),
        INTERNAL,
    )
    assert out == "AAAA [CARD] BBBB"
    assert "3456" not in out
    assert "1234" not in out


def test_the_stronger_grade_wins_on_the_same_span():
    """**같은 스팬에 full 과 partial 이 걸리면 full 이 이겨야 한다.**

    역순 치환에서는 full 이 통째로 사라지고 partial 이 남긴 뒷자리가 노출됐다 —
    카드 뒷 4자리가 실제로 그렇게 살아남았다. 약한 쪽을 고르면 규칙을 켠 의미가 없다.
    """
    text = "AAAA 1234-5678-9012-3456 BBBB"
    out = _apply(
        text,
        (_det("weak", "partial", [(5, 24)], "[P]", keep_tail=4),
         _det("strong", "full", [(5, 24)], "[FULL]")),
        INTERNAL,
    )
    assert out == "AAAA [FULL] BBBB"
    assert "3456" not in out


def test_partial_keep_tail_does_not_survive_a_stronger_overlap():
    """full 이 이겼는데 partial 의 keep_tail 이 남으면 뒷자리가 샌다."""
    out = _apply(
        "카드 4111111111111111 끝",
        (_det("a", "partial", [(3, 19)], "[P]", keep_tail=6),
         _det("b", "full", [(3, 19)], "[F]")),
        INTERNAL,
    )
    assert out == "카드 [F] 끝"


def test_partial_still_keeps_its_tail_when_nothing_overlaps():
    """겹침 처리가 정상 partial 동작을 망가뜨리지 않는다."""
    out = _apply("폰 010-1234-5678 끝", (_det("p", "partial", [(2, 15)], "[P]", 4),), INTERNAL)
    assert out == "폰 [P]5678 끝"


def test_adjacent_but_not_overlapping_spans_are_kept_separate():
    """맞닿기만 한 스팬은 합치지 않는다 — 합치면 라벨 하나로 뭉개진다."""
    out = _apply("AAABBB", (_det("a", "full", [(0, 3)], "[A]"),
                            _det("b", "full", [(3, 6)], "[B]")), INTERNAL)
    assert out == "[A][B]"


def test_three_way_overlap_collapses_to_one_replacement():
    out = _apply(
        "0123456789",
        (_det("a", "full", [(1, 5)], "[A]"),
         _det("b", "full", [(3, 8)], "[B]"),
         _det("c", "full", [(6, 9)], "[C]")),
        INTERNAL,
    )
    assert out.count("[") == 1
    assert out == "0[A]9"


async def test_two_real_rules_overlapping_in_one_prompt():
    """합성 스팬이 아니라 실제 규칙 두 개가 겹치는 경우."""
    wide = GuardRule(id="wide", kind="pattern", action="full",
                     pattern=r"고객 \d{6}-\d{7} 님", label="[고객]")
    guard = Guard(make_config((RRN_RULE, wide)))
    result = await guard.inspect("고객 900101-1234568 님 안녕", locales=["ko_KR"])

    assert "900101" not in result.storable_prompt
    assert "1234568" not in result.storable_prompt


# ── 정규식 캐시 (H2) ─────────────────────────────────────────────────────────


async def test_editing_a_rule_pattern_takes_effect_immediately():
    """**관리자가 고쳤다고 믿는 규칙이 옛 규칙으로 돌면 안 된다.**

    캐시 키가 rule.id 이던 시절에는 재기동 전까지 옛 패턴이 적용됐다.
    """
    guard = Guard(make_config(()))
    before = GuardRule(id="mine", kind="pattern", action="full",
                       pattern=r"AAA\d+", label="[A]")
    after = GuardRule(id="mine", kind="pattern", action="full",
                      pattern=r"BBB\d+", label="[B]")

    first = await guard.inspect("AAA123 BBB456", tenant_rules=(before,))
    assert first.storable_prompt == "[A] BBB456"

    second = await guard.inspect("AAA123 BBB456", tenant_rules=(after,))
    assert second.storable_prompt == "AAA123 [B]", "패턴 수정이 반영되지 않았다"


async def test_two_tenants_can_use_the_same_rule_id_with_different_patterns():
    """같은 id 를 다른 패턴으로 쓰는 두 테넌트가 서로 간섭하면 안 된다."""
    guard = Guard(make_config(()))
    acme = GuardRule(id="secret", kind="pattern", action="full",
                     pattern=r"ACME-\d+", label="[ACME]")
    globex = GuardRule(id="secret", kind="pattern", action="full",
                       pattern=r"GLBX-\d+", label="[GLBX]")

    a = await guard.inspect("ACME-1 GLBX-2", tenant_rules=(acme,))
    b = await guard.inspect("ACME-1 GLBX-2", tenant_rules=(globex,))

    assert a.storable_prompt == "[ACME] GLBX-2"
    assert b.storable_prompt == "ACME-1 [GLBX]", "다른 테넌트의 패턴이 적용됐다"


def test_the_compile_cache_is_bounded():
    """상한 없는 캐시는 멀티테넌트에서 그대로 메모리 누수다."""
    from app.guard import MAX_COMPILED_PATTERNS

    guard = Guard(make_config(()))
    for n in range(MAX_COMPILED_PATTERNS + 20):
        guard._compile(rf"unique-pattern-{n}-\d+")
    assert len(guard._compiled) <= MAX_COMPILED_PATTERNS
