"""운영 인터페이스 — 메트릭 · 구조화 로그 · 진단 번들.

설치처는 이미 자기 모니터링을 갖고 있다. 우리가 대시보드를 하나 더 주는 것보다
**그들이 쓰는 것에 물리는 쪽**이 낫다 — 그래서 Prometheus/OpenMetrics 노출이고,
로그는 stdout 에 JSON 한 줄이다.

세 가지를 절대 담지 않는다: **프롬프트 본문 · 응답 본문 · 비밀.**
로그와 메트릭은 대개 중앙 수집기로 흘러가고, 거기에 프롬프트가 쌓이면 보관 기간도
접근 감사도 그 수집기 밖에서 전부 무력화된다.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .notify import redact

PREFIX = "llmcc"


# ── 구조화 로그 ──────────────────────────────────────────────────────────────


class JsonFormatter(logging.Formatter):
    """JSON 한 줄. 수집기가 파싱하기 좋고 사람도 읽을 수 있다.

    `ensure_ascii=False` 인 이유는 감사 로그와 같다 — 한글이 `\\uXXXX` 가 되면
    사람이 못 읽고 크기가 두 배가 된다.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, Mapping):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str = "INFO", stream=None) -> None:
    """루트 로거를 JSON stdout 으로. 기동에서 한 번 부른다."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """구조화 필드와 함께 남긴다. **본문·비밀은 `redact()` 가 걸러낸다.**"""
    logger.info(message, extra={"fields": fields})


# ── 메트릭 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str          # gauge | counter
    help: str
    samples: tuple[tuple[Mapping[str, str], float], ...]

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        for labels, value in self.samples:
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            suffix = f"{{{rendered}}}" if rendered else ""
            lines.append(f"{self.name}{suffix} {_number(value)}")
        return lines


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _number(value: float) -> str:
    # Prometheus 는 Infinity/NaN 을 허용하지만, 무한대를 실어 보내는 게이지는
    # 대시보드에서 축을 망가뜨린다. 없는 값은 애초에 샘플을 만들지 않는다.
    return repr(float(value))


def render_metrics(metrics: Iterable[Metric]) -> str:
    lines: list[str] = []
    for metric in metrics:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def collect(
    *,
    store: Any,
    cluster: Any,
    scheduler: Any = None,
    registrar: Any = None,
    notifier: Any = None,
    vault: Any = None,
    version: str = "",
    airgap: bool = False,
    thresholds: Any = None,
    roles: Any = None,
) -> list[Metric]:
    """지금 상태를 메트릭으로.

    **테넌트 이름을 라벨에 넣지 않는다.** 메트릭은 대개 설치처 전체가 보는
    대시보드로 흘러가고, 거기에 테넌트별 소비량이 뜨면 그것도 정보 유출이다.
    테넌트별 숫자는 인증이 걸린 관제 API 에만 있다.
    """
    metrics: list[Metric] = []

    def gauge(name: str, help_text: str, samples) -> None:
        metrics.append(Metric(f"{PREFIX}_{name}", "gauge", help_text, tuple(samples)))

    gauge(
        "build_info", "빌드 정보",
        [({"version": version or "dev", "airgap": str(airgap).lower()}, 1.0)],
    )
    gauge("up", "컨트롤 플레인 생존", [({}, 1.0)])
    gauge(
        "raw_prompt_storage_enabled",
        "원문 보관 활성 여부. 0 이면 KEK 가 없어 마스킹본만 저장된다",
        [({}, 1.0 if (vault is not None and vault.enabled) else 0.0)],
    )
    gauge("schema_version", "DB 스키마 버전", [({}, float(store.schema_version))])

    # -- 잡 --
    gauge(
        "jobs", "상태별 잡 수",
        [({"status": status}, float(count)) for status, count in store.job_counts().items()],
    )
    gauge(
        "jobs_waiting", "대기 사유별 잡 수. 관제의 1급 카드와 같은 값이다",
        [({"reason": reason}, float(n)) for reason, n in store.queued_wait_reasons().items()],
    )

    # -- 노드 --
    nodes = cluster.snapshot()
    gauge(
        "node_up", "노드가 healthy 인가. unknown 은 0.5 — 아직 모른다는 뜻이다",
        [
            (
                {"node": n["node"], "boundary": n["data_boundary"], "provider": n["provider"]},
                {"healthy": 1.0, "unknown": 0.5}.get(n["status"], 0.0),
            )
            for n in nodes
        ],
    )
    gauge(
        "node_running", "노드에서 실행 중인 잡 수",
        [({"node": n["node"]}, float(n["running"])) for n in nodes],
    )
    gauge(
        "node_slots", "노드 동시 실행 상한",
        [({"node": n["node"]}, float(n["max_concurrent"])) for n in nodes],
    )
    gauge(
        "node_memory_reserved_gb", "노드에 예약된 모델 메모리",
        [({"node": n["node"]}, float(n["mem_reserved_gb"])) for n in nodes],
    )

    single_homed = cluster.single_homed_roles()
    gauge(
        "single_homed_roles",
        "한 노드에만 모델이 있는 역할 수. 그 노드가 포화되면 그 역할만 굶는다",
        [({}, float(len(single_homed)))],
    )

    # -- 레인 --
    if scheduler is not None:
        lanes = scheduler.snapshot()
        gauge(
            "lane_running", "레인별 실행 중",
            [({"lane": lane}, float(s["running"])) for lane, s in lanes.items()],
        )
        gauge(
            "lane_queued", "레인별 대기",
            [({"lane": lane}, float(s["queued"])) for lane, s in lanes.items()],
        )
        gauge(
            "lane_concurrency", "레인 동시성 상한",
            [({"lane": lane}, float(s["max_concurrent"])) for lane, s in lanes.items()],
        )
        gauge(
            "lane_scan_truncated",
            "스캔 창이 잘렸는가. 상시 1 이면 창을 늘리거나 노드를 늘려야 한다",
            [({"lane": lane}, 1.0 if s["scan_truncated"] else 0.0) for lane, s in lanes.items()],
        )
        gauge(
            "lane_starvation_trips",
            "기아 방지 발동 횟수. 임계를 넘으면 배치 경합이다",
            [({"lane": lane}, float(s["starvation_trips"])) for lane, s in lanes.items()],
        )

    # -- 가드 --
    gauge(
        "guard_events", "등급별 가드 탐지 수. **규칙 ID 와 등급만이고 값은 없다**",
        [
            ({"action": action, "stage": stage}, float(n))
            for (action, stage), n in store.filter_event_counts().items()
        ],
    )
    gauge(
        "guard_review_queue",
        "사람이 판정해야 할 audit 히트 수. 이게 밀리면 승격 게이트가 열리지 않는다",
        [({}, float(store.unreviewed_filter_event_count()))],
    )

    # -- 모델 --
    if registrar is not None:
        gauge(
            "model_requests_pending",
            "승인 대기 중인 모델 설치 요청. 이게 쌓이면 그 역할의 잡이 대기한다",
            [({}, float(registrar.pending_count()))],
        )

    # -- 비용 --
    gauge(
        "cost_usd_30d", "최근 30일 누적 비용(전 테넌트 합계)",
        [({}, float(store.total_spend_since(time.time() - 30 * 86400)))],
    )

    # -- 토큰 처리율 --
    #
    # **한도가 아니라 계기다.** 레이트리밋은 건/분이고 예산은 달러인데, 무료 경로는
    # 달러가 0 이라 200KB 프롬프트 1건과 1KB 1건이 같은 1건이다. 상한을 걸기 전에
    # 설치처의 분포부터 봐야 하고, 그 분포를 볼 자리가 여기다.
    #
    # 테넌트 라벨은 여기에도 없다 — 전 테넌트 합계뿐이다.
    rate = store.token_rate()
    gauge(
        "tokens_per_minute",
        "최근 창의 분당 토큰 처리율(전 테넌트 합계). **한도가 아니라 계기다**",
        [
            ({"direction": "input"}, rate["input_tokens_per_minute"]),
            ({"direction": "output"}, rate["output_tokens_per_minute"]),
        ],
    )

    # -- 라우팅 --
    #
    # 라벨은 **역할과 라우트 키뿐이다.** 둘 다 설정 어휘라 카디널리티가 유한하고,
    # 테넌트 라벨 금지 원칙은 여기서도 그대로다.
    if roles is not None:
        routed_roles = {name for name, role in roles.items() if role.routing is not None}
        if routed_roles:
            counts = store.route_counts()
            decisions = [
                ({"role": role, "route": route}, float(n))
                for role, route, n in sorted(counts, key=lambda row: (row[0], row[1] or ""))
                if route and role in routed_roles
            ]
            if decisions:
                gauge(
                    "route_decisions",
                    "라우트별 배치 건수(전 테넌트 합계)",
                    decisions,
                )
            # **라우팅을 켠 역할의 `route` 없음 = 분류가 답을 못 준 것이다.**
            # 이것이 0 이 아니라고 고장은 아니다 — fail-to-default 라 결과는 정상이고,
            # 계속 높으면 `description` 을 고치라는 신호다.
            gauge(
                "route_failures",
                "라우팅을 켠 역할인데 라우트가 안 정해진 건수(기본 모델로 갔다)",
                [
                    ({"role": role}, float(n))
                    for role, route, n in sorted(counts)
                    if route is None and role in routed_roles
                ],
            )

    # -- 알림 --
    if notifier is not None:
        # **"발송" 과 "발생" 은 다르다.**
        #
        # `history` 는 채널이 하나도 없어도, 전 채널이 실패해도 늘어난다. 그것을
        # `sent` 라고 부르면 대시보드가 "알림이 나가고 있다" 고 말하는데 실제로는
        # 아무 데도 안 간다 — 알림이 막으려던 상황이 정확히 그것이다.
        gauge(
            "notifications_raised", "발생한 알림 사건 수(채널 도달 여부와 무관)",
            [({}, float(len(notifier.history)))],
        )
        gauge(
            "notification_channels", "붙어 있는 알림 채널 수. 0 이면 아무 데도 안 간다",
            [({}, float(len(notifier.channel_names)))],
        )

    # -- 임계값 -- 문서·UI·경고가 같은 값을 인용하도록 노출한다.
    if thresholds is not None:
        gauge(
            "threshold", "증설·전환 트리거 임계값",
            [
                ({"name": name}, float(value))
                for name, value in sorted(vars(thresholds).items())
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ],
        )

    return metrics


# ── 진단 번들 ────────────────────────────────────────────────────────────────

#: 진단 번들에 담을 환경 변수의 접두사. 값은 **마스킹해서** 담는다 —
#: 지원 요청에 첨부되는 파일이라 원값이 들어가면 그대로 유출이다.
DIAGNOSTIC_ENV_PREFIXES = ("LCC_",)


def mask_secret(value: str) -> str:
    """비밀을 길이만 남기고 지운다. 설정 여부는 알 수 있고 값은 알 수 없다."""
    if not value:
        return ""
    return f"<설정됨 · {len(value)}자>"


#: 테넌트 신원을 나르는 키. **번들에서만** 지운다 — 관제 UI 와 알림 채널에서는
#: 이 값이 있어야 한다. 플랫폼 관리자는 자기 테넌트를 다 알고, 예산 알림은
#: "어느 테넌트가 80% 를 썼는가" 가 곧 내용이다.
#:
#: 경계는 **누가 그 파일을 받는가**에 있다. 번들은 설치처가 벤더에게 보낸다.
TENANT_BEARING_KEYS = frozenset({"tenant", "tenant_id", "tenant_affinity"})


def strip_tenant_identity(value: Any) -> Any:
    """번들에서 테넌트 신원을 지운다. **구조를 훑어서 지운다.**

    필드를 손으로 골라 담으면 그 목록이 표가 되고, 표는 반드시 어긋난다 —
    실제로 어긋났다. 번들의 `config` 절은 의식적으로 `tenant_affinity_count`
    만 담았는데, 같은 번들의 `cluster` 절이 `cluster.snapshot()` 을 통째로
    실으면서 **같은 값을 원문 테넌트 ID 로 다시 넣고 있었다.**

    수를 남기는 것이 중요하다. "전용 노드에 테넌트 2곳" 은 진단에 필요한 사실이고,
    그게 누구인지는 벤더가 알 일이 아니다.
    """
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if key in TENANT_BEARING_KEYS:
                if isinstance(item, str):
                    clean[key] = "(마스킹됨)" if item else item
                    continue
                if isinstance(item, (list, tuple)):
                    # 목록은 수만 남긴다 — 있다는 사실은 진단에 필요하다.
                    clean[f"{key}_count"] = len(item)
                    continue
            clean[key] = strip_tenant_identity(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [strip_tenant_identity(item) for item in value]
    return value


def diagnostic_bundle(
    *,
    store: Any,
    cluster: Any,
    config: Any,
    scheduler: Any = None,
    registrar: Any = None,
    notifier: Any = None,
    vault: Any = None,
    env: Mapping[str, str] | None = None,
    version: str = "",
    airgap: bool = False,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """지원 요청에 첨부할 진단 정보.

    **설치처가 이 파일을 그대로 보낸다는 전제로 만든다.** 그래서 비밀은 마스킹하고,
    프롬프트·응답 본문과 테넌트 이름은 아예 담지 않는다 — 담으면 설치처가
    지원 채널로 자기 고객 데이터를 보내게 된다.

    테넌트 신원 제거는 **마지막에 구조 전체를 훑어서** 한다(`strip_tenant_identity`).
    절마다 손으로 고르면 그 목록이 표가 되고, 표는 어긋난다 — 실제로 아래 `config`
    절은 `tenant_affinity_count` 만 담는데 `cluster` 절이 스냅샷을 통째로 실으면서
    같은 값을 원문 ID 로 다시 넣고 있었다.
    """
    env = env if env is not None else {}
    secrets_present = {
        key: mask_secret(value)
        for key, value in sorted(env.items())
        if key.startswith(DIAGNOSTIC_ENV_PREFIXES)
    }

    bundle = {
        "generated_at": now(),
        "product": {
            "version": version,
            "schema_version": store.schema_version,
            "airgap": airgap,
            "raw_prompt_storage": bool(vault is not None and vault.enabled),
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        # 설정은 담되 비밀은 이름만 — api_key_env 는 변수 **이름**이라 안전하다.
        "config": {
            "nodes": [
                {
                    "name": n.name, "provider": n.provider,
                    "data_boundary": n.data_boundary,
                    "base_url_set": bool(n.base_url),
                    "auth_configured": bool(n.api_key_env or n.auth_header_env),
                    "max_concurrent": n.max_concurrent,
                    "mem_budget_gb": n.mem_budget_gb,
                    "tags": list(n.tags),
                    "tenant_affinity_count": len(n.tenant_affinity),
                }
                for n in config.nodes.values()
            ],
            "roles": [
                {
                    "name": r.name, "kind": r.kind, "lane": r.lane,
                    "placement": list(r.placement), "internal_only": r.internal_only,
                    "timeout": r.timeout,
                }
                for r in config.roles.values()
            ],
            "lanes": {
                name: {"max_concurrent": l.max_concurrent,
                       "starvation_seconds": l.starvation_seconds}
                for name, l in config.lanes.items()
            },
            "guard_rules": [
                {"id": r.id, "kind": r.kind, "locale_pack": r.locale_pack,
                 "checksum": r.checksum, "action": r.action}
                for r in config.guard_rules
            ],
            "thresholds": dict(vars(config.thresholds)),
        },
        "environment": secrets_present,
        "cluster": cluster.snapshot(),
        "single_homed_roles": cluster.single_homed_roles(),
        "lanes": scheduler.snapshot() if scheduler else {},
        "jobs": store.job_counts(),
        "waiting_by_reason": store.queued_wait_reasons(),
        "model_requests": registrar.snapshot() if registrar else [],
        "notifications": notifier.snapshot() if notifier else {},
        # 최근 오류 — 잡 본문은 없고 코드와 사유만이다.
        "recent_errors": store.recent_job_errors(limit=50),
        "counts": {
            "tenants": store.tenant_count(),
            "jobs": sum(store.job_counts().values()),
        },
    }
    return strip_tenant_identity(bundle)
