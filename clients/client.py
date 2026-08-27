"""LLM ControlCenter — 단일 파일 파이썬 클라이언트.

이 파일 하나를 복사해 넣으면 통합이 끝난다. **표준 라이브러리만 쓴다** — 설치처가
의존성 승인 절차를 밟아야 한다면 그것 자체가 우회로를 만드는 이유가 되기 때문이다.

    from client import ControlCenter

    cc = ControlCenter("https://llm.example.com", token="lcc_...")
    print(cc.generate("summarize", "요약할 내용", end_user="u_8f3a91").text)

### 알아 둘 것 세 가지

**① 역할 이름이 계약이고 모델은 정책이다.** 모델명을 하드코딩하지 않는다 —
어느 모델로 돌지·어느 기계에서 돌지·돈이 드는지는 관리자가 역할 뒤에서 바꾼다.

**② 분기는 HTTP 상태와 `retryable` 로 한다.** `message` 는 로케일에 따라 바뀌므로
한국어 메시지로 분기한 코드는 영어 환경에서 조용히 실패한다.

**③ `wait` 를 쓰면 폴링이 사라진다.** 어쩔 수 없이 폴링해야 하면 서버가 준
`retry_after` 를 지킨다 — 고정 간격 폴링은 큐가 길어질수록 관제 서버를 때리고,
그 서버는 자기 부하가 아니라 **클러스터 포화의 증상으로** 죽는다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = ["ControlCenter", "Result", "ControlCenterError", "Blocked", "RateLimited"]

DEFAULT_TIMEOUT = 320.0


class ControlCenterError(Exception):
    """API 오류.

    분기는 `status` 와 `retryable` 로 한다. `code` 는 로케일과 무관하게 고정이고
    `message` 는 사람에게 보여 주기 위한 것이다.
    """

    def __init__(self, status: int, body: Mapping[str, Any]):
        self.status = status
        self.code = body.get("code", "unknown")
        self.retryable = bool(body.get("retryable", False))
        self.params = dict(body)
        super().__init__(body.get("message") or f"{self.code} ({status})")


class Blocked(ControlCenterError):
    """가드가 막았다. `rules` 에 위반 규칙 ID 가 있고 **원문은 실리지 않는다.**"""

    @property
    def rules(self) -> list[str]:
        return [r.strip() for r in str(self.params.get("rules", "")).split(",") if r.strip()]


class RateLimited(ControlCenterError):
    """한도 초과. `scope` 가 **어느 단계**(tenant · service · end_user)인지 알려준다.

    자기 서비스 한도를 올려도 안 풀리는 이유가 테넌트 총량인 경우가 있다.
    """

    @property
    def scope(self) -> str:
        return str(self.params.get("scope", ""))

    @property
    def retry_after(self) -> float | None:
        """서버가 준 재시도 간격(초). 없으면 `None`.

        이 속성이 없어서 아래 예시의 한도 초과 처리 코드가 `AttributeError` 로
        죽었다 — **오류 계약 시범이 목적인 파일에서 정확히 그 시범 경로가.**
        설치처 개발자가 그대로 복사해 쓰는 코드다.
        """
        value = self.params.get("retry_after")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


@dataclass
class Result:
    """생성 결과. **완료든 대기든 모양이 같다** — 호출자 코드에 분기가 필요 없다."""

    job_id: str
    status: str
    text: str = ""
    error: str | None = None
    error_code: str | None = None
    role: str = ""
    model: str | None = None
    node: str | None = None
    attempts: int = 0
    queue_position: int | None = None
    retry_after: float | None = None
    wait_reason: str | None = None
    guard_actions: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status != "pending"

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "Result":
        return cls(
            job_id=body.get("job_id", ""),
            status=body.get("status", ""),
            text=body.get("response") or "",
            error=body.get("error"),
            error_code=body.get("error_code"),
            role=body.get("role", ""),
            model=body.get("model"),
            node=body.get("node"),
            attempts=int(body.get("attempts", 0)),
            queue_position=body.get("queue_position"),
            retry_after=body.get("retry_after"),
            wait_reason=body.get("wait_reason"),
            guard_actions=dict(body.get("guard_actions") or {}),
            raw=dict(body),
        )


class ControlCenter:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        locale: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.locale = locale

    # -- 소비자 경로 ----------------------------------------------------------

    def generate(
        self,
        role: str,
        prompt: str,
        *,
        system: str | None = None,
        end_user: str | None = None,
        priority: int = 0,
        wait: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Result:
        """생성 요청.

        `end_user` 는 **불투명 식별자**여야 한다. 서버가 테넌트 솔트로 해싱하므로
        이메일을 넣어도 DB 에 이메일이 남지는 않지만, 그건 사고를 줄이는 장치이지
        계약이 아니다.
        """
        body: dict[str, Any] = {"role": role, "prompt": prompt}
        if system is not None:
            body["system"] = system
        if end_user is not None:
            body["end_user"] = end_user
        if priority:
            body["priority"] = priority
        if wait is not None:
            body["wait"] = wait
        if metadata:
            body["metadata"] = dict(metadata)
        return Result.from_body(self._request("POST", "/v1/generate", body))

    def embed(
        self, role: str, inputs: str | Sequence[str], *, end_user: str | None = None
    ) -> dict[str, Any]:
        """임베딩. 동기지만 가드·배치·경계·비용은 생성과 같은 관문을 지난다."""
        body: dict[str, Any] = {"role": role, "input": inputs}
        if end_user is not None:
            body["end_user"] = end_user
        return self._request("POST", "/v1/embed", body)

    def job(self, job_id: str, *, wait: float | None = None) -> Result:
        path = f"/v1/jobs/{job_id}"
        if wait:
            path += f"?wait={wait}"
        return Result.from_body(self._request("GET", path))

    def cancel(self, job_id: str) -> Result:
        return Result.from_body(self._request("DELETE", f"/v1/jobs/{job_id}"))

    def roles(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/v1/roles")["roles"])

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def meta(self) -> dict[str, Any]:
        """기계가 읽는 계약 — 역할·한도·오류 코드. **이 토큰 기준으로 생성된다.**"""
        return self._request("GET", "/v1/meta")

    # -- 완료까지 --------------------------------------------------------------

    def run(
        self,
        role: str,
        prompt: str,
        *,
        deadline: float = 600.0,
        wait: float = 30.0,
        **kwargs: Any,
    ) -> Result:
        """끝날 때까지 기다린다.

        `wait` 로 최대한 서버에서 기다리고, 그래도 안 끝나면 **서버가 준
        `retry_after` 를 지켜** 다시 묻는다. 고정 간격으로 폴링하지 않는다.
        """
        started = time.monotonic()
        result = self.generate(role, prompt, wait=wait, **kwargs)

        while not result.done:
            if time.monotonic() - started > deadline:
                return result
            delay = result.retry_after if result.retry_after else 2.0
            time.sleep(min(delay, max(1.0, deadline - (time.monotonic() - started))))
            result = self.job(result.job_id, wait=wait)
        return result

    # -- 전송 -----------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if self.locale:
            headers["Accept-Language"] = self.locale

        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"code": "unknown", "message": raw}
            raise _error_for(exc.code, parsed) from None
        return json.loads(raw) if raw else {}


def _error_for(status: int, body: Mapping[str, Any]) -> ControlCenterError:
    """상태 코드로 예외 종류를 고른다. **문자열로 분기하지 않는다.**"""
    if status == 422 and body.get("code") == "guard_blocked":
        return Blocked(status, body)
    if status == 429:
        return RateLimited(status, body)
    return ControlCenterError(status, body)


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import os

    parser = argparse.ArgumentParser(description="LLM ControlCenter 단일 파일 클라이언트")
    parser.add_argument("--base-url", default=os.environ.get("LCC_URL", "http://localhost:8610"))
    parser.add_argument("--token", default=os.environ.get("LCC_TOKEN", ""))
    parser.add_argument("--role", default="summarize")
    parser.add_argument("--end-user")
    parser.add_argument("prompt", nargs="?")
    args = parser.parse_args()

    cc = ControlCenter(args.base_url, args.token)
    if not args.prompt:
        for role in cc.roles():
            print(f"{role['name']:20s} {role['kind']:10s} {role['timeout_seconds']}s")
        raise SystemExit(0)

    try:
        result = cc.run(args.role, args.prompt, end_user=args.end_user)
    except Blocked as blocked:
        print(f"차단됨: {', '.join(blocked.rules)}")
        raise SystemExit(2)
    except RateLimited as limited:
        print(f"한도 초과 ({limited.scope}) — {limited.retry_after or 'retry later'}")
        raise SystemExit(3)

    print(result.text if result.ok else f"{result.status}: {result.error}")
