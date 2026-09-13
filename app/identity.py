"""신원 해싱 — 받은 식별자를 그대로 저장하지 않기 위한 원시함수들.

**소비자가 실수로 이메일을 `end_user` 로 넣어도 DB 에 이메일이 남으면 안 된다.**
계약에 "불투명 식별자를 보내라" 고 적는 것만으로는 부족하다 — 적힌 계약은 어겨지고,
어겨진 순간 개인정보가 저장된다. 그래서 받는 쪽에서 해싱한다.

해싱에 **테넌트별 솔트**를 쓰는 이유는 두 가지다:

1. 같은 사람이 두 테넌트를 쓸 때 그것을 대조할 수 없게 한다(테넌트 간 상관 방지).
2. 탐색 공간이 좁은 값(전화번호·주민번호)은 솔트 없이 해싱하면
   **전수조사로 역산된다.** 해시가 새 유출 경로가 되면 나머지 노력이 무의미해진다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: 해시 길이(hex 문자 수). 128비트면 충돌 걱정 없이 충분하고 저장도 가볍다.
HASH_HEX_LEN = 32

SALT_BYTES = 32


def new_salt() -> bytes:
    """테넌트 생성 시 한 번 만들고 그 테넌트가 살아 있는 동안 바뀌지 않는다.

    솔트가 바뀌면 과거 해시와 대조가 끊겨 사용량 귀속이 갈라진다.
    """
    return secrets.token_bytes(SALT_BYTES)


def _hmac_hex(key: bytes, message: str) -> str:
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:HASH_HEX_LEN]


def hash_end_user(raw: str | None, salt: bytes) -> str | None:
    """엔드유저 식별자를 해싱한다. `None` 은 그대로 통과시킨다(표기 안 함)."""
    if raw is None:
        return None
    normalized = raw.strip()
    if not normalized:
        return None
    return _hmac_hex(salt, normalized)


def hash_prompt(masked_text: str, salt: bytes) -> str:
    """프롬프트 해시.

    **반드시 마스킹된 텍스트를 받는다.** 원문을 해싱하면 안 되는 이유:
    주민번호처럼 탐색 공간이 좁은 값은 해시를 전수조사해서 복원할 수 있다.
    마스킹 후 해싱하면 PII 는 이미 치환돼 있어 복원할 것 자체가 없고,
    테넌트 솔트가 테넌트 간 대조도 막는다.

    용도는 중복 감지와 가드 2단 분류 캐시 키다.
    """
    return _hmac_hex(salt, masked_text)


def hash_system(text: str | None) -> str | None:
    """system 프롬프트 해시 — **솔트를 쓰지 않는다.**

    프롬프트 전략은 저엔트로피 개인정보가 아니라서 역산 위험이 없고,
    솔트를 빼야 "이 역할이 같은 프롬프트를 쓰는가" 를 테넌트를 넘어 비교할 수 있다.
    프롬프트 변경과 품질 변화의 상관을 재려면 이 비교가 필요하다.
    """
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_HEX_LEN]


def looks_like_pii(raw: str) -> str | None:
    """엔드유저 식별자가 대놓고 개인정보로 보이면 무엇처럼 보이는지 돌려준다.

    해싱하므로 저장은 안전하지만, 소비자가 계약을 오해하고 있다는 신호이므로
    경고를 남길 수 있게 한다. 차단하지는 않는다 — 이미 해싱돼 저장되기 때문에
    거부할 실익이 없고, 거부하면 소비자가 우회로를 만든다.
    """
    value = raw.strip()
    if "@" in value and "." in value.rsplit("@", 1)[-1]:
        return "email"
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) >= 10 and len(digits) == len(value.replace("-", "").replace(" ", "")):
        return "phone_or_id"
    return None
