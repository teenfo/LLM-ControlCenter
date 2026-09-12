"""목 서버 — **노드도 토큰도 없이** 통합 코드를 완성하기 위한 것.

설치처 개발자가 붙이기 어려우면 우회로를 만들고, 우회로는 가드도 비용도 감사도
지나지 않는다. 그게 이 제품에서 가장 비싼 실패다. 그래서 진입 장벽을 서비스가 치운다.

    python mock_server.py --port 8610
    LCC_URL=http://localhost:8610 LCC_TOKEN=any python client.py "요약할 내용"

플러그인을 만들 때는 트리거 흉내를 켠다 — `--plugin-events` 는 끝난 잡마다 `job.finished`
이벤트를 아웃박스에 쌓고(`POST /v1/plugin/events`, ack 커서·at-least-once·`limit: 0` 은 ack 만),
`--plugin-tick-every N` 은 N 초마다 한 번 `POST /v1/plugin/tick` 이 `due: true` 를 준다. 플래그가
없으면 진짜처럼 tick 은 `due: false`, events 는 409 `plugin_no_event_trigger` 다.

**역할 목록을 실제 설정에서 읽는다** — 손으로 적으면 진짜 서버와 역할 이름이
어긋나고, 어긋난 채로 통합이 끝나면 배포 당일에 404 를 만난다. 설정을 못 찾으면
그 사실을 말하고 최소 목록으로 뜬다(조용히 다른 이름을 쓰지 않는다).

표준 라이브러리만 쓴다. 오류 계약(코드 + retryable + 사람용 메시지)과 `wait`
동작, 가드 차단까지 진짜와 같은 모양으로 흉내 낸다 — **모양이 다르면 흉내 낼
이유가 없다.**
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    "summarize": {"kind": "generate", "lane": "interactive", "timeout_seconds": 120,
                  "max_prompt_chars": 200000, "has_default_system": True},
    "embed": {"kind": "embed", "lane": "batch", "timeout_seconds": 60,
              "max_prompt_chars": 8000, "has_default_system": False},
    "chat": {"kind": "chat", "lane": "interactive", "timeout_seconds": 180,
             "max_prompt_chars": 24000, "has_default_system": True},
}

#: 목에서 재현하는 가드 규칙. 진짜 규칙의 부분집합이며 **체크섬은 없다** —
#: 목의 일은 통합 코드가 422 를 다루게 만드는 것이지 판정 정확도가 아니다.
MOCK_GUARD = (
    ("kr_rrn", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
)

_jobs: dict[str, dict[str, Any]] = {}

#: 플러그인 트리거 흉내의 상태. 진짜의 `plugin_events` 아웃박스와 `event_cursor` 에 해당한다.
_outbox: list[dict[str, Any]] = []          # 종결 이벤트 — id 는 1부터 순서대로
_event_cursor = 0                           # ack 로만 앞으로 가는 커서
_next_tick_at: float | None = None          # 다음 예정 시각. 첫 질문 뒤 한 주기부터
_plugin_off = False                         # True 면 플러그인 경로가 401 — "끄면 선다" 를 흉내 낸다
_plugin_calls: list[tuple[str, dict[str, Any]]] = []   # 플러그인 경로 호출 기록 (테스트용)


def load_roles(config_dir: Path | None) -> tuple[dict[str, dict[str, Any]], str]:
    """실제 `roles.yaml` 에서 역할을 읽는다. 없으면 최소 목록 + 사유."""
    if config_dir is None:
        return dict(DEFAULT_ROLES), "설정 디렉터리를 주지 않아 기본 역할로 뜹니다"
    path = Path(config_dir) / "roles.yaml"
    if not path.exists():
        return dict(DEFAULT_ROLES), f"{path} 가 없어 기본 역할로 뜹니다"

    # PyYAML 없이도 뜬다 — 목 서버는 의존성 없이 돌아야 한다.
    roles: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace() and line.rstrip().endswith(":"):
            name = line.rstrip()[:-1].strip()
            current = None if name.startswith("_") else name
            if current:
                roles[current] = {
                    "kind": "generate", "lane": "interactive",
                    "timeout_seconds": 120, "max_prompt_chars": 200000,
                    "has_default_system": False,
                }
            continue
        if current and ":" in line:
            key, _, value = line.strip().partition(":")
            value = value.split("#")[0].strip().strip("\"'")
            if key == "kind":
                roles[current]["kind"] = value
            elif key == "lane":
                roles[current]["lane"] = value
            elif key == "timeout" and value.isdigit():
                roles[current]["timeout_seconds"] = int(value)
            elif key == "max_prompt_chars" and value.isdigit():
                roles[current]["max_prompt_chars"] = int(value)
            elif key == "system":
                roles[current]["has_default_system"] = True
    return (roles or dict(DEFAULT_ROLES)), f"{path} 에서 역할 {len(roles)}개를 읽었습니다"


class Handler(BaseHTTPRequestHandler):
    roles: dict[str, dict[str, Any]] = dict(DEFAULT_ROLES)
    latency: float = 0.0
    #: 플러그인 트리거 흉내. 없으면 진짜처럼 tick 은 `due: false`, events 는 409 다.
    plugin_tick_every: float | None = None
    plugin_events: bool = False

    server_version = "llmcc-mock"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("  mock  " + (fmt % args) + "\n")

    # -- 응답 -----------------------------------------------------------------

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if status == 200 and isinstance(body, dict) and body.get("status") == "pending":
            self.send_header("Retry-After", str(int(body.get("retry_after") or 2)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, code: str, message: str, **params: Any) -> None:
        # 진짜와 같은 모양이다 — 기계용 코드와 사람용 메시지를 둘 다 싣는다.
        self._send(status, {
            "code": code, "message": message,
            "retryable": status in (429, 503, 504),
            **params,
        })

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer ") or not header[7:].strip():
            self._error(401, "unauthorized", "인증 토큰이 없거나 올바르지 않습니다.")
            return False
        return True

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            self._error(400, "invalid_json", "요청 본문이 올바른 JSON이 아닙니다.")
            return None
        return parsed if isinstance(parsed, dict) else {}

    # -- 라우팅 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/healthz":
            self._send(200, {"ok": True, "version": "mock", "api": "v1"})
            return
        if not self._authorized():
            return
        if path == "/v1/roles":
            self._send(200, {
                "roles": [{"name": n, **spec} for n, spec in sorted(self.roles.items())],
                "limits": {"rate_limit_service_per_min": 60, "status_poll_per_min": 600},
            })
            return
        if path == "/v1/status":
            self._send(200, {
                "lanes": {"interactive": {"running": 0, "queued": 0, "max_concurrent": 2}},
                "nodes": {"total": 1, "healthy": 1, "draining": 0},
                "single_homed_roles": {}, "airgap": False,
            })
            return
        if path == "/v1/meta":
            self._send(200, self._meta())
            return
        if path.startswith("/v1/jobs/"):
            job = _jobs.get(path.rsplit("/", 1)[-1])
            if job is None:
                self._error(404, "job_not_found", "작업을 찾을 수 없습니다.")
                return
            self._send(200, self._settle(job))
            return
        self._error(404, "not_found", "요청한 리소스를 찾을 수 없습니다.")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._authorized():
            return
        body = self._body()
        if body is None:
            return

        if path == "/v1/generate":
            self._generate(body)
        elif path == "/v1/chat":
            self._chat(body)
        elif path == "/v1/embed":
            self._embed(body)
        elif path == "/v1/plugin/tick":
            self._plugin_tick(body)
        elif path == "/v1/plugin/events":
            self._plugin_events(body)
        else:
            self._error(404, "not_found", "요청한 리소스를 찾을 수 없습니다.")

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        job = _jobs.get(self.path.rsplit("/", 1)[-1])
        if job is None:
            self._error(404, "job_not_found", "작업을 찾을 수 없습니다.")
            return
        job["status"] = "cancelled"
        self._emit_finish(job)
        self._send(200, self._settle(job))

    # -- 동작 -----------------------------------------------------------------

    def _generate(self, body: dict[str, Any]) -> None:
        role = str(body.get("role") or "")
        prompt = body.get("prompt")
        if not role:
            self._error(400, "missing_field", "필수 항목이 없습니다: role", field="role")
            return
        if not prompt:
            self._error(400, "missing_field", "필수 항목이 없습니다: prompt", field="prompt")
            return

        spec = self.roles.get(role)
        if spec is None:
            self._error(404, "unknown_role", f"알 수 없는 역할입니다: {role}", role=role)
            return
        if spec["kind"] != "generate":
            self._error(400, "wrong_kind", f"역할 '{role}'은(는) 이 엔드포인트로 호출할 수 없습니다.",
                        role=role, kind=spec["kind"])
            return
        if len(prompt) > spec["max_prompt_chars"]:
            self._error(413, "payload_too_large", "입력이 한도를 초과했습니다.",
                        size=len(prompt), limit=spec["max_prompt_chars"])
            return

        hits = [rule for rule, pattern in MOCK_GUARD if pattern.search(prompt)]
        if hits:
            # **원문은 응답에 실리지 않는다.** 규칙 ID 만 나간다.
            self._error(422, "guard_blocked",
                        f"민감 정보가 감지되어 요청이 차단되었습니다 (규칙: {', '.join(hits)}).",
                        rules=", ".join(hits))
            return

        self._enqueue(role, prompt, body)

    def _chat(self, body: dict[str, Any]) -> None:
        """대화 — 턴 배열을 받는다. 진짜 서버와 같은 모양으로 거절하고, 답은 마지막 사용자 턴의 해시다."""
        role = str(body.get("role") or "")
        messages = body.get("messages")
        if not role:
            self._error(400, "missing_field", "필수 항목이 없습니다: role", field="role")
            return
        if messages is None:
            self._error(400, "missing_field", "필수 항목이 없습니다: messages", field="messages")
            return

        spec = self.roles.get(role)
        if spec is None:
            self._error(404, "unknown_role", f"알 수 없는 역할입니다: {role}", role=role)
            return
        if spec["kind"] != "chat":
            self._error(400, "wrong_kind", f"역할 '{role}'은(는) 이 엔드포인트로 호출할 수 없습니다.",
                        role=role, kind=spec["kind"])
            return
        turns = messages if isinstance(messages, list) else None
        if not turns:
            self._error(400, "empty_input", "입력이 비어 있습니다.")
            return
        well_formed = all(
            isinstance(t, dict) and t.get("role") in ("user", "assistant")
            and isinstance(t.get("content"), str) and t["content"].strip()
            for t in turns
        )
        if not well_formed or turns[0]["role"] != "user" or turns[-1]["role"] != "user":
            self._error(400, "invalid_field", "항목 'messages'의 값이 올바르지 않습니다.", field="messages")
            return
        joined = "\n".join(t["content"] for t in turns)
        if len(joined) > spec["max_prompt_chars"]:
            self._error(413, "payload_too_large", "입력이 한도를 초과했습니다.",
                        size=len(joined), limit=spec["max_prompt_chars"])
            return
        hits = [rule for rule, pattern in MOCK_GUARD if pattern.search(joined)]
        if hits:
            self._error(422, "guard_blocked",
                        f"민감 정보가 감지되어 요청이 차단되었습니다 (규칙: {', '.join(hits)}).",
                        rules=", ".join(hits))
            return
        self._enqueue(role, turns[-1]["content"], body)

    def _enqueue(self, role: str, seed: str, body: dict[str, Any]) -> None:
        """가짜 잡을 만들고 `wait` 만큼 기다렸다가 돌려준다 — generate 와 chat 이 같은 꼬리를 쓴다."""
        job_id = uuid.uuid4().hex[:16]
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        end_user = body.get("end_user")
        _jobs[job_id] = {
            "job_id": job_id, "role": role, "attempts": 0, "guard_actions": {},
            "status": "pending", "ready_at": time.time() + self.latency,
            "response": f"[mock:{role}] {digest}",
            "model": "mock-model", "node": "mock-node", "tier": "internal",
            # 플러그인 이벤트 흉내가 쓰는 것 — 응답 모양에는 안 실린다(`_settle` 이 뺀다).
            "_kind": "chat" if "messages" in body else "generate",
            "_prompt": (
                json.dumps({"messages": body.get("messages")}, ensure_ascii=False)
                if "messages" in body else str(body.get("prompt") or "")
            ),
            "_end_user": hashlib.sha256(str(end_user).encode("utf-8")).hexdigest()[:32] if end_user else None,
            "_created_at": time.time(),
        }
        wait = float(body.get("wait", 30) or 0)
        deadline = time.time() + min(wait, 300.0)
        while time.time() < deadline and _jobs[job_id]["ready_at"] > time.time():
            time.sleep(0.05)
        self._send(200, self._settle(_jobs[job_id]))

    def _embed(self, body: dict[str, Any]) -> None:
        role = str(body.get("role") or "")
        spec = self.roles.get(role)
        if spec is None:
            self._error(404, "unknown_role", f"알 수 없는 역할입니다: {role}", role=role)
            return
        if spec["kind"] != "embed":
            self._error(400, "wrong_kind", f"역할 '{role}'은(는) 이 엔드포인트로 호출할 수 없습니다.",
                        role=role, kind=spec["kind"])
            return

        raw = body.get("input")
        inputs = [raw] if isinstance(raw, str) else list(raw or [])
        if not inputs:
            self._error(400, "empty_input", "입력이 비어 있습니다.")
            return

        vectors = []
        for text in inputs:
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            vectors.append([digest[i] / 255.0 for i in range(8)])
        self._send(200, {
            "job_id": uuid.uuid4().hex[:16], "model": "mock-embed", "node": "mock-node",
            "tier": "internal", "vectors": vectors, "input_tokens": sum(len(t) for t in inputs) // 4,
            "guard_actions": {},
        })

    def _settle(self, job: dict[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in job.items() if not k.startswith("_")}
        ready_at = result.pop("ready_at", 0)
        if result["status"] == "pending":
            if time.time() >= ready_at:
                result["status"] = "ok"
                job["status"] = "ok"
                self._emit_finish(job)
            else:
                result.pop("response", None)
                result["queue_position"] = 0
                result["retry_after"] = 2.0
        return result

    # -- 플러그인 트리거 흉내 ---------------------------------------------------

    def _emit_finish(self, job: dict[str, Any]) -> None:
        """종결 한 건을 아웃박스에 쌓는다 — 진짜의 `event_payload` 와 같은 칸이다.

        진짜는 DB 트리거가 종결과 같은 트랜잭션에서 쌓고, 플러그인이 만든 잡의 종결은 내주지
        않는다(재귀 방지). 목은 "종결 하나 = 이벤트 하나" 만 흉내 낸다 — 통합 코드가 배치·ack·
        at-least-once 를 다루게 만드는 것이 목적이지 판정 정확도가 아니다.
        """
        if not self.plugin_events or job.get("_emitted"):
            return
        job["_emitted"] = True
        finished = time.time()
        _outbox.append({
            "id": len(_outbox) + 1, "kind": "job.finished", "ts": finished,
            "job_id": job["job_id"], "tenant": "mock", "service": "mock-web",
            "end_user": job.get("_end_user"), "job_kind": job.get("_kind", "generate"),
            "role": job["role"], "route": None, "status": job["status"],
            "error": None, "error_code": None,
            "model": job["model"], "node": job["node"], "boundary": "internal",
            "prompt": job.get("_prompt"), "system": None,
            "output": job["response"] if job["status"] == "ok" else None,
            "usage": {"input_tokens": len(job.get("_prompt") or "") // 4,
                      "output_tokens": len(job["response"]) // 4, "cost_usd": 0.0},
            "created_at": job.get("_created_at"), "started_at": job.get("_created_at"),
            "finished_at": finished,
        })

    def _plugin_gate(self, path: str, body: dict[str, Any]) -> bool:
        _plugin_calls.append((path, dict(body)))
        if _plugin_off:
            # 끄면 선다 — 진짜는 `active_service` 가 401 을 낸다.
            self._error(401, "unauthorized", "이 서비스는 비활성 상태입니다.")
            return False
        return True

    def _plugin_tick(self, body: dict[str, Any]) -> None:
        global _next_tick_at
        if not self._plugin_gate("/v1/plugin/tick", body):
            return
        every = self.plugin_tick_every
        if not every:
            self._send(200, {"id": "mock.plugin", "due": False, "scheduled_for": None, "next_run_at": None})
            return
        now = time.time()
        if _next_tick_at is None:
            _next_tick_at = now + every
        if now >= _next_tick_at:
            scheduled = _next_tick_at
            # 밀린 것을 몰아 돌리지 않는다 — 다음 예정은 지금 기준이다(진짜와 같다).
            _next_tick_at = now + every
            self._send(200, {"id": "mock.plugin", "due": True, "scheduled_for": scheduled, "next_run_at": _next_tick_at})
            return
        self._send(200, {"id": "mock.plugin", "due": False, "scheduled_for": None, "next_run_at": _next_tick_at})

    def _plugin_events(self, body: dict[str, Any]) -> None:
        global _event_cursor
        if not self._plugin_gate("/v1/plugin/events", body):
            return
        if not self.plugin_events:
            self._error(409, "plugin_no_event_trigger", "이 플러그인은 이벤트 트리거를 선언하지 않았습니다.")
            return
        ack = body.get("ack")
        if ack is not None and (isinstance(ack, bool) or not isinstance(ack, int) or ack < 0):
            self._error(400, "invalid_field", "필드 값이 올바르지 않습니다: ack", field="ack")
            return
        limit = body.get("limit", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            self._error(400, "invalid_field", "필드 값이 올바르지 않습니다: limit", field="limit")
            return
        if ack is not None:
            # 앞으로만, 있는 것까지만 — 진짜의 CAS 와 같은 성질이다.
            _event_cursor = max(_event_cursor, min(int(ack), len(_outbox)))
        batch = _outbox[_event_cursor:_event_cursor + min(limit, 200)]
        cursor = batch[-1]["id"] if batch else _event_cursor
        self._send(200, {
            "id": "mock.plugin", "events": batch, "cursor": cursor,
            "pending": len(_outbox) - cursor,
        })

    def _meta(self) -> dict[str, Any]:
        return {
            "product": "llm-controlcenter", "version": "mock", "api_version": "v1",
            "roles": [{"name": n, **spec} for n, spec in sorted(self.roles.items())],
            "wait": {"default_seconds": 30.0, "max_seconds": 300.0},
            "error_handling": {
                "branch_on": ["http_status", "retryable"],
                "never_branch_on": ["message"],
                "error_codes": [
                    {"code": "unauthorized", "status": 401, "retryable": False},
                    {"code": "unknown_role", "status": 404, "retryable": False},
                    {"code": "guard_blocked", "status": 422, "retryable": False},
                    {"code": "rate_limited", "status": 429, "retryable": True},
                    {"code": "no_placement", "status": 503, "retryable": True},
                ],
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM ControlCenter 목 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8610)
    parser.add_argument("--config", help="roles.yaml 이 있는 설정 디렉터리")
    parser.add_argument("--latency", type=float, default=0.0,
                        help="완료까지의 지연(초). 폴링 경로를 시험할 때 쓴다")
    parser.add_argument("--plugin-events", action="store_true",
                        help="끝난 잡마다 job.finished 이벤트를 쌓는다 — POST /v1/plugin/events 로 받는다")
    parser.add_argument("--plugin-tick-every", type=float, default=None, metavar="SECONDS",
                        help="N 초마다 한 번 POST /v1/plugin/tick 이 due: true 를 준다")
    args = parser.parse_args(argv)

    roles, note = load_roles(Path(args.config) if args.config else None)
    Handler.roles = roles
    Handler.latency = args.latency
    Handler.plugin_events = bool(args.plugin_events)
    Handler.plugin_tick_every = args.plugin_tick_every
    if args.plugin_events or args.plugin_tick_every:
        print(f"  플러그인 트리거 흉내: events={'on' if args.plugin_events else 'off'} "
              f"tick_every={args.plugin_tick_every or '-'}", file=sys.stderr)

    print(f"  {note}", file=sys.stderr)
    print(f"  역할: {', '.join(sorted(roles))}", file=sys.stderr)
    print(f"  목 서버 http://{args.host}:{args.port} — 아무 토큰이나 통과합니다.",
          file=sys.stderr)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
