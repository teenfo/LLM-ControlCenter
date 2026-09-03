"""설정 로딩과 검증.

이 모듈은 YAML 을 읽어 동결 데이터클래스로 바꾸고, 잘못된 값을 **기동 시점에** 잡는다.
런타임에 터지는 설정 오류는 프로덕션에서 가장 비싼 종류의 버그다.

두 가지 원칙이 여기 박혀 있다:

1. **역할 이름이 계약이고, 모델은 정책이다.** 소비자는 모델명을 모른다.
2. **데이터 경계는 노드 속성이다.** `provider: ollama` 라고 내부인 것이 아니다 —
   임대 GPU 의 Ollama 는 소프트웨어가 같아도 프롬프트가 남의 기계로 나간다.
   그래서 경계는 선언으로만 받고 추론하지 않으며, 기본값은 external(fail-safe)이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

# ── 상수 ────────────────────────────────────────────────────────────────────

INTERNAL = "internal"
EXTERNAL = "external"
BOUNDARIES = (INTERNAL, EXTERNAL)

KINDS = ("generate", "embed")

MAX_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_PROMPT_CHARS = 200_000

GUARD_ACTIONS = ("off", "audit", "partial", "full", "block")

#: `partial` 등급이 남길 수 있는 뒷자리의 상한.
#:
#: 상한이 없으면 `keep_tail: 100` 같은 값이 통과하고, 그 규칙은 **값 전체를 남기는
#: "마스킹"** 이 된다 — 관제 화면에는 마스킹 규칙으로 표시되면서. 안 켜진 필터보다
#: 나쁜 것이 켜져 있다고 표시되는 안 듣는 필터다.
MAX_KEEP_TAIL = 8

#: 런타임 오버라이드로 덮어쓸 수 있는 역할 필드.
OVERRIDABLE_ROLE_FIELDS = frozenset(
    {"model", "lane", "timeout", "options", "max_prompt_chars", "placement", "tier_models"}
)

#: 덮어쓸 수 **없는** 역할 필드.
#:   kind   — embed 로 바꾸면 그 역할이 큐를 우회하는 동기 경로로 넘어간다.
#:   system — "프롬프트는 호출자 소유" 계약과 충돌한다.
#:   internal_only — 안전장치를 설정으로 풀 수 있으면 안전장치가 아니다.
FROZEN_ROLE_FIELDS = frozenset({"kind", "system", "internal_only", "name"})


class ConfigError(ValueError):
    """설정이 유효하지 않다. 기동을 멈춰야 하는 종류의 오류."""


# ── 도메인 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Node:
    """추론을 실행할 수 있는 한 곳.

    선언은 정적(시드 YAML 또는 DB), 상태는 동적(헬스 루프가 채운다).
    이 클래스는 선언만 담는다 — 살아 있는지·무슨 모델을 갖고 있는지는 cluster.py 의 몫이다.
    """

    name: str
    provider: str
    base_url: str | None = None
    api_key_env: str | None = None
    auth_header_env: str | None = None
    #: internal | external. 기본값이 external 인 것은 의도적이다(fail-safe).
    data_boundary: str = EXTERNAL
    mem_budget_gb: float | None = None
    max_concurrent: int = 1
    tags: tuple[str, ...] = ()
    #: 클라우드 노드가 선언한 지원 모델. 비어 있으면 프로바이더에게 물어본다.
    models: tuple[str, ...] = ()
    #: 비어 있지 않으면 그 테넌트들만 이 노드를 쓸 수 있다.
    tenant_affinity: tuple[str, ...] = ()
    enabled: bool = True
    metered_override: bool | None = None

    @property
    def is_internal(self) -> bool:
        return self.data_boundary == INTERNAL

    def matches_tier(self, tier: str) -> bool:
        """티어 선택자와 매칭되는가. 티어는 노드 이름 또는 태그다."""
        return tier == self.name or tier in self.tags

    def allows_tenant(self, tenant: str) -> bool:
        return not self.tenant_affinity or tenant in self.tenant_affinity


@dataclass(frozen=True)
class RouteSpec:
    """한 라우트가 바꾸는 것 — **모델뿐이다.**

    `placement` 도 `internal_only` 도 여기 없다. 있으면 라우트가 역할의 경계를 넓힐 수
    있게 되고, 그것은 "경계는 좁아지기만 한다"(불변식 I2)를 정면으로 깬다. 파서가
    그 키들을 **읽지 않는 것이 아니라 거부한다** — 안 읽으면 관리자는 적어 둔 값이
    듣는 줄 안다.
    """

    model: str
    #: 분류 프롬프트에 실리는 재료. **없으면 판정할 근거가 없다** — 가드 LLM 규칙이
    #: `description` 을 필수로 두는 것과 같은 이유다.
    description: str
    tier_models: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier_models", dict(self.tier_models or {}))


@dataclass(frozen=True)
class RoleRouting:
    """역할 안의 라우팅 정책. **역할 단위 옵트인이다** — 안 적으면 아무것도 안 바뀐다."""

    classifier: str
    routes: Mapping[str, RouteSpec]

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", dict(self.routes))


@dataclass(frozen=True)
class Role:
    """모델 정책에 이름을 붙인 것. 소비자와의 계약 단위."""

    name: str
    model: str
    kind: str = "generate"
    lane: str = "interactive"
    timeout: int = 120
    options: Mapping[str, Any] = None  # type: ignore[assignment]
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    #: 티어 선택자를 선호도 순으로. 여기 없는 티어로는 절대 배치되지 않는다.
    placement: tuple[str, ...] = (INTERNAL,)
    #: 티어별 모델 덮어쓰기. 없으면 `model` 을 쓴다.
    tier_models: Mapping[str, str] = None  # type: ignore[assignment]
    #: 요청에 system 이 없을 때만 쓰는 기본값. 요청이 항상 우선한다.
    system: str | None = None
    #: True 면 external 경계 노드에 절대 배치되지 않는다. 오버라이드로 풀 수 없다.
    internal_only: bool = False
    #: 라우팅 정책. `None` 이면 이 역할은 라우팅을 **호출조차 하지 않는다.**
    routing: "RoleRouting | None" = None

    def __post_init__(self) -> None:
        # 동결 데이터클래스에서 가변 기본값을 피하기 위한 처리.
        object.__setattr__(self, "options", dict(self.options or {}))
        object.__setattr__(self, "tier_models", dict(self.tier_models or {}))

    def model_for_tier(self, tier: str) -> str:
        """이 티어에서 쓸 모델. 티어별 덮어쓰기가 없으면 기본 모델."""
        return self.tier_models.get(tier, self.model)

    @property
    def is_embed(self) -> bool:
        return self.kind == "embed"


@dataclass(frozen=True)
class Lane:
    name: str
    max_concurrent: int = 1
    #: 이만큼 기다린 잡은 모델 친화·최소 부하보다 우선한다(기아 방지).
    starvation_seconds: int = 300


@dataclass(frozen=True)
class GuardRule:
    """가드 규칙 하나.

    `action` 은 문자열이거나 티어별 매핑이다. 마스킹 정도를 관리자가 정하기 때문이다.
    """

    id: str
    kind: str  # "pattern" | "llm"
    action: Any  # str | {internal: str, external: str}
    label: str = ""
    pattern: str | None = None
    #: 체크섬 검증기 이름. 없으면 패턴만으로 판정한다.
    #: 체크섬이 없으면 숫자 나열이 전부 PII 가 되고, 오탐이 쏟아지면 관리자가 규칙을 꺼버린다.
    checksum: str | None = None
    #: 패턴은 맞는데 **체크섬이 틀린** 매치에 적용할 등급. 기본은 버린다(`off`).
    #:
    #: 버리는 것이 늘 맞지는 않다. 한국 주민등록번호는 2020-10 부여체계 개편으로
    #: 뒷자리가 임의번호가 되어 **체크섬이 성립하지 않는다** — 그 이후 발급·재발급된
    #: 번호는 전부 "체크섬 실패 = PII 아님" 으로 읽혀 마스킹 없이 통과한다.
    #: 체크섬을 없애면 오탐이 쏟아져 관리자가 규칙을 꺼버리고(C2), 그대로 두면
    #: 진짜 PII 가 샌다. 그래서 **세 번째 칸**을 둔다 — `audit` 로 남겨서 보이게 한다.
    checksum_failed_action: str | None = None
    keep_tail: int = 0
    description: str | None = None
    #: 이 규칙이 속한 로케일 팩. common 은 항상 켜진다.
    locale_pack: str = "common"

    def action_for_boundary(self, boundary: str) -> str:
        """이 경계로 나갈 때 적용할 등급."""
        if isinstance(self.action, str):
            return self.action
        return self.action.get(boundary, "audit")

    @property
    def is_llm(self) -> bool:
        return self.kind == "llm"


@dataclass(frozen=True)
class GuardSettings:
    on_classifier_error: str = "mask"
    raw_prompt_retention_days: int = 7
    stage1_threadpool_threshold_bytes: int = 16_384
    promotion_max_false_positive_rate: float = 0.02
    classifier_min_schema_compliance: float = 0.98


@dataclass(frozen=True)
class Thresholds:
    """증설·전환 트리거. 관제 UI 경고와 문서가 이 값 하나를 함께 인용한다."""

    lane_utilization_warn: float = 0.70
    queue_wait_p95_seconds: int = 30
    starvation_trips_per_day: int = 10
    node_memory_reject_rate: float = 0.05
    single_homed_node_utilization: float = 0.60
    cost_budget_burn_warn: float = 0.80
    db_size_gb_migrate: int = 50
    tenant_count_scale_profile: int = 20
    scan_window_per_lane: int = 50
    administrative_wait_timeout_seconds: int = 900
    health_failures_to_unhealthy: int = 3
    health_successes_to_healthy: int = 2
    health_probe_interval_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: tuple[int, ...] = (2, 4, 8)
    #: 시간당 이 이상 차단되면 알린다. 규칙을 잘못 켰거나 소비자가 잘못 붙였다는 신호다.
    guard_block_spike_per_hour: int = 50
    #: 분류 실패율이 이 이상이면 알린다. **분류 실패는 판정이 아니다** —
    #: on_classifier_error 정책을 타지만, 그 사건이 늘고 있다는 것은 사람이 알아야 한다.
    classifier_failure_rate_warn: float = 0.10


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    provider: str
    est_size_gb: float = 0.0
    purpose: str = "general"
    note: str = ""


@dataclass(frozen=True)
class Pricing:
    """프로바이더·모델별 100만 토큰당 단가."""

    table: Mapping[str, Mapping[str, Mapping[str, float]]]
    assumed_max_output_tokens: int = 2048

    def rate(self, provider: str, model: str) -> tuple[float, float]:
        """(입력 단가, 출력 단가). 모르는 조합은 0 으로 본다."""
        by_provider = self.table.get(provider, {})
        entry = by_provider.get(model) or by_provider.get("*") or {}
        return (
            float(entry.get("input_per_mtok", 0.0)),
            float(entry.get("output_per_mtok", 0.0)),
        )


@dataclass(frozen=True)
class Config:
    nodes: Mapping[str, Node]
    roles: Mapping[str, Role]
    lanes: Mapping[str, Lane]
    guard_rules: tuple[GuardRule, ...]
    guard_settings: GuardSettings
    pricing: Pricing
    thresholds: Thresholds
    catalog: tuple[CatalogEntry, ...]
    #: `always_on: true` 를 선언한 팩 이름. **설정의 플래그가 실제로 여기로 온다.**
    #:
    #: 예전에는 `rules_for_locales` 가 `"common"` 을 이름으로 하드코딩해서, YAML 의
    #: `always_on` 은 읽는 사람에게만 참인 죽은 플래그였다 — `always_on: true` 를 단
    #: 새 팩이 조용히 안 켜졌고, 그 사실은 그 팩이 잡아야 할 것을 놓칠 때까지 안 드러난다.
    always_on_packs: frozenset[str] = frozenset({"common"})

    def rules_for_locales(self, locales: Iterable[str]) -> tuple[GuardRule, ...]:
        """켜진 로케일 팩 + **항상 켜지는 팩**의 규칙.

        팩을 안 켜면 그 나라 PII 는 안 잡힌다. 그래서 관제 UI 가 켜진 팩을 상시 표시한다 —
        안 켜진 필터는 없는 필터인데, 다국어에서는 켰다고 착각하기가 더 쉽다.

        로케일과 무관하게 켜져야 하는 것이 둘이다: 카드번호·이메일처럼 형태가 만국
        공통인 것(`common`), 그리고 **인젝션 구문처럼 공격자가 언어를 고르는 것**.
        한국어 테넌트에 영어 탈옥 문장을 넣는 것을 막는 데 테넌트의 로케일은 아무
        상관이 없다.
        """
        wanted = {*self.always_on_packs, *locales}
        return tuple(r for r in self.guard_rules if r.locale_pack in wanted)


# ── 로딩 ────────────────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"설정 파일이 없다: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 의 최상위는 매핑이어야 한다")
    return data


def node_from_dict(name: str, raw: Mapping[str, Any]) -> Node:
    """선언 하나를 `Node` 로. YAML 시드와 런타임 등록이 **같은 검증을 지난다.**

    두 경로가 각자 검증하면 UI 로 등록한 노드만 통과하는 조합이 생기고, 그 조합이
    하필 `data_boundary` 같은 안전 필드일 때 아무도 모르게 경계가 열린다.
    """
    return _node_from_dict(name, raw)


def _node_from_dict(name: str, raw: Mapping[str, Any]) -> Node:
    boundary = raw.get("data_boundary", EXTERNAL)
    if boundary not in BOUNDARIES:
        raise ConfigError(
            f"노드 {name}: data_boundary 는 {BOUNDARIES} 중 하나여야 한다 (받은 값: {boundary!r})"
        )
    max_concurrent = int(raw.get("max_concurrent", 1))
    if max_concurrent < 1:
        raise ConfigError(f"노드 {name}: max_concurrent 는 1 이상이어야 한다")

    mem = raw.get("mem_budget_gb")
    if mem is not None and float(mem) <= 0:
        raise ConfigError(f"노드 {name}: mem_budget_gb 는 0 보다 커야 한다")

    return Node(
        name=name,
        provider=str(raw.get("provider") or ""),
        base_url=raw.get("base_url"),
        api_key_env=raw.get("api_key_env"),
        auth_header_env=raw.get("auth_header_env"),
        data_boundary=boundary,
        mem_budget_gb=float(mem) if mem is not None else None,
        max_concurrent=max_concurrent,
        tags=tuple(raw.get("tags") or ()),
        models=tuple(raw.get("models") or ()),
        tenant_affinity=tuple(raw.get("tenant_affinity") or ()),
        enabled=bool(raw.get("enabled", True)),
        metered_override=raw.get("metered_override"),
    )


def _role_from_dict(name: str, raw: Mapping[str, Any]) -> Role:
    kind = raw.get("kind", "generate")
    if kind not in KINDS:
        raise ConfigError(f"역할 {name}: kind 는 {KINDS} 중 하나여야 한다 (받은 값: {kind!r})")

    timeout = int(raw.get("timeout", 120))
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ConfigError(f"역할 {name}: timeout 은 1~{MAX_TIMEOUT_SECONDS} 초여야 한다")

    model = raw.get("model")
    if not model:
        raise ConfigError(f"역할 {name}: model 이 필요하다")

    placement = tuple(raw.get("placement") or (INTERNAL,))
    if not placement:
        raise ConfigError(f"역할 {name}: placement 가 비어 있으면 어디에도 배치할 수 없다")

    max_chars = int(raw.get("max_prompt_chars") or DEFAULT_MAX_PROMPT_CHARS)
    if max_chars < 1:
        raise ConfigError(f"역할 {name}: max_prompt_chars 는 1 이상이어야 한다")

    return Role(
        name=name,
        model=str(model),
        kind=kind,
        lane=str(raw.get("lane", "interactive")),
        timeout=timeout,
        options=raw.get("options") or {},
        max_prompt_chars=max_chars,
        placement=placement,
        tier_models=raw.get("tier_models") or {},
        system=raw.get("system"),
        internal_only=bool(raw.get("internal_only", False)),
        routing=_routing_from_dict(name, raw.get("routing")),
    )


#: 라우트가 가질 수 **없는** 키. 여기 있는 것은 역할의 것이고 라우트는 모델만 바꾼다.
FORBIDDEN_ROUTE_KEYS = ("placement", "internal_only", "lane", "kind", "system")


def _routing_from_dict(role_name: str, raw: Any) -> "RoleRouting | None":
    """`routing:` 절을 읽는다. **기동 시점에 거부하는 것이 요지다.**

    라우팅 오류는 런타임에 조용히 드러난다 — 분류가 실패하면 기본 모델로 도니까
    "설정이 틀렸다" 가 아니라 "라우팅이 원래 잘 안 된다" 로 읽힌다. 그래서 틀릴 수
    있는 것을 전부 기동 시점으로 끌어올린다.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"역할 {role_name}: routing 은 매핑이어야 한다")

    classifier = raw.get("classifier")
    if not classifier:
        raise ConfigError(f"역할 {role_name}: routing.classifier 가 필요하다")

    routes_raw = raw.get("routes") or {}
    if not routes_raw:
        raise ConfigError(
            f"역할 {role_name}: routing.routes 가 비어 있다 — 고를 것이 없으면 "
            "라우팅을 켜지 않는 것과 같고, 켰다고 믿는 쪽이 나쁘다"
        )

    routes: dict[str, RouteSpec] = {}
    for key, spec in routes_raw.items():
        key = str(key)
        # **파서가 고를 수 있는 키만 받는다.** 판정 파싱은 마지막 줄에서
        # `[A-Za-z0-9_]+` 토큰을 집으므로(어휘 교집합), 한글·붙임표 키는 설정은
        # 통과하는데 **영원히 선택 불가**다 — 켰다고 믿는 것이 가장 나쁘다
        # (QA R-LOW1). `NONE` 은 "해당 없음" 답과, 밑줄 시작은 판정 센티널
        # (`_failed`·`_none`)과 충돌하므로 예약이다.
        if not re.fullmatch(r"[A-Za-z0-9_]+", key) or key.startswith("_"):
            raise ConfigError(
                f"역할 {role_name}: 라우트 키 {key!r} 는 쓸 수 없다 — 영숫자·밑줄만"
                " 가능하고(판정 파서가 그 토큰만 집는다) 밑줄 시작은 예약이다"
            )
        if key.upper() == "NONE":
            raise ConfigError(
                f"역할 {role_name}: 라우트 키 NONE 은 예약이다 — 분류기의 "
                "'해당 없음' 답과 구분할 수 없다"
            )
        if not isinstance(spec, Mapping):
            raise ConfigError(f"역할 {role_name}: 라우트 {key} 가 매핑이 아니다")
        present = [k for k in FORBIDDEN_ROUTE_KEYS if k in spec]
        if present:
            raise ConfigError(
                f"역할 {role_name}: 라우트 {key} 에 {', '.join(present)} 을 둘 수 없다 — "
                "라우트는 모델만 바꾼다. 경계·레인·종류는 역할의 것이고, "
                "라우트가 그것을 넓히면 경계가 좁아지기만 한다는 계약이 깨진다"
            )
        if not spec.get("model"):
            raise ConfigError(f"역할 {role_name}: 라우트 {key} 에 model 이 없다")
        if not spec.get("description"):
            raise ConfigError(
                f"역할 {role_name}: 라우트 {key} 에 description 이 없다 — "
                "분류기가 무엇을 보고 이 라우트를 고르는지가 그 문장이다"
            )
        unknown = set(spec) - {"model", "description", "tier_models"}
        if unknown:
            # 모르는 키를 묵살하면 관리자는 `timeout: 5` 가 듣는 줄 안다 —
            # 노드 등록이 모르는 키를 거부하는 것과 같은 이유다(QA R-LOW2).
            raise ConfigError(
                f"역할 {role_name}: 라우트 {key} 의 모르는 키 {sorted(unknown)} — "
                "라우트가 받는 것은 model·description·tier_models 뿐이다"
            )
        routes[key] = RouteSpec(
            model=str(spec["model"]),
            description=str(spec["description"]),
            tier_models=spec.get("tier_models") or {},
        )

    return RoleRouting(classifier=str(classifier), routes=routes)


