"""플러그인 런타임(`clients/plugin.py`) — 계약이 정한 성질을 **라이브러리가** 지키는가.

목 서버(트리거 흉내를 켠 변형)로 루프의 규칙을, uvicorn 으로 띄운 **실제 앱**으로 왕복을 본다.
플러그인마다 다시 짜면 하나는 ack 순서를 틀린다 — 그래서 이 파일이 있다.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app import plugins
from app.main import VERSION
from app.store import TenantScope
from tests.test_plugins import EVENTED, bundle, drive, platform_tenant, signing_key, submit

# pytest 는 픽스처를 모듈 이름 공간에서 찾는다 — 가져온 픽스처를 참조로 붙들어 둔다(정적 검사도 만족).
_BORROWED_FIXTURES = (platform_tenant, signing_key)

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bundled_{name}", ROOT / "clients" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def sdk():
    return _load("plugin")


@pytest.fixture
def mock():
    """트리거 흉내를 켠 목 서버. 모듈을 매번 새로 실으므로 아웃박스·커서가 테스트마다 비어 있다."""
    server = _load("mock_server")
    roles, _note = server.load_roles(ROOT / "config")
    handler = type("Handler", (server.Handler,), {
        "roles": roles, "latency": 0.0, "plugin_events": True, "plugin_tick_every": None,
    })
    httpd, base = _serve(handler)
    try:
        yield base, server, handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def _finish_jobs(sdk, base, n):
    api = sdk.ControlCenter(base, "lcc_mock")
    return [api.generate("summarize", f"요약 {i}", wait=5).job_id for i in range(n)]


def _plugin(sdk, base, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return sdk.Plugin(base, "lcc_mock", **kwargs)


# ── ack 규칙 ─────────────────────────────────────────────────────────────────


def test_the_sdk_acks_only_after_every_handler_in_the_batch_succeeded(sdk, mock):
    base, server, _ = mock
    _finish_jobs(sdk, base, 3)
    seen = []

    report = _plugin(sdk, base).run(once=True, on_event=seen.append)

    assert [e.job_id for e in seen] and len(seen) == 3 and report.acked == 3
    assert server._event_cursor == 3, "처리를 마친 배치의 커서가 ack 됐다"
    last_path, last_body = server._plugin_calls[-1]
    assert (last_path, last_body.get("limit")) == ("/v1/plugin/events", 0), "확정은 limit 0 으로 — 한 건을 더 받지 않는다"
    assert _plugin(sdk, base).pull().events == [], "ack 뒤에는 같은 배치가 다시 오지 않는다"


def test_a_failing_handler_leaves_the_batch_unacked_and_it_is_redelivered(sdk, mock):
    base, server, _ = mock
    _finish_jobs(sdk, base, 3)

    def boom(event):
        if event.id == 2:
            raise RuntimeError("두 번째에서 죽는다")

    first = _plugin(sdk, base).run(once=True, on_event=boom)
    assert first.errors == 1 and first.acked == 0
    assert server._event_cursor == 0, "핸들러가 죽으면 ack 하지 않는다"

    seen = []
    second = _plugin(sdk, base).run(once=True, on_event=seen.append)
    assert [e.id for e in seen] == [1, 2, 3], "같은 배치가 그대로 다시 온다(at-least-once)"
    assert second.acked == 3 and server._event_cursor == 3


def test_the_sdk_sends_the_final_ack_when_the_backlog_is_drained(sdk, mock):
    base, server, _ = mock
    _finish_jobs(sdk, base, 5)
    seen = []

    report = _plugin(sdk, base).run(once=True, on_event=seen.append, batch=2)

    assert len(seen) == 5 and report.acked == 5 and server._event_cursor == 5
    pulls = [body for path, body in server._plugin_calls if path == "/v1/plugin/events"]
    assert [b.get("limit") for b in pulls] == [2, 0, 2, 0, 2, 0], "배치마다 처리 → ack(limit 0) 순서다"


# ── 무엇을 묻고 무엇을 안 묻나 ──────────────────────────────────────────────


def test_the_sdk_does_not_poll_what_the_plugin_did_not_declare(sdk, mock):
    base, server, handler = mock
    handler.plugin_tick_every = 60.0
    stop = threading.Event()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            stop.set()

    _plugin(sdk, base, sleep=sleep).run(on_tick=lambda t: None, stop=stop, jitter=0.0)
    paths = [path for path, _ in server._plugin_calls]
    assert "/v1/plugin/tick" in paths and "/v1/plugin/events" not in paths

    server._plugin_calls.clear()
    stop.clear(); sleeps.clear()
    _plugin(sdk, base, sleep=sleep).run(on_event=lambda e: None, stop=stop, jitter=0.0)
    paths = [path for path, _ in server._plugin_calls]
    assert "/v1/plugin/events" in paths and "/v1/plugin/tick" not in paths


def test_the_sdk_paces_ticks_by_next_run_at_within_the_clamp(sdk, mock):
    """다음 질문은 서버가 준 `next_run_at` 기준이다 — 상한·하한 안에서."""
    base, server, handler = mock
    handler.plugin_tick_every = 1000.0          # 다음 예정이 멀다 → 상한
    waited = []
    stop = threading.Event()

    def sleep(seconds):
        waited.append(seconds)
        stop.set()

    plugin = _plugin(sdk, base, sleep=sleep)
    plugin.run(on_tick=lambda t: None, stop=stop, min_interval=5, max_interval=120, jitter=0.0)
    assert sum(waited) == pytest.approx(120, abs=1.0)

    handler.plugin_tick_every = 2.0             # 다음 예정이 가깝다 → 하한
    server._next_tick_at = None
    waited.clear(); stop.clear()
    plugin.run(on_tick=lambda t: None, stop=stop, min_interval=5, max_interval=120, jitter=0.0)
    assert sum(waited) == pytest.approx(5, abs=0.6)


def test_the_sdk_stops_pulling_events_after_409_but_keeps_ticking(sdk, mock):
    base, server, handler = mock
    handler.plugin_events = False
    handler.plugin_tick_every = 30.0
    stop = threading.Event()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            stop.set()

    report = _plugin(sdk, base, sleep=sleep).run(
        on_tick=lambda t: None, on_event=lambda e: None, stop=stop, jitter=0.0,
    )
    paths = [path for path, _ in server._plugin_calls]
    assert paths.count("/v1/plugin/events") == 1, "409 뒤에는 이벤트를 다시 묻지 않는다"
    assert paths.count("/v1/plugin/tick") == 3 and report.stopped_by == "stop"


def test_the_sdk_waits_on_401_and_recovers_when_the_plugin_is_switched_back_on(sdk, mock):
    base, server, _ = mock
    _finish_jobs(sdk, base, 1)
    server._plugin_off = True
    stop = threading.Event()
    waited = []
    seen = []

    def sleep(seconds):
        waited.append(seconds)
        server._plugin_off = False               # 관리자가 다시 켰다
        if len(waited) >= 2:
            stop.set()

    report = _plugin(sdk, base, sleep=sleep).run(
        on_event=seen.append, stop=stop, min_interval=5, max_interval=90, jitter=0.0,
    )
    assert report.errors == 1 and waited[0] == pytest.approx(90, abs=0.6), "401 은 상한 간격으로 기다린다"
    assert [e.id for e in seen] == [1], "다시 켜지면 밀린 이벤트를 받는다"

    server._plugin_off = True
    exited = _plugin(sdk, base).run(on_event=seen.append, once=True, on_unauthorized="exit")
    assert exited.stopped_by == "unauthorized"


def test_once_makes_a_single_pass_for_cron_style_use(sdk, mock):
    base, server, handler = mock
    handler.plugin_tick_every = 3600.0
    server._next_tick_at = time.time() - 1      # 예정이 이미 지났다 — 다음은 한 시간 뒤
    _finish_jobs(sdk, base, 2)
    ticks, seen = [], []

    report = _plugin(sdk, base).run(once=True, on_tick=ticks.append, on_event=seen.append)

    assert report.stopped_by == "once" and report.ticks == 1 and len(seen) == 2
    assert ticks[0].due and ticks[0].scheduled_for is not None
    again = _plugin(sdk, base).run(once=True, on_tick=ticks.append, on_event=seen.append)
    assert again.ticks == 0 and len(seen) == 2, "한 번 준 예정은 다시 주지 않고, ack 한 이벤트는 다시 오지 않는다"


def test_the_sdk_needs_client_py_next_to_it(tmp_path):
    """두 파일이 한 벌이다 — 없으면 어디서 받는지 말하고 죽는다."""
    lone = tmp_path / "plugin.py"
    lone.write_text((ROOT / "clients" / "plugin.py").read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("lone_plugin", lone)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ImportError) as caught:
        spec.loader.exec_module(module)
    assert "/v1/client/client.py" in str(caught.value)


def test_plugin_sdk_parses_as_python_3_9():
    """플러그인은 호스트와 다른 기계에서 돈다 — 그 기계의 파이썬이 3.11 이라는 보장이 없다."""
    source = (ROOT / "clients" / "plugin.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))
    assert "import tomllib" not in source and "match " not in source.replace("match(", "")


# ── 실제 호스트 ──────────────────────────────────────────────────────────────


@pytest.fixture
def live_base_url(harness):
    """하네스 앱을 uvicorn 으로 소켓에 띄운다 — 표준 라이브러리 SDK 는 TestClient 를 못 쓴다."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(harness.app, host="127.0.0.1", port=0, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "uvicorn 이 5초 안에 뜨지 않았다"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


