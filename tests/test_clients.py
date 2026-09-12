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


# ── 플러그인 트리거 흉내 ────────────────────────────────────────────────────


def _serve(server, **attrs):
    roles, _note = server.load_roles(ROOT / "config")
    handler = type("Handler", (server.Handler,), {"roles": roles, "latency": 0.0, **attrs})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_the_mock_server_answers_409_without_an_event_subscription():
    """플래그가 없으면 진짜처럼 — tick 은 예정 없음, events 는 409 다."""
    server = _load("mock_server")
    httpd, base = _serve(server)
    try:
        sdk = _load("client")
        api = sdk.ControlCenter(base, "lcc_test_token")
        assert api._request("POST", "/v1/plugin/tick", {}) == {
            "id": "mock.plugin", "due": False, "scheduled_for": None, "next_run_at": None,
        }
        with pytest.raises(sdk.ControlCenterError) as exc:
            api._request("POST", "/v1/plugin/events", {})
        assert (exc.value.status, exc.value.code) == (409, "plugin_no_event_trigger")
    finally:
        httpd.shutdown(); httpd.server_close()


def test_the_mock_server_ticks_once_per_period():
    server = _load("mock_server")
    httpd, base = _serve(server, plugin_tick_every=0.05)
    try:
        sdk = _load("client")
        api = sdk.ControlCenter(base, "lcc_test_token")
        first = api._request("POST", "/v1/plugin/tick", {})
        assert first["due"] is False and first["next_run_at"], "첫 예정은 한 주기 뒤다"
        import time as _time
        _time.sleep(0.06)
        due = api._request("POST", "/v1/plugin/tick", {})
        assert due["due"] is True and due["scheduled_for"] == pytest.approx(first["next_run_at"])
        assert api._request("POST", "/v1/plugin/tick", {})["due"] is False, "한 번 준 예정은 다시 주지 않는다"
    finally:
        httpd.shutdown(); httpd.server_close()


def test_the_mock_server_delivers_finished_jobs_at_least_once():
    server = _load("mock_server")
    httpd, base = _serve(server, plugin_events=True)
    try:
        sdk = _load("client")
        api = sdk.ControlCenter(base, "lcc_test_token")
        ids = [api.generate("summarize", f"요약 {i}", wait=5, end_user="u1").job_id for i in range(2)]
        first = api._request("POST", "/v1/plugin/events", {"limit": 50})
        assert [e["job_id"] for e in first["events"]] == ids and first["pending"] == 0
        event = first["events"][0]
        assert event["kind"] == "job.finished" and event["status"] == "ok" and event["end_user"]
        assert set(event) >= {"prompt", "output", "usage", "boundary", "model", "node", "job_kind", "finished_at"}
        again = api._request("POST", "/v1/plugin/events", {"limit": 50})
        assert [e["id"] for e in again["events"]] == [1, 2], "ack 전에는 같은 배치가 다시 온다"
        acked = api._request("POST", "/v1/plugin/events", {"ack": first["cursor"], "limit": 0})
        assert acked["events"] == [] and acked["pending"] == 0 and acked["cursor"] == 2
        assert api._request("POST", "/v1/plugin/events", {"limit": 50})["events"] == []
        with pytest.raises(sdk.ControlCenterError) as exc:
            api._request("POST", "/v1/plugin/events", {"limit": -1})
        assert exc.value.code == "invalid_field"
    finally:
        httpd.shutdown(); httpd.server_close()