def _guard_rule_from_dict(raw: Mapping[str, Any], locale_pack: str) -> GuardRule:
    rule_id = raw.get("id")
    if not rule_id:
        raise ConfigError(f"가드 규칙에 id 가 없다 (팩: {locale_pack})")

    kind = raw.get("kind")
    if kind not in ("pattern", "llm"):
        raise ConfigError(f"가드 규칙 {rule_id}: kind 는 pattern 또는 llm 이어야 한다")

    action = raw.get("action", "audit")
    for value in [action] if isinstance(action, str) else action.values():
        if value not in GUARD_ACTIONS:
            raise ConfigError(
                f"가드 규칙 {rule_id}: action 은 {GUARD_ACTIONS} 중 하나여야 한다 (받은 값: {value!r})"
            )

    pattern = raw.get("pattern")
    if kind == "pattern":
        if not pattern:
            raise ConfigError(f"가드 규칙 {rule_id}: pattern 규칙에는 pattern 이 필요하다")
        try:
            re.compile(pattern)
        except re.error as exc:  # 잘못된 정규식이 런타임에 터지지 않게 여기서 잡는다
            raise ConfigError(f"가드 규칙 {rule_id}: 정규식이 잘못됐다 — {exc}") from exc
    elif not raw.get("description"):
        raise ConfigError(
            f"가드 규칙 {rule_id}: llm 규칙에는 description 이 필요하다 "
            "— 관리자가 맥락을 문장으로 정의하는 것이 이 규칙의 전부다"
        )

    failed_action = raw.get("checksum_failed_action")
    if failed_action is not None:
        if not raw.get("checksum"):
            raise ConfigError(
                f"가드 규칙 {rule_id}: checksum 없이 checksum_failed_action 은 의미가 없다"
            )
        if failed_action not in GUARD_ACTIONS:
            raise ConfigError(
                f"가드 규칙 {rule_id}: checksum_failed_action 은 {GUARD_ACTIONS} 중 하나여야 한다"
            )

    return GuardRule(
        id=str(rule_id),
        kind=kind,
        action=action,
        label=str(raw.get("label", "")),
        pattern=pattern,
        checksum=raw.get("checksum"),
        checksum_failed_action=failed_action,
        keep_tail=int(raw.get("keep_tail", 0)),
        description=raw.get("description"),
        locale_pack=locale_pack,
    )


