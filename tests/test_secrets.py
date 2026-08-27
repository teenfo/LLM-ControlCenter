"""시크릿 탐지 — **개인정보와 나란히 새는 것이 자격증명이다.**

개발자가 설정 파일을 통째로 붙여 넣거나, 모델이 문맥에 있던 키를 응답에 되풀이한다.
출력 축이 열린 뒤로 후자가 실재하는 경로가 됐다.

### "고엔트로피 문자열" 은 기각했다

설계 감사서는 고엔트로피 탐지를 제안했다. 실측하면 **섀넌 엔트로피는 진짜 시크릿과
정상 문자열을 가르지 못한다** — 아래 `test_entropy_cannot_separate_secrets` 가 그
측정을 고정한다. 그래서 신호를 엔트로피가 아니라 **접두사와 문맥**에서 얻는다.

### 이 파일의 절반은 오탐 코퍼스다

시크릿 규칙은 설치처의 README·설정 파일·운영 대화에서 걸리기 쉽다. 그리고
**오탐이 쏟아지면 관리자가 규칙을 꺼버린다 — 안 켜진 필터는 없는 필터다.**
`BENIGN` 이 그 회귀 코퍼스이고, 규칙을 넓힐 때마다 여기가 먼저 실패해야 한다.
"""

from __future__ import annotations

import math
import secrets as pysecrets
from collections import Counter

import pytest

from app.config import EXTERNAL, INTERNAL, load_config
from app.guard import Guard, credential_shape
from tests.conftest import seed_tenant

#: 승격 게이트와 **같은 임계**를 쓴다. 문서의 숫자와 테스트의 숫자가 갈리면
#: 둘 중 하나는 거짓말이고, 어느 쪽인지 아무도 모른다.
MAX_FALSE_POSITIVE_RATE = 0.02


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme")


@pytest.fixture(scope="module")
def shipped():
    return load_config("config")


@pytest.fixture(scope="module")
def guard(shipped):
    return Guard(shipped)


def token() -> str:
    return f"lcc_{pysecrets.token_hex(4)}_{pysecrets.token_urlsafe(32)}"


#: 잡혀야 하는 것.
SECRETS = (
    "키는 AKIAIOSFODNN7EXAMPLE 입니다",
    "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 로 클론하세요",
    "sk_live_9999999999999999999999 로 결제했습니다",
    "xoxb-123456789012-abcdefghijkl 로 슬랙에 붙입니다",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...",
    'api_key = "sk_live_51H8xQ2eZvKYlo2C1a9kFvR"',
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "password: Tr0ub4dor&3xKcd9mQ",
    '"client_secret": "GOCSPX-1a2B3c4D5e6F7g8H9i0J"',
    "DB_PASSWORD=p@ssw0rd-Pr0duct10n-2026",
)

#: **걸리면 안 되는 것.** 설치처 문서·설정·대화에서 실제로 나올 법한 줄들이다.
BENIGN = (
    'api_key = "YOUR_API_KEY_HERE"',
    "password = None",
    "secret: TODO",
    'api_key = os.environ["LCC_KEY"]',
    'password = "xxxxxxxxxxxx"',
    "token = ${GITHUB_TOKEN}",
    'secretary = "JohnSmithington"',
    "max_tokens = 4096",
    "api_key 를 발급받으려면 관리 콘솔에서 신청하세요",
    "password 정책은 최소 12자 이상입니다",
    "client_secret 은 환경변수로 관리합니다",
    "API_KEY=<발급받은 키를 여기 넣으세요>",
    "password: ${DB_PASSWORD}",
    'api_key: "" # 비워두면 비활성화됩니다',
    "access_token = null",
    "secret_key = None  # 설정하지 않음",
    "export API_KEY=$MY_API_KEY",
    "PASSWORD=placeholder",
    "credentials = boto3.Session().get_credentials()",
    "auth_token: changeme-before-deploy",
    "api_key 항목이 비어 있으면 기동이 실패합니다.",
    "# password 는 반드시 vault 에서 읽으세요",
    "the access token expires after one hour",
    "client_secret 는 6개월마다 회전합니다",
    'password_hash = "$2b$12$abcdefghijklmnopqrstuv"',
    "api_key_id = 12345678",
    "token_budget = 1000000",
    "SECRET_KEY_BASE 를 설정하지 않으면 세션이 깨집니다",
    "password 재설정 링크를 보내드렸습니다",
    'api_key = "test_key_do_not_use"',
    "AKIA 는 AWS 액세스 키 접두사입니다",
    "리포지토리는 github.com/org/repo 입니다",
    "개인키는 -----BEGIN 으로 시작합니다",
    "3분기 실적을 요약해 주세요.",
)


