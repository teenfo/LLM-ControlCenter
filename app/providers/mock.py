"""목 프로바이더 — 테스트와 Demo 프로파일의 공용 경로.

**마지막에 붙이는 포장이 아니라 개발 중 상시 사용하는 경로다.** 실제 노드 없이 테스트를
돌리려면 어차피 필요하고, 그게 그대로 데모가 된다 — GPU 없는 노트북 한 대로 클러스터
제품을 시연할 수 있다는 것이 영업·PoC 에서 큰 차이를 만든다.

목으로 시연되는 것: 테넌시 격리 · 가드 1단 패턴 · 배치 라우팅 · **장애 폴백** ·
비용 예약/정산 · 관제 UI 전체.
목으로 안 되는 것: 실제 생성 품질, 가드 2단 LLM 분류(내부 노드 전용).

결정론적이다 — 같은 입력에 같은 출력을 준다. 데모에서 매번 다른 결과가 나오면
"뭐가 바뀐 거지" 를 설명하느라 시연이 산으로 간다.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Mapping, Sequence

from .base import (
    BackendError,
    Capabilities,
    EmbeddingResult,
    GenerationResult,
    HealthResult,
    ModelNotFound,
    register,
)

EMBED_DIMENSIONS = 8


#: 호출 기록의 상한. 시연을 며칠 켜 둬도 메모리가 늘지 않게.
MAX_CALL_LOG = 1000


#: 분류기 프롬프트의 카나리아 표식. 파이프라인의 `CANARY_MARK` 와 같은 값이지만
#: **임포트하지 않는다** — 프로바이더가 파이프라인을 알면 의존 방향이 뒤집힌다.
#: 두 상수가 갈리면 `test_the_mock_answers_the_classifier_compliantly` 가 실패한다.
_CANARY = re.compile(r"CANARY=([0-9a-f]{16})")


#: 라우팅 프롬프트의 선택지 절. `pipeline._routing_prompt` 의 형식과 같은 값이지만
#: 카나리아와 같은 이유로 **임포트하지 않는다.** 형식이 갈리면
#: `test_the_mock_answers_the_routing_prompt_with_a_key` 가 실패한다.
_ROUTE_CATALOG = re.compile(r"\[선택지\]\n((?:- [^:\n]+:[^\n]*\n)+)")


def _compliant_classifier_reply(prompt: str) -> str | None:
    """분류기·라우터 프롬프트면 **형식을 지키는 모델**처럼 답한다.

    목은 "제어 가능한 가짜 백엔드" 다. 카나리아를 안 돌려주면 파이프라인이 그것을
    지시 이탈로 읽어 `on_classifier_error` 를 태우므로, 목으로 도는 데모와 테스트가
    전부 분류 실패 상태가 된다 — 목이 흉내 내야 할 것은 **인증을 통과한 모델**이다.

    인젝션에 넘어간 모델을 흉내 내려면 `reply` 를 직접 지정한다. 그쪽이 우선한다.
    """
    found = _CANARY.search(prompt)
    if not found:
        return None

    # 라우팅 프롬프트(선택지 절이 있다)에는 **키 하나를 결정론적으로** 고른다.
    # NONE 만 내면 라우팅 데모가 판정 없는 화면이 된다 — README 가 약속하는 것은
    # 배선(판정이 모델을 바꾸고 잡에 박힌다)의 시연이고, 그것은 키를 내야 보인다.
    # 입력 해시로 고르므로 같은 프롬프트는 같은 라우트다 — 데모에서 매번 다른
    # 결과가 나오면 "뭐가 바뀐 거지" 를 설명하느라 시연이 산으로 간다.
    catalog = _ROUTE_CATALOG.search(prompt)
    if catalog:
        keys = [line.split(":", 1)[0][2:] for line in catalog.group(1).splitlines()]
        material = prompt.split("[자료 시작", 1)[-1]
        pick = keys[int(hashlib.sha256(material.encode()).hexdigest(), 16) % len(keys)]
        return f"CANARY={found.group(1)}\n{pick}"

    # 가드 분류에는 판정하지 않는다. 목이 맥락을 진짜로 읽을 수는 없고, 읽는
    # 척하면 가드 2단이 목에서 "동작하는 것처럼" 보여 진짜 모델 없이 통과한다.
    # 라우팅은 다르다 — 오판의 대가가 보안이 아니라 비용이라 데모에 안전하다.
    return f"CANARY={found.group(1)}\nNONE"


class MockProvider:
    """제어 가능한 가짜 백엔드.

    데모에서 노드를 죽이고 폴백이 도는 것을 보여주려면 실패를 연출할 수 있어야 한다 —
    클러스터 제품의 핵심 데모가 바로 그것이다.
    """

    name = "mock"

    def __init__(self, node: Any, **_: Any):
        self.node_name = node.name
        self._models = list(node.models or ["demo-small"])
        self._loaded: str | None = None

        # 노드가 metered_override 를 선언하면 과금 노드처럼 행동한다.
        # 데모에서 예산 소진 → 무료 경로 강등을 보여주기 위한 것이다.
        metered = bool(getattr(node, "metered_override", False))
        self.capabilities = Capabilities(
            # 과금 노드는 클라우드처럼, 아니면 로컬처럼 행동한다.
            requires_model_install=not metered,
            uses_memory_budget=not metered,
            metered=metered,
            supports_embed=True,
        )

        # -- 연출 손잡이 (테스트·데모에서 직접 만진다) --
        #
        # `offline` 태그를 달면 죽은 채로 시작한다. 데모 시드가 "안 붙는 노드" 를
        # 미리 심어 두고 등록 화면·노드 그리드가 그것을 어떻게 보여주는지 시연할 수
        # 있어야 하기 때문이다 — 살아 있는 노드만 있는 데모는 관제를 못 보여준다.
        self.online = "offline" not in tuple(getattr(node, "tags", ()) or ())
        self.fail_next = 0            # 다음 N회 호출을 실패시킨다
        self.fail_retryable = True
        #: 이 텍스트로 답한다. `None` 이면 결정론적 기본 응답.
        #:
        #: **출력 축을 시연·검증하려면 모델이 개인정보를 뱉을 수 있어야 한다.**
        #: 기본 응답은 해시라 PII 가 절대 안 나오고, 그러면 응답 마스킹이 도는지
        #: 데모에서도 테스트에서도 확인할 방법이 없다 — 켜지지 않은 필터와
        #: 확인할 수 없는 필터는 실무에서 같은 것이다.
        self.reply: str | None = None
        self.installed: set[str] = set(self._models)
        #: 데모·테스트용 호출 기록. **Demo 프로파일은 상시 구동 대상이라**
        #: 상한이 없으면 며칠 켜 둔 시연 노트북에서 그냥 메모리 누수다.
        self.call_log: list[dict[str, Any]] = []

    # -- 연출 --------------------------------------------------------------

    def kill(self) -> None:
        """노드를 죽인다. 데모에서 폴백을 보여줄 때 쓴다."""
        self.online = False

    def revive(self) -> None:
        self.online = True

    def _guard(self) -> None:
        if not self.online:
            raise BackendError(
                f"노드 {self.node_name} 오프라인", retryable=True, code="node_unreachable"
            )
        if self.fail_next > 0:
            self.fail_next -= 1
            raise BackendError(
                f"노드 {self.node_name} 연출된 실패", retryable=self.fail_retryable
            )

    # -- 생성 --------------------------------------------------------------

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
        self._guard()
        if self.capabilities.requires_model_install and model not in self.installed:
            raise ModelNotFound(model, self.node_name)

        self._log({"op": "generate", "model": model, "chars": len(prompt)})
        self._loaded = model

        digest = hashlib.sha256(f"{model}|{system}|{prompt}".encode()).hexdigest()[:12]
        # 토큰 수를 길이에서 유도한다 — 비용 계산 경로가 실제처럼 동작해야
        # 데모에서 예산 소진을 보여줄 수 있다.
        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(digest) // 2)

        return GenerationResult(
            text=self.reply if self.reply is not None
            else _compliant_classifier_reply(prompt)
            or f"[mock:{self.node_name}/{model}] {digest}",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            metrics={"mock": True, "node": self.node_name},
        )

    async def embed(
        self, *, model: str, inputs: Sequence[str], timeout: float = 60.0
    ) -> EmbeddingResult:
        self._guard()
        if self.capabilities.requires_model_install and model not in self.installed:
            raise ModelNotFound(model, self.node_name)

        self._log({"op": "embed", "model": model, "count": len(inputs)})

        vectors = []
        for text in inputs:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append([digest[i] / 255.0 for i in range(EMBED_DIMENSIONS)])

        return EmbeddingResult(
            vectors=vectors,
            model=model,
            input_tokens=sum(max(1, len(t) // 4) for t in inputs),
        )

    # -- 헬스·모델 ----------------------------------------------------------

    def _log(self, entry: dict[str, Any]) -> None:
        self.call_log.append(entry)
        if len(self.call_log) > MAX_CALL_LOG:
            del self.call_log[:-MAX_CALL_LOG]

    async def health(self, *, timeout: float = 10.0) -> HealthResult:
        if not self.online:
            return HealthResult(ok=False, error="오프라인")
        return HealthResult(
            ok=True, models=tuple(sorted(self.installed)), loaded_model=self._loaded
        )

    async def pull(
        self, model: str, *, on_progress: Callable[[int], None] | None = None
    ) -> None:
        self._guard()
        for percent in (0, 25, 50, 75, 100):
            if on_progress:
                on_progress(percent)
        self.installed.add(model)

    async def delete(self, model: str) -> None:
        self.installed.discard(model)

    async def close(self) -> None:
        return None


@register("mock")
def _build(node: Any, **kwargs: Any) -> MockProvider:
    return MockProvider(node, **kwargs)
