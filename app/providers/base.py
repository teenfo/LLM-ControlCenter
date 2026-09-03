"""프로바이더 인터페이스.

로컬과 클라우드는 "텍스트를 생성한다" 만 같고 나머지가 다르다 — 모델 설치가 필요한지,
메모리 예산에 계상되는지, 돈이 드는지. 스케줄러가 `if node.provider == "ollama"` 로
분기하면 프로바이더를 추가할 때마다 스케줄러가 뜯긴다. 그래서 차이를 **능력 플래그**로
인터페이스 뒤에 숨긴다.

**데이터 경계는 여기 없다.** `provider: ollama` 라고 경계 안인 것이 아니다 —
임대 GPU 의 Ollama 는 소프트웨어가 같아도 프롬프트가 남의 기계로 나간다.
경계는 노드 속성(`config.Node.data_boundary`)이고, 그것이 이 설계의 핵심 안전장치다.

**재시도 가능 여부는 프로바이더가 판정한다.** 컨텍스트 초과처럼 다시 해도 같은 결과인
것을 재시도하면 시간과 돈만 쓴다. 무엇이 일시적 실패인지는 백엔드를 아는 쪽이 안다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class Capabilities:
    """프로바이더가 무엇을 요구하는가. 스케줄러는 이것만 본다."""

    #: 배치 전에 모델 보유를 확인해야 하는가(로컬은 pull 이 필요하다).
    requires_model_install: bool = False
    #: 노드 메모리 예산에 계상되는가.
    uses_memory_budget: bool = False
    #: 비용이 발생하는가.
    metered: bool = False
    #: 임베딩을 지원하는가.
    supports_embed: bool = True


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: 프로바이더별 세부 지표(로드 시간·프롬프트 평가 시간 등). 관제 UI 가 그대로 보여준다.
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: Sequence[Sequence[float]]
    model: str
    input_tokens: int = 0


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    models: tuple[str, ...] = ()
    loaded_model: str | None = None
    error: str | None = None


class BackendError(RuntimeError):
    """백엔드 호출 실패.

    `retryable` 이 이 예외의 존재 이유다 — 스케줄러는 무엇이 일시적인지 모르고,
    프로바이더는 안다.
    """

    def __init__(self, message: str, *, retryable: bool, code: str = "backend_unavailable"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class ModelNotFound(BackendError):
    """모델이 그 노드에 없다. 재시도해도 같으므로 설치 요청 경로로 넘어간다."""

    def __init__(self, model: str, node: str = ""):
        super().__init__(
            f"모델 {model!r} 이(가) 노드 {node!r} 에 없다",
            retryable=False,
            code="model_not_installed",
        )
        self.model = model


class Provider(Protocol):
    """백엔드 한 종류. 노드 하나에 인스턴스 하나가 대응한다."""

    name: str
    capabilities: Capabilities

    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None = None,
        options: Mapping[str, Any] | None = None,
        timeout: float = 120.0,
        max_tokens: int | None = None,
    ) -> GenerationResult: ...

    async def embed(
        self, *, model: str, inputs: Sequence[str], timeout: float = 60.0
    ) -> EmbeddingResult: ...

    async def health(self, *, timeout: float = 10.0) -> HealthResult: ...

    async def pull(
        self, model: str, *, on_progress: Callable[[int], None] | None = None
    ) -> None: ...

    async def delete(self, model: str) -> None: ...

    async def close(self) -> None: ...


class UnsupportedOperation(BackendError):
    def __init__(self, operation: str, provider: str):
        super().__init__(
            f"{provider} 는 {operation} 를 지원하지 않는다", retryable=False,
            code="unsupported_operation",
        )


# ── 재시도 판정 ─────────────────────────────────────────────────────────────

#: 다시 해도 같은 결과인 것들. 재시도하면 시간과 돈만 쓴다.
NON_RETRYABLE_HTTP = frozenset({400, 401, 403, 404, 413, 422})


def http_status_is_retryable(status: int) -> bool:
    """HTTP 상태로 재시도 가능 여부를 판정한다.

    429 는 재시도한다 — 백오프를 두면 풀린다.
    4xx 나머지는 요청 자체가 잘못됐다는 뜻이라 다시 해도 같다.
    """
    if status == 429:
        return True
    if status in NON_RETRYABLE_HTTP:
        return False
    return status >= 500


#: 프로바이더 이름 → 생성자. 새 프로바이더는 여기만 늘어난다.
_REGISTRY: dict[str, Callable[..., Provider]] = {}


def register(name: str) -> Callable[[Callable[..., Provider]], Callable[..., Provider]]:
    def decorator(factory: Callable[..., Provider]) -> Callable[..., Provider]:
        _REGISTRY[name] = factory
        return factory

    return decorator


def build_provider(node: Any, **kwargs: Any) -> Provider:
    """노드 선언에서 프로바이더를 만든다."""
    factory = _REGISTRY.get(node.provider)
    if factory is None:
        raise ValueError(
            f"알 수 없는 프로바이더: {node.provider!r} (등록된 것: {sorted(_REGISTRY)})"
        )
    return factory(node, **kwargs)


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
