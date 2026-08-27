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
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .config import EXTERNAL, INTERNAL, Config, GuardRule, GuardSettings
from .i18n import ApiError

#: 등급 강도. 테넌트는 이 순서에서 **올릴 수만** 있다.
ACTION_STRENGTH = {"off": 0, "audit": 1, "partial": 2, "full": 3, "block": 4}

#: 컴파일 캐시 상한. 넘으면 통째로 비운다 — LRU 를 만들 만큼 값비싼 연산이 아니고,
#: 상한 없는 캐시는 멀티테넌트에서 그대로 메모리 누수다.
MAX_COMPILED_PATTERNS = 512

STAGE_PATTERN = "pattern"

#: 체크섬을 통과하지 못한 매치의 규칙 id 접미사. 확신도가 다른 탐지를 같은 id 로
#: 뭉치면 승격 게이트의 오탐률 표본이 섞이고 관제 UI 가 둘을 구분하지 못한다.
UNVERIFIED_SUFFIX = ":unverified"

#: 검사 전에 지우는 문자. **보이지 않으면서 패턴만 깨뜨린다** — 주민번호 사이에
#: zero-width space 하나를 넣으면 사람 눈에는 그대로인데 정규식은 안 걸린다.
INVISIBLE_CHARS = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")


def normalize_for_match(text: str) -> tuple[str, list[int] | None]:
    """검사용으로 정규화한 텍스트와 **원문 인덱스 지도**.

    전각 하이픈(`－`)·NBSP·전각 숫자로 쓴 주민번호는 눈에는 같은데 패턴에 안
    걸린다. 실측으로 전각 하이픈과 NBSP 가 실제로 규칙을 빠져나갔다.

    그런데 **정규화한 텍스트에 마스킹할 수는 없다.** 탐지 위치(오프셋)는 원문에
    적용돼야 하고, NFKC 는 길이를 바꾼다(`㈜` → `(주)`). 그래서 문자 단위로
    정규화하면서 각 결과 문자가 원문 어디서 왔는지를 함께 들고 온다.

    반환의 지도가 `None` 이면 원문과 같다는 뜻이다 — 대부분의 프롬프트가 여기
    해당하고, 그때는 문자 단위 순회 비용을 아예 안 낸다.
    """
    if not text:
        return text, None
    # 빠른 길: 통째로 정규화해 보고 그대로면 지도가 필요 없다(C 구현, 한 번).
    if not INVISIBLE_CHARS & set(text) and unicodedata.normalize("NFKC", text) == text:
        return text, None

    folded: list[str] = []
    index: list[int] = []
    for position, char in enumerate(text):
        if char in INVISIBLE_CHARS:
            continue
        for produced in unicodedata.normalize("NFKC", char):
            folded.append(produced)
            index.append(position)
    return "".join(folded), index

STAGE_LLM = "llm"

#: 출력(응답)에서 걸린 1단 히트. **입력과 뭉치지 않는다.**
#:
#: 두 사건은 관리자에게 전혀 다른 뜻이다. 입력 히트는 소비자가 보낸 것이고,
#: 출력 히트는 **모델이 만들어 낸 것**이다. 후자가 늘면 고칠 곳은 규칙이 아니라
#: 프롬프트다. 한 통계로 뭉치면 그 구분이 사라져 아무도 원인을 못 찾는다.
STAGE_OUTPUT = "output"


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


#: 자격증명 자리에 흔히 들어가는 자리표시자. 이것들이 걸리면 규칙이 문서·템플릿에서
#: 쏟아지고, 오탐이 쏟아지면 관리자가 규칙을 꺼버린다.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\.{3,}|todo|tbd|none|null|nil|true|false|placeholder"
    r"|changeme[\w\-]*|your[\w\-]*|<[^>]*>|\$\{?\w+\}?|example[\w\-]*|test[\w\-]*"
    r"|dummy[\w\-]*|redacted|secret|password|[a-z_]*here)$"
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
#: `boto3.Session` 처럼 점으로 이은 식별자. 코드지 자격증명이 아니다.
_DOTTED_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
#: 이 꼬리가 붙은 이름은 **값이 자격증명이 아니다.** `password_hash` 는 해시고,
#: `api_key_id` 는 식별자다 — 이것들까지 마스킹하면 정상 설정 문서가 망가진다.
_NOT_CREDENTIAL_SUFFIX = re.compile(
    r"(?i)_(?:hash|hashed|digest|algo|algorithm|policy|name|file|path|env|var"
    r"|type|format|len|length|count|budget|limit|expiry|ttl|id)$"
)


