"""운영 인터페이스 — 알림 · 메트릭 · 로그 · 진단 번들.

세 가지를 못박는다.
**① 알림은 상태 전이에서만 나가고, 실패가 파이프라인을 죽이지 않는다.**
**② 로그·메트릭·번들 어디에도 프롬프트 본문과 비밀이 없다.**
**③ 메트릭 라벨에 테넌트 이름이 없다** — 설치처 전체가 보는 대시보드로 흘러가기 때문이다.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging

import pytest

from app.notify import (
    KNOWN_EVENTS,
    Notifier,
    RecordingChannel,
    channels_from_env,
    redact,
)
from app.observability import (
    JsonFormatter,
    diagnostic_bundle,
    log_event,
    mask_secret,
)
from app.store import TenantScope
from tests.conftest import FakeClock, auth

VALID_CARD = "4111 1111 1111 1111"


def submit(client, tokens, prompt="안녕", **extra):
    body = {"role": "summarize", "prompt": prompt, "wait": 0, **extra}
    return client.post("/v1/generate", json=body, headers=auth(tokens["service"])).json()


# ── 알림: 상태 전이에서만 ────────────────────────────────────────────────────


def test_notifier_only_fires_on_a_transition():
    channel = RecordingChannel()
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)

    notifier.observe("node:a", "healthy", event="node_recovered", node="a")   # 첫 관측
    assert channel.sent == []                                                 # 좋은 소식은 삼킨다

    notifier.observe("node:a", "unhealthy", event="node_offline", node="a")
    assert len(channel.sent) == 1

    for _ in range(5):
        notifier.observe("node:a", "unhealthy", event="node_offline", node="a")
    assert len(channel.sent) == 1     # 죽어 있는 동안 반복해서 보내지 않는다

    notifier.observe("node:a", "healthy", event="node_recovered", node="a")
    assert len(channel.sent) == 2     # 회복은 전이이므로 보낸다


def test_startup_does_not_announce_recovery():
    """**배포할 때마다 알림이 쏟아지면 아무도 그 채널을 안 본다.**

    기동 시 모든 노드가 unknown → healthy 로 처음 전이하는 것처럼 보인다.
    """
    channel = RecordingChannel()
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)
    for name in ("a", "b", "c"):
        notifier.observe(f"node:{name}", "healthy", event="node_recovered", node=name)
    assert channel.sent == []


def test_startup_does_announce_a_node_that_is_already_down():
    """나쁜 소식은 첫 관측에도 보낸다 — 재시작 시점에 죽어 있으면 사람이 알아야 한다."""
    channel = RecordingChannel()
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)
    notifier.observe("node:a", "unhealthy", event="node_offline", node="a")
    assert len(channel.sent) == 1


def test_seed_sets_the_baseline_without_sending():
    channel = RecordingChannel()
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)
    notifier.seed("node:a", "unhealthy")
    notifier.observe("node:a", "unhealthy", event="node_offline", node="a")
    assert channel.sent == []


def test_a_dead_channel_never_kills_the_caller():
    """**웹훅 URL 오타 하나로 추론이 멈추면 알림이 장애의 원인이 된다.**"""
    broken = RecordingChannel(fail=True)
    good = RecordingChannel()
    notifier = Notifier([broken, good], now=FakeClock(), min_interval_seconds=0.0)

    assert notifier.send("node_offline", node="a") is True   # 예외가 안 나온다
    assert good.sent, "한 채널이 죽어도 다른 채널로는 나가야 한다"


def test_minimum_interval_stops_a_flapping_subject_from_flooding():
    clock = FakeClock()
    channel = RecordingChannel()
    notifier = Notifier([channel], now=clock, min_interval_seconds=300.0)

    for state in ("unhealthy", "healthy", "unhealthy", "healthy"):
        notifier.observe("node:a", state, event="node_offline", node="a")
    assert len(channel.sent) == 1

    clock.advance(301)
    notifier.observe("node:a", "unhealthy", event="node_offline", node="a")
    assert len(channel.sent) == 2


def test_unknown_events_are_dropped():
    """새 알림을 추가할 때 목록과 로케일 카탈로그를 함께 손대게 강제한다."""
    channel = RecordingChannel()
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)
    assert notifier.send("완전히_새로운_사건") is False
    assert channel.sent == []


@pytest.mark.parametrize("event", KNOWN_EVENTS)
def test_every_known_event_has_a_message_in_every_locale(event):
    """알림 이벤트를 추가하고 번역을 안 달면 여기서 실패한다."""
    import json as _json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "locales"
    for path in root.glob("*.json"):
        catalog = _json.loads(path.read_text(encoding="utf-8"))
        assert f"notify.{event}" in catalog, f"{path.name} 에 notify.{event} 가 없다"


# ── 알림: 본문을 담지 않는다 ─────────────────────────────────────────────────


def test_redact_strips_prompts_and_secrets():
    clean = redact({
        "node": "in-1",
        "prompt": "고객 주민번호는 ...",
        "response": "생성 결과",
        "api_key": "sk-secret",
        "auth_token": "abc",
        "count": 3,
    })
    assert clean == {"node": "in-1", "count": 3}


def test_redact_reaches_into_nested_detail():
    clean = redact({"outer": {"prompt": "본문", "node": "in-1"}})
    assert clean == {"outer": {"node": "in-1"}}


def test_a_notification_never_carries_the_prompt():
    channel = RecordingChannel()
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)
    notifier.send("node_offline", node="in-1", prompt=f"카드 {VALID_CARD}")

    dumped = json.dumps(channel.sent, ensure_ascii=False)
    assert VALID_CARD not in dumped
    assert "prompt" not in dumped


def test_channels_come_from_the_environment():
    """웹훅 URL 과 SMTP 자격증명은 설치처마다 다르고 비밀에 가깝다."""
    assert channels_from_env({}) == []
    channels = channels_from_env({"LCC_NOTIFY_WEBHOOK": "https://hooks.example/x"})
    assert [c.name for c in channels] == ["webhook"]

    both = channels_from_env({
        "LCC_NOTIFY_WEBHOOK": "https://hooks.example/x",
        "LCC_SMTP_HOST": "mail.example",
        "LCC_SMTP_TO": "ops@example, sec@example",
    })
    assert [c.name for c in both] == ["webhook", "smtp"]
    assert both[1].recipients == ["ops@example", "sec@example"]


# ── 알림: 실제 배선 ──────────────────────────────────────────────────────────


def test_node_health_transitions_reach_the_notifier(harness):
    """헬스 판정은 클러스터가 하고, **보낼지 말지는 알림기가 정한다.**"""
    harness.notifier.seed("node:in-1", "healthy")
    for _ in range(harness.config.thresholds.health_failures_to_unhealthy):
        harness.cluster.record_failure("in-1", "연결 실패")

    events = [s["detail"] for s in harness.channel.sent]
    assert {"node": "in-1"} in events


def test_budget_exhaustion_notifies_once_per_band(harness, client, acme, globex):
    scope = TenantScope("acme")
    harness.store._conn.execute(
        "UPDATE tenants SET budget_usd_per_month = 10.0 WHERE id='acme'"
    )
    harness.store.record_usage(scope, service_id="acme-web", role="summarize", cost_usd=8.5)

    harness.scheduler.run_watches()
    warned = [s for s in harness.channel.sent if "percent" in s["detail"]]
    assert warned and warned[0]["detail"]["tenant"] == "acme"

    before = len(harness.channel.sent)
    harness.scheduler.run_watches()
    assert len(harness.channel.sent) == before   # 같은 구간에서 다시 안 보낸다

    harness.store.record_usage(scope, service_id="acme-web", role="summarize", cost_usd=5.0)
    harness.scheduler.run_watches()
    assert len(harness.channel.sent) > before    # warn → exhausted 는 전이다


def test_classifier_failures_are_counted_separately(harness, config, store, clock):
    """**분류 실패는 "민감하지 않음" 판정이 아니다** — 별도로 집계된다."""
    from app.config import GuardRule
    from app.guard import Guard
    from app.pipeline import Pipeline
    from tests.conftest import seed_tenant
    from tests.test_pipeline import principal_for
    import asyncio

    async def broken(text, rules):
        raise RuntimeError("분류 모델이 죽었다")

    rules = (*config.guard_rules,
             GuardRule(id="deal", kind="llm", action="audit", description="인수합병"))
    tightened = type(config)(**{**config.__dict__, "guard_rules": rules})
    guard = Guard(tightened, classifier=broken)
    pipeline = Pipeline(tightened, store, harness.cluster, guard, vault=harness.vault, now=clock)
    principal = principal_for(seed_tenant(harness, "acme"))

    asyncio.run(pipeline.submit(principal, role="summarize", prompt="문장", wait=0))

    assert store.classifier_failure_rate(since=0) == 1.0
    events = store.list_filter_events(TenantScope("acme"))
    assert any(e["rule_id"] == "_classifier_failed" for e in events)


# ── 메트릭 ──────────────────────────────────────────────────────────────────


def metrics_text(client, tokens):
    response = client.get("/metrics", headers=auth(tokens["platform_admin"]))
    assert response.status_code == 200
    return response.text


def test_metrics_are_openmetrics_shaped(client, acme):
    text = metrics_text(client, acme)
    assert "# HELP llmcc_up" in text
    assert "# TYPE llmcc_up gauge" in text
    assert "llmcc_up 1.0" in text


def test_metrics_never_label_by_tenant(harness, client, acme, globex):
    """**설치처 전체가 보는 대시보드에 테넌트별 소비량이 뜨면 그것도 정보 유출이다.**"""
    submit(client, acme, prompt="에크미")
    submit(client, globex, prompt="글로벡스")

    text = metrics_text(client, acme)
    assert "acme" not in text
    assert "globex" not in text
    assert 'tenant="' not in text


def test_metrics_never_carry_prompts(client, acme):
    secret = "고유한비밀문구-XZ9"
    submit(client, acme, prompt=f"{secret} 그리고 카드 {VALID_CARD}")
    text = metrics_text(client, acme)
    assert VALID_CARD not in text
    assert secret not in text


def test_metrics_expose_the_operational_signals(harness, client, acme):
    submit(client, acme)
    text = metrics_text(client, acme)
    for name in (
        "llmcc_jobs", "llmcc_jobs_waiting", "llmcc_node_up", "llmcc_lane_queued",
        "llmcc_lane_scan_truncated", "llmcc_guard_events", "llmcc_guard_review_queue",
        "llmcc_single_homed_roles", "llmcc_raw_prompt_storage_enabled",
        "llmcc_threshold", "llmcc_schema_version",
    ):
        assert f"# TYPE {name} " in text, name


def test_metrics_report_unknown_node_health_as_half(harness, client, acme):
    """`unknown` 은 "죽었다" 가 아니라 "아직 모른다" 다. 0 으로 보내면 오경보가 된다."""
    harness.cluster.undrain("in-1")   # unknown 으로 되돌린다
    text = metrics_text(client, acme)
    assert 'llmcc_node_up{boundary="internal",node="in-1",provider="mock"} 0.5' in text


def test_metrics_need_platform_admin(client, acme):
    assert client.get("/metrics", headers=auth(acme["tenant_admin"])).status_code == 403


def test_metric_labels_are_escaped():
    from app.observability import Metric

    rendered = "\n".join(
        Metric("x", "gauge", "h", ((({"n": 'a"b\nc'}), 1.0),)).render()
    )
    assert 'n="a\\"b c"' in rendered


# ── 구조화 로그 ──────────────────────────────────────────────────────────────


def test_logs_are_one_json_line_with_readable_korean():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("llmcc.test.json")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_event(logger, "잡 완료", job_id="abc", node="in-1")

    line = stream.getvalue().strip()
    assert "\\uc7a1" not in line          # ensure_ascii=False — 사람이 읽을 수 있다
    parsed = json.loads(line)
    assert parsed["msg"] == "잡 완료"
    assert parsed["job_id"] == "abc"


def test_logs_drop_prompt_bodies_and_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("llmcc.test.redact")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_event(logger, "잡 실패", node="in-1", prompt=f"카드 {VALID_CARD}", api_key="sk-x")

    line = stream.getvalue()
    assert VALID_CARD not in line and "sk-x" not in line
    assert json.loads(line.strip())["node"] == "in-1"


# ── 진단 번들 ────────────────────────────────────────────────────────────────


def bundle_of(client, tokens):
    response = client.get("/v1/platform/diagnostics", headers=auth(tokens["platform_admin"]))
    assert response.status_code == 200
    return response.json()


def test_diagnostic_bundle_masks_secrets(harness, acme):
    bundle = diagnostic_bundle(
        store=harness.store, cluster=harness.cluster, config=harness.config,
        scheduler=harness.scheduler, registrar=harness.registrar,
        notifier=harness.notifier, vault=harness.vault,
        env={"LCC_PROMPT_KEY": "aGVsbG8td29ybGQtc2VjcmV0", "PATH": "/usr/bin"},
        version="0.1.0", now=harness.clock,
    )
    assert "aGVsbG8t" not in json.dumps(bundle)
    assert bundle["environment"]["LCC_PROMPT_KEY"].startswith("<설정됨")
    assert "PATH" not in bundle["environment"]     # LCC_ 접두사만 담는다


def test_mask_secret_reveals_length_not_value():
    assert mask_secret("") == ""
    assert "abc" not in mask_secret("abcdef")
    assert "6" in mask_secret("abcdef")


def test_diagnostic_bundle_carries_no_prompts_or_tenant_names(harness, client, acme, globex):
    """**설치처가 이 파일을 그대로 지원 채널로 보낸다는 전제로 만든다.**"""
    submit(client, acme, prompt=f"카드 {VALID_CARD}", end_user="hong@example.com")
    submit(client, globex, prompt="글로벡스 비밀")

    dumped = json.dumps(bundle_of(client, acme), ensure_ascii=False)
    assert VALID_CARD not in dumped
    assert "글로벡스 비밀" not in dumped
    assert "hong@example.com" not in dumped
    assert '"acme"' not in dumped and '"globex"' not in dumped


def test_diagnostic_bundle_answers_the_usual_support_questions(client, acme):
    bundle = bundle_of(client, acme)
    assert bundle["product"]["schema_version"] >= 1
    assert bundle["config"]["nodes"] and bundle["config"]["roles"]
    assert "thresholds" in bundle["config"]
    assert "single_homed_roles" in bundle
    assert "waiting_by_reason" in bundle
    assert bundle["counts"]["tenants"] >= 1


def test_diagnostic_bundle_shows_node_auth_without_the_credential(client, acme):
    bundle = bundle_of(client, acme)
    for node in bundle["config"]["nodes"]:
        assert set(node) >= {"name", "data_boundary", "auth_configured"}
        assert "api_key" not in node and "auth_header" not in node


def test_diagnostic_bundle_recent_errors_have_no_bodies(harness, client, acme):
    scope = TenantScope("acme")
    job = submit(client, acme, prompt=f"카드 {VALID_CARD}")
    harness.store.update_job(
        scope, job["job_id"], status="failed",
        error_code="backend_unavailable", error="노드 연결 실패", finished_at=1.0,
    )
    errors = bundle_of(client, acme)["recent_errors"]
    assert errors and errors[0]["error_code"] == "backend_unavailable"
    assert "prompt" not in errors[0] and "response" not in errors[0]


def test_diagnostics_are_audited_and_platform_only(harness, client, acme):
    assert client.get(
        "/v1/platform/diagnostics", headers=auth(acme["tenant_admin"])
    ).status_code == 403

    bundle_of(client, acme)
    rows = harness.store._conn.execute(
        "SELECT * FROM admin_audit WHERE action='diagnostic_bundle'"
    ).fetchall()
    assert rows


# ── 채널 현황 ────────────────────────────────────────────────────────────────


def test_notification_status_says_when_nothing_is_configured(harness, client, acme):
    """**안 붙은 것을 모르는 것이 가장 흔한 실패다.**"""
    harness.app.state.ctx.notifier = Notifier([], now=harness.clock)
    body = client.get(
        "/v1/platform/notifications", headers=auth(acme["platform_admin"])
    ).json()
    assert body["configured"] is False
    assert body["channels"] == []


def test_test_notification_actually_reaches_the_channel(harness, client, acme):
    client.post(
        "/v1/platform/notifications", json={}, headers=auth(acme["platform_admin"])
    )
    assert any(s["detail"].get("node") == "(테스트)" for s in harness.channel.sent)


# ── 감사 H10 — 알림이 이벤트 루프를 붙잡고 있었다 ────────────────────────────
#
# ③("실패가 파이프라인을 죽이지 않는다")은 예외만 삼켜서는 지켜지지 않는다.
# **느린 채널은 예외를 내지 않고 그냥 붙잡고 있는다** — 웹훅 5초, SMTP 10초를
# 동기로 기다리는 동안 관제 센터의 다른 모든 요청이 멈춘다.


class SlowChannel:
    """붙잡고 있는 채널. 죽은 채널이 아니라 **느린** 채널이다."""

    name = "slow"
    blocking = True

    def __init__(self, seconds: float = 0.4) -> None:
        self.seconds = seconds
        self.sent = 0

    def send(self, subject, body, detail):
        import time as _time

        _time.sleep(self.seconds)
        self.sent += 1


async def test_a_slow_channel_does_not_stall_the_event_loop():
    """**같은 판정이 예외에는 걸리고 지연에는 안 걸리면 반쪽짜리다.**"""
    import asyncio
    import time as _time

    slow = SlowChannel(0.4)
    notifier = Notifier([slow], now=FakeClock(), min_interval_seconds=0.0)

    started = _time.monotonic()
    notifier.send("node_offline", node="n1")
    # 루프가 살아 있으면 이 왕복이 즉시 끝난다. 막혀 있으면 채널을 기다린다.
    await asyncio.sleep(0)
    elapsed = _time.monotonic() - started

    assert elapsed < 0.2, f"알림이 이벤트 루프를 {elapsed:.2f}초 붙잡았다"

    await notifier.drain()
    assert slow.sent == 1, "넘긴 뒤 실제로 보내지지 않았다"


async def test_the_offloaded_send_still_swallows_failures():
    """스레드로 넘겼다고 예외가 밖으로 새면 안 된다."""
    channel = RecordingChannel(fail=True)
    channel.blocking = True          # 네트워크 채널인 척한다
    notifier = Notifier([channel], now=FakeClock(), min_interval_seconds=0.0)

    assert notifier.send("node_offline", node="n1") is True
    await notifier.drain()           # 예외가 여기로 나오면 실패다


async def test_a_slow_channel_does_not_stall_a_request(harness, client, acme):
    """실제 요청 경로에서도 같아야 한다 — 단위 테스트만으로는 배선을 못 본다."""
    import time as _time

    harness.notifier.add_channel(SlowChannel(0.4))

    started = _time.monotonic()
    response = client.post(
        "/v1/platform/notifications", headers=auth(acme["platform_admin"])
    )
    elapsed = _time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.3, f"알림 테스트 요청이 {elapsed:.2f}초 걸렸다"
    await harness.notifier.drain()


def test_a_channel_is_assumed_to_block_unless_it_says_otherwise():
    """**모르는 채널을 인메모리로 가정하면 그 채널이 루프를 세운다.**"""
    class Bare:
        name = "bare"

        def send(self, subject, body, detail):
            pass

    from app.notify import SmtpChannel, WebhookChannel

    assert WebhookChannel(url="http://x").blocking is True
    assert SmtpChannel(host="x").blocking is True
    # 표기가 없으면 블로킹으로 본다.
    assert getattr(Bare(), "blocking", True) is True
    # 인메모리 채널만 인라인으로 돈다 — 안 그러면 테스트가 발송을 기다려야 한다.
    assert RecordingChannel().blocking is False


# ── 감사 H13 — 진단 번들이 테넌트 신원을 흘린다 ──────────────────────────────
#
# 번들은 **설치처가 벤더에게 보내는 파일**이다. `config` 절은 의식적으로
# `tenant_affinity_count` 만 담았는데, 같은 번들의 `cluster` 절이
# `cluster.snapshot()` 을 통째로 실으면서 같은 값을 원문 ID 로 다시 넣었다.
# 절마다 손으로 고르면 그 목록이 표가 되고, 표는 반드시 어긋난다.


def _bundle_with_a_dedicated_node(harness):
    from app.observability import diagnostic_bundle

    node = harness.cluster.nodes["in-1"].node
    harness.cluster.nodes["in-1"].node = dataclasses.replace(
        node, tenant_affinity=("acme", "globex")
    )
    return diagnostic_bundle(
        store=harness.store, cluster=harness.cluster, config=harness.config,
        scheduler=harness.scheduler, registrar=harness.registrar,
        notifier=harness.notifier, vault=harness.vault, now=harness.clock,
    )


def test_the_bundle_never_names_a_tenant(harness, acme, globex):
    """설치처가 지원 채널로 자기 고객 이름을 보내게 두지 않는다."""
    bundle = _bundle_with_a_dedicated_node(harness)
    text = json.dumps(bundle, ensure_ascii=False)

    assert "acme" not in text, "전용 노드 설정으로 테넌트 이름이 샜다"
    assert "globex" not in text


def test_the_bundle_still_says_how_many_tenants_are_pinned(harness, acme):
    """**수는 남긴다.** "전용 노드에 테넌트 2곳" 은 진단에 필요한 사실이다.

    누구인지가 벤더가 알 일이 아닐 뿐이다.
    """
    bundle = _bundle_with_a_dedicated_node(harness)
    node = next(n for n in bundle["cluster"] if n["node"] == "in-1")

    assert node["tenant_affinity_count"] == 2
    assert "tenant_affinity" not in node


def test_a_budget_alert_does_not_carry_the_tenant_into_the_bundle(harness, acme):
    """예산 알림은 "어느 테넌트가 80%를 썼는가" 가 곧 내용이다 — 채널에는 남는다.

    그 이력이 번들에 실려 벤더에게 가는 것이 문제다. **경계는 누가 받는가에 있다.**
    """
    from app.observability import diagnostic_bundle

    harness.notifier.send("budget_warn", tenant="acme", percent=80)
    assert any(
        h["detail"].get("tenant") == "acme" for h in harness.notifier.history
    ), "알림 이력에서 테넌트가 사라졌다 — 채널에는 있어야 한다"

    bundle = diagnostic_bundle(
        store=harness.store, cluster=harness.cluster, config=harness.config,
        notifier=harness.notifier, vault=harness.vault, now=harness.clock,
    )
    assert "acme" not in json.dumps(bundle, ensure_ascii=False)


def test_the_tenant_count_survives_the_strip(harness, acme, globex):
    """`counts.tenants` 는 수다 — 지우면 안 된다. 문자열만 지운다."""
    from app.observability import diagnostic_bundle

    bundle = diagnostic_bundle(
        store=harness.store, cluster=harness.cluster, config=harness.config,
        vault=harness.vault, now=harness.clock,
    )
    assert bundle["counts"]["tenants"] == 2


def test_the_strip_walks_the_whole_structure():
    """절마다 손으로 고르면 그 목록이 표가 되고, 표는 어긋난다 — 실제로 어긋났다."""
    from app.observability import strip_tenant_identity

    nested = {"a": [{"b": {"tenant_id": "acme", "keep": 1}}], "tenant": "globex"}
    clean = strip_tenant_identity(nested)

    assert clean["a"][0]["b"]["tenant_id"] == "(마스킹됨)"
    assert clean["a"][0]["b"]["keep"] == 1
    assert clean["tenant"] == "(마스킹됨)"


def test_the_admin_view_still_shows_who_is_pinned(harness, acme):
    """관제 UI 에서는 지우지 않는다 — 플랫폼 관리자는 자기 테넌트를 다 안다."""
    node = harness.cluster.nodes["in-1"].node
    harness.cluster.nodes["in-1"].node = dataclasses.replace(
        node, tenant_affinity=("acme",)
    )
    snapshot = next(n for n in harness.cluster.snapshot() if n["node"] == "in-1")

    assert snapshot["tenant_affinity"] == ["acme"]
