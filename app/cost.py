"""비용 예약과 정산.

**확인만 하면 안 된다.** 동시 디스패치 N건이 각각 "예산 남음" 을 확인하고 전부 통과한 뒤
완료되면 합계가 예산을 넘는다. 그래서 디스패치 시점에 상한 비용을 **예약(reserve)** 하고,
완료 시 실제 사용량으로 **정산(settle)** 한다.

로컬 노드는 단가 0 으로 같은 경로를 탄다. 분기하지 않는다 — 분기하면 언젠가 한쪽에만
로직이 추가되고 두 경로가 갈라진다.

예산 초과 시 그 서비스의 **metered 티어만** 막힌다. placement 에 내부 티어가 있으면
계속 돈다 — "예산이 떨어지면 무료 경로로 자동 강등" 이 의도한 동작이다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .config import Pricing
from .store import SqliteStore, TenantScope

#: 입력 토큰을 문자 수에서 추정할 때 쓰는 비율. 예약은 상한 계산이라 보수적으로 잡는다.
CHARS_PER_TOKEN = 3.0


@dataclass(frozen=True)
class BudgetStatus:
    """예산 현황 한 단계(테넌트 또는 서비스)."""

    limit: float | None
    spent: float
    reserved: float

    @property
    def committed(self) -> float:
        """이미 쓴 것 + 예약된 것. 남은 여유를 볼 때 이 값을 본다."""
        return self.spent + self.reserved

    @property
    def remaining(self) -> float:
        if self.limit is None:
            return float("inf")
        return max(0.0, self.limit - self.committed)

    @property
    def burn_rate(self) -> float:
        """소진율 0~1. 경고 임계와 비교한다."""
        if not self.limit:
            return 0.0
        return min(1.0, self.committed / self.limit)

    def can_afford(self, amount: float) -> bool:
        return self.limit is None or self.committed + amount <= self.limit


class CostAccountant:
    """단가 조회 · 상한 추정 · 예산 확인 · 정산."""

    def __init__(
        self,
        pricing: Pricing,
        store: SqliteStore,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._pricing = pricing
        self._store = store
        self._now = now

    # -- 추정 -----------------------------------------------------------------

    def estimate_upper_bound(
        self,
        *,
        provider: str,
        model: str,
        prompt_chars: int,
        max_output_tokens: int | None = None,
    ) -> float:
        """이 잡이 최대로 쓸 수 있는 비용.

        예약은 상한이어야 한다 — 평균으로 예약하면 절반의 잡이 예산을 넘긴 뒤에 드러난다.
        """
        input_rate, output_rate = self._pricing.rate(provider, model)
        if input_rate == 0.0 and output_rate == 0.0:
            return 0.0  # 로컬 노드. 같은 경로를 타되 0 이 계상된다

        input_tokens = prompt_chars / CHARS_PER_TOKEN
        output_tokens = max_output_tokens or self._pricing.assumed_max_output_tokens

        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000

    def actual_cost(
        self, *, provider: str, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        input_rate, output_rate = self._pricing.rate(provider, model)
        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000

    # -- 예산 -----------------------------------------------------------------

    def period_start(self) -> float:
        """현재 청구 기간의 시작(30일 롤링).

        달력 월이 아니라 롤링 창을 쓰는 이유: 설치처의 시간대·회계 월을 모르는데
        달력 월을 가정하면 월초에 예산이 통째로 리셋되는 절벽이 생긴다.
        """
        return self._now() - 30 * 86400

    def budget_status(
        self, scope: TenantScope, *, limit: float | None, service_id: str | None = None
    ) -> BudgetStatus:
        since = self.period_start()
        return BudgetStatus(
            limit=limit,
            spent=self._store.spend_since(scope, since, service_id=service_id),
            reserved=self._store.reserved_cost(scope, service_id=service_id),
        )

    def can_afford(
        self,
        scope: TenantScope,
        amount: float,
        *,
        tenant_limit: float | None,
        service_limit: float | None = None,
        service_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """테넌트·서비스 두 단계를 확인한다. 반환은 (가능한가, 걸린 단계)."""
        if amount <= 0:
            return True, None  # 무료 경로는 예산을 소모하지 않는다

        tenant = self.budget_status(scope, limit=tenant_limit)
        if not tenant.can_afford(amount):
            return False, "tenant"

        if service_limit is not None and service_id:
            service = self.budget_status(scope, limit=service_limit, service_id=service_id)
            if not service.can_afford(amount):
                return False, "service"

        return True, None

    # -- 정산 -----------------------------------------------------------------

    def settle(
        self,
        scope: TenantScope,
        job_id: str,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        node: str,
        role: str,
        service_id: str,
        status: str,
        duration_ms: int,
        end_user_hash: str | None = None,
    ) -> float:
        """실제 사용량으로 정산하고 사용량 행을 남긴다.

        예약은 `jobs.cost_reserved_usd` 를 0 으로 만들어 해제한다 — 예약이 남아 있으면
        예산이 영원히 묶인다.
        """
        cost = self.actual_cost(
            provider=provider, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

        self._store.update_job(
            scope, job_id,
            cost_usd=cost, cost_reserved_usd=0.0,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        self._store.record_usage(
            scope,
            service_id=service_id, end_user_hash=end_user_hash, job_id=job_id,
            role=role, model=model, node=node, provider=provider,
            input_tokens=input_tokens, output_tokens=output_tokens,
            duration_ms=duration_ms, status=status, cost_usd=cost,
        )
        return cost

    def release_reservation(self, scope: TenantScope, job_id: str) -> None:
        """정산 없이 예약만 푼다. 잡이 취소되거나 배치 전에 죽었을 때.

        이걸 빼먹으면 예산이 조용히 묶이고, 관리자는 "왜 예산이 남았는데 막히지" 를
        디버깅하게 된다.
        """
        self._store.update_job(scope, job_id, cost_reserved_usd=0.0)
