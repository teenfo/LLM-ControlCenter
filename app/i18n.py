"""다국어 — 문자열 카탈로그와 로케일 협상.

**기계용 코드는 절대 번역하지 않는다.**

이 원칙이 이 모듈의 전부다. 소비자는 오류 문자열이 아니라 HTTP 상태와 `code` 로 분기해야
하는데, 다국어를 넣으면 그 계약이 깨지기 쉬워진다 — 한국어 메시지로 분기하던 소비자 코드가
영어 환경에서 조용히 실패한다. 그래서 응답에 **둘 다** 싣는다:

    {"code": "rate_limited", "scope": "tenant", "message": "요청 한도를 초과했습니다"}
     └── 분기는 이것으로                              └── 표시는 이것으로

번역되지 않는 것: HTTP 상태 · `code` · `retryable` · 가드 규칙 ID · 역할/노드/모델 이름.
번역되는 것: `message` · 관제 UI 라벨 · 알림 본문 · 문서.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_LOCALE = "ko-KR"

#: 로케일 코드 → 가드 로케일 팩 이름. 팩 이름은 설정 파일 키라서 번역 대상이 아니다.
LOCALE_TO_GUARD_PACK = {
    "ko-KR": "ko_KR",
    "en-US": "en_US",
    "ja-JP": "ja_JP",
}


@dataclass(frozen=True)
class ApiError(Exception):
    """기계용 코드와 사람용 메시지를 함께 나르는 오류.

    `code` 는 계약이므로 로케일과 무관하게 고정이다. 메시지만 렌더 시점에 번역된다.
    """

    code: str
    status: int = 400
    retryable: bool = False
    #: 메시지 템플릿에 채울 값. 값 자체는 번역하지 않는다(역할 이름·노드 이름 등).
    params: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params or {}))

    def __str__(self) -> str:  # 로그용. 사용자에게 보이는 문자열이 아니다
        return f"{self.code}({self.status})"


class Translator:
    """로케일별 문자열 카탈로그.

    없는 키는 키 자체를 돌려준다 — 번역 누락이 예외로 서비스를 멈추게 하지 않는다.
    누락은 로그로 드러내고 화면은 계속 동작하는 쪽이 낫다.
    """

    def __init__(self, catalogs: Mapping[str, Mapping[str, str]], default: str = DEFAULT_LOCALE):
        self._catalogs = dict(catalogs)
        self._default = default if default in catalogs else next(iter(catalogs), DEFAULT_LOCALE)

    @classmethod
    def from_dir(cls, locales_dir: str | Path, default: str = DEFAULT_LOCALE) -> "Translator":
        base = Path(locales_dir)
        catalogs: dict[str, dict[str, str]] = {}
        for path in sorted(base.glob("*.json")):
            catalogs[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        if not catalogs:
            raise FileNotFoundError(f"로케일 카탈로그가 없다: {base}")
        return cls(catalogs, default)

    @property
    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalogs))

    def catalog(self, locale: str | None = None) -> dict[str, str]:
        """한 로케일의 문자열 전부. 관제 UI 가 렌더 전에 통째로 받아 간다.

        기본 로케일을 아래에 깔고 요청 로케일을 덮는다 — 새 키가 한쪽에만 있어도
        화면에 키 문자열이 그대로 뜨지 않는다.
        """
        chosen = locale if locale in self._catalogs else self._default
        merged = dict(self._catalogs.get(self._default, {}))
        merged.update(self._catalogs.get(chosen, {}))
        return merged

    @property
    def default(self) -> str:
        return self._default

    def t(self, key: str, locale: str | None = None, **params: Any) -> str:
        """키를 번역한다. 누락 시 기본 로케일 → 키 순으로 폴백한다."""
        catalog = self._catalogs.get(locale or self._default) or {}
        template = catalog.get(key)
        if template is None:
            template = (self._catalogs.get(self._default) or {}).get(key, key)
        try:
            return template.format(**params)
        except (KeyError, IndexError):
            # 템플릿과 인자가 안 맞아도 화면은 떠야 한다.
            return template

    def render_error(self, error: ApiError, locale: str | None = None) -> dict[str, Any]:
        """오류를 응답 본문으로. 코드와 메시지를 함께 싣는다."""
        body: dict[str, Any] = {
            "code": error.code,          # 번역 안 됨 — 소비자는 이것으로 분기한다
            "retryable": error.retryable,
            "message": self.t(f"error.{error.code}", locale, **error.params),
        }
        # params 는 그대로 노출한다. 어느 단계에서 걸렸는지 같은 정보가 여기 실린다.
        body.update(error.params)
        return body


def negotiate_locale(
    available: Iterable[str],
    accept_language: str | None = None,
    user_locale: str | None = None,
    tenant_default: str | None = None,
    platform_default: str = DEFAULT_LOCALE,
) -> str:
    """로케일 협상.

    우선순위: 사용자 설정 → Accept-Language → 테넌트 기본값 → 플랫폼 기본값.
    멀티테넌트이므로 테넌트마다 기본 로케일이 다를 수 있다.
    """
    options = list(available)
    if not options:
        return platform_default

    def pick(candidate: str | None) -> str | None:
        if not candidate:
            return None
        if candidate in options:
            return candidate
        # ko → ko-KR 처럼 언어 코드만 온 경우
        prefix = candidate.split("-")[0].lower()
        for option in options:
            if option.split("-")[0].lower() == prefix:
                return option
        return None

    if found := pick(user_locale):
        return found

    for tag in _parse_accept_language(accept_language):
        if found := pick(tag):
            return found

    if found := pick(tenant_default):
        return found
    return pick(platform_default) or options[0]


def _parse_accept_language(header: str | None) -> list[str]:
    """Accept-Language 를 q 값 내림차순 태그 목록으로."""
    if not header:
        return []
    entries: list[tuple[float, str]] = []
    for part in header.split(","):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        quality = 1.0
        if params.strip().startswith("q="):
            try:
                quality = float(params.strip()[2:])
            except ValueError:
                quality = 0.0
        entries.append((quality, tag.strip()))
    return [tag for _, tag in sorted(entries, key=lambda e: e[0], reverse=True)]


def guard_pack_for(locale: str) -> str | None:
    """로케일에 대응하는 가드 로케일 팩 이름.

    팩을 안 켜면 그 나라 PII 는 안 잡힌다. 부트스트랩이 테넌트 로케일을 보고
    해당 팩을 자동으로 켜되, 관제 UI 는 켜진 팩을 상시 표시해야 한다.
    """
    return LOCALE_TO_GUARD_PACK.get(locale)
