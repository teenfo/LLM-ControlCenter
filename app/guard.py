"""가드 — 프롬프트에서 개인정보·민감정보를 걸러내는 2단 관문.

    1단  결정론적 패턴   PII 정규식 + 체크섬 검증 (µs, 비용 0, 감사 가능)
    2단  로컬 LLM 분류   관리자가 문장으로 정의한 "맥락"

**2단은 내부 경계 노드 전용으로 강제한다.** "이거 민감해?" 를 물으려고 원문을 경계 밖으로
보내면 필터가 스스로 유출 경로가 된다. 강제는 역할의 `internal_only` 가 하고, 이 모듈은
분류기가 그 역할을 타도록 위임만 한다.

### 경계를 모르는 채로 마스킹해야 하는 문제

필터는 **배치 전에** 돈다(순서가 계약이다). 그런데 티어별 등급
(`{internal: audit, external: block}`)은 어느 경계로 갈지 알아야 정해진다.

가장 엄격한 등급을 일괄 적용하면 안에서만 돌 수 있었던 잡까지 막히고,
가장 느슨한 등급을 적용하면 밖으로 새어나간다. 그래서 **경계별로 각각 마스킹하고,
차단 등급에 걸린 경계를 잡의 허용 경계에서 빼는** 방식을 쓴다.

허용 경계가 비면 그때 차단이다. 이 축소는 `cluster.effective_placement` 의
"안전은 교집합" 과 같은 메커니즘으로 배치에 반영된다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .config import EXTERNAL, INTERNAL, Config, GuardRule, GuardSettings
from .i18n import ApiError

#: 등급 강도. 테넌트는 이 순서에서 **올릴 수만** 있다.
ACTION_STRENGTH = {"off": 0, "audit": 1, "partial": 2, "full": 3, "block": 4}

STAGE_PATTERN = "pattern"
STAGE_LLM = "llm"


# ── 체크섬 검증기 ────────────────────────────────────────────────────────────
#
# 체크섬이 없으면 숫자 나열이 전부 PII 가 되고, 오탐이 쏟아지면 관리자가 규칙을
# 꺼버린다 — 안 켜진 필터는 없는 필터다.


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def luhn(value: str) -> bool:
    """카드번호. 무작위 16자리가 통과할 확률은 1/10 이다."""
    digits = _digits(value)
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def kr_rrn(value: str) -> bool:
    """주민등록번호 13자리."""
    digits = _digits(value)
    if len(digits) != 13:
        return False
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    total = sum(int(d) * w for d, w in zip(digits, weights))
    return (11 - total % 11) % 10 == int(digits[12])


def kr_biz(value: str) -> bool:
    """사업자등록번호 10자리."""
    digits = _digits(value)
    if len(digits) != 10:
        return False
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    total = sum(int(d) * w for d, w in zip(digits[:9], weights))
    total += (int(digits[8]) * 5) // 10
    return (10 - total % 10) % 10 == int(digits[9])


def jp_mynumber(value: str) -> bool:
    """マイナンバー 12자리."""
    digits = _digits(value)
    if len(digits) != 12:
        return False
    body = [int(d) for d in digits[:11]][::-1]
    total = sum(d * (n + 2 if n < 6 else n - 4) for n, d in enumerate(body))
    remainder = total % 11
    return int(digits[11]) == (0 if remainder <= 1 else 11 - remainder)


def iban_mod97(value: str) -> bool:
    compact = "".join(c for c in value.upper() if c.isalnum())
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        numeric = "".join(
            str(int(c, 36)) if c.isalpha() else c for c in rearranged
        )
        return int(numeric) % 97 == 1
    except ValueError:
        return False


CHECKSUMS: Mapping[str, Callable[[str], bool]] = {
    "luhn": luhn,
    "kr_rrn": kr_rrn,
    "kr_biz": kr_biz,
    "jp_mynumber": jp_mynumber,
    "iban_mod97": iban_mod97,
}


# ── 결과 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Detection:
    rule_id: str
    stage: str
    #: 경계별로 적용된 등급.
    actions: Mapping[str, str]
    #: 프롬프트 본문에서 걸린 위치.
    spans: tuple[tuple[int, int], ...] = ()
    #: system 프롬프트에서 걸린 위치. **필드마다 따로 잡지 않으면 한쪽이 우회된다.**
    system_spans: tuple[tuple[int, int], ...] = ()
    label: str = ""
    #: `partial` 등급이 남길 뒷자리 수. 규칙에서 그대로 가져온다.
    keep_tail: int = 0

    @property
    def match_count(self) -> int:
        return (len(self.spans) + len(self.system_spans)) or 1


@dataclass(frozen=True)
class GuardResult:
    """가드 판정.

    `allowed_boundaries` 가 이 결과의 핵심이다 — 차단 등급에 걸린 경계가 빠진 집합이며,
    비어 있으면 어디에도 못 간다.
    """

    allowed_boundaries: frozenset[str]
    #: 경계별 마스킹된 프롬프트. 저장에는 가장 느슨한(내부) 것을 쓰고,
    #: 실제 전송에는 그 노드의 경계에 맞는 것을 쓴다.
    prompts: Mapping[str, str]
    systems: Mapping[str, str | None]
    detections: tuple[Detection, ...] = ()
    blocked_rules: tuple[str, ...] = ()
    #: 2단 분류를 **시도했는가.** 실패율의 분모다 — 시도하지 않은 요청까지 세면
    #: 실패율이 희석돼 경보가 안 울린다.
    classifier_attempted: bool = False
    classifier_failed: bool = False

    @property
    def blocked(self) -> bool:
        return not self.allowed_boundaries

    def prompt_for(self, boundary: str) -> str:
        return self.prompts.get(boundary, self.prompts.get(INTERNAL, ""))

    def system_for(self, boundary: str) -> str | None:
        return self.systems.get(boundary, self.systems.get(INTERNAL))

    @property
    def storable_prompt(self) -> str:
        """DB 에 남길 마스킹본. 가장 느슨한 경계 기준이되 마스킹은 이미 적용돼 있다."""
        for boundary in (INTERNAL, EXTERNAL):
            if boundary in self.prompts:
                return self.prompts[boundary]
        return ""


#: 2단 분류기. 프롬프트와 맥락 규칙을 받아 매칭된 규칙 id 집합을 돌려준다.
#: 반드시 내부 경계 노드에서 실행돼야 하며, 그 강제는 역할 설정이 한다.
Classifier = Callable[[str, Sequence[GuardRule]], Awaitable[set[str]]]


# ── 가드 ────────────────────────────────────────────────────────────────────


class Guard:
    def __init__(
        self,
        config: Config,
        *,
        classifier: Classifier | None = None,
        settings: GuardSettings | None = None,
        grace_mode: bool = False,
    ) -> None:
        self._config = config
        self._settings = settings or config.guard_settings
        self._classifier = classifier
        # 도입 첫날 유예. `block` 을 `audit` 로 낮춘다 — 처음부터 차단으로 켜면
        # 도입 첫날 프로덕션이 서고, 그러면 설치처는 규칙을 통째로 꺼버린다.
        # **켜져 있다는 사실을 화면과 API 가 계속 알려야 한다**(조용한 유예가 더 나쁘다).
        self._grace_mode = grace_mode
        self._compiled: dict[str, re.Pattern[str]] = {
            rule.id: re.compile(rule.pattern)
            for rule in config.guard_rules
            if rule.kind == "pattern" and rule.pattern
        }

    @property
    def grace_mode(self) -> bool:
        """유예 모드인가. 관제 UI 와 `/v1/session` 이 상시 표시해야 하는 값이다."""
        return self._grace_mode

    def set_grace_mode(self, enabled: bool) -> None:
        self._grace_mode = bool(enabled)

    def set_classifier(self, classifier: Classifier | None) -> None:
        """2단 분류기를 나중에 꽂는다.

        배선이 원형이기 때문이다 — 가드는 분류기가 필요하고, 분류기는 클러스터 배치가
        필요하고, 파이프라인은 가드가 필요하다. 생성자에서 다 묶으려면 셋 중 하나를
        반쯤 만든 채로 넘겨야 하고, 그러면 "가드가 분류기 없이 도는 조합" 이 조용히
        정상 경로가 된다. **한 줄로 명시적으로 꽂는 쪽이 낫다.**
        """
        self._classifier = classifier

    @property
    def has_classifier(self) -> bool:
        """2단이 살아 있는가. 관제 UI 가 이것을 표시해야 한다 —
        분류기가 안 꽂힌 설치는 맥락 규칙을 만들어도 아무것도 판정하지 않는다."""
        return self._classifier is not None

    # -- 규칙 해석 -------------------------------------------------------------

    def rules_for(
        self,
        locales: Iterable[str] = (),
        tenant_rules: Sequence[GuardRule] = (),
    ) -> tuple[GuardRule, ...]:
        """베이스라인 + 테넌트 규칙. **테넌트는 조일 수만 있다.**

        플랫폼이 정한 PII 차단을 테넌트가 끌 수 있으면 제품의 보증이 사라진다.
        같은 id 의 규칙이 겹치면 더 강한 등급을 채택한다.
        """
        merged: dict[str, GuardRule] = {
            rule.id: rule for rule in self._config.rules_for_locales(locales)
        }

        for rule in tenant_rules:
            baseline = merged.get(rule.id)
            if baseline is None:
                merged[rule.id] = rule
                continue
            merged[rule.id] = _stronger_of(baseline, rule)

        for rule in merged.values():
            if rule.kind == "pattern" and rule.id not in self._compiled and rule.pattern:
                self._compiled[rule.id] = re.compile(rule.pattern)

        rules = tuple(merged.values())
        return tuple(_downgraded(r) for r in rules) if self._grace_mode else rules

    def validate_rule(self, raw: Mapping[str, Any]) -> None:
        """테넌트가 저장하려는 규칙이 말이 되는가.

        저장 시점에 잡지 않으면 **다음 요청에서 정규식 컴파일 에러로 가드 전체가
        멈춘다** — 필터가 죽으면 프롬프트가 검사 없이 나가거나 서비스가 통째로 선다.
        어느 쪽이든 규칙 하나를 잘못 적은 대가로는 지나치다.
        """
        rule_id = str(raw.get("id") or "")
        if not rule_id:
            raise ApiError("missing_field", status=400, params={"field": "id"})

        kind = str(raw.get("kind") or "pattern")
        if kind not in ("pattern", "llm"):
            raise ApiError("invalid_field", status=400, params={"field": "kind"})

        action = raw.get("action")
        grades = action.values() if isinstance(action, Mapping) else [action]
        for grade in grades:
            if grade not in ACTION_STRENGTH:
                raise ApiError("invalid_field", status=400, params={"field": "action"})
        if isinstance(action, Mapping) and set(action) - {INTERNAL, EXTERNAL}:
            raise ApiError("invalid_field", status=400, params={"field": "action"})

        checksum = raw.get("checksum")
        if checksum and checksum not in CHECKSUMS:
            raise ApiError("invalid_field", status=400, params={"field": "checksum"})

        if kind == "pattern":
            pattern = raw.get("pattern")
            if not pattern:
                raise ApiError("missing_field", status=400, params={"field": "pattern"})
            try:
                re.compile(pattern)
            except re.error:
                raise ApiError("invalid_field", status=400, params={"field": "pattern"})
        elif not raw.get("description"):
            # LLM 규칙은 설명이 곧 판정 기준이다. 비어 있으면 분류기가 물어볼 것이 없다.
            raise ApiError("missing_field", status=400, params={"field": "description"})

    # -- 검사 -----------------------------------------------------------------

    async def inspect(
        self,
        prompt: str,
        *,
        system: str | None = None,
        locales: Iterable[str] = (),
        tenant_rules: Sequence[GuardRule] = (),
        candidate_boundaries: Iterable[str] = (INTERNAL, EXTERNAL),
        allow_classifier: bool = True,
    ) -> GuardResult:
        """프롬프트를 검사하고 경계별 마스킹본과 허용 경계를 돌려준다."""
        rules = self.rules_for(locales, tenant_rules)
        boundaries = frozenset(candidate_boundaries)

        pattern_hits = await self._run_stage1(prompt, system, rules)

        context_rules = [r for r in rules if r.is_llm]
        classifier_failed = False
        llm_hits: set[str] = set()

        classifier_attempted = bool(context_rules) and allow_classifier
        if classifier_attempted:
            try:
                if self._classifier is None:
                    raise RuntimeError("분류기가 없다")
                # 1단 마스킹본을 넘긴다 — 분류기에도 원문을 주지 않는다.
                pre_masked = _apply(prompt, pattern_hits, INTERNAL)
                llm_hits = await self._classifier(pre_masked, context_rules)
            except Exception:
                classifier_failed = True

        detections = list(pattern_hits)
        for rule in context_rules:
            if rule.id in llm_hits:
                detections.append(
                    Detection(
                        rule_id=rule.id, stage=STAGE_LLM,
                        actions={b: rule.action_for_boundary(b) for b in boundaries},
                        label=rule.label,
                    )
                )

        # 분류 실패는 판정이 아니다 — 정책을 타되 그 사건을 별도로 집계한다.
        if classifier_failed:
            boundaries = self._apply_classifier_failure(boundaries)

        allowed = {
            boundary
            for boundary in boundaries
            if not any(d.actions.get(boundary) == "block" for d in detections)
        }
        blocked_rules = tuple(
            sorted(
                {
                    d.rule_id
                    for d in detections
                    if any(a == "block" for a in d.actions.values())
                }
            )
        )

        return GuardResult(
            allowed_boundaries=frozenset(allowed),
            prompts={b: _apply(prompt, detections, b) for b in (INTERNAL, EXTERNAL)},
            systems={
                b: (_apply(system, detections, b, field="system_spans") if system else None)
                for b in (INTERNAL, EXTERNAL)
            },
            detections=tuple(detections),
            blocked_rules=blocked_rules,
            classifier_attempted=classifier_attempted,
            classifier_failed=classifier_failed,
        )

    async def _run_stage1(
        self, prompt: str, system: str | None, rules: Sequence[GuardRule]
    ) -> list[Detection]:
        """1단 패턴. 큰 프롬프트는 **스레드 풀로 넘긴다.**

        200KB × 20패턴 = 40~80ms 를 이벤트 루프 위에서 동기로 돌면 그동안 다른 모든
        요청이 멈춘다.
        """
        haystack = prompt if system is None else f"{prompt}\n{system}"
        pattern_rules = [r for r in rules if r.kind == "pattern"]

        if len(haystack.encode("utf-8")) > self._settings.stage1_threadpool_threshold_bytes:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._scan, prompt, system, pattern_rules
            )
        return self._scan(prompt, system, pattern_rules)

    def _scan(
        self, prompt: str, system: str | None, rules: Sequence[GuardRule]
    ) -> list[Detection]:
        """프롬프트와 system 을 **각각** 훑는다.

        한쪽만 훑으면 그 필드가 필터를 통째로 우회한다 — 소비자가 system 에
        개인정보를 넣으면 마스킹 없이 나간다.
        """
        detections: list[Detection] = []

        for rule in rules:
            compiled = self._compiled.get(rule.id)
            if compiled is None:
                continue

            spans = self._match_spans(compiled, rule, prompt)
            system_spans = self._match_spans(compiled, rule, system or "")

            if spans or system_spans:
                detections.append(
                    Detection(
                        rule_id=rule.id, stage=STAGE_PATTERN,
                        actions={
                            INTERNAL: rule.action_for_boundary(INTERNAL),
                            EXTERNAL: rule.action_for_boundary(EXTERNAL),
                        },
                        spans=tuple(spans),
                        system_spans=tuple(system_spans),
                        label=rule.label or f"[{rule.id}]",
                        keep_tail=rule.keep_tail,
                    )
                )
        return detections

    def probe_rule(self, rule: GuardRule, text: str) -> int:
        """이 규칙이 텍스트에서 몇 번 걸리는가. 정답셋 평가가 쓰는 원시 연산이다.

        `inspect()` 는 등급·경계·계층을 전부 적용하므로 "이 규칙이 이 문장을 잡는가" 만
        묻기에는 과하다. 평가는 규칙 하나를 격리해서 봐야 한다.
        """
        if rule.kind != "pattern" or not rule.pattern:
            return 0
        compiled = self._compiled.get(rule.id)
        if compiled is None:
            compiled = self._compiled[rule.id] = re.compile(rule.pattern)
        return len(self._match_spans(compiled, rule, text))

    @staticmethod
    def _match_spans(
        compiled: re.Pattern[str], rule: GuardRule, text: str
    ) -> list[tuple[int, int]]:
        if not text:
            return []
        validator = CHECKSUMS.get(rule.checksum) if rule.checksum else None
        return [
            match.span()
            for match in compiled.finditer(text)
            # 체크섬 불일치는 오탐이다. 걸러내지 않으면 숫자 나열이 전부 PII 가 된다.
            if validator is None or validator(match.group(0))
        ]

    def _apply_classifier_failure(self, boundaries: frozenset[str]) -> frozenset[str]:
        policy = self._settings.on_classifier_error
        if policy == "block":
            return frozenset()
        if policy == "mask":
            # 판정할 수 없으면 경계 밖으로는 안 내보낸다.
            return boundaries - {EXTERNAL}
        return boundaries   # allow


# ── 마스킹 적용 ──────────────────────────────────────────────────────────────


def _apply(
    text: str | None,
    detections: Sequence[Detection],
    boundary: str,
    *,
    field: str = "spans",
) -> str:
    """탐지 결과를 텍스트에 적용한다.

    뒤에서 앞으로 치환한다 — 앞에서부터 하면 오프셋이 밀려 다음 스팬이 어긋난다.
    """
    if not text:
        return text or ""

    replacements: list[tuple[int, int, str]] = []
    for detection in detections:
        action = detection.actions.get(boundary, "audit")
        if action in ("off", "audit", "block"):
            # audit 은 통과시키되 기록만 한다. block 은 애초에 전송되지 않는다.
            continue
        for start, end in getattr(detection, field):
            original = text[start:end]
            if action == "partial" and detection.keep_tail:
                masked = f"{detection.label}{original[-detection.keep_tail:]}"
            else:
                masked = detection.label
            replacements.append((start, end, masked))

    for start, end, masked in sorted(replacements, key=lambda r: r[0], reverse=True):
        text = text[:start] + masked + text[end:]
    return text


#: 유예 모드에서 `block` 이 내려앉는 등급. **`audit` 이 아니라 `full` 이다.**
#:
#: `audit` 로 내리면 도입 첫날 며칠 동안 주민번호가 **마스킹 없이** 노드로 나간다 —
#: 개인정보를 거르려고 산 제품이 도입 첫 주에 정확히 그것을 안 하는 셈이다.
#: `full` 은 요청을 세우지 않으므로 첫날 장애를 막는 목적은 그대로 달성하면서
#: 개인정보는 계속 가린다.
#:
#: 대가: `audit` 였다면 "이 규칙이 오탐이었을 때 사용자 요청이 멀쩡했는가" 까지
#: 볼 수 있었지만 `full` 에서는 오탐이 프롬프트를 훼손한다. 그래도 **탐지 기록은
#: 동일하게 남으므로 오탐률 측정과 승격 게이트는 그대로 작동한다** — 잃는 것이
#: 얻는 것보다 작다.
GRACE_FALLBACK = "full"


def _downgraded(rule: GuardRule) -> GuardRule:
    """유예 모드에서 `block` 을 `full` 로 낮춘다.

    **탐지는 그대로 한다** — 낮추는 것은 동작뿐이고, 그 기록이 곧 승격 게이트의
    재료다. 마스킹 등급(`partial`·`full`)은 건드리지 않는다.
    """
    def soften(action: str) -> str:
        return GRACE_FALLBACK if action == "block" else action

    if isinstance(rule.action, str):
        if rule.action != "block":
            return rule
        return replace(rule, action=GRACE_FALLBACK)

    lowered = {b: soften(a) for b, a in rule.action.items()}
    if lowered == dict(rule.action):
        return rule
    return replace(rule, action=lowered)


def _stronger_of(baseline: GuardRule, candidate: GuardRule) -> GuardRule:
    """두 규칙 중 더 강한 등급을 경계별로 채택한다."""
    from dataclasses import replace

    merged_action: dict[str, str] = {}
    for boundary in (INTERNAL, EXTERNAL):
        base = baseline.action_for_boundary(boundary)
        cand = candidate.action_for_boundary(boundary)
        merged_action[boundary] = (
            cand if ACTION_STRENGTH.get(cand, 0) > ACTION_STRENGTH.get(base, 0) else base
        )

    if merged_action[INTERNAL] == merged_action[EXTERNAL]:
        return replace(baseline, action=merged_action[INTERNAL])
    return replace(baseline, action=merged_action)