def secret_hits(verdict) -> set[str]:
    return {d.rule_id for d in verdict.detections if d.rule_id.startswith("secret_")}


# ── 엔트로피는 왜 안 되는가 ─────────────────────────────────────────────────


def test_entropy_cannot_separate_secrets():
    """**감사서가 제안한 "고엔트로피 문자열" 탐지를 기각한 근거다.**

    임계를 어디에 두든 진짜 키를 놓치거나 모든 git SHA 를 잡는다. 이 테스트는
    그 사실을 숫자로 고정한다 — 나중에 누군가 "엔트로피로 하면 되지 않나" 를
    다시 물을 때 재측정 비용을 없애기 위해서다.
    """

    def entropy(text: str) -> float:
        counts = Counter(text)
        total = len(text)
        return -sum((n / total) * math.log2(n / total) for n in counts.values())

    # **값을 못박는다.** 무작위로 만들면 어느 쪽이 높은지가 실행마다 달라져서
    # 테스트가 동전 던지기가 된다 — 겹친다는 주장 자체가 그 흔들림의 원인이므로,
    # 실제로 측정한 표본을 그대로 박아 둔다.
    hex_secret = "e45308448fc13c45ca556af03a88a42dbf6028b3"       # token_hex(20)
    b64_secret = "jNPeyjdZbt4HhamkpGtzhxO8Xvd/xCZh"               # 진짜 시크릿
    a_uuid = "c127ebc8-4c41-4ea9-8d4e-feff19a8b67c"               # 정상
    b64_english = "dGhlIHF1aWNrIGJyb3duIGZveCBqdW1wcyBvdmVyIHRoZSBsYXp5IGRvZw=="

    # 두 분포가 **겹친다.** 진짜 시크릿을 잡는 임계는 UUID 도 잡고,
    # base64 영문을 거르는 임계는 진짜 base64 시크릿도 거른다.
    assert entropy(hex_secret) < entropy(a_uuid), "표본이 바뀌었다 — 재측정하라"
    assert entropy(b64_english) > entropy(b64_secret)

    # 그래서 어떤 임계를 골라도 한쪽이 틀린다.
    for threshold in (3.5, 4.0, 4.5, 5.0):
        misses = entropy(hex_secret) < threshold or entropy(b64_secret) < threshold
        false_hits = entropy(a_uuid) >= threshold or entropy(b64_english) >= threshold
        assert misses or false_hits, f"임계 {threshold} 가 둘을 갈랐다 — 재검토하라"


# ── 탐지 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", SECRETS, ids=lambda t: t[:24])
async def test_every_secret_is_detected(guard, text):
    assert secret_hits(await guard.inspect(text)), f"시크릿을 놓쳤다: {text[:40]!r}"


async def test_the_value_is_actually_removed(guard):
    """탐지만 하고 값이 남으면 아무것도 한 것이 아니다."""
    verdict = await guard.inspect("키는 AKIAIOSFODNN7EXAMPLE 입니다")

    assert "AKIAIOSFODNN7EXAMPLE" not in verdict.prompts[EXTERNAL]
    assert "AKIAIOSFODNN7EXAMPLE" not in verdict.prompts[INTERNAL]
    assert "[시크릿]" in verdict.prompts[EXTERNAL]


async def test_our_own_service_token_is_a_secret(guard):
    """**이 제품의 토큰이 프롬프트에 실려 나가면 인증이 통째로 뚫린다.**

    설치처 개발자가 자기 토큰을 붙여 넣는 일은 실제로 일어난다.
    """
    raw = token()
    verdict = await guard.inspect(f"이 토큰으로 호출했습니다: {raw}")

    assert "secret_vendor_key" in secret_hits(verdict)
    assert raw not in verdict.prompts[EXTERNAL]


