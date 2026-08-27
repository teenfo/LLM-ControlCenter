"""프로바이더 계약.

가장 중요한 것: **데이터 경계가 `Capabilities` 에 없다.** 경계를 프로바이더 속성으로
두면 임대 GPU 의 Ollama 가 자동으로 "내부" 가 되고, 가드 분류기의 내부 전용 보장이
그 순간 무너진다.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from app.config import Node
from app.providers import (
    BackendError,
    Capabilities,
    ModelNotFound,
    UnsupportedOperation,
    build_provider,
    http_status_is_retryable,
    known_providers,
)
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaProvider


# ── 능력 플래그 ──────────────────────────────────────────────────────────────


def test_capabilities_has_no_data_boundary_field():
    """경계는 노드 속성이지 프로바이더 속성이 아니다.

    여기 두면 `provider: ollama` 가 곧 "내부" 가 되어, 임대 GPU 에 올린 Ollama 가
    소프트웨어가 같다는 이유로 경계 안 취급을 받는다.
    """
    fields = {f.name for f in dataclasses.fields(Capabilities)}
    assert not fields & {"local", "internal", "data_boundary", "boundary"}


def test_local_and_cloud_capabilities_differ_as_designed():
    local = build_provider(Node(name="n", provider="ollama", base_url="http://x"))
    assert local.capabilities.requires_model_install is True
    assert local.capabilities.uses_memory_budget is True
    assert local.capabilities.metered is False

    cloud = AnthropicProvider(Node(name="c", provider="anthropic"), client=object())
    assert cloud.capabilities.requires_model_install is False
    assert cloud.capabilities.uses_memory_budget is False
    assert cloud.capabilities.metered is True


def test_all_expected_providers_are_registered():
    assert set(known_providers()) == {"anthropic", "mock", "ollama"}


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="알 수 없는 프로바이더"):
        build_provider(Node(name="n", provider="nope"))


def test_ollama_requires_base_url():
    with pytest.raises(ValueError, match="base_url"):
        build_provider(Node(name="n", provider="ollama"))


# ── 재시도 판정 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,retryable",
    [
        (429, True),    # 백오프하면 풀린다
        (500, True),
        (503, True),
        (400, False),   # 요청이 잘못됐다 — 다시 해도 같다
        (401, False),
        (403, False),
        (404, False),
        (413, False),   # 컨텍스트 초과
        (422, False),
    ],
)
def test_http_retry_judgment(status, retryable):
    assert http_status_is_retryable(status) is retryable


def test_model_not_found_is_not_retryable():
    """설치 요청 경로로 넘어가야 하지, 같은 노드에 다시 던지면 안 된다."""
    err = ModelNotFound("m", "n")
    assert err.retryable is False
    assert err.code == "model_not_installed"


# ── 목 프로바이더 ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_node() -> Node:
    return Node(
        name="mock-a", provider="mock", data_boundary="internal",
        models=("demo-small", "demo-medium"),
    )


@pytest.fixture
def mock(mock_node) -> MockProvider:
    return build_provider(mock_node)


async def test_mock_generate_is_deterministic(mock):
    """데모에서 매번 다른 결과가 나오면 "뭐가 바뀐 거지" 를 설명하느라 시연이 산으로 간다."""
    a = await mock.generate(model="demo-small", prompt="안녕")
    b = await mock.generate(model="demo-small", prompt="안녕")
    assert a.text == b.text

    c = await mock.generate(model="demo-small", prompt="다른 입력")
    assert c.text != a.text


async def test_mock_reports_token_counts(mock):
    """비용 계산 경로가 실제처럼 동작해야 데모에서 예산 소진을 보여줄 수 있다."""
    result = await mock.generate(model="demo-small", prompt="가" * 400)
    assert result.input_tokens > 0
    assert result.output_tokens > 0


async def test_mock_node_can_be_killed_for_failover_demos(mock):
    """노드를 죽여 폴백이 도는 것을 보여주는 게 클러스터 제품의 핵심 데모다."""
    mock.kill()

    with pytest.raises(BackendError) as exc:
        await mock.generate(model="demo-small", prompt="x")
    assert exc.value.retryable is True
    assert (await mock.health()).ok is False

    mock.revive()
    assert (await mock.health()).ok is True


async def test_mock_missing_model_raises_not_found(mock):
    with pytest.raises(ModelNotFound):
        await mock.generate(model="never-installed", prompt="x")


async def test_mock_pull_installs_and_reports_progress(mock):
    seen: list[int] = []
    await mock.pull("new-model", on_progress=seen.append)

    assert seen[-1] == 100
    assert "new-model" in (await mock.health()).models
    await mock.generate(model="new-model", prompt="x")  # 이제 돈다


async def test_mock_metered_node_behaves_like_cloud():
    """metered_override 노드는 설치도 메모리 예산도 없이 과금만 된다."""
    node = Node(name="c", provider="mock", metered_override=True, models=("cloud-m",))
    provider = build_provider(node)

    assert provider.capabilities.metered is True
    assert provider.capabilities.requires_model_install is False
    # 설치 확인을 안 하므로 선언 안 된 모델도 그냥 돈다(클라우드처럼).
    await provider.generate(model="anything", prompt="x")


async def test_mock_embed_returns_vectors(mock):
    result = await mock.embed(model="demo-small", inputs=["가", "나"])
    assert len(result.vectors) == 2
    assert len(result.vectors[0]) == 8


# ── Ollama 오류 매핑 ─────────────────────────────────────────────────────────


def ollama_with(handler) -> OllamaProvider:
    node = Node(name="n1", provider="ollama", base_url="http://node")
    return OllamaProvider(node, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_ollama_parses_generation_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "response": "요약 결과",
            "model": "m",
            "prompt_eval_count": 120,
            "eval_count": 34,
            "eval_duration": 1_000_000_000,
            "total_duration": 3_000_000_000,
        })

    result = await ollama_with(handler).generate(model="m", prompt="문서")

    assert result.text == "요약 결과"
    assert result.input_tokens == 120
    assert result.output_tokens == 34
    # tok/s 의 정직한 근거는 eval_duration 이다 — total 은 모델 로드를 포함한다.
    assert result.metrics["eval_duration_ns"] == 1_000_000_000
    assert result.metrics["total_duration_ns"] == 3_000_000_000


async def test_ollama_missing_model_becomes_model_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model 'x' not found")

    with pytest.raises(ModelNotFound):
        await ollama_with(handler).generate(model="x", prompt="p")


@pytest.mark.parametrize("status,retryable", [(500, True), (429, True), (400, False)])
async def test_ollama_status_maps_to_retryable(status, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="boom")

    with pytest.raises(BackendError) as exc:
        await ollama_with(handler).generate(model="m", prompt="p")
    assert exc.value.retryable is retryable


async def test_ollama_connection_failure_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(BackendError) as exc:
        await ollama_with(handler).generate(model="m", prompt="p")
    assert exc.value.retryable is True
    assert exc.value.code == "node_unreachable"


async def test_ollama_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(BackendError) as exc:
        await ollama_with(handler).generate(model="m", prompt="p")
    assert exc.value.retryable is True


async def test_ollama_health_lists_models():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "a"}, {"name": "b"}]})
        return httpx.Response(200, json={"models": [{"name": "a"}]})

    health = await ollama_with(handler).health()
    assert health.ok is True
    assert health.models == ("a", "b")
    assert health.loaded_model == "a"


async def test_ollama_health_survives_missing_ps_endpoint():
    """로드된 모델을 못 알아내도 노드가 죽은 것은 아니다.

    모델 친화는 최적화일 뿐이라 없어도 배치는 돈다.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "a"}]})
        raise httpx.ConnectError("no /api/ps")

    health = await ollama_with(handler).health()
    assert health.ok is True
    assert health.loaded_model is None