def _install_live(harness, signing_key):
    installed = plugins.install(
        harness.store, bundle(EVENTED, key=signing_key), actor="t", data_dir=harness.data_dir,
        trust_dir=harness.trust_dir, tenant_id="_platform", host_version=VERSION,
        now=harness.clock,
    )
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    return installed.token


def test_the_sdk_round_trips_against_the_real_host(
    sdk, live_base_url, harness, client, acme, signing_key, platform_tenant
):
    """설치 → 켜기 → 종결 하나 → SDK 가 받아 ack → 밀림 0. 목이 아니라 **진짜** 앞문이다."""
    token = _install_live(harness, signing_key)
    plugin = sdk.Plugin(live_base_url, token, name="live")
    seen = []

    assert plugin.run(once=True, on_event=seen.append).events == 0

    job_id = submit(client, acme["service"], prompt="실제 호스트 왕복")
    drive(harness)
    assert harness.store.get_job(TenantScope("acme"), job_id).status == "ok"

    report = plugin.run(once=True, on_event=seen.append)
    assert report.events == 1 and report.acked == 1
    [event] = seen
    assert event.job_id == job_id and event.ok and event.tenant == "acme" and event.role == "summarize"
    snapshot = plugins.snapshot(harness.store, data_dir=harness.data_dir)
    assert snapshot[0]["events_pending"] == 0 and snapshot[0]["last_event_at"] is not None


