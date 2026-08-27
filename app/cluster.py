"""클러스터 — 노드 레지스트리 · 헬스 · 배치 라우팅 · 원자적 예약.

이전 스케줄러는 **잡 하나**를 골랐다. 여기서는 **(잡, 노드) 쌍**을 고른다.

세 가지가 이 모듈의 요지다:

1. **선택과 예약이 원자적이다.** 필터를 통과시킨 뒤 따로 배치하면 레인 루프가 병렬로
   도는 순간 같은 슬롯을 두 잡이 잡는다. 메모리도 같다 — 20GB 잔여를 두 잡이 각각
   확인하고 각각 18GB 를 올린다.
2. **티어가 모델 친화보다 위다.** 뒤집으면 경계 밖에 같은 모델이 웜으로 떠 있다는
   이유로 내부 우선 선언을 무시하고 과금 경로를 탄다. 성능 휴리스틱이 비용·데이터
   경계 정책을 이길 수 없다.
3. **용량 불가와 행정적 부재를 나눈다.** "티어가 전부 비활성이면 즉시 실패" 는 틀렸다 —
   관리자가 노드 한 대를 5분 정비하려고 내리면 그 티어 잡이 전부 하드 실패한다.

동기 임베딩 경로도 이 모듈의 같은 `place()` 를 부른다. 큐만 우회하고 배치·경계·비용은
우회하지 않는다.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import EXTERNAL, INTERNAL, Config, ConfigError, Node, Role, node_from_dict
from .cost import CostAccountant
from .i18n import ApiError
from .providers import HealthResult, Provider, build_provider
from .store import SqliteStore, TenantScope

# 배치 결과
PLACED = "placed"
WAIT = "wait"   # 행정적 부재 — 대기시키고 타임아웃 후 실패
FAIL = "fail"   # 용량 불가 — 즉시 실패시킨다

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"
DRAINING = "draining"

#: 직전 실패 노드 탈락 사유. **다른 후보가 없으면 되살린다** — 배제는 선호이지
#: 금지가 아니다. 노드 한 대짜리 구성에서 금지는 곧 재시도 불능이다.
LAST_FAILED = "last_failed_node"

#: **이 잡의 생애 동안 변하지 않는** 탈락 사유. 관리자가 무엇을 해도 풀리지 않는다 —
#: 역할의 `internal_only` 는 오버라이드 불가 필드이고, 가드 판정은 제출 시점에 잡에
#: 박힌다. 전부 이것뿐이면 기다릴 이유가 없으므로 즉시 실패시킨다.
#:
#: 에어갭·`tenant_affinity`·헬스는 여기 없다 — 관리자가 되돌릴 수 있고, 되돌릴 수
#: 있는 것을 하드 실패시키면 정비 5분에 그 티어 잡이 전멸한다(B5).
PERMANENT_REJECTIONS = frozenset({"boundary_internal_only", "boundary_blocked_by_guard"})

#: 노드 등록 본문이 받는 필드. `name` 은 따로 뽑으므로 여기 없다.
NODE_REGISTRATION_FIELDS = frozenset(
    {
        "provider", "base_url", "api_key_env", "auth_header_env", "data_boundary",
        "mem_budget_gb", "max_concurrent", "tags", "models", "tenant_affinity",
        "enabled", "metered_override",
    }
)


@dataclass
class NodeState:
    """노드의 런타임 상태. 선언(`Node`)과 분리돼 있다."""

    node: Node
    provider: Provider
    status: str = UNKNOWN
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    models: frozenset[str] = frozenset()
    loaded_model: str | None = None
    running: int = 0
    reserved_mem_gb: float = 0.0
    last_probe_at: float | None = None
    last_error: str | None = None
    #: 강제 드레이닝됐는가. 실행 중인 잡을 세는 것과는 별개다 — 그 둘을 섞으면
    #: 드레이닝을 풀었을 때 노드가 비어 있는 것처럼 보인다.
    force_drained: bool = False

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def available_slots(self) -> int:
        return max(0, self.node.max_concurrent - self.running)

    @property
    def free_mem_gb(self) -> float:
        if self.node.mem_budget_gb is None:
            return float("inf")
        return max(0.0, self.node.mem_budget_gb - self.reserved_mem_gb)

    @property
    def load_ratio(self) -> float:
        return self.running / max(1, self.node.max_concurrent)

    @property
    def is_metered(self) -> bool:
        return self.provider.capabilities.metered


@dataclass(frozen=True)
class Placement:
    """확정된 배치. 이 객체가 존재한다는 것은 슬롯·메모리·비용이 이미 차감됐다는 뜻이다."""

    job_id: str
    node: str
    model: str
    tier: str
    provider: str
    reserved_cost_usd: float = 0.0
    reserved_mem_gb: float = 0.0


@dataclass(frozen=True)
class PlacementResult:
    outcome: str
    placement: Placement | None = None
    #: 사람이 읽는 대기·실패 사유. 관제 UI 의 "대기 사유별 잡 수" 카드가 이것으로 묶는다.
    reason: str | None = None
    code: str | None = None
    #: 노드별 탈락 사유. 진단용이며 UI 가 "왜 안 도는지" 를 설명하는 근거다.
    rejections: Mapping[str, str] = field(default_factory=dict)


class Cluster:
    """노드 풀. 배치 결정과 예약을 소유한다."""

    def __init__(
        self,
        config: Config,
        store: SqliteStore,
        *,
        accountant: CostAccountant | None = None,
        providers: Mapping[str, Provider] | None = None,
        now: Callable[[], float] = time.time,
        notifier: Any = None,
        airgap: bool = False,
    ) -> None:
        self._config = config
        self._store = store
        self._now = now
        # 에어갭이면 경계 밖 노드로 아예 배치하지 않는다. 등록만 막으면 시드 설정에
        # 이미 들어 있던 클라우드 노드가 그대로 살아 있고, **설정에 남아 있는데
        # 조용히 나가는 것**이 에어갭에서 가장 나쁜 실패다. 관리자가 드레이닝을
        # 풀어도 되돌릴 수 없도록 배치 필터에 건다.
        self._airgap = airgap
        # 알림기가 없어도 동작한다 — 관제는 알림 없이도 서고, 알림 배선이 없다고
        # 추론이 멈추면 안 된다. 있으면 헬스 전이를 그쪽에 알린다.
        self._notifier = notifier
        self._accountant = accountant or CostAccountant(config.pricing, store, now=now)

        # 예약의 임계 구역을 지키는 락. 안에서 await 하지 않는다 —
        # 확인과 차감 사이에 다른 코루틴이 끼어들면 원자성이 깨진다.
        self._lock = threading.Lock()

        # **YAML 은 시드이고 DB 가 권위다.** 순서가 중요하다 — 시드를 먼저 깔고
        # DB 선언으로 덮는다. 반대로 하면 관제 UI 에서 고친 노드가 재기동 때마다
        # YAML 값으로 되돌아간다.
        self._nodes: dict[str, NodeState] = {}
        for name, node in config.nodes.items():
            self._install(node, providers)
        for declaration in self._load_declarations():
            self._install(declaration, providers)

        self._model_sizes = {e.name: e.est_size_gb for e in config.catalog}

    def _install(
        self, node: Node, providers: Mapping[str, Provider] | None = None
    ) -> NodeState:
        provider = (providers or {}).get(node.name) or build_provider(node)
        state = NodeState(node=node, provider=provider)
        self._nodes[node.name] = state
        return state

    def _load_declarations(self) -> list[Node]:
        """DB 에 저장된 노드 선언. **읽기 실패가 기동을 막지 않는다.**

        노드 한 줄이 깨졌다고 컨트롤 플레인이 안 뜨면 그 줄을 고칠 방법도 없어진다.
        """
        found: list[Node] = []
        try:
            rows = self._store.list_nodes()
        except Exception:
            return found
        for row in rows:
            name = row.pop("name", "")
            try:
                found.append(node_from_dict(name, row))
            except ConfigError:
                continue
        return found

    # -- 조회 -----------------------------------------------------------------

    @property
    def airgap(self) -> bool:
        return self._airgap

    @property
    def nodes(self) -> Mapping[str, NodeState]:
        return self._nodes

    def state(self, node: str) -> NodeState | None:
        return self._nodes.get(node)

    def provider_for(self, node: str) -> Provider:
        return self._nodes[node].provider

    def metered_nodes(self) -> tuple[str, ...]:
        """과금 노드 목록. 크래시 복구가 이중 청구를 피하는 데 쓴다."""
        return tuple(n.name for n in self._nodes.values() if n.is_metered)

    def model_size_gb(self, model: str) -> float:
        """카탈로그가 아는 크기. 모르면 0 — 모르는 것을 이유로 잡을 죽이지 않는다."""
        return self._model_sizes.get(model, 0.0)

    # -- 배치 -----------------------------------------------------------------

    def effective_placement(self, role: Role, snapshot: Sequence[str]) -> tuple[str, ...]:
        """**스냅샷 ∩ 현재 역할 설정.** 좁히기만 하고 넓히지 않는다.

        스냅샷만 쓰면 안전 통제가 우회된다 — 비용 사고로 관리자가 external 티어를 뺐는데
        큐에 있던 잡은 스냅샷에 그것을 들고 그대로 나간다. 스냅샷은 재현성에는 맞지만
        안전 통제에는 정확히 반대로 동작한다.
        """
        if not snapshot:
            return tuple(role.placement)
        current = set(role.placement)
        return tuple(tier for tier in snapshot if tier in current)

    def place(
        self,
        *,
        job_id: str,
        tenant_id: str,
        service_id: str,
        role: Role,
        placement_snapshot: Sequence[str] = (),
        prompt: str = "",
        tenant_budget: float | None = None,
        service_budget: float | None = None,
        last_failed_node: str | None = None,
        max_output_tokens: int | None = None,
        allowed_boundaries: Iterable[str] = (INTERNAL, EXTERNAL),
    ) -> PlacementResult:
        """(잡, 노드) 쌍을 고르고 슬롯·메모리·비용을 즉시 차감한다.

        동기 함수다. 안에서 await 하지 않으므로 확인과 차감 사이에 아무도 끼어들지 못한다.
        """
        tiers = self.effective_placement(role, placement_snapshot)
        if not tiers:
            # 교집합이 비었다 = 관리자가 티어를 뺐다. 되돌릴 수 있으므로 대기시킨다.
            return PlacementResult(
                WAIT, reason="placement_narrowed", code="no_placement"
            )

        scope = TenantScope(tenant_id)
        rejections: dict[str, str] = {}
        boundaries = frozenset(allowed_boundaries)

        with self._lock:
            # **예산 현황은 락 안에서 한 번만 읽는다.**
            #
            # 밖에서 읽으면 읽기와 예약 사이가 벌어져 동시 배치 두 건이 각각
            # "예산 남음" 을 보고 둘 다 예약한다 — 락을 둔 이유가 정확히 그것을
            # 막는 것이다(B3-1). 후보마다 읽으면 노드 수 × 2회가 되므로 한 번만
            # 읽고, metered 후보를 만나기 전까지는 아예 안 읽는다.
            budget: Any = None
            candidates: list[tuple[int, NodeState, str, str, float, float]] = []
            # 직전 실패 노드만 남았을 때 쓸 후보. **배제는 선호이지 금지가 아니다** —
            # 아래 주석 참고.
            fallback: list[tuple[int, NodeState, str, str, float, float]] = []

            for tier_index, tier in enumerate(tiers):
                for state in self._nodes.values():
                    if not state.node.matches_tier(tier):
                        continue

                    model = role.model_for_tier(tier)
                    verdict = self._reject_reason(
                        state, role, model, tenant_id, last_failed_node, boundaries
                    )
                    if verdict and verdict != LAST_FAILED:
                        rejections.setdefault(state.name, verdict)
                        continue

                    cost = self._accountant.estimate_upper_bound(
                        provider=state.node.provider, model=model,
                        prompt=prompt, max_output_tokens=max_output_tokens,
                    )
                    if cost > 0 and budget is None:
                        budget = self._accountant.budget_snapshot(
                            scope, tenant_limit=tenant_budget,
                            service_limit=service_budget, service_id=service_id,
                        )
                    affordable, tripped = (
                        (True, None) if cost <= 0
                        else self._accountant.afford_with(budget, cost)
                    )
                    if not affordable:
                        rejections.setdefault(state.name, f"budget_exceeded:{tripped}")
                        continue

                    mem = self.model_size_gb(model) if state.provider.capabilities.uses_memory_budget else 0.0
                    entry = (tier_index, state, model, tier, cost, mem)
                    (fallback if verdict == LAST_FAILED else candidates).append(entry)

            if not candidates and fallback:
                # **다른 후보가 있을 때만 배제한다.**
                #
                # 재시도가 재배치를 동반해야 한다는 것(B7)은 죽은 노드로 3회 재시도하고
                # 끝나지 말라는 뜻이지, 노드를 영구히 금지하라는 뜻이 아니었다.
                # 노드가 한 대뿐인 Starter 구성에서는 배제가 곧 **재시도 불능**이고,
                # 크래시 복구가 `last_failed_node` 를 심기 때문에 컨트롤 플레인 재기동만
                # 해도 그 잡들이 통째로 멈춘다.
                #
                # 정말 죽은 노드는 연속 실패 3회에 `unhealthy` 가 되어 위에서 걸린다.
                # 그것이 사실이고 `last_failed_node` 는 힌트다. 힌트가 사실을 이기면 안 된다.
                # 재시도 백오프(2→4→8초)가 되돌아가는 간격을 이미 벌려 준다.
                for name in tuple(rejections):
                    if rejections[name] == LAST_FAILED:
                        del rejections[name]
                candidates = fallback

            if not candidates:
                return self._no_candidate_result(role, tiers, rejections)

            tier_index, state, model, tier, cost, mem = min(
                candidates, key=lambda c: self._rank(c[0], c[1], c[2])
            )

            # 차감 — 여기까지 락 안이다.
            state.running += 1
            state.reserved_mem_gb += mem

            # **비용 예약도 락 안에서 한다.**
            #
            # 밖에서 하면 확인과 차감 사이가 벌어져, 동시 배치 두 건이 각각
            # "예산 남음" 을 보고 둘 다 예약한다 — 락을 둔 이유가 정확히
            # 그것을 막는 것인데(B3-1) 비용만 그 밖에 있었다.
            if cost > 0:
                self._store.update_job(scope, job_id, cost_reserved_usd=cost)

        return PlacementResult(
            PLACED,
            placement=Placement(
                job_id=job_id, node=state.name, model=model, tier=tier,
                provider=state.node.provider,
                reserved_cost_usd=cost, reserved_mem_gb=mem,
            ),
            rejections=rejections,
        )

    def release(self, placement: Placement) -> None:
        """슬롯과 메모리를 되돌린다. 잡이 어떻게 끝나든 반드시 불러야 한다.

        안 부르면 노드가 조용히 가득 찬 것처럼 보이고, 관리자는 부하가 없는데 큐가
        쌓이는 것을 디버깅하게 된다.
        """
        with self._lock:
            state = self._nodes.get(placement.node)
            if state is None:
                return
            state.running = max(0, state.running - 1)
            state.reserved_mem_gb = max(0.0, state.reserved_mem_gb - placement.reserved_mem_gb)

    # -- 필터 -----------------------------------------------------------------

    def _reject_reason(
        self,
        state: NodeState,
        role: Role,
        model: str,
        tenant_id: str,
        last_failed_node: str | None,
        allowed_boundaries: frozenset[str] = frozenset({INTERNAL, EXTERNAL}),
    ) -> str | None:
        """노드가 이 잡을 받을 수 없는 이유. `None` 이면 후보다."""
        node = state.node

        # **바뀔 수 없는 것을 먼저 본다.**
        #
        # 순서가 진단의 정확도를 정한다. 헬스를 먼저 보면, 경계 때문에 애초에 못 가는
        # 노드가 마침 unhealthy 일 때 "unhealthy" 로 보고되고, 그 잡은 노드가 살아나면
        # 될 것처럼 대기하다 900초 뒤 `administrative_wait_timeout` 으로 죽는다 —
        # 관리자를 기다렸다는 뜻인데 사실은 어떤 관리자도 풀 수 없는 조건이었다.
        #
        # 데이터 경계 — 두 겹이다.
        #   ① 역할의 internal_only: 어떤 상황에서도 경계 밖에 가지 않는다(설정으로 못 푼다).
        #   ② 가드가 좁힌 허용 경계: 차단 등급에 걸린 경계가 여기서 빠져 있다.
        # 둘 다 **이 잡의 생애 동안 변하지 않는다** — 역할의 internal_only 는 오버라이드
        # 불가 필드이고, 가드 판정은 제출 시점에 잡에 박힌다.
        if role.internal_only and not node.is_internal:
            return "boundary_internal_only"
        if node.data_boundary not in allowed_boundaries:
            return "boundary_blocked_by_guard"

        # 아래부터는 관리자가 되돌릴 수 있는 것들이다.
        if self._airgap and not node.is_internal:
            return "airgap_external_disabled"

        if not node.allows_tenant(tenant_id):
            return "tenant_affinity"

        if not node.enabled:
            return "disabled"
        if state.status == DRAINING:
            return "draining"

        # unknown 은 통과시킨다. 헬스 정보를 아직 못 받았다는 이유로 큐를 멈추면
        # 기동 직후가 항상 정지 상태가 된다.
        if state.status == UNHEALTHY:
            return "unhealthy"

        if state.provider.capabilities.requires_model_install and model not in state.models:
            # 헬스 프로브 전에는 models 가 비어 있다. 그때는 막지 않는다 —
            # 모른다는 이유로 막으면 기동 직후 전부 대기한다.
            if state.models:
                return "model_not_installed"

        if state.provider.capabilities.uses_memory_budget:
            needed = self.model_size_gb(model)
            if needed and needed > state.free_mem_gb:
                return "memory_budget"

        if state.available_slots <= 0:
            return "no_slot"

        if last_failed_node and state.name == last_failed_node:
            # 재시도는 재배치를 동반한다 — 죽은 노드로 3회 재시도하고 끝나면 안 된다.
            # **다만 이건 마지막 필터다.** 호출자가 다른 후보가 없을 때 되살릴 수
            # 있도록 맨 뒤에 두고, 여기까지 온 노드는 그 외의 조건을 모두 통과했다.
            return LAST_FAILED

        return None

    def _rank(self, tier_index: int, state: NodeState, model: str) -> tuple:
        """정렬 키. 앞에 오는 것이 우선이다.

        티어 → 모델 친화 → 최소 부하. **티어가 맨 앞인 것이 정책의 전부다.**
        """
        model_is_warm = 0 if state.loaded_model == model else 1
        return (tier_index, model_is_warm, state.load_ratio, state.name)

    def _no_candidate_result(
        self, role: Role, tiers: Sequence[str], rejections: Mapping[str, str]
    ) -> PlacementResult:
        """후보가 없을 때 — 영원히 못 도는가, 지금만 못 도는가.

        판정 기준은 현재 사용량이 아니라 노드 **용량**이다. 21GB 모델을 20GB 노드뿐인
        클러스터에 던지면 큐가 비어도 못 도니까 조용히 쌓아두지 않는다.
        """
        # **영원히 못 도는 것을 900초 기다리게 하지 않는다.**
        #
        # 모든 후보가 이 잡의 생애 동안 변하지 않는 이유로 탈락했다면, 기다려도
        # 달라지지 않는다. 그런데도 WAIT 로 두면 소비자는 15분을 매달린 뒤
        # `administrative_wait_timeout` 을 받는다 — "관리자를 기다렸다" 는 뜻이라
        # 실제 원인(가드가 경계를 좁혔다)을 아무도 못 찾는다.
        if rejections and all(r in PERMANENT_REJECTIONS for r in rejections.values()):
            return PlacementResult(
                FAIL,
                reason=_dominant_reason(rejections),
                code="boundary_impossible",
                rejections=rejections,
            )

        for tier in tiers:
            for state in self._nodes.values():
                if not state.node.matches_tier(tier):
                    continue
                if role.internal_only and not state.node.is_internal:
                    continue

                model = role.model_for_tier(tier)
                needed = self.model_size_gb(model)
                capacity = state.node.mem_budget_gb
                if (
                    not state.provider.capabilities.uses_memory_budget
                    or not needed
                    or capacity is None
                    or capacity >= needed
                ):
                    # 용량은 있다. 지금 못 도는 것은 행정적·일시적 이유다.
                    reason = _dominant_reason(rejections)
                    return PlacementResult(
                        WAIT, reason=reason, code="no_placement", rejections=rejections
                    )

        return PlacementResult(
            FAIL,
            reason="capacity_impossible",
            code="capacity_impossible",
            rejections=rejections,
        )

    # -- 헬스 -----------------------------------------------------------------

    def record_success(self, node: str) -> None:
        """성공 1회. **회복은 연속 2회를 요구한다** — 플래핑 방지."""
        state = self._nodes.get(node)
        if state is None:
            return
        state.consecutive_failures = 0
        state.consecutive_successes += 1
        if (
            state.status != DRAINING
            and state.consecutive_successes >= self._config.thresholds.health_successes_to_healthy
        ):
            state.status = HEALTHY
            state.last_error = None
        self._persist_health(state)
        self._announce(state)

    def record_failure(self, node: str, error: str = "") -> None:
        """실패 1회. **1회로 죽이지 않는다** — 연속 3회여야 unhealthy 다."""
        state = self._nodes.get(node)
        if state is None:
            return
        state.consecutive_successes = 0
        state.consecutive_failures += 1
        state.last_error = error or state.last_error
        if state.consecutive_failures >= self._config.thresholds.health_failures_to_unhealthy:
            if state.status != DRAINING:
                state.status = UNHEALTHY
        self._persist_health(state)
        self._announce(state)

    async def probe(self, node: str) -> bool:
        """노드에 헬스 요청을 보내고 모델 인벤토리를 갱신한다."""
        state = self._nodes.get(node)
        if state is None:
            return False

        # **프로바이더가 예외를 던져도 여기서 끝난다.**
        #
        # 노드가 200 에 비정형 본문을 주면 `health()` 안의 JSON 파싱이 터진다.
        # 그 예외가 여기를 지나 `probe_all` 까지 올라가면 **그 다음 노드들이 이번
        # 주기에 프로브를 못 받고**, 정작 문제의 노드는 unhealthy 판정도 못 받아
        # 계속 배치된다. 배경 루프의 `suppress` 가 그 사실을 통째로 삼킨다.
        #
        # 프로바이더는 나중에 추가되는 것이고, 이 모듈이 그것을 신뢰할 이유가 없다.
        try:
            result = await state.provider.health()
        except Exception as exc:
            result = HealthResult(ok=False, error=f"프로브 예외: {exc}")
        state.last_probe_at = self._now()

        if result.ok:
            state.models = frozenset(result.models)
            state.loaded_model = result.loaded_model
            self.record_success(node)
        else:
            self.record_failure(node, result.error or "probe 실패")
        return result.ok

    async def probe_all(self) -> dict[str, bool]:
        """모든 노드를 **동시에** 프로브한다.

        순차로 돌면 죽은 노드 N 대에 한 바퀴가 N × 타임아웃이다 — 노드 5대가
        죽으면 50초짜리 주기가 되고, 그 사이 살아난 노드는 계속 못 쓴다.
        느린 노드 하나가 나머지 전부의 갱신을 미루면 안 된다.
        """
        names = list(self._nodes)
        results = await asyncio.gather(
            *(self.probe(name) for name in names), return_exceptions=True
        )
        return {
            name: (result is True)
            for name, result in zip(names, results)
        }

    # -- 등록 -----------------------------------------------------------------

    async def register_node(
        self, raw: Mapping[str, Any], *, actor: str = "", airgap: bool = False
    ) -> tuple[NodeState, bool]:
        """노드를 편입한다. **노드에는 아무것도 설치하지 않는다** — URL 만 받는다.

        등록 즉시 프로브해서 도달성·모델 인벤토리를 수집하고, 실패해도 등록은 남긴다.
        실패를 등록 실패로 처리하면 관리자가 오타와 "아직 안 켬" 을 구분할 수 없다.
        **다만 그 사실을 반환값으로 즉시 알린다** — 설치 후에 조용히 안 붙는 것이
        제품에서 가장 나쁜 경험이다.

        반환의 두 번째 값이 프로브 도달 여부다. `status` 와 다른 값인 것이 중요하다 —
        헬스는 플래핑 방지를 위해 연속 2회 성공을 요구하므로 방금 등록한 노드는 잘
        붙었어도 `unknown` 이다. **"연결됐는가" 와 "안정적인가" 는 다른 질문이고,
        등록 화면이 필요한 것은 앞쪽이다.**

        자동 발견은 하지 않는다. 노드가 조용히 늘면 비용과 데이터 경계가 조용히 는다.
        """
        node = _node_from_registration(dict(raw))

        if node.name in self._nodes:
            raise ApiError("invalid_field", status=409, params={"field": "name"})

        # 경계 밖 노드는 공개망을 지난다는 뜻이므로 TLS·인증을 필수로 강제한다.
        if not node.is_internal and node.base_url:
            if not node.base_url.lower().startswith("https://"):
                raise ApiError("external_node_requires_auth", status=400)
            if not (node.api_key_env or node.auth_header_env):
                raise ApiError("external_node_requires_auth", status=400)

        if airgap and not node.is_internal:
            # 설정에 남아 있는데 조용히 실패하는 것이 최악이다. 등록에서 거절한다.
            raise ApiError("airgap_cloud_disabled", status=400)

        # **먼저 영속화한다.** 메모리에만 두면 재기동 한 번에 사라지고,
        # 그 노드에서 돌던 잡은 복구 후 배치 불가가 된다.
        self._store.save_node(
            {
                "name": node.name, "provider": node.provider,
                "base_url": node.base_url, "api_key_env": node.api_key_env,
                "auth_header_env": node.auth_header_env,
                "data_boundary": node.data_boundary,
                "mem_budget_gb": node.mem_budget_gb,
                "max_concurrent": node.max_concurrent,
                "tags": list(node.tags), "models": list(node.models),
                "tenant_affinity": list(node.tenant_affinity),
                "enabled": node.enabled, "metered_override": node.metered_override,
            },
            actor=actor or "platform_admin",
        )
        state = self._install(node)
        self._store.audit(
            actor or "platform_admin", "register_node", target=node.name,
            detail={
                "provider": node.provider,
                "data_boundary": node.data_boundary,
                "max_concurrent": node.max_concurrent,
                "tenant_affinity": list(node.tenant_affinity),
            },
        )

        try:
            reachable = await self.probe(node.name)
        except Exception as exc:  # 프로브 실패가 등록을 되돌리지는 않는다
            self.record_failure(node.name, str(exc))
            reachable = False
        return state, reachable

    def drain(self, node: str, *, force: bool = False) -> None:
        """노드를 비활성화한다.

        즉시 차단이 아니라 `draining` 이다 — 신규 배치를 막고 실행 중인 잡은 완료시킨다.
        즉시 비우려면 `force=True`.
        """
        state = self._nodes.get(node)
        if state is None:
            return
        state.status = DRAINING
        if force:
            # **카운터를 0 으로 밀지 않는다.**
            #
            # 실행 중인 잡은 여전히 노드에서 돌고 있고, 끝날 때 `release()` 가
            # 카운터를 한 번 더 내린다(`max(0, ...)` 라 0 에 머문다). 그 상태로
            # `undrain` 하면 노드는 비어 있는 것처럼 보이는데 실제로는 잡이
            # 돌고 있어서 **동시성 상한을 넘겨 배치된다.**
            #
            # 강제 드레이닝의 목적은 "신규를 즉시 막는 것" 이고 그것은
            # `DRAINING` 상태가 이미 한다. 세는 것과 막는 것은 다른 일이다.
            state.force_drained = True
        self._persist_health(state)

    def undrain(self, node: str) -> None:
        state = self._nodes.get(node)
        if state is None:
            return
        state.force_drained = False
        state.status = UNKNOWN
        state.consecutive_failures = 0
        state.consecutive_successes = 0
        self._persist_health(state)

    def _announce(self, state: NodeState) -> None:
        """헬스 전이를 알림기에 넘긴다. **보낼지 말지는 알림기가 정한다.**

        여기서 판정하면 성공·실패 두 경로가 각자 규칙을 갖게 되고, 언젠가 한쪽만
        고쳐져서 "죽을 때는 알리는데 살아날 때는 안 알리는" 상태가 된다.
        """
        if self._notifier is None:
            return
        event = "node_recovered" if state.status == HEALTHY else "node_offline"
        if state.status not in (HEALTHY, UNHEALTHY):
            return   # unknown·draining 은 전이 알림 대상이 아니다
        self._notifier.observe(
            f"node:{state.name}", state.status, event=event, node=state.name
        )

    def _persist_health(self, state: NodeState) -> None:
        self._store.upsert_node_health(
            state.name,
            status=state.status,
            consecutive_failures=state.consecutive_failures,
            consecutive_successes=state.consecutive_successes,
            last_probe_at=state.last_probe_at,
            loaded_model=state.loaded_model,
            error=state.last_error,
            models=sorted(state.models),
        )

    # -- 관제 -----------------------------------------------------------------

    def single_homed_roles(self) -> dict[str, str]:
        """한 노드에만 있는 모델을 쓰는 역할 → 그 노드.

        자동 복제는 하지 않는다(용량 판단은 사람 몫). 다만 **관제 센터가 안 보여주면
        사람이 판단할 수가 없다** — 모델이 노드 A 에만 있고 A 가 포화면 B·C 가 놀아도
        그 역할만 계속 굶는다.
        """
        result: dict[str, str] = {}
        for role in self._config.roles.values():
            for tier in role.placement:
                model = role.model_for_tier(tier)
                hosts = [
                    s.name
                    for s in self._nodes.values()
                    if s.node.matches_tier(tier)
                    and s.node.enabled
                    and (not s.provider.capabilities.requires_model_install or model in s.models)
                ]
                if len(hosts) == 1:
                    result[role.name] = hosts[0]
        return result

    def snapshot(self) -> list[dict[str, Any]]:
        """관제 UI 용 노드 그리드."""
        return [
            {
                "node": s.name,
                "provider": s.node.provider,
                "data_boundary": s.node.data_boundary,
                "status": s.status,
                "enabled": s.node.enabled,
                "running": s.running,
                "max_concurrent": s.node.max_concurrent,
                "load_ratio": round(s.load_ratio, 3),
                "mem_budget_gb": s.node.mem_budget_gb,
                "mem_reserved_gb": round(s.reserved_mem_gb, 2),
                "models": sorted(s.models),
                "loaded_model": s.loaded_model,
                "metered": s.is_metered,
                "tenant_affinity": list(s.node.tenant_affinity),
                "last_error": s.last_error,
                # 에어갭에서 꺼진 노드는 그 사실이 보여야 한다.
                "disabled_by_airgap": self._airgap and not s.node.is_internal,
            }
            for s in self._nodes.values()
        ]


def _node_from_registration(raw: dict[str, Any]) -> Node:
    """등록 본문을 `Node` 로. **YAML 시드와 같은 검증기를 지난다.**

    `data_boundary` 를 안 적으면 `external` 이 된다(fail-safe) — 안 적은 노드를
    내부로 간주하면 실수가 새는 쪽으로 향한다.
    """
    name = str(raw.pop("name", "") or "").strip()
    if not name:
        raise ApiError("missing_field", status=400, params={"field": "name"})
    if not raw.get("provider"):
        raise ApiError("missing_field", status=400, params={"field": "provider"})

    # **모르는 키를 거절한다.** YAML 은 전진 호환을 위해 모르는 키를 흘려보내지만
    # 등록 API 는 방금 사람이 친 값이다. `data_boundry` 오타를 조용히 무시하면
    # external 기본값이 적용되고, 아무도 모르는 채 경계가 정해진다.
    unknown = sorted(set(raw) - NODE_REGISTRATION_FIELDS)
    if unknown:
        raise ApiError("invalid_field", status=400, params={"field": ", ".join(unknown)})

    try:
        return node_from_dict(name, raw)
    except ConfigError as exc:
        raise ApiError("invalid_field", status=400, params={"field": str(exc)})


def _dominant_reason(rejections: Mapping[str, str]) -> str:
    """가장 흔한 탈락 사유. UI 가 "노드 정비로 대기 중 12건" 처럼 묶어 보여준다."""
    if not rejections:
        return "no_matching_node"
    counts: dict[str, int] = {}
    for reason in rejections.values():
        counts[reason] = counts.get(reason, 0) + 1
    return max(counts, key=lambda r: counts[r])