def _load_guard(
    raw: Mapping[str, Any],
) -> tuple[tuple[GuardRule, ...], GuardSettings, frozenset[str]]:
    rules: list[GuardRule] = []
    seen: set[str] = set()
    # `common` 은 이름으로도 항상 켜진다 — 기존 설정이 그 팩에 `always_on` 을
    # 안 적었을 수 있고, 플래그를 진짜로 만드는 변경이 그 팩을 끄면 안 된다.
    always_on: set[str] = {"common"}

    for pack_name, pack in (raw.get("locale_packs") or {}).items():
        if (pack or {}).get("always_on"):
            always_on.add(pack_name)
        for rule_raw in (pack or {}).get("rules") or ():
            rule = _guard_rule_from_dict(rule_raw, pack_name)
            if rule.id in seen:
                raise ConfigError(f"가드 규칙 id 가 중복됐다: {rule.id}")
            seen.add(rule.id)
            rules.append(rule)

    for rule_raw in raw.get("context_rules") or ():
        rule = _guard_rule_from_dict(rule_raw, "common")
        if rule.id in seen:
            raise ConfigError(f"가드 규칙 id 가 중복됐다: {rule.id}")
        seen.add(rule.id)
        rules.append(rule)

    settings_raw = raw.get("settings") or {}
    on_error = settings_raw.get("on_classifier_error", "mask")
    if on_error not in ("block", "mask", "allow"):
        raise ConfigError("on_classifier_error 는 block | mask | allow 중 하나여야 한다")

    settings = GuardSettings(
        on_classifier_error=on_error,
        raw_prompt_retention_days=int(settings_raw.get("raw_prompt_retention_days", 7)),
        stage1_threadpool_threshold_bytes=int(
            settings_raw.get("stage1_threadpool_threshold_bytes", 16_384)
        ),
        promotion_max_false_positive_rate=float(
            settings_raw.get("promotion_max_false_positive_rate", 0.02)
        ),
        classifier_min_schema_compliance=float(
            settings_raw.get("classifier_min_schema_compliance", 0.98)
        ),
    )
    return tuple(rules), settings, frozenset(always_on)