def test_the_sdk_reads_401_as_switched_off_on_the_real_host(
    sdk, live_base_url, harness, acme, signing_key, platform_tenant
):
    token = _install_live(harness, signing_key)
    plugin = sdk.Plugin(live_base_url, token, name="live")
    assert plugin.run(once=True, on_event=lambda e: None).stopped_by == "once"

    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t", now=harness.clock)
    off = plugin.run(once=True, on_event=lambda e: None, on_unauthorized="exit")
    assert off.stopped_by == "unauthorized"

    # 플러그인 토큰이 아닌 보통 서비스 토큰은 404 — 멈춘다.
    stranger = sdk.Plugin(live_base_url, acme["service"], name="stranger")
    assert stranger.run(once=True, on_event=lambda e: None).stopped_by == "not_a_plugin_token"


def test_a_failing_tick_handler_is_a_missed_run_not_an_unacked_batch(sdk, mock, caplog):
    """tick 은 배치가 아니다 — 클레임은 한 번뿐이라 핸들러가 죽으면 그 예정은 지나간다. 이벤트 풀은 계속된다."""
    import logging

    base, server, handler = mock
    handler.plugin_tick_every = 3600.0
    server._next_tick_at = time.time() - 1      # 예정이 이미 지났다 — 이번 바퀴에 due 다
    _finish_jobs(sdk, base, 1)
    seen = []

    def on_tick(_tick):
        raise RuntimeError("보고서 생성 실패")

    with caplog.at_level(logging.ERROR, logger="lcc.plugin.plugin"):
        report = _plugin(sdk, base).run(on_tick=on_tick, on_event=seen.append, once=True)

    assert (report.ticks, report.errors, report.events, report.acked) == (1, 1, 1, 1), report
    messages = [record.getMessage() for record in caplog.records]
    assert any("tick 핸들러 예외" in m and "지나갔다" in m for m in messages), messages
    assert not any("ack 하지 않았다" in m for m in messages), "tick 실패를 배치 미확정처럼 말하면 사람이 재전달을 기다린다"


def test_the_plugin_runtime_keeps_the_clients_default_timeout(sdk, mock):
    """런타임이 클라이언트 타임아웃을 줄이면 같은 클라이언트의 `generate(wait=30)` 이 서버보다 먼저 끊긴다 — 실제로 겪었다."""
    base, _server, _handler = mock
    client_module = sys.modules[[m for m in sys.modules if m in ("bundled_client", "lcc_client")][0]]
    assert _plugin(sdk, base).llm.timeout == client_module.DEFAULT_TIMEOUT
    assert _plugin(sdk, base, timeout=7.0).llm.timeout == 7.0
