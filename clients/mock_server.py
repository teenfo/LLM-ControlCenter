"""목 서버 — **노드도 토큰도 없이** 통합 코드를 완성하기 위한 것.

설치처 개발자가 붙이기 어려우면 우회로를 만들고, 우회로는 가드도 비용도 감사도
지나지 않는다. 그게 이 제품에서 가장 비싼 실패다. 그래서 진입 장벽을 서비스가 치운다.

    python mock_server.py --port 8610
    LCC_URL=http://localhost:8610 LCC_TOKEN=any python client.py "요약할 내용"

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
}

#: 목에서 재현하는 가드 규칙. 진짜 규칙의 부분집합이며 **체크섬은 없다** —
#: 목의 일은 통합 코드가 422 를 다루게 만드는 것이지 판정 정확도가 아니다.
MOCK_GUARD = (
    ("kr_rrn", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
)

_jobs: dict[str, dict[str, Any]] = {}


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
        elif path == "/v1/embed":
            self._embed(body)
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

        job_id = uuid.uuid4().hex[:16]
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        _jobs[job_id] = {
            "job_id": job_id, "role": role, "attempts": 0, "guard_actions": {},
            "status": "pending", "ready_at": time.time() + self.latency,
            "response": f"[mock:{role}] {digest}",
            "model": "mock-model", "node": "mock-node", "tier": "internal",
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
        result = dict(job)
        ready_at = result.pop("ready_at", 0)
        if result["status"] == "pending":
            if time.time() >= ready_at:
                result["status"] = "ok"
                job["status"] = "ok"
            else:
                result.pop("response", None)
                result["queue_position"] = 0
                result["retry_after"] = 2.0
        return result

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
    args = parser.parse_args(argv)

    roles, note = load_roles(Path(args.config) if args.config else None)
    Handler.roles = roles
    Handler.latency = args.latency

    print(f"  {note}", file=sys.stderr)
    print(f"  역할: {', '.join(sorted(roles))}", file=sys.stderr)
    print(f"  목 서버 http://{args.host}:{args.port} — 아무 토큰이나 통과합니다.",
          file=sys.stderr)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