def load_config(config_dir: str | Path) -> Config:
    """설정 디렉터리 전체를 읽어 검증한다.

    검증 실패는 `ConfigError` 로 기동을 멈춘다. 런타임에 드러나는 설정 오류보다
    기동이 안 되는 쪽이 훨씬 싸다.
    """
    base = Path(config_dir)

    nodes = {
        name: _node_from_dict(name, raw or {})
        for name, raw in _read_yaml(base / "nodes.yaml").items()
    }
    roles = {
        name: _role_from_dict(name, raw or {})
        for name, raw in _read_yaml(base / "roles.yaml").items()
    }
    lanes = {
        name: Lane(
            name=name,
            max_concurrent=int((raw or {}).get("max_concurrent", 1)),
            starvation_seconds=int((raw or {}).get("starvation_seconds", 300)),
        )
        for name, raw in _read_yaml(base / "lanes.yaml").items()
    }

    guard_rules, guard_settings, always_on_packs = _load_guard(
        _read_yaml(base / "guard.yaml")
    )

    pricing_raw = _read_yaml(base / "pricing.yaml")
    defaults = pricing_raw.pop("defaults", {}) or {}
    pricing = Pricing(
        table=pricing_raw,
        assumed_max_output_tokens=int(defaults.get("assumed_max_output_tokens", 2048)),
    )

    thresholds_raw = _read_yaml(base / "thresholds.yaml")
    backoff = thresholds_raw.pop("retry_backoff_seconds", None)
    thresholds = Thresholds(
        **{k: v for k, v in thresholds_raw.items() if k in Thresholds.__annotations__},
        retry_backoff_seconds=tuple(backoff or (2, 4, 8)),
    )

    catalog = tuple(
        CatalogEntry(
            name=str(entry["name"]),
            provider=str(entry.get("provider", "")),
            est_size_gb=float(entry.get("est_size_gb", 0.0)),
            purpose=str(entry.get("purpose", "general")),
            note=str(entry.get("note", "")),
        )
        for entry in (_read_yaml(base / "catalog.yaml").get("models") or ())
    )

    config = Config(
        nodes=nodes,
        roles=roles,
        lanes=lanes,
        guard_rules=guard_rules,
        guard_settings=guard_settings,
        pricing=pricing,
        thresholds=thresholds,
        catalog=catalog,
        always_on_packs=always_on_packs,
    )
    validate_cross_references(config)
    return config