async def test_ollama_health_reports_failure_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    health = await ollama_with(handler).health()
    assert health.ok is False
    assert health.error


# ── Anthropic 프로바이더 ─────────────────────────────────────────────────────


class FakeUsage:
    input_tokens = 100
    output_tokens = 20
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text="응답", stop_reason="end_turn", stop_details=None):
        self.content = [FakeBlock(text)]
        self.model = "some-model"
        self.usage = FakeUsage()
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_payload: dict | None = None

    async def create(self, **payload):
        self.last_payload = payload
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeAnthropicClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def anthropic_with(response) -> AnthropicProvider:
    node = Node(name="cloud", provider="anthropic", data_boundary="external")
    return AnthropicProvider(node, client=FakeAnthropicClient(response))


async def test_anthropic_extracts_text_and_usage():
    provider = anthropic_with(FakeResponse("요약"))
    result = await provider.generate(model="claude-opus-5", prompt="문서", max_tokens=500)

    assert result.text == "요약"
    assert result.input_tokens == 100
    assert result.output_tokens == 20


async def test_anthropic_refusal_is_surfaced_not_silently_empty():
    """안전 거부는 HTTP 200 으로 온다. 확인 안 하면 빈 텍스트가 정상 결과처럼 흘러간다."""
    class Details:
        category = "cyber"

    provider = anthropic_with(FakeResponse("", stop_reason="refusal", stop_details=Details()))

    with pytest.raises(BackendError) as exc:
        await provider.generate(model="claude-opus-5", prompt="x")
    assert exc.value.code == "model_refusal"
    assert exc.value.retryable is False, "거부를 재시도하면 돈만 쓴다"


