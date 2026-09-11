"""번들 클라이언트와 목 서버 — 둘 다 표준 라이브러리만 쓰며, 계약의 모양이 진짜 서버와 같아야 한다.

`test_packaging.py` 가 "표준 라이브러리만" 을 보고, 여기는 **둘이 서로 맞물리는가**를 본다 —
SDK 가 보내는 본문을 목이 진짜처럼 판정해야, 목으로 만든 통합 코드가 실물에서도 돈다.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bundled_{name}", ROOT / "clients" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `from __future__ import annotations` 를 쓰는 dataclass 는 모듈이 sys.modules 에 있어야 정의된다.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mock_base_url():
    server = _load("mock_server")
    roles, _note = server.load_roles(ROOT / "config")
    handler = type("Handler", (server.Handler,), {"roles": roles, "latency": 0.0})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_sdk_chat_round_trips_through_the_mock_server(mock_base_url):
    """`run(messages=)` 는 대화 경로로 가고, 목은 실제 `roles.yaml` 의 `chat` 역할을 안다."""
    sdk = _load("client")
    api = sdk.ControlCenter(mock_base_url, "lcc_test_token")

    result = api.run("chat", messages=[
        {"role": "user", "content": "안녕"}, {"role": "assistant", "content": "네"},
        {"role": "user", "content": "질문"},
    ])

    assert result.ok and result.text.startswith("[mock:chat]")


def test_the_mock_server_rejects_chat_shapes_like_the_real_one(mock_base_url):
    sdk = _load("client")
    api = sdk.ControlCenter(mock_base_url, "lcc_test_token")

    with pytest.raises(sdk.ControlCenterError) as exc:
        api.chat("summarize", [{"role": "user", "content": "x"}])
    assert (exc.value.status, exc.value.code) == (400, "wrong_kind")

    with pytest.raises(sdk.ControlCenterError) as exc:
        api.chat("chat", [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
    assert exc.value.code == "invalid_field"

    with pytest.raises(sdk.ControlCenterError) as exc:
        api.generate("chat", "안녕")
    assert exc.value.code == "wrong_kind"

    with pytest.raises(sdk.ControlCenterError) as exc:
        api.chat("chat", [{"role": "user", "content": "주민번호 990101-1234563"}])
    assert exc.value.code == "guard_blocked"


def test_run_needs_a_prompt_or_messages(mock_base_url):
    sdk = _load("client")
    api = sdk.ControlCenter(mock_base_url, "lcc_test_token")
    with pytest.raises(ValueError):
        api.run("chat")