def _validate_routing(config: Config, role: Role, catalog_models: set[str]) -> None:
    """라우팅이 다른 파일과 맞물리는 지점. `routing` 절만 봐서는 못 잡는 것들.

    **분류 역할이 `internal_only` 여야 하는 것이 여기서 가장 중요하다.** 라우터는
    소비자 프롬프트(마스킹본)를 LLM 에 보여준다. 그 역할이 경계 밖에 배치될 수 있으면
    라우팅을 켠 것만으로 프롬프트가 밖으로 나가는 경로가 생긴다 — 가드 2단 분류기에
    같은 제약이 걸려 있는 것과 같은 이유다.
    """
    routing = role.routing
    assert routing is not None

    classifier = config.roles.get(routing.classifier)
    if classifier is None:
        raise ConfigError(
            f"역할 {role.name}: 라우팅 분류 역할 {routing.classifier!r} 이 없다"
        )
    if not classifier.internal_only:
        raise ConfigError(
            f"역할 {role.name}: 라우팅 분류 역할 {routing.classifier!r} 이 "
            "internal_only 가 아니다 — 라우터는 소비자 프롬프트를 LLM 에 보여주므로 "
            "경계 밖에 배치될 수 있으면 그 자체가 유출 경로다"
        )

    # 카탈로그에 없는 모델은 설치 요청 경로도 못 타므로 그 라우트는 영원히 안 돈다.
    # 라우팅은 실패해도 기본 모델로 도니까 **조용히** 안 도는 것이 문제다.
    if catalog_models:
        for key, spec in routing.routes.items():
            unknown = [
                model
                for model in (spec.model, *spec.tier_models.values())
                if model not in catalog_models
            ]
            if unknown:
                raise ConfigError(
                    f"역할 {role.name}: 라우트 {key} 의 모델 {unknown} 이 "
                    "카탈로그에 없다 — 그 라우트는 조용히 안 돈다"
                )


