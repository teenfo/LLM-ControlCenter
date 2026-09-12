"""LLM ControlCenter — 플러그인 런타임 (단일 파일).

`client.py` 를 같은 디렉터리에 두고 이 파일을 복사하면 플러그인의 골격이 끝난다.
둘 다 서버의 `GET /v1/client/` 에서 내려받는다. **표준 라이브러리만 쓴다.**

    from plugin import Plugin

    plugin = Plugin.from_env()                       # LCC_URL · LCC_TOKEN
    plugin.run(on_event=lambda event: print(event.role, event.status, event.latency))

### 이 파일이 대신 지켜 주는 계약

**① 처리가 끝난 배치만 ack 한다.** 이벤트는 at-least-once 다 — ack 하기 전에 죽으면 같은 배치를
다시 받는다. 핸들러가 예외를 던지면 ack 하지 않으므로 그 배치는 다음에 다시 온다. 이벤트 `id` 로
중복을 거르는 것은 플러그인 몫이다.

**② 선언하지 않은 것은 묻지 않는다.** `on_tick` 이 없으면 tick 을 안 묻고, `on_event` 가 없으면
이벤트를 안 당긴다. 이벤트 구독이 없는 플러그인(409)은 이벤트 풀만 멈추고 tick 은 계속한다.

**③ 401 은 "꺼졌다" 는 뜻이다.** 관리자가 플러그인을 끄거나 토큰을 회전·폐기하면 모든 경로가
401 이 된다. 기본은 상한 간격으로 계속 묻는다(다시 켜면 살아난다) — `on_unauthorized="exit"` 면
멈춘다. 404 `not_found` 는 이 토큰이 플러그인 것이 아니라는 뜻이라 즉시 멈춘다.

**④ 서버가 준 시각을 따른다.** tick 의 `next_run_at` 으로 다음 질문 시각을 잡고(하한·상한 안에서),
429 는 `retry_after` 를, 네트워크 오류는 지수 백오프를 지킨다. 고정 간격으로 때리지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "Plugin", "Tick", "Event", "Pull", "RunReport",
    "ControlCenter", "ControlCenterError", "Blocked", "RateLimited",
]


def _client_module():
    """옆의 `client.py` 를 쓴다 — HTTP·오류 계약을 두 벌로 두지 않기 위해서다.

    이미 같은 파일이 실려 있으면(테스트가 다른 이름으로 올렸거나, 플러그인이 먼저 import 했거나)
    그 모듈을 그대로 쓴다 — 예외 클래스가 두 벌이 되면 `except ControlCenterError` 가 안 잡힌다.
    """
    here = Path(__file__).resolve().with_name("client.py")
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            if Path(file).resolve() == here and hasattr(module, "ControlCenter"):
                return module
        except (OSError, ValueError):
            continue
    if not here.is_file():
        raise ImportError(
            "client.py 를 plugin.py 와 같은 디렉터리에 두세요 — 서버의 GET /v1/client/client.py 에서 내려받습니다"
        )
    spec = importlib.util.spec_from_file_location("lcc_client", here)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module          # dataclass 는 sys.modules 등록 뒤에 실행돼야 한다
    spec.loader.exec_module(module)
    return module


_client = _client_module()
ControlCenter = _client.ControlCenter
ControlCenterError = _client.ControlCenterError
Blocked = _client.Blocked
RateLimited = _client.RateLimited


# ── 서버가 주는 모양 ────────────────────────────────────────────────────────


@dataclass
class Tick:
    """`POST /v1/plugin/tick` 의 답. `due` 면 이번 실행은 **이 프로세스 것**이다(복제본이 여럿이어도 한 번)."""

    due: bool
    #: 가져갔을 때, 그 실행이 예정돼 있던 시각. 얼마나 늦었는지는 이것으로 안다.
    scheduled_for: Optional[float] = None
    #: 다음 예정. 폴링 간격의 근거다.
    next_run_at: Optional[float] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "Tick":
        return cls(
            due=bool(body.get("due")), scheduled_for=body.get("scheduled_for"),
            next_run_at=body.get("next_run_at"), raw=dict(body),
        )

    @property
    def late_by(self) -> Optional[float]:
        """예정보다 몇 초 늦게 가져갔나. 밀린 것을 따라잡을지는 플러그인이 정한다."""
        if self.scheduled_for is None:
            return None
        return max(0.0, time.time() - float(self.scheduled_for))


@dataclass
class Event:
    """`job.finished` 한 건 — **모델이 본 것**(마스킹 뒤)과 **나간 응답**(출력 가드 뒤)이다. 원문은 없다."""

    id: int
    kind: str
    ts: float
    job_id: str
    tenant: Optional[str]
    service: Optional[str]
    end_user: Optional[str]
    job_kind: Optional[str]
    role: Optional[str]
    route: Any
    status: str
    error: Optional[str]
    error_code: Optional[str]
    model: Optional[str]
    node: Optional[str]
    boundary: Optional[str]
    prompt: Optional[str]
    system: Optional[str]
    output: Optional[str]
    usage: Mapping[str, Any]
    created_at: Optional[float]
    started_at: Optional[float]
    finished_at: Optional[float]
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "Event":
        return cls(
            id=int(body.get("id", 0)), kind=str(body.get("kind", "")), ts=float(body.get("ts") or 0.0),
            job_id=str(body.get("job_id", "")), tenant=body.get("tenant"), service=body.get("service"),
            end_user=body.get("end_user"), job_kind=body.get("job_kind"), role=body.get("role"),
            route=body.get("route"), status=str(body.get("status", "")), error=body.get("error"),
            error_code=body.get("error_code"), model=body.get("model"), node=body.get("node"),
            boundary=body.get("boundary"), prompt=body.get("prompt"), system=body.get("system"),
            output=body.get("output"), usage=dict(body.get("usage") or {}),
            created_at=body.get("created_at"), started_at=body.get("started_at"),
            finished_at=body.get("finished_at"), raw=dict(body),
        )

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def latency(self) -> Optional[float]:
        """노드에서 걸린 시간(초). 노드에 안 간 잡(큐 취소·배치 실패)은 `None`."""
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, float(self.finished_at) - float(self.started_at))

    @property
    def messages(self) -> Optional[list]:
        """대화 잡이면 저장된 턴 배열, 아니면 `None`. 프롬프트는 마스킹본이다."""
        if self.job_kind != "chat" or not self.prompt:
            return None
        try:
            return list(json.loads(self.prompt).get("messages") or [])
        except (ValueError, AttributeError):
            return None


@dataclass
class Pull:
    """한 번의 풀. `cursor` 는 이번에 **훑은** 마지막 이벤트 — 처리를 마친 뒤 그대로 ack 한다."""

    events: list
    cursor: int
    pending: int


@dataclass
class RunReport:
    """`run()` 이 끝났을 때의 집계. 테스트와 진단이 읽는다."""

    ticks: int = 0
    events: int = 0
    acked: int = 0
    errors: int = 0
    stopped_by: Optional[str] = None


# ── 런타임 ───────────────────────────────────────────────────────────────────


class _HandlerFailed(Exception):
    """플러그인 코드(핸들러)가 던진 예외 — API 오류와 갈라서 다룬다."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(repr(cause))
        self.cause = cause


