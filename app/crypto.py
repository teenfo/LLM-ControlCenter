"""원문 암호화 — 마스터 KEK 와 테넌트별 DEK.

두 겹으로 나눈 이유가 두 가지다:

1. **격리** — 테넌트 A 의 암호문이 새도 B 의 키로는 못 연다. 단일 키였다면
   격리 버그 하나가 전체 유출이다.
2. **파기(crypto-shredding)** — 테넌트 DEK 를 폐기하면 그 테넌트의 암호문은
   **백업에 남아 있어도 복호화가 불가능하다.** 보존 기간 설정이 백업 앞에서
   무의미해지는 문제를 구조적으로 푸는 유일한 수단이다.

**KEK 가 없으면 원문 보관을 아예 비활성화한다(fail-closed).** 키 설정을 깜빡한 채
원문이 평문으로 쌓이는 사고를 구조적으로 막는다 — "키가 없으면 평문으로라도 저장" 은
가장 나쁜 기본값이다.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-256
KEY_BYTES = 32
#: AES-GCM 권장 논스 길이. 레코드마다 새로 만든다.
NONCE_BYTES = 12

ENV_MASTER_KEY = "LCC_PROMPT_KEY"


class CryptoError(RuntimeError):
    pass


def _aad_bytes(aad: str | None) -> bytes | None:
    """AAD 를 바이트로. `None` 은 바인딩 없음(옛 암호문 호환)."""
    return aad.encode("utf-8") if aad else None


class KeyDestroyed(CryptoError):
    """DEK 가 폐기됐다. 이 테넌트의 암호문은 영구히 열 수 없다 — 의도된 동작이다."""


@dataclass(frozen=True)
class Sealed:
    """봉인된 바이트. 논스와 암호문을 함께 나른다."""

    nonce: bytes
    ciphertext: bytes


def generate_master_key() -> str:
    """부트스트랩에서 한 번 만들고 **1회만 표시**한다.

    잃어버리면 기존 암호문을 영구히 열 수 없다. 백업과 다른 곳에 보관해야 하며,
    같은 곳에 두면 백업 유출이 곧 원문 유출이 된다.
    """
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def load_master_key(env_var: str = ENV_MASTER_KEY) -> bytes | None:
    """환경에서 마스터 KEK 를 읽는다. 없으면 `None` — 그것이 곧 원문 보관 비활성화다."""
    raw = os.environ.get(env_var)
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise CryptoError(f"{env_var} 가 유효한 base64 가 아니다") from exc
    if len(key) != KEY_BYTES:
        raise CryptoError(f"{env_var} 는 base64 로 인코딩된 {KEY_BYTES}바이트여야 한다")
    return key


class KeyVault:
    """마스터 KEK 로 테넌트 DEK 를 감싸고 푼다.

    `enabled` 가 False 면 이 금고는 아무것도 암호화하지 않고, 호출자는 **원문을
    저장하지 않아야 한다.** 평문으로 대체 저장하는 경로는 존재하지 않는다.
    """

    def __init__(self, master_key: bytes | None):
        if master_key is not None and len(master_key) != KEY_BYTES:
            raise CryptoError(f"마스터 키는 {KEY_BYTES}바이트여야 한다")
        self._kek = AESGCM(master_key) if master_key else None

    @classmethod
    def from_env(cls, env_var: str = ENV_MASTER_KEY) -> "KeyVault":
        return cls(load_master_key(env_var))

    @property
    def enabled(self) -> bool:
        """원문 보관이 가능한가. False 면 마스킹본만 저장된다."""
        return self._kek is not None

    # -- DEK 수명주기 ---------------------------------------------------------

    def create_dek(self) -> bytes | None:
        """새 테넌트 DEK 를 만들어 KEK 로 감싼 바이트를 돌려준다.

        평문 DEK 는 이 함수 밖으로 나가지 않는다.
        """
        if self._kek is None:
            return None
        dek = secrets.token_bytes(KEY_BYTES)
        nonce = secrets.token_bytes(NONCE_BYTES)
        return nonce + self._kek.encrypt(nonce, dek, None)

    def _unwrap(self, wrapped: bytes | None) -> AESGCM:
        if self._kek is None:
            raise CryptoError("마스터 KEK 가 없다")
        if not wrapped:
            # 파기된 테넌트. 암호문이 남아 있어도 열 수 없다.
            raise KeyDestroyed("이 테넌트의 DEK 가 폐기됐다")
        nonce, blob = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            return AESGCM(self._kek.decrypt(nonce, blob, None))
        except InvalidTag as exc:
            raise CryptoError("DEK 를 풀 수 없다 — 마스터 KEK 가 다르다") from exc

    # -- 봉인·해제 ------------------------------------------------------------

    def seal(
        self, wrapped_dek: bytes | None, plaintext: str, *, aad: str | None = None
    ) -> Sealed | None:
        """원문을 봉인한다. 금고가 꺼져 있으면 `None` — 호출자는 저장하지 않는다.

        `aad` 는 이 암호문이 **어느 레코드의 것인지**를 태그에 묶는다(AES-GCM 의
        추가 인증 데이터). 묶어 두면 DB 에 쓸 수 있는 공격자가 잡 A 의 암호문을
        잡 B 의 행에 옮겨 넣어도 복호화가 실패한다 — 안 묶으면 그 이식이 성공하고,
        **감사가 남는 정상 열람 경로를 통해** 남의 프롬프트가 화면에 뜬다.

        키만으로는 막히지 않는다. 같은 테넌트 안에서는 DEK 가 하나라서 A 의
        암호문이 B 의 키로 열린다 — 그것이 이 바인딩이 필요한 이유다.
        """
        if not self.enabled:
            return None
        dek = self._unwrap(wrapped_dek)
        nonce = secrets.token_bytes(NONCE_BYTES)   # 레코드마다 새 논스
        return Sealed(
            nonce, dek.encrypt(nonce, plaintext.encode("utf-8"), _aad_bytes(aad))
        )

    def open(
        self, wrapped_dek: bytes | None, sealed: Sealed, *, aad: str | None = None
    ) -> str:
        """봉인을 푼다. 열람은 감사에 남겨야 한다 — 그 책임은 호출자에게 있다."""
        dek = self._unwrap(wrapped_dek)
        try:
            return dek.decrypt(
                sealed.nonce, sealed.ciphertext, _aad_bytes(aad)
            ).decode("utf-8")
        except InvalidTag:
            pass

        if aad is None:
            raise CryptoError("암호문이 손상됐거나 키가 맞지 않는다")

        # **바인딩 도입 이전에 봉인된 암호문**은 AAD 가 없다. 그것까지 못 열게 되면
        # 업그레이드가 곧 원문 손실이므로 한 번 더 시도한다. 새 암호문은 전부
        # 묶여 있고, 이 경로는 보존 기간이 지나 옛 암호문이 사라지면 자연히 죽는다.
        try:
            return dek.decrypt(sealed.nonce, sealed.ciphertext, None).decode("utf-8")
        except InvalidTag as exc:
            raise CryptoError("암호문이 손상됐거나 키가 맞지 않는다") from exc
