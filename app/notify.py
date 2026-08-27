"""알림 — 상태 전이에서만, 실패를 삼키고, 본문을 담지 않는다.

**관제 센터가 알림 없이는 관제를 못 한다.** 기준은 하나다 — "사람이 모르면 조용히
멈추는 지점" 이 정확히 알림의 자리다.

세 원칙이 이 모듈의 전부다.

**① 상태 전이에서만 보낸다.** 노드가 죽어 있는 동안 30초마다 "노드 오프라인" 을
보내면 이틀 뒤 아무도 그 채널을 안 본다. 안 보는 알림은 없는 알림이다.

**② 기동 시 "복구됨" 을 보내지 않는다.** 프로세스가 재시작할 때마다 모든 노드가
unknown → healthy 로 처음 전이하는 것처럼 보인다. 배포할 때마다 알림이 쏟아지면
①과 같은 결말이 된다. 그래서 첫 관측은 **기록만 하고 보내지 않는다.**

**③ 실패가 파이프라인을 죽이지 않는다.** 웹훅 URL 오타 하나로 추론이 멈추면
알림이 장애의 원인이 된다. 예외를 삼키고 로그만 남긴다.

그리고 **비밀·프롬프트·응답 본문을 담지 않는다.** 알림 채널은 대개 사내 메신저이고,
거기에 프롬프트가 흘러가면 가드의 모든 노력이 마지막 한 걸음에서 무의미해진다.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable, Mapping, Protocol, Sequence

from .i18n import DEFAULT_LOCALE, Translator

log = logging.getLogger("llmcc.notify")

ENV_WEBHOOK = "LCC_NOTIFY_WEBHOOK"
ENV_SMTP_HOST = "LCC_SMTP_HOST"
ENV_SMTP_PORT = "LCC_SMTP_PORT"
ENV_SMTP_USER = "LCC_SMTP_USER"
ENV_SMTP_PASSWORD = "LCC_SMTP_PASSWORD"
ENV_SMTP_FROM = "LCC_SMTP_FROM"
ENV_SMTP_TO = "LCC_SMTP_TO"
ENV_SMTP_TLS = "LCC_SMTP_TLS"

#: 알림 본문에 절대 담지 않는 키. 호출자가 실수로 넘겨도 여기서 걸러진다.
#: **거를 수 있게 만들어 두는 쪽이, 안 넘기기로 약속하는 쪽보다 낫다.**
REDACTED_KEYS = frozenset(
    {
        "prompt", "prompt_masked", "prompt_external", "response", "text",
        "system", "system_masked", "system_external", "token", "raw",
        "api_key", "password", "secret", "key", "dek", "kek",
        "end_user", "value", "match", "matched",
    }
)

#: 이 이벤트들만 보낸다. 목록에 없는 이벤트는 조용히 버린다 — 새 알림을 추가할 때
#: 여기와 로케일 카탈로그를 함께 손대게 강제하는 것이 목적이다.
KNOWN_EVENTS: tuple[str, ...] = (
    "node_offline",
    "node_recovered",
    "model_approval_pending",
    "model_ready",
    "model_failed",
    "budget_warn",
    "budget_exhausted",
    "guard_blocks_spike",
    "classifier_error_rate",
    "crash_recovery_needs_review",
)

#: 좋은 소식인 이벤트. 기동 직후에는 보내지 않는다(원칙 ②).
RECOVERY_EVENTS = frozenset({"node_recovered", "model_ready"})


def redact(detail: Mapping[str, Any]) -> dict[str, Any]:
    """알림 본문에서 담으면 안 되는 것을 지운다."""
    clean: dict[str, Any] = {}
    for key, value in detail.items():
        lowered = key.lower()
        if lowered in REDACTED_KEYS or any(bad in lowered for bad in ("secret", "token", "password")):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, Mapping):
            clean[key] = redact(value)
        elif isinstance(value, (list, tuple)):
            clean[key] = [v for v in value if isinstance(v, (str, int, float, bool))]
    return clean


# ── 채널 ────────────────────────────────────────────────────────────────────


class Channel(Protocol):
    name: str

    def send(self, subject: str, body: str, detail: Mapping[str, Any]) -> None: ...


@dataclass
class WebhookChannel:
    """Slack/Teams 호환 웹훅.

    두 서비스 모두 `{"text": ...}` 를 받으므로 그 최소 공통분모만 쓴다. 서비스별
    카드 포맷을 쓰면 한쪽에서만 예쁘고 다른 쪽에서 깨진다.
    """

    url: str
    timeout: float = 5.0
    name: str = "webhook"

    def send(self, subject: str, body: str, detail: Mapping[str, Any]) -> None:
        import httpx

        payload = {"text": f"[{subject}] {body}", "attachments": [{"fields": detail}]}
        httpx.post(self.url, json=payload, timeout=self.timeout).raise_for_status()


@dataclass
class SmtpChannel:
    host: str
    port: int = 25
    sender: str = "llm-controlcenter@localhost"
    recipients: Sequence[str] = ()
    username: str | None = None
    password: str | None = None
    use_tls: bool = False
    timeout: float = 10.0
    name: str = "smtp"

    def send(self, subject: str, body: str, detail: Mapping[str, Any]) -> None:
        if not self.recipients:
            return
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        lines = [body, ""]
        lines += [f"{k}: {v}" for k, v in sorted(detail.items())]
        message.set_content("\n".join(lines))

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)


@dataclass
class RecordingChannel:
    """테스트·데모용. 보낸 것을 그대로 들고 있는다."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False
    name: str = "recording"

    def send(self, subject: str, body: str, detail: Mapping[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("채널이 죽었다")
        self.sent.append({"subject": subject, "body": body, "detail": dict(detail)})


def channels_from_env(env: Mapping[str, str] | None = None) -> list[Channel]:
    """환경에서 채널을 만든다.

    웹훅 URL 과 SMTP 자격증명은 설치처마다 다르고 비밀에 가까우므로 설정 파일이
    아니라 환경에서 읽는다 — 마스터 KEK 와 같은 이유다.
    """
    env = env if env is not None else os.environ
    found: list[Channel] = []

    if url := env.get(ENV_WEBHOOK):
        found.append(WebhookChannel(url=url))

    if host := env.get(ENV_SMTP_HOST):
        recipients = [r.strip() for r in (env.get(ENV_SMTP_TO) or "").split(",") if r.strip()]
        found.append(
            SmtpChannel(
                host=host,
                port=int(env.get(ENV_SMTP_PORT) or 25),
                sender=env.get(ENV_SMTP_FROM) or "llm-controlcenter@localhost",
                recipients=recipients,
                username=env.get(ENV_SMTP_USER),
                password=env.get(ENV_SMTP_PASSWORD),
                use_tls=(env.get(ENV_SMTP_TLS) or "").lower() in ("1", "true", "yes"),
            )
        )
    return found


# ── 알림기 ──────────────────────────────────────────────────────────────────


class Notifier:
    """상태 전이 알림기.

    호출자(`cluster` · `models` · `scheduler`)는 "지금 상태가 이렇다" 만 말하고,
    보낼지 말지는 여기서 정한다. 판정을 호출자마다 두면 어떤 경로는 중복으로 보내고
    어떤 경로는 안 보내게 된다.
    """

    def __init__(
        self,
        channels: Sequence[Channel] = (),
        *,
        translator: Translator | None = None,
        locale: str = DEFAULT_LOCALE,
        now: Callable[[], float] = time.time,
        #: 같은 사건을 이 초 안에 다시 보내지 않는다. 전이 판정이 흔들려도
        #: 채널이 도배되지 않게 하는 두 번째 방벽이다.
        min_interval_seconds: float = 300.0,
    ) -> None:
        self._channels = list(channels)
        self._translator = translator
        self._locale = locale
        self._now = now
        self._min_interval = min_interval_seconds

        self._lock = threading.Lock()
        self._state: dict[str, str] = {}
        self._last_sent: dict[str, float] = {}
        self._started_at = now()
        #: 진단·테스트용 이력. 채널이 없어도 무엇이 발생했는지는 남는다.
        self.history: list[dict[str, Any]] = []

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._channels)

    def add_channel(self, channel: Channel) -> None:
        self._channels.append(channel)

    # -- 전이 판정 -------------------------------------------------------------

    def observe(self, subject: str, state: str, *, event: str, **detail: Any) -> bool:
        """`subject` 의 상태를 관측한다. **바뀐 순간에만** 알림이 나간다.

        첫 관측은 기록만 하고 보내지 않는다 — 기동할 때마다 모든 노드가 처음
        전이하는 것처럼 보이면 배포마다 알림이 쏟아진다.
        """
        with self._lock:
            previous = self._state.get(subject)
            self._state[subject] = state
            if previous == state:
                return False
            first_observation = previous is None

        if first_observation:
            # 기동 직후의 좋은 소식은 삼킨다. 나쁜 소식은 보낸다 — 재시작 시점에
            # 이미 죽어 있는 노드는 사람이 알아야 한다.
            if event in RECOVERY_EVENTS:
                return False
        return self.send(event, **detail)

    def seed(self, subject: str, state: str) -> None:
        """관측 없이 상태만 심는다. 기동 시 현재 상태를 기준선으로 잡을 때 쓴다."""
        with self._lock:
            self._state[subject] = state

    # -- 발송 -----------------------------------------------------------------

    def send(self, event: str, **detail: Any) -> bool:
        """알림 한 건. **어떤 예외도 밖으로 나가지 않는다.**"""
        if event not in KNOWN_EVENTS:
            log.warning("알 수 없는 알림 이벤트: %s", event)
            return False

        clean = redact(detail)
        key = f"{event}:{clean.get('node') or clean.get('tenant') or clean.get('model') or ''}"
        now = self._now()
        with self._lock:
            if now - self._last_sent.get(key, -1e9) < self._min_interval:
                return False
            self._last_sent[key] = now

        body = self._render(event, clean)
        self.history.append({"ts": now, "event": event, "detail": clean, "body": body})

        for channel in self._channels:
            try:
                channel.send(f"LLM ControlCenter · {event}", body, clean)
            except Exception as exc:
                # **알림 실패가 파이프라인을 죽이지 않는다.** 웹훅 URL 오타 하나로
                # 추론이 멈추면 알림이 장애의 원인이 된다.
                log.warning(
                    "알림 발송 실패 (채널=%s 이벤트=%s): %s", channel.name, event, exc
                )
        return True

    def _render(self, event: str, detail: Mapping[str, Any]) -> str:
        if self._translator is None:
            return f"{event} {json.dumps(detail, ensure_ascii=False, sort_keys=True)}"
        return self._translator.t(f"notify.{event}", self._locale, **detail)

    def as_callable(self) -> Callable[[str, Mapping[str, Any]], None]:
        """`registrar` · `scheduler` 가 받는 `notify(event, detail)` 모양으로."""

        def notify(event: str, detail: Mapping[str, Any]) -> None:
            self.send(event, **dict(detail))

        return notify

    # -- 관제 -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "channels": list(self.channel_names),
            "tracked_subjects": len(self._state),
            "sent": len(self.history),
            "recent": [
                {"ts": h["ts"], "event": h["event"], "detail": h["detail"]}
                for h in self.history[-20:]
            ],
        }