@pytest.mark.parametrize(
    "response",
    [
        "설정은 api_key = sk_live_51H8xQ2eZvKYlo2C1a9kFvR 입니다",
        "문맥에 있던 키는 AKIAIOSFODNN7EXAMPLE 였습니다",
    ],
    ids=["assignment", "vendor"],
)
async def test_a_secret_in_the_response_is_masked_too(guard, response):
    """출력 축에도 같은 규칙이 걸린다.

    **모델이 문맥에 있던 키를 응답에 되풀이하는 경로**가 이 팩의 주요 표적이다 —
    입력만 거르면 프롬프트에서 가린 키가 응답으로 되돌아 나온다.
    """
    verdict = await guard.inspect_output(response)

    assert verdict.redacted, f"응답의 시크릿이 안 가려졌다: {response[:40]!r}"
    for leaked in ("sk_live_51H8xQ2eZvKYlo2C1a9kFvR", "AKIAIOSFODNN7EXAMPLE"):
        assert leaked not in verdict.masked


# ── 오탐 — 이 파일의 절반 ───────────────────────────────────────────────────


@pytest.mark.parametrize("text", BENIGN, ids=lambda t: t[:24])
async def test_ordinary_text_is_not_flagged(guard, text):
    """**오탐이 쏟아지면 관리자가 규칙을 꺼버린다.**"""
    hits = secret_hits(await guard.inspect(text))
    assert not hits, f"정상 문장이 시크릿으로 걸렸다: {text[:44]!r} → {sorted(hits)}"


async def test_the_false_positive_rate_clears_the_promotion_gate(guard):
    """규칙이 `full` 로 출하되는 근거를 **숫자로** 남긴다.

    베이스라인 원칙은 "체크섬으로 검증되는 규칙은 오탐률이 낮으므로 마스킹으로
    시작한다" 이다. `credential_shape` 는 Luhn 같은 수학적 검증기가 아니라
    자리표시자 휴리스틱이므로, 확신의 근거가 계산이 아니라 이 코퍼스다.
    """
    misses = [t for t in BENIGN if secret_hits(await guard.inspect(t))]
    rate = len(misses) / len(BENIGN)

    assert rate <= MAX_FALSE_POSITIVE_RATE, f"오탐률 {rate:.1%}: {misses}"


# ── 검증기 단위 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        'api_key = "YOUR_API_KEY_HERE"',
        "password = None",
        "secret: TODO",
        "auth_token: changeme-before-deploy",
        "credentials = boto3.Session",
        'password_hash = "$2b$12$abcdefghijklmnop"',
        "api_key_id = 123456789012",
        'password = "xxxxxxxxxxxx"',
    ],
)
def test_the_shape_check_rejects_non_credentials(value):
    assert not credential_shape(value), f"자리표시자를 자격증명으로 봤다: {value!r}"


@pytest.mark.parametrize(
    "value",
    [
        'api_key = "sk_live_51H8xQ2eZvKYlo2C1a9kFvR"',
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfi",
        "password: Tr0ub4dor&3xKcd9mQ",
    ],
)
def test_the_shape_check_accepts_real_credentials(value):
    assert credential_shape(value), f"진짜 자격증명을 놓쳤다: {value!r}"


def test_a_hash_length_value_is_not_rejected():
    """**40자리 hex 를 해시로 보고 버리면 `token_hex(20)` 토큰이 함께 빠진다.**

    여기까지 온 값은 이미 `auth_token=` 문맥을 지났다 — 모양보다 문맥이 더
    믿을 만한 증거다.
    """
    assert credential_shape(f"auth_token = {pysecrets.token_hex(20)}")


def test_the_padding_of_a_base64_value_does_not_confuse_the_split():
    """값 끝의 `=` 에서 다시 자르면 값이 잘려 검증이 어긋난다."""
    assert credential_shape("api_key = YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=")


# ── 팩 배선 ─────────────────────────────────────────────────────────────────


def test_the_secrets_pack_is_on_regardless_of_locale(shipped):
    """자격증명 형식은 나라가 아니라 벤더가 정한다."""
    for locales in ([], ["ko_KR"], ["en_US"], ["ja_JP"]):
        ids = {r.id for r in shipped.rules_for_locales(locales)}
        assert any(i.startswith("secret_") for i in ids), f"{locales} 에서 꺼졌다"


def test_the_contract_tells_consumers_that_credentials_are_masked(client, acme):
    """설정 파일을 붙여 넣고 디버깅을 요청하는 것은 흔한 사용법이다.

    값이 가려져서 간다는 사실을 안 알리면, 소비자는 모델이 왜 못 도와주는지 모른다.
    """
    from tests.conftest import auth

    guide = client.get("/v1/integration", headers=auth(acme["service"])).text

    assert "자격증명도 마스킹된다" in guide
    assert "자기 토큰을 프롬프트에 넣지 않는다" in guide
