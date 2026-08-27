"""Anthropic 프로바이더 — 경계 밖 노드.

능력: 모델 설치 불필요 · 메모리 예산 미적용 · **과금됨**.

공식 SDK(`anthropic`)를 쓰되 **선택적 의존성**으로 둔다. 로컬 노드만 쓰는 설치처가
클라우드 SDK 를 강제로 받을 이유가 없고, 에어갭 설치에서는 아예 설치되지 않는다.
그래서 import 를 모듈 최상단이 아니라 생성자에서 한다.

    pip install 'llm-controlcenter[anthropic]'

**SDK 자동 재시도를 끈다(`max_retries=0`).** 재시도는 스케줄러의 몫이고, 스케줄러의
재시도는 **재배치를 동반한다** — SDK 가 안에서 같은 노드로 다시 던지면 그 설계가 무너지고
과금만 두 배가 된다.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Sequence

from .base import (
    BackendError,
    Capabilities,
    EmbeddingResult,
    GenerationResult,
    HealthResult,
    UnsupportedOperation,
    register,
)

CAPABILITIES = Capabilities(
    requires_model_install=False,   # 클라우드 모델은 pull 이 없다
    uses_memory_budget=False,       # 남의 메모리라 우리 예산에 계상되지 않는다
    metered=True,                   # 토큰당 과금 — 비용 예약/정산 경로를 탄다
    supports_embed=False,           # Messages API 에 임베딩이 없다
)

#: 이 API 가 그대로 받는 샘플링 옵션. 모르는 키를 던지면 400 이 나고 재시도해도
#: 같으므로, 아는 것만 통과시킨다 — 다만 **아는 것은 통과시킨다.**
SAMPLING_OPTIONS = ("temperature", "top_p", "top_k", "stop_sequences")

DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider:
    name = "anthropic"
    capabilities = CAPABILITIES

    def __init__(self, node: Any, *, client: Any = None):
        self.node_name = node.name
        self._declared_models = tuple(node.models or ())

        if client is not None:
            # 테스트·데모에서 가짜 클라이언트를 주입한다.
            self._client = client
            self._errors = _NullErrors()
            return

        try:
            import anthropic  # noqa: PLC0415 — 선택적 의존성이라 지연 import 한다
        except ImportError as exc:
            raise BackendError(
                "anthropic 패키지가 없다. "
                "pip install 'llm-controlcenter[anthropic]' 로 설치한다",
                retryable=False,
                code="provider_not_installed",
            ) from exc

        api_key = os.environ.get(node.api_key_env or "ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendError(
                f"노드 {node.name}: {node.api_key_env or 'ANTHROPIC_API_KEY'} 가 비어 있다",
                retryable=False,
                code="missing_credentials",
            )

        # max_retries=0 — 재시도는 스케줄러가 하고, 그 재시도는 재배치를 동반한다.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
        self._errors = anthropic

    # -- 오류 분류 -------------------------------------------------------------

    def _translate(self, exc: Exception) -> BackendError:
        """SDK 예외를 재시도 가능 여부가 붙은 오류로 바꾼다.

        무엇이 일시적인지는 백엔드를 아는 쪽이 안다 — 스케줄러는 모른다.
        """
        errors = self._errors
        status = getattr(exc, "status_code", None)

        for name, retryable, code in (
            ("APITimeoutError", True, "backend_unavailable"),
            ("RateLimitError", True, "rate_limited"),
            ("APIConnectionError", True, "node_unreachable"),
            ("AuthenticationError", False, "missing_credentials"),
            ("PermissionDeniedError", False, "missing_credentials"),
            ("NotFoundError", False, "model_not_installed"),
            ("BadRequestError", False, "invalid_request"),
        ):
            cls = getattr(errors, name, None)
            if cls is not None and isinstance(exc, cls):
                return BackendError(str(exc), retryable=retryable, code=code)

        if status is not None:
            # 5xx 는 일시적, 나머지 4xx 는 다시 해도 같다.
            return BackendError(str(exc), retryable=status >= 500)

        return BackendError(str(exc), retryable=True)

    # -- 생성 -----------------------------------------------------------------

    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None = None,
        options: Mapping[str, Any] | None = None,
        timeout: float = 120.0,
        max_tokens: int | None = None,
    ) -> GenerationResult:
        opts = dict(options or {})
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens or opts.pop("max_tokens", DEFAULT_MAX_TOKENS)),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        # 역할 옵션 중 이 API 가 아는 것만 넘긴다. 모르는 키를 그대로 던지면 400 이 나고,
        # 그것은 재시도해도 같은 결과라 잡이 그냥 죽는다.
        if "effort" in opts:
            payload["output_config"] = {"effort": opts["effort"]}
        if "thinking" in opts:
            payload["thinking"] = opts["thinking"]

        # **샘플링 옵션을 조용히 버리지 않는다.**
        #
        # 버리면 같은 역할이 티어에 따라 다른 샘플링으로 돈다 — 로컬에서는
        # `temperature: 0.2` 로 결정적인데 경계 밖으로 나가면 기본값이다.
        # 역할이 정책인데 그 정책의 일부가 경로에 따라 사라지는 셈이고,
        # 품질 비교(C8 의 프롬프트 해시)가 그 차이를 설명하지 못한다.
        for key in SAMPLING_OPTIONS:
            if key in opts and opts[key] is not None:
                payload[key] = opts[key]

        try:
            response = await self._client.messages.create(
                **payload, timeout=timeout
            )
        except Exception as exc:  # SDK 예외 계층 전체를 여기서 분류한다
            raise self._translate(exc) from exc

        # 안전 거부는 오류가 아니라 완료된 응답이다(HTTP 200). 본문을 읽기 전에 확인한다 —
        # 확인 안 하면 빈 텍스트가 정상 결과처럼 조용히 흘러간다.
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise BackendError(
                f"모델이 요청을 거부했다 (분류: {getattr(details, 'category', None)})",
                retryable=False,
                code="model_refusal",
            )

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        usage = getattr(response, "usage", None)

        return GenerationResult(
            text=text,
            model=getattr(response, "model", model),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            metrics={
                "stop_reason": stop_reason,
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(
                    usage, "cache_creation_input_tokens", None
                ),
            },
        )

    async def embed(
        self, *, model: str, inputs: Sequence[str], timeout: float = 60.0
    ) -> EmbeddingResult:
        raise UnsupportedOperation("embed", self.name)

    # -- 헬스·모델 -------------------------------------------------------------

    async def health(self, *, timeout: float = 10.0) -> HealthResult:
        """모델 목록으로 도달성을 확인한다.

        노드가 모델을 선언했으면 그것을 쓰고, 안 했으면 API 에 물어본다.
        """
        try:
            # **받은 timeout 을 실제로 넘긴다.** 안 넘기면 SDK 기본값(보통 10분)이
            # 적용되어 헬스 프로브 하나가 주기를 통째로 잡아먹는다.
            listing = await self._client.models.list(timeout=timeout)
            available = tuple(m.id for m in listing.data)
        except Exception as exc:
            return HealthResult(ok=False, error=str(self._translate(exc)))

        return HealthResult(ok=True, models=self._declared_models or available)

    async def pull(
        self, model: str, *, on_progress: Callable[[int], None] | None = None
    ) -> None:
        raise UnsupportedOperation("pull", self.name)

    async def delete(self, model: str) -> None:
        raise UnsupportedOperation("delete", self.name)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


class _NullErrors:
    """클라이언트를 주입했을 때 쓰는 빈 예외 계층 — `getattr` 이 전부 None 을 준다."""


@register("anthropic")
def _build(node: Any, **kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(node, **kwargs)