async def test_anthropic_passes_only_known_options():
    """모르는 옵션 키를 그대로 던지면 400 이 나고, 그건 재시도해도 같아서 잡이 죽는다.

    **다만 아는 것은 통과시킨다.** 예전에는 `temperature` 까지 버려서 같은 역할이
    티어에 따라 다른 샘플링으로 돌았다 — 로컬에서는 결정적인데 경계 밖에서는
    기본값이었다. 역할이 정책인데 그 정책의 일부가 경로에 따라 사라지는 셈이다.
    """
    provider = anthropic_with(FakeResponse())
    await provider.generate(
        model="claude-opus-5", prompt="p", system="s",
        options={"effort": "low", "temperature": 0.7, "num_predict": 99},
    )

    payload = provider._client.messages.last_payload
    assert payload["output_config"] == {"effort": "low"}
    assert payload["temperature"] == 0.7, "샘플링 옵션이 조용히 사라졌다"
    assert "num_predict" not in payload, "모르는 키는 여전히 안 넘긴다"
    assert payload["system"] == "s"


async def test_the_same_role_samples_the_same_way_on_every_tier():
    """티어가 달라도 역할이 정한 샘플링은 같아야 한다 — 품질 비교의 전제다."""
    from app.providers.anthropic_provider import SAMPLING_OPTIONS

    provider = anthropic_with(FakeResponse())
    options = {"temperature": 0.2, "top_p": 0.9, "top_k": 40}
    await provider.generate(model="m", prompt="p", options=options)

    payload = provider._client.messages.last_payload
    for key, value in options.items():
        assert key in SAMPLING_OPTIONS
        assert payload[key] == value


async def test_anthropic_does_not_support_embed():
    provider = anthropic_with(FakeResponse())
    with pytest.raises(UnsupportedOperation):
        await provider.embed(model="m", inputs=["x"])


@pytest.mark.parametrize("operation", ["pull", "delete"])
async def test_anthropic_has_no_model_install_lifecycle(operation):
    provider = anthropic_with(FakeResponse())
    with pytest.raises(UnsupportedOperation):
        await getattr(provider, operation)("model")


async def test_anthropic_unknown_exception_defaults_to_retryable():
    """분류할 수 없는 실패는 일시적인 것으로 본다 — 영구 실패로 단정하면 잡을 잃는다."""
    provider = anthropic_with(RuntimeError("무언가 잘못됨"))
    with pytest.raises(BackendError) as exc:
        await provider.generate(model="m", prompt="p")
    assert exc.value.retryable is True