def validate_cross_references(config: Config) -> None:
    """파일을 넘나드는 검증. 개별 파일만 봐서는 못 잡는 것들."""
    catalog_models = {entry.name for entry in config.catalog}

    for role in config.roles.values():
        if role.lane not in config.lanes:
            raise ConfigError(
                f"역할 {role.name}: 레인 {role.lane!r} 이 lanes.yaml 에 없다"
            )

        if role.routing is not None:
            _validate_routing(config, role, catalog_models)

        # placement 티어가 어떤 노드와도 매칭되지 않으면 그 역할은 영원히 못 돈다.
        # 시드 노드만 보고 판정하므로 경고성이지만, 오타를 기동 시점에 잡아준다.
        if config.nodes:
            matched = [
                tier
                for tier in role.placement
                if any(node.matches_tier(tier) for node in config.nodes.values())
            ]
            if not matched:
                raise ConfigError(
                    f"역할 {role.name}: placement {role.placement} 가 어떤 노드와도 매칭되지 않는다"
                )

        # internal_only 역할이 external 노드에 닿을 수 있으면 안전장치가 무의미하다.
        if role.internal_only:
            leaking = [
                node.name
                for node in config.nodes.values()
                if not node.is_internal
                and any(node.matches_tier(tier) for tier in role.placement)
            ]
            if leaking:
                raise ConfigError(
                    f"역할 {role.name}: internal_only 인데 external 노드 {leaking} 에 닿는다. "
                    "가드 분류기가 원문을 경계 밖으로 보내는 경로가 생긴다"
                )