def credential_shape(value: str) -> bool:
    """대입 문맥에서 잡힌 값이 **진짜 자격증명처럼 생겼는가.**

    ### 왜 엔트로피가 아닌가

    감사서는 "고엔트로피 문자열" 탐지를 제안했다. 실측해 보면 **섀넌 엔트로피는 이
    둘을 가르지 못한다**:

        token_hex(20) 시크릿   3.635 bits/char
        UUID                   3.663
        base64 로 인코딩한 영문  4.930   ← 진짜 base64 시크릿(4.539)보다 높다

    임계를 어디에 두든 진짜 키를 놓치거나 모든 git SHA 를 잡는다. 그래서 엔트로피를
    쓰지 않고 **문맥**(`api_key = …`)을 신호로 삼고, 이 함수는 그 문맥에서 걸린 값이
    자리표시자인지만 걸러낸다.

    ### 해시 길이로 거르지 않는다

    40자리 hex 를 해시로 보고 버리면 `token_hex(20)` 으로 만든 진짜 토큰이 함께
    빠진다. 여기까지 온 값은 이미 `api_key=` 같은 문맥을 지났으므로, 모양보다
    **문맥이 더 믿을 만한 증거다.**
    """
    # **검증기는 매치 전체를 받는다**(`_match_spans` 는 `group(0)` 을 넘긴다).
    # `api_key = "sk_live_…"` 가 통째로 들어오므로 이름과 값을 여기서 가른다 —
    # 안 가르면 키워드만으로 아래 조건이 전부 만족돼 검증기가 늘 참이 된다.
    #
    # 첫 구분자에서만 자른다. base64 값 끝의 `=` 패딩에서 다시 자르면 안 된다.
    parts = re.split(r"[:=]", value, maxsplit=1)
    if len(parts) < 2:
        return False
    name, raw = parts[0], parts[1]
    if _NOT_CREDENTIAL_SUFFIX.search(name.strip().strip("\"'")):
        return False

    text = raw.strip().strip("\"'")
    if len(text) < 12 or _PLACEHOLDER.match(text) or _UUID.match(text):
        return False
    if _DOTTED_IDENT.match(text):
        return False
    if len(set(text)) < 6:
        # `xxxxxxxxxxxx` · `000000000000` 처럼 사실상 한 글자짜리.
        return False
    # 진짜 자격증명은 거의 항상 숫자나 기호를 포함한다. 영문자만이면 낱말이다 —
    # `secretary = "JohnSmithington"` 이 걸리는 것을 막는 마지막 칸이다.
    return any(char.isdigit() or char in "+/=_-&!@#$%^*." for char in text)


