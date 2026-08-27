"""Ollama 프로바이더 — 로컬 노드.

능력: 모델 설치 필요 · 메모리 예산 계상 · 과금 없음.

주의: 이 프로바이더를 쓴다고 데이터 경계 안인 것이 **아니다.** 임대 GPU 에 Ollama 를
올리면 같은 코드로 붙지만 프롬프트는 남의 기계로 나간다. 경계는 노드가 선언한다.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Sequence

import httpx

from .base import (
    BackendError,
    Capabilities,
    EmbeddingResult,
    GenerationResult,
    HealthResult,
    ModelNotFound,
    http_status_is_retryable,
    register,
)

CAPABILITIES = Capabilities(
    requires_model_install=True,
    uses_memory_budget=True,
    metered=False,
    supports_embed=True,
)

#: 모델을 붙잡고 있는 시간. 클러스터에서는 짧게 잡는다 —
#: 큰 모델 여러 개를 기본값(수 분)으로 붙잡으면 노드 전체가 느려진다.
DEFAULT_KEEP_ALIVE = "60s"


class OllamaProvider:
    name = "ollama"
    capabilities = CAPABILITIES

    def __init__(self, node: Any, *, client: httpx.AsyncClient | None = None):
        if not node.base_url:
            raise ValueError(f"노드 {node.name}: ollama 프로바이더는 base_url 이 필요하다")

        self.node_name = node.name
        self._base_url = str(node.base_url).rstrip("/")
        self._owns_client = client is None

        headers = {}
        if node.auth_header_env:
            token = os.environ.get(node.auth_header_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(headers=headers)

    # -- 내부 -----------------------------------------------------------------

    async def _post(self, path: str, payload: Mapping[str, Any], timeout: float) -> dict:
        try:
            response = await self._client.post(
                f"{self._base_url}{path}", json=dict(payload), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise BackendError(f"노드 {self.node_name} 응답 시간 초과", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"노드 {self.node_name} 에 연결할 수 없다: {exc}", retryable=True,
                code="node_unreachable",
            ) from exc

        if response.status_code >= 400:
            body = response.text[:500]
            if response.status_code == 404 or "not found" in body.lower():
                raise ModelNotFound(dict(payload).get("model", "?"), self.node_name)
            raise BackendError(
                f"노드 {self.node_name} 오류 {response.status_code}: {body}",
                retryable=http_status_is_retryable(response.status_code),
            )
        return response.json()

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
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": DEFAULT_KEEP_ALIVE,
            "options": dict(options or {}),
        }
        if system:
            payload["system"] = system
        if max_tokens:
            payload["options"]["num_predict"] = int(max_tokens)

        data = await self._post("/api/generate", payload, timeout)

        return GenerationResult(
            text=data.get("response", ""),
            model=data.get("model", model),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            metrics={
                # tok/s 의 정직한 근거는 eval_count / eval_duration 이다 —
                # total_duration 은 모델 로드 시간을 포함해 콜드/웜 비교를 무의미하게 만든다.
                "load_duration_ns": data.get("load_duration"),
                "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
                "eval_duration_ns": data.get("eval_duration"),
                "total_duration_ns": data.get("total_duration"),
            },
        )

    async def embed(
        self, *, model: str, inputs: Sequence[str], timeout: float = 60.0
    ) -> EmbeddingResult:
        data = await self._post(
            "/api/embed",
            {"model": model, "input": list(inputs), "keep_alive": DEFAULT_KEEP_ALIVE},
            timeout,
        )
        return EmbeddingResult(
            vectors=data.get("embeddings", []),
            model=data.get("model", model),
            input_tokens=int(data.get("prompt_eval_count", 0)),
        )

    # -- 헬스·모델 -------------------------------------------------------------

    async def health(self, *, timeout: float = 10.0) -> HealthResult:
        try:
            response = await self._client.get(f"{self._base_url}/api/tags", timeout=timeout)
            response.raise_for_status()
            models = tuple(m["name"] for m in response.json().get("models", []))
        except httpx.HTTPError as exc:
            return HealthResult(ok=False, error=str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            # **200 인데 우리가 아는 모양이 아니다.** 앞에 리버스 프록시가 서서
            # 로그인 페이지를 돌려주는 구성이 흔하다. 도달은 했지만 이 노드는
            # Ollama 로서 쓸 수 없으므로 실패로 본다 — 예외를 위로 올리면
            # 프로브 사이클 전체가 끊긴다.
            return HealthResult(ok=False, error=f"응답을 해석할 수 없다: {exc}")

        loaded = None
        try:
            running = await self._client.get(f"{self._base_url}/api/ps", timeout=timeout)
            if running.status_code == 200:
                entries = running.json().get("models", [])
                loaded = entries[0]["name"] if entries else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            # 로드된 모델을 못 알아내도 노드가 죽은 것은 아니다.
            # 모델 친화는 최적화일 뿐이라 없어도 배치는 돈다.
            pass

        return HealthResult(ok=True, models=models, loaded_model=loaded)

    async def pull(
        self, model: str, *, on_progress: Callable[[int], None] | None = None
    ) -> None:
        """모델을 내려받는다. 진행률을 0~100 으로 보고한다."""
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/api/pull",
                json={"model": model, "stream": True},
                timeout=None,  # pull 은 수십 분이 걸릴 수 있다
            ) as response:
                if response.status_code >= 400:
                    raise BackendError(
                        f"pull 실패 {response.status_code}", retryable=True
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise BackendError(str(event["error"]), retryable=False)
                    total, completed = event.get("total"), event.get("completed")
                    if on_progress and total:
                        on_progress(int(completed or 0) * 100 // int(total))
        except httpx.HTTPError as exc:
            raise BackendError(f"pull 중 연결 실패: {exc}", retryable=True) from exc

        if on_progress:
            on_progress(100)

    async def delete(self, model: str) -> None:
        try:
            response = await self._client.request(
                "DELETE", f"{self._base_url}/api/delete", json={"model": model}, timeout=30.0
            )
        except httpx.HTTPError as exc:
            raise BackendError(f"삭제 실패: {exc}", retryable=True) from exc
        if response.status_code >= 400 and response.status_code != 404:
            raise BackendError(
                f"삭제 실패 {response.status_code}", retryable=False
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@register("ollama")
def _build(node: Any, **kwargs: Any) -> OllamaProvider:
    return OllamaProvider(node, **kwargs)