# ── 런타임 오버라이드 ────────────────────────────────────────────────────────


def validate_role_fields(fields: Mapping[str, Any]) -> dict[str, str]:
    """오버라이드 필드를 검증한다. 반환값은 {필드: 사유} 형태의 오류 목록(비면 통과)."""
    errors: dict[str, str] = {}

    for key, value in fields.items():
        if key in FROZEN_ROLE_FIELDS:
            errors[key] = "이 필드는 오버라이드할 수 없다"
            continue
        if key not in OVERRIDABLE_ROLE_FIELDS:
            errors[key] = "알 수 없는 필드"
            continue

        if key == "timeout":
            try:
                if not 1 <= int(value) <= MAX_TIMEOUT_SECONDS:
                    errors[key] = f"1~{MAX_TIMEOUT_SECONDS} 범위를 벗어났다"
            except (TypeError, ValueError):
                errors[key] = "정수가 아니다"
        elif key == "placement":
            if not isinstance(value, (list, tuple)) or not value:
                errors[key] = "비어 있지 않은 목록이어야 한다"
        elif key == "options" and not isinstance(value, Mapping):
            errors[key] = "매핑이어야 한다"
        elif key == "tier_models" and not isinstance(value, Mapping):
            errors[key] = "매핑이어야 한다"
        elif key == "max_prompt_chars":
            try:
                if int(value) < 1:
                    errors[key] = "1 이상이어야 한다"
            except (TypeError, ValueError):
                errors[key] = "정수가 아니다"

    return errors