CHECKSUMS: Mapping[str, Callable[[str], bool]] = {
    "luhn": luhn,
    "kr_rrn": kr_rrn,
    "kr_biz": kr_biz,
    "jp_mynumber": jp_mynumber,
    "iban_mod97": iban_mod97,
    "credential_shape": credential_shape,
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


#: 출력 축이 쓰는 경계. **응답은 컨트롤 플레인을 떠나 소비자에게 간다** — 소비자는
#: 설치처의 애플리케이션이고, 경계 기준으로는 밖이다.
#:
#: 규칙을 `{internal: audit, external: full}` 로 둔 관리자의 뜻은 "안에서는 보되
#: 밖으로는 가려라" 이다. 응답에 `internal` 등급을 적용하면 그 값이 가려지지 않은
#: 채 소비자에게 나가고, 관리자의 뜻과 정반대가 된다.
OUTPUT_BOUNDARY = EXTERNAL


@dataclass(frozen=True)
class OutputResult:
    """응답 검사 결과. 입력과 달리 **한 벌만** 만든다.

    입력은 어느 노드로 가느냐에 따라 경계가 갈려서 두 벌이 필요했다. 응답은 갈 곳이
    소비자 하나뿐이라 갈릴 것이 없다 — 저장된 것이 곧 소비자가 받은 것이고, 그 등식이
    "소비자가 실제로 무엇을 봤는가" 를 디버깅할 때 가장 쓸모 있는 불변식이다.
    """

    masked: str
    detections: tuple[Detection, ...] = ()

    @property
    def redacted(self) -> bool:
        """실제로 무언가를 가렸는가. `audit` 만 걸린 경우는 False 다."""
        return any(
            d.actions.get(OUTPUT_BOUNDARY) not in (None, "off", "audit")
            for d in self.detections
        )


def _output_action(action: str) -> str:
    """출력에서의 등급. **`block` 은 `full` 로 강등한다.**

    입력의 `block` 은 "이 요청을 아예 처리하지 않는다" 이고 아무것도 낭비되지 않는다.
    출력의 `block` 은 이미 추론이 끝난 뒤다 — 응답을 통째로 버리면 소비자는 비용만
    내고 아무것도 못 받는다. `full` 이면 위반 값은 나가지 않고 나머지는 쓸 수 있으며,
    원문은 봉인돼 있어 관리자가 감사 남기고 열어 볼 수 있다.

    유예 모드의 `block → full` 강등과 같은 발상이다. 1단 패턴만으로는 "이 응답이
    통째로 위험하다" 를 판정할 수 없다는 점도 근거다 — 그것은 2단의 영역이고,
    출력 2단은 지연·비용을 재고 나서 붙인다.

    설치처가 "출력에 주민번호가 있으면 아예 안 준다" 를 요구하면 여기에 설정 축을
    하나 더 두면 된다. 적용 지점이 이 함수 하나라 그때의 변경 범위가 작다.
    """
    return "full" if action == "block" else action


def rules_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[GuardRule, ...]:
    """테넌트가 추가한 규칙 행 → `GuardRule`.

    **읽는 곳이 둘이면 해석도 둘이 된다.** 입력은 파이프라인이, 출력은 스케줄러가
    같은 테이블을 읽는데 조립을 각자 하면 한쪽이 컬럼 하나를 빠뜨린다 — 그러면 그
    테넌트의 규칙이 입력에서는 강하고 출력에서는 약해지고, 그 비대칭은 아무 데도
    안 드러난다. `roles.py` 가 역할 해석에 대해 하는 일과 같은 발상이다.

    **완화는 여기서 막지 않는다** — `rules_for()` 가 베이스라인과 병합하며 강한
    쪽을 채택한다. 판정이 두 곳에 있으면 언젠가 갈린다.
    """
    return tuple(
        GuardRule(
            id=raw["id"],
            kind=raw["kind"],
            action=raw["action"],
            label=raw["label"],
            pattern=raw["pattern"],
            checksum=raw["checksum"],
            keep_tail=raw["keep_tail"],
            description=raw["description"],
            locale_pack=raw["locale_pack"],
        )
        for raw in rows
    )


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
        # **캐시 키는 패턴 문자열이지 규칙 id 가 아니다.**
        #
        # id 로 캐싱하면 두 가지가 조용히 깨진다. ① 테넌트가 패턴을 고쳐도 재기동
        # 전까지 옛 패턴이 돈다 — 관리자는 고쳤다고 믿는데 아니다. ② 테넌트 A 와 B
        # 가 같은 id 를 다른 패턴으로 등록하면 먼저 컴파일된 쪽이 둘 다에 적용된다.
        # 둘 다 "필터가 켜져 있는데 안 잡는" 상태이고, 그건 안 켜진 필터와 같다.
        self._compiled: dict[str, re.Pattern[str]] = {}
        for rule in config.guard_rules:
            if rule.kind == "pattern" and rule.pattern:
                self._compile(rule.pattern)

    def _compile(self, pattern: str) -> re.Pattern[str]:
        """패턴을 컴파일해 캐시한다. **키가 패턴이라 수정이 즉시 반영된다.**

        캐시가 무한히 자라지 않도록 상한을 둔다. 테넌트마다 규칙을 몇 개씩 넣는
        멀티테넌트에서는 상한 없는 캐시가 그대로 메모리 누수다.
        """
        compiled = self._compiled.get(pattern)
        if compiled is None:
            if len(self._compiled) >= MAX_COMPILED_PATTERNS:
                self._compiled.clear()
            compiled = self._compiled[pattern] = re.compile(pattern)
        return compiled

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
            if rule.kind == "pattern" and rule.pattern:
                self._compile(rule.pattern)

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

    async def inspect_output(
        self,
        text: str,
        *,
        locales: Iterable[str] = (),
        tenant_rules: Sequence[GuardRule] = (),
    ) -> OutputResult:
        """**응답을 검사한다.** 입력 1단과 같은 규칙, 같은 스레드 풀 오프로드.

        입력만 거르고 출력을 안 거르면 제품의 한 문장이 절반만 참이다. 응답에
        민감정보가 실리는 경로는 가정이 아니다 — 요약·추출 작업의 산출물 자체가
        개인정보이거나, 모델이 마스킹되지 않은 문맥을 재구성하거나, 인젝션이
        시스템 프롬프트를 응답으로 끌어낸다.

        **1단만 돈다.** 2단(LLM 분류)을 출력에 걸면 응답마다 추론이 한 번 더
        늘어난다 — 지연과 비용을 실측한 뒤에 붙일 일이지, 켜 놓고 나중에 재는
        것은 순서가 거꾸로다. `_run_stage1` 이 패턴 규칙만 고르므로 맥락 규칙은
        여기서 자동으로 빠진다.
        """
        if not text:
            return OutputResult(masked=text or "")

        rules = self.rules_for(locales, tenant_rules)
        hits = await self._run_stage1(text, None, rules)

        # 등급을 출력용으로 다시 매긴다. `_apply` 는 `block` 을 "애초에 전송되지
        # 않는다" 로 보고 건너뛰므로, 강등을 여기서 안 하면 **차단 규칙에 걸린 값이
        # 마스킹도 안 된 채 그대로 나간다.** 가장 강한 등급이 가장 약하게 동작하는
        # 뒤집힘이라, 이 한 줄이 빠지면 출력 축 전체가 거꾸로 선다.
        detections = tuple(
            replace(
                hit,
                actions={
                    OUTPUT_BOUNDARY: _output_action(
                        hit.actions.get(OUTPUT_BOUNDARY, "audit")
                    )
                },
            )
            for hit in hits
        )
        return OutputResult(
            masked=_apply(text, detections, OUTPUT_BOUNDARY),
            detections=detections,
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
            if rule.kind != "pattern" or not rule.pattern:
                continue
            compiled = self._compile(rule.pattern)

            spans, unverified = self._match_spans(compiled, rule, prompt)
            system_spans, system_unverified = self._match_spans(
                compiled, rule, system or ""
            )

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

            # 체크섬이 틀린 매치는 **다른 규칙 id 로** 낸다. 같은 id 로 뭉치면
            # 승격 게이트가 "이 규칙의 오탐률" 을 재는 표본에 확신도가 다른 둘이
            # 섞이고, 관제 UI 도 둘을 구분하지 못한다.
            failed_action = rule.checksum_failed_action
            if failed_action and failed_action != "off" and (unverified or system_unverified):
                detections.append(
                    Detection(
                        rule_id=f"{rule.id}{UNVERIFIED_SUFFIX}", stage=STAGE_PATTERN,
                        actions={INTERNAL: failed_action, EXTERNAL: failed_action},
                        spans=tuple(unverified),
                        system_spans=tuple(system_unverified),
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
        verified, _ = self._match_spans(self._compile(rule.pattern), rule, text)
        return len(verified)

    @staticmethod
    def _match_spans(
        compiled: re.Pattern[str], rule: GuardRule, text: str
    ) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """(체크섬을 통과한 스팬, 통과하지 못한 스팬).

        둘을 나누는 이유: 체크섬 불일치를 **버리는 것**이 기본이지만 늘 맞지는
        않다. 버리면 숫자 나열이 전부 PII 가 되는 오탐 홍수를 막는 대신,
        체크섬 자체가 성립하지 않게 된 식별자 체계(2020-10 이후 주민등록번호)를
        통째로 놓친다. 나눠서 돌려주면 규칙이 그 둘에 다른 등급을 줄 수 있다.
        """
        if not text:
            return [], []

        # **정규화한 사본에서 찾고, 위치는 원문으로 되돌린다.**
        # 정규화본에 마스킹하면 소비자가 보낸 것과 다른 텍스트가 저장·전송된다.
        haystack, index = normalize_for_match(text)

        def to_source(span: tuple[int, int]) -> tuple[int, int]:
            if index is None:
                return span
            start, end = span
            if start >= len(index):
                return span
            return index[start], index[min(end, len(index)) - 1] + 1

        validator = CHECKSUMS.get(rule.checksum) if rule.checksum else None
        if validator is None:
            return [to_source(m.span()) for m in compiled.finditer(haystack)], []

        verified: list[tuple[int, int]] = []
        unverified: list[tuple[int, int]] = []
        for match in compiled.finditer(haystack):
            target = verified if validator(match.group(0)) else unverified
            target.append(to_source(match.span()))
        return verified, unverified

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

    # (시작, 끝, 등급, 라벨, keep_tail) — 겹침을 풀기 전이라 아직 치환하지 않는다.
    hits: list[tuple[int, int, str, str, int]] = []
    for detection in detections:
        action = detection.actions.get(boundary, "audit")
        if action in ("off", "audit", "block"):
            # audit 은 통과시키되 기록만 한다. block 은 애초에 전송되지 않는다.
            continue
        for start, end in getattr(detection, field):
            hits.append(
                (start, end, action, detection.label, detection.keep_tail)
            )

    for start, end, action, label, keep_tail in reversed(_coalesce(hits)):
        original = text[start:end]
        if action == "partial" and keep_tail:
            masked = f"{label}{original[-keep_tail:]}"
        else:
            masked = label
        text = text[:start] + masked + text[end:]
    return text


def _coalesce(
    hits: Sequence[tuple[int, int, str, str, int]],
) -> list[tuple[int, int, str, str, int]]:
    """겹치는 탐지를 하나로 합친다. **합치지 않으면 마스킹이 깨진다.**

    치환을 역순으로 하면 오프셋이 안 밀린다는 것은 스팬이 서로 안 겹칠 때만
    참이다. 겹치면 안쪽을 먼저 치환한 뒤 바깥 스팬이 **이미 바뀐 텍스트**를
    가리켜, 엉뚱한 구간이 잘리거나 개인정보 일부가 그대로 남는다.

    같은 구간에 `full` 과 `partial` 이 함께 걸리는 경우가 특히 나쁘다 —
    역순 치환에서 `full` 이 통째로 사라지고 `partial` 이 남긴 뒷자리가 노출된다.
    실제로 카드번호 뒷 4자리가 그렇게 살아남았다.

    합칠 때는 **더 강한 등급을 채택한다.** 겹친 두 규칙 중 하나가 전체 치환을
    요구했다면 그 요구가 이겨야 한다 — 약한 쪽을 고르면 규칙을 켠 의미가 없다.
    """
    if not hits:
        return []

    merged: list[list] = []
    for start, end, action, label, keep_tail in sorted(hits):
        if merged and start < merged[-1][1]:
            group = merged[-1]
            group[1] = max(group[1], end)
            if ACTION_STRENGTH.get(action, 0) > ACTION_STRENGTH.get(group[2], 0):
                # 더 강한 등급이 이긴다. 라벨과 keep_tail 도 그 등급을 따라간다 —
                # `full` 이 이겼는데 `partial` 의 keep_tail 이 남으면 뒷자리가 샌다.
                group[2], group[3], group[4] = action, label, keep_tail
        else:
            merged.append([start, end, action, label, keep_tail])

    return [tuple(group) for group in merged]


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