def _call(handler: Callable[[Any], Any], argument: Any) -> None:
    try:
        handler(argument)
    except Exception as exc:  # noqa: BLE001 — 핸들러가 무엇을 던지든 루프는 살아야 한다
        raise _HandlerFailed(exc) from exc


class Plugin:
    """플러그인 하나의 런타임. 같은 토큰으로 `llm`(생성·대화·임베딩)도 부른다."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        locale: Optional[str] = None,
        name: str = "plugin",
        log: Optional[logging.Logger] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.llm = ControlCenter(base_url, token, timeout=timeout, locale=locale)
        self.name = name
        self.log = log or logging.getLogger(f"lcc.plugin.{name}")
        # 잠은 주입할 수 있다 — 테스트가 실제로 잠들지 않게. 기본은 `stop` 이 서면 바로 깨는 대기다.
        self._sleep = sleep
        self._acked: Optional[int] = None

    @classmethod
    def from_env(cls, prefix: str = "LCC", **kwargs: Any) -> "Plugin":
        """`LCC_URL` · `LCC_TOKEN` 에서 만든다. 토큰은 설치 응답이나 토큰 회전에서 **한 번만** 나온다."""
        url = os.environ.get(f"{prefix}_URL", "")
        token = os.environ.get(f"{prefix}_TOKEN", "")
        if not url or not token:
            raise RuntimeError(f"{prefix}_URL 과 {prefix}_TOKEN 이 필요하다 — 플러그인 설치 응답의 토큰을 넣는다")
        return cls(url, token, **kwargs)

    # -- 플러그인 경로 (자기 토큰으로) ------------------------------------------

    def tick(self) -> Tick:
        """"지금 내 차례인가" — 예정이 지났으면 복제본이 여럿이어도 **한 번만** `due` 다."""
        return Tick.from_body(self.llm._request("POST", "/v1/plugin/tick", {}))

    def pull(self, *, ack: Optional[int] = None, limit: int = 50) -> Pull:
        """못 본 종결을 받는다. `ack` 는 처리를 **마친** 배치의 `cursor` 다. `limit=0` 은 ack 만이다."""
        body: dict = {"limit": int(limit)}
        if ack is not None:
            body["ack"] = int(ack)
        data = self.llm._request("POST", "/v1/plugin/events", body)
        return Pull(
            events=[Event.from_body(e) for e in data.get("events") or []],
            cursor=int(data.get("cursor") or 0),
            pending=int(data.get("pending") or 0),
        )

    def ack(self, cursor: int) -> Pull:
        """배치를 확정한다 — 한 건도 더 받지 않는다(`limit: 0`)."""
        return self.pull(ack=cursor, limit=0)

    # -- 루프 -------------------------------------------------------------------

    def run(
        self,
        *,
        on_tick: Optional[Callable[[Tick], Any]] = None,
        on_event: Optional[Callable[[Event], Any]] = None,
        min_interval: float = 5.0,
        max_interval: float = 300.0,
        batch: int = 50,
        once: bool = False,
        stop: Optional[threading.Event] = None,
        on_unauthorized: str = "wait",
        jitter: float = 0.1,
    ) -> RunReport:
        """멈출 때까지(또는 `once` 면 한 바퀴) 돈다.

        - `on_tick(tick)` 은 `due` 일 때만 불린다. 예외를 던지면 그 실행은 지나간다 — 클레임은 한 번뿐이다
        - `on_event(event)` 는 이벤트마다 불린다. **배치의 모든 핸들러가 성공한 뒤에만** ack 한다
        - `stop` 은 `threading.Event`. 없으면 만들고, 메인 스레드면 SIGTERM/SIGINT 로도 세운다
        - `on_unauthorized` 는 `"wait"`(기본 — 상한 간격으로 계속) 또는 `"exit"`
        """
        if on_tick is None and on_event is None:
            raise ValueError("on_tick 이나 on_event 중 하나는 있어야 한다")
        if on_unauthorized not in ("wait", "exit"):
            raise ValueError("on_unauthorized 는 'wait' 또는 'exit' 이다")
        lo = max(1.0, float(min_interval))
        hi = max(lo, float(max_interval))
        report = RunReport()
        stop = stop or threading.Event()
        wait = self._sleep or (lambda seconds: stop.wait(seconds))
        restore = self._install_signal_handlers(stop)
        want_events = on_event is not None
        backoff = lo
        try:
            while not stop.is_set():
                delay = hi
                try:
                    if on_tick is not None:
                        tick = self.tick()
                        if tick.due:
                            report.ticks += 1
                            _call(on_tick, tick)
                        delay = min(delay, self._delay_until(tick.next_run_at, lo, hi))
                    if want_events:
                        subscribed = self._drain(on_event, batch, report)
                        if subscribed is False:
                            want_events = False
                            self.log.info("이벤트 구독이 없는 플러그인이다 — 이벤트 풀을 멈춘다")
                        else:
                            delay = min(delay, lo)
                    backoff = lo
                except ControlCenterError as exc:
                    report.errors += 1
                    if exc.status == 401:
                        if on_unauthorized == "exit":
                            report.stopped_by = "unauthorized"
                            break
                        self.log.warning("401 — 플러그인이 꺼졌거나 토큰이 죽었다. %.0f초 뒤 다시 묻는다", hi)
                        delay = hi
                    elif exc.status == 404 and exc.code == "not_found":
                        self.log.error("이 토큰은 플러그인 것이 아니다(404 not_found) — 멈춘다")
                        report.stopped_by = "not_a_plugin_token"
                        break
                    elif exc.status == 429:
                        retry_after = getattr(exc, "retry_after", None)
                        delay = float(retry_after) if retry_after else lo
                    else:
                        self.log.warning("API 오류 %s %s — %.0f초 뒤 재시도", exc.status, exc.code, backoff)
                        delay = backoff
                        backoff = min(backoff * 2, hi)
                except _HandlerFailed as failed:
                    # 핸들러의 예외. ack 하지 않았으므로 그 배치는 다시 온다.
                    report.errors += 1
                    self.log.error("핸들러 예외 %r — 이 배치는 ack 하지 않았다. %.0f초 뒤 다시 받는다",
                                   failed.cause, backoff)
                    delay = backoff
                    backoff = min(backoff * 2, hi)
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    report.errors += 1
                    self.log.warning("연결 실패 %s — %.0f초 뒤 재시도", exc, backoff)
                    delay = backoff
                    backoff = min(backoff * 2, hi)
                if once:
                    report.stopped_by = report.stopped_by or "once"
                    break
                if jitter:
                    delay += random.uniform(0.0, delay * float(jitter))
                if delay > 0 and not stop.is_set():
                    wait(delay)
        finally:
            restore()
        if report.stopped_by is None:
            report.stopped_by = "stop"
        return report

    def _drain(self, on_event: Callable[[Event], Any], batch: int, report: RunReport) -> bool:
        """pending 이 0 이 될 때까지 받아 처리하고 ack 한다. 구독이 없으면(409) `False`."""
        for _ in range(1000):
            try:
                pull = self.pull(limit=batch)
            except ControlCenterError as exc:
                if exc.status == 409 and exc.code == "plugin_no_event_trigger":
                    return False
                raise
            for event in pull.events:
                report.events += 1
                _call(on_event, event)
            # 빈 배치라도 커서는 전진할 수 있다(내주지 않은 종결을 훑은 것) — 그때도 확정한다.
            moved = self._acked is not None and pull.cursor > self._acked
            if pull.events or pull.pending or moved:
                self.ack(pull.cursor)
                report.acked += len(pull.events)
            self._acked = pull.cursor
            if pull.pending <= 0:
                return True
        return True

    def _delay_until(self, next_run_at: Optional[float], lo: float, hi: float) -> float:
        if next_run_at is None:
            return hi
        return max(lo, min(hi, float(next_run_at) - time.time()))

    def _install_signal_handlers(self, stop: threading.Event) -> Callable[[], None]:
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, lambda *_args: stop.set())
            except (ValueError, OSError):
                pass

        def restore() -> None:
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

        return restore


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="LLM ControlCenter 플러그인 런타임 — 진단")
    parser.add_argument("--base-url", default=os.environ.get("LCC_URL", "http://localhost:8610"))
    parser.add_argument("--token", default=os.environ.get("LCC_TOKEN", ""))
    parser.add_argument("--tick", action="store_true", help="tick 한 번을 묻고 답을 찍는다")
    parser.add_argument("--events", action="store_true", help="이벤트 한 배치를 받아 찍는다 (ack 하지 않는다)")
    parser.add_argument("--once", action="store_true", help="한 바퀴 돈다 — tick 과 이벤트를 찍고 ack 한다")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    me = Plugin(args.base_url, args.token, name="diag")
    if args.tick:
        print(json.dumps(me.tick().raw, ensure_ascii=False, indent=2))
    if args.events:
        got = me.pull()
        print(json.dumps({"cursor": got.cursor, "pending": got.pending,
                          "events": [e.raw for e in got.events]}, ensure_ascii=False, indent=2))
    if args.once:
        result = me.run(
            once=True,
            on_tick=lambda t: print("tick due — scheduled_for", t.scheduled_for),
            on_event=lambda e: print("event", e.id, e.role, e.status, e.job_id),
        )
        print(result)
    if not (args.tick or args.events or args.once):
        parser.print_help()
