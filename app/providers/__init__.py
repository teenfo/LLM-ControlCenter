"""프로바이더 레지스트리.

각 모듈이 import 시점에 `@register` 로 자신을 등록한다. 새 프로바이더를 추가할 때
스케줄러나 클러스터를 건드리지 않는 것이 이 구조의 요지다 —
스케줄러는 프로바이더 이름을 모르고 능력 플래그만 본다.
"""

from .base import (
    BackendError,
    Capabilities,
    EmbeddingResult,
    GenerationResult,
    HealthResult,
    ModelNotFound,
    Provider,
    UnsupportedOperation,
    build_provider,
    http_status_is_retryable,
    known_providers,
)

# import 부수효과로 등록된다. 순서는 무관하다.
from . import anthropic_provider as _anthropic  # noqa: F401
from . import mock as _mock  # noqa: F401
from . import ollama as _ollama  # noqa: F401

__all__ = [
    "BackendError",
    "Capabilities",
    "EmbeddingResult",
    "GenerationResult",
    "HealthResult",
    "ModelNotFound",
    "Provider",
    "UnsupportedOperation",
    "build_provider",
    "http_status_is_retryable",
    "known_providers",
]