def apply_override(role: Role, fields: Mapping[str, Any]) -> Role:
    """역할에 오버라이드를 얹는다. 호출 전에 `validate_role_fields` 로 걸러야 한다.

    `internal_only` 역할의 placement 는 얹은 뒤에도 좁아지기만 한다 —
    안전장치를 설정으로 풀 수 있으면 안전장치가 아니다.
    """
    clean = {k: v for k, v in fields.items() if k in OVERRIDABLE_ROLE_FIELDS}

    if "placement" in clean:
        clean["placement"] = tuple(clean["placement"])
    if "options" in clean:
        clean["options"] = dict(clean["options"])
    if "tier_models" in clean:
        clean["tier_models"] = dict(clean["tier_models"])
    if "timeout" in clean:
        clean["timeout"] = int(clean["timeout"])
    if "max_prompt_chars" in clean:
        clean["max_prompt_chars"] = int(clean["max_prompt_chars"])

    return replace(role, **clean)


def merge_overrides(
    roles: Mapping[str, Role], overrides: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Role], dict[str, dict[str, str]]]:
    """오버라이드를 역할 위에 병합한다.

    **잘못된 오버라이드 행이 기동을 막지 않는다.** 건너뛰고 사유를 함께 돌려주며,
    상태 API 가 그것을 노출한다. 데이터 한 줄 때문에 서비스가 안 뜨면 롤백이 더 어려워진다.
    """
    merged = dict(roles)
    invalid: dict[str, dict[str, str]] = {}

    for role_name, fields in overrides.items():
        if role_name not in roles:
            invalid[role_name] = {"role": "알 수 없는 역할"}
            continue
        errors = validate_role_fields(fields)
        if errors:
            invalid[role_name] = errors
            continue
        merged[role_name] = apply_override(roles[role_name], fields)

    return merged, invalid
