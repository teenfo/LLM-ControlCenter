"""스케줄러 — 무엇을 언제 어떤 순서로 넘길지.

배치 결정(어느 노드로 갈지)은 `cluster.place()` 가 이미 한다. 이 모듈이 정하는 것은
**대기 잡 중 무엇을 먼저 시도할지**와 **실패했을 때 어떻게 되돌릴지**뿐이다.

선택 순서는 위에서부터 우선한다:

    1. 기아 방지   — 임계를 넘게 기다린 잡. 티어·친화·부하보다 위다
    2. 테넌트 공정성 — 라운드로빈. 한 테넌트가 큐를 채워도 다른 테넌트가 안 굶는다
    3. 우선순위·나이 — 그 안에서 priority DESC, created_at ASC

레이트리밋(입구)과 공정성(큐 안)은 다른 장치다. 큐가 이미 한 테넌트로 가득 찬 뒤에는
공정성만으로 되돌릴 수 없으므로 둘 다 필요하다.

**스캔 창은 유한하다.** 틱마다 `대기 잡 × 노드` 전수 검사는 큐 1,000 × 노드 10 이면
0.5초마다 10,000회다. 상한을 두되 **잘린 사실을 상태에 노출한다** — 조용히 자르면
"전부 검토했다" 로 읽힌다.

이 클래스는 **싱글턴으로 떠야 한다.** API 워커마다 돌면 같은 잡이 여러 번 배치된다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .cluster import FAIL, WAIT, Cluster, Placement
from .config import EXTERNAL, Config
from .cost import CostAccountant
from .models import ModelRegistrar
from .providers import BackendError
from .store import SqliteStore, TenantScope

LANE_POLL_SECONDS = 0.5


@dataclass
class LaneStats:
    """관제 UI 가 읽는 레인 상태."""

    running: int = 0
    queued: int = 0
    #: 스캔 창에 잘린 잡이 있었는가. 조용히 자르지 않기 위한 표시.
    scan_truncated: bool = False
    starvation_trips: int = 0
    wait_reasons: dict[str, int] = field(default_factory=dict)


class Scheduler:
    def __init__(
        self,
        config: Config,
        store: SqliteStore,
        cluster: Cluster,
        *,
        accountant: CostAccountant | None = None,
        registrar: ModelRegistrar | None = None,
        now: Callable[[], float] = time.time,
        notify: Callable[[str, dict[str, Any]], None] | None = None,
        notifier: Any = None,
    ) -> None:
        self._config = config
        self._store = store
        self._cluster = cluster
        self._accountant = accountant or CostAccountant(config.pricing, store, now=now)
        self._registrar = registrar
        self._now = now
        self._notify = notify or (notifier.as_callable() if notifier else (lambda e, d: None))
        # 전이 판정이 필요한 알림(예산 경고 등)은 알림기가 직접 있어야 한다.
        self._notifier = notifier

        self._thresholds = config.thresholds
        self._lane_running: dict[str, int] = {name: 0 for name in config.lanes}
        self._stats: dict[str, LaneStats] = {name: LaneStats() for name in config.lanes}
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = asyncio.Event()

    # -- 수명주기 --------------------------------------------------------------

    async def start(self) -> None:
        """레인 루프와 배경 루프를 띄운다.

        기동 시 크래시 복구를 먼저 돌린다 — 과금 노드에서 돌던 잡은 자동 재큐하지 않고
        `needs_review` 로 남겨 이중 청구를 드러낸다.
        """
        recovered = self._store.recover_running_jobs(self._cluster.metered_nodes())
        if recovered["needs_review"]:
            self._notify("crash_recovery_needs_review", recovered)

        self._stopping.clear()
        for lane in self._config.lanes:
            self._tasks.append(asyncio.create_task(self._lane_loop(lane), name=f"lane:{lane}"))
        self._tasks.append(asyncio.create_task(self._health_loop(), name="health"))
        self._tasks.append(asyncio.create_task(self._models_loop(), name="models"))
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))
        self._tasks.append(asyncio.create_task(self._watch_loop(), name="watch"))

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    # -- 레인 루프 -------------------------------------------------------------

    async def _lane_loop(self, lane: str) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick(lane)
            except Exception:  # 한 틱의 실패가 레인을 죽이면 안 된다
                pass
            await asyncio.sleep(LANE_POLL_SECONDS)

    async def tick(self, lane: str) -> int:
        """레인 한 틱. 배치 가능한 잡을 슬롯이 허용하는 만큼 띄운다.

        테스트가 직접 부를 수 있도록 루프와 분리했다.
        """
        lane_config = self._config.lanes.get(lane)
        if lane_config is None:
            return 0

        stats = self._stats[lane]
        stats.queued = self._store.count_queued(lane)
        stats.wait_reasons = {}

        free = lane_config.max_concurrent - self._lane_running[lane]
        if free <= 0:
            return 0

        candidates, truncated = self._select(lane)
        stats.scan_truncated = truncated

        dispatched = 0
        for job in candidates:
            if dispatched >= free:
                break
            if await self._try_dispatch(job, lane):
                dispatched += 1
        return dispatched

    def _select(self, lane: str) -> tuple[list[Any], bool]:
        """이 레인에서 시도할 잡을 순서대로. 반환은 (후보, 스캔 창 절단 여부)."""
        window = self._thresholds.scan_window_per_lane
        rows = self._store.claim_queued(lane, limit=window + 1)
        truncated = len(rows) > window
        rows = rows[:window]

        now = self._now()
        ready = [job for job in rows if self._retry_ready(job, now)]

        # ① 기아 방지 — 임계를 넘게 기다린 잡은 무조건 먼저.
        threshold = self._config.lanes[lane].starvation_seconds
        starved = [job for job in ready if now - job.created_at > threshold]
        if starved:
            self._stats[lane].starvation_trips += 1
            return sorted(starved, key=lambda j: j.created_at), truncated

        # ② 테넌트 공정성 — 라운드로빈으로 섞는다.
        return _round_robin_by_tenant(ready), truncated

    def _retry_ready(self, job: Any, now: float) -> bool:
        """재시도 백오프가 지났는가.

        백오프를 `sleep` 으로 구현하면 그 레인이 통째로 멈춘다. 대신 시각을 기록하고
        지나기 전까지 후보에서 뺀다 — 다른 잡은 그동안 계속 돈다.
        """
        if job.attempts <= 0 or job.wait_since is None:
            return True
        if job.wait_reason != "retry_backoff":
            return True

        backoff = self._thresholds.retry_backoff_seconds
        delay = backoff[min(job.attempts - 1, len(backoff) - 1)]
        return now - job.wait_since >= delay

    # -- 디스패치 --------------------------------------------------------------

    async def _try_dispatch(self, job: Any, lane: str) -> bool:
        scope = TenantScope(job.tenant_id)
        role = self._config.roles.get(job.role)
        if role is None:
            self._fail(scope, job, "unknown_role", f"역할 {job.role} 이 없다")
            return False

        tenant = self._store.get_tenant(job.tenant_id)
        service = self._store.get_service(scope, job.service_id)

        result = self._cluster.place(
            job_id=job.id,
            tenant_id=job.tenant_id,
            service_id=job.service_id,
            role=role,
            placement_snapshot=job.placement,
            prompt_chars=0,
            tenant_budget=tenant["budget_usd_per_month"] if tenant else None,
            service_budget=service["budget_usd_per_month"] if service else None,
            last_failed_node=job.last_failed_node,
            allowed_boundaries=job.allowed_boundaries,
        )

        if result.outcome == FAIL:
            self._fail(scope, job, result.code or "no_placement", result.reason or "")
            return False

        if result.outcome == WAIT:
            self._record_wait(scope, job, result.reason or "no_placement", lane)
            return False

        placement = result.placement
        assert placement is not None

        self._lane_running[lane] += 1
        self._stats[lane].running = self._lane_running[lane]
        self._store.update_job(
            scope, job.id,
            status="running", node=placement.node, model=placement.model,
            tier=placement.tier, started_at=self._now(),
            wait_reason=None, wait_since=None,
        )
        asyncio.create_task(self._execute(scope, job.id, lane, placement))
        return True

    def _record_wait(self, scope: TenantScope, job: Any, reason: str, lane: str) -> None:
        """대기 사유를 남기고, 행정적 부재가 너무 길면 끝낸다.

        관리자가 노드를 정비하려고 내린 것은 되돌릴 수 있으므로 즉시 실패시키지 않는다.
        다만 영원히 기다리게 두면 소비자가 이유를 모른 채 매달린다.
        """
        stats = self._stats[lane]
        stats.wait_reasons[reason] = stats.wait_reasons.get(reason, 0) + 1

        now = self._now()
        if job.wait_since is None:
            self._store.update_job(scope, job.id, wait_reason=reason, wait_since=now)
            return

        if now - job.wait_since > self._thresholds.administrative_wait_timeout_seconds:
            self._fail(scope, job, "administrative_wait_timeout", reason)
        elif job.wait_reason != reason:
            self._store.update_job(scope, job.id, wait_reason=reason)

    async def _execute(
        self, scope: TenantScope, job_id: str, lane: str, placement: Placement
    ) -> None:
        """실제 추론. 잡 본문은 **여기서 처음 읽는다** — 배치 결정에는 필요 없었다."""
        started = self._now()
        try:
            job = self._store.get_job(scope, job_id)
            if job is None:
                return

            state = self._cluster.state(placement.node)
            provider = state.provider
            role = self._config.roles[job.role]

            # 경계 밖으로 나가면 더 강하게 마스킹된 변형을 보낸다.
            # 한 벌만 저장했다면 이 구분이 여기서 사라진다.
            outbound = state.node.data_boundary == EXTERNAL
            prompt = (job.prompt_external if outbound and job.prompt_external else job.prompt_masked) or ""
            system = (job.system_external if outbound and job.system_external else job.system_masked)

            if role.is_embed:
                embedding = await provider.embed(
                    model=placement.model, inputs=[prompt], timeout=job.timeout_s
                )
                await self._succeed(
                    scope, job, placement, lane, started,
                    text="", input_tokens=embedding.input_tokens, output_tokens=0,
                    metrics={"vectors": len(embedding.vectors)},
                )
                return

            result = await provider.generate(
                model=placement.model, prompt=prompt, system=system,
                options=job.options, timeout=job.timeout_s,
            )
            await self._succeed(
                scope, job, placement, lane, started,
                text=result.text,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                metrics=dict(result.metrics),
            )

        except BackendError as exc:
            await self._handle_failure(scope, job_id, placement, lane, exc)
        except Exception as exc:  # 예상 못 한 실패도 잡을 매달아 두지 않는다
            await self._handle_failure(
                scope, job_id, placement, lane,
                BackendError(str(exc), retryable=False, code="internal"),
            )
        finally:
            self._lane_running[lane] = max(0, self._lane_running[lane] - 1)
            self._stats[lane].running = self._lane_running[lane]
            self._cluster.release(placement)

    async def _succeed(
        self, scope: TenantScope, job: Any, placement: Placement, lane: str,
        started: float, *, text: str, input_tokens: int, output_tokens: int,
        metrics: Mapping[str, Any],
    ) -> None:
        self._cluster.record_success(placement.node)
        duration_ms = int((self._now() - started) * 1000)

        self._store.update_job(
            scope, job.id,
            status="ok", response=text, finished_at=self._now(), metrics=dict(metrics),
        )
        self._accountant.settle(
            scope, job.id,
            provider=placement.provider, model=placement.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            node=placement.node, role=job.role, service_id=job.service_id,
            status="ok", duration_ms=duration_ms, end_user_hash=job.end_user_hash,
        )

    async def _handle_failure(
        self, scope: TenantScope, job_id: str, placement: Placement,
        lane: str, exc: BackendError,
    ) -> None:
        self._cluster.record_failure(placement.node, str(exc))
        job = self._store.get_job(scope, job_id)
        if job is None:
            return

        attempts = job.attempts + 1
        retriable = exc.retryable and attempts <= self._thresholds.max_retries

        if retriable:
            # **재시도는 재배치를 동반한다.** 직전 실패 노드를 남겨 배치가 그 노드를 피한다.
            self._store.update_job(
                scope, job_id,
                status="queued", attempts=attempts,
                last_failed_node=placement.node, node=None, started_at=None,
                wait_reason="retry_backoff", wait_since=self._now(),
                error=str(exc), error_code=exc.code,
            )
            return

        self._store.update_job(
            scope, job_id,
            status="failed", attempts=attempts, error=str(exc), error_code=exc.code,
            finished_at=self._now(),
        )
        # 실패해도 소비된 토큰은 있을 수 있다. 예약을 풀지 않으면 예산이 영원히 묶인다.
        self._accountant.release_reservation(scope, job_id)
        self._store.record_usage(
            scope, service_id=job.service_id, job_id=job_id, role=job.role,
            model=placement.model, node=placement.node, provider=placement.provider,
            status="failed", end_user_hash=job.end_user_hash,
        )

    def _fail(self, scope: TenantScope, job: Any, code: str, reason: str) -> None:
        self._store.update_job(
            scope, job.id,
            status="failed", error_code=code, error=reason, finished_at=self._now(),
        )
        self._accountant.release_reservation(scope, job.id)

    # -- 배경 루프 -------------------------------------------------------------

    async def _health_loop(self) -> None:
        interval = self._thresholds.health_probe_interval_seconds
        while not self._stopping.is_set():
            with contextlib.suppress(Exception):
                await self._cluster.probe_all()
            await asyncio.sleep(interval)

    async def _models_loop(self) -> None:
        """미설치 탐지 · 승인된 모델 설치. 레지스트러가 없으면 아무것도 안 한다."""
        while not self._stopping.is_set():
            if self._registrar is not None:
                with contextlib.suppress(Exception):
                    self._registrar.detect_missing()
                    await self._registrar.process_approved()
            await asyncio.sleep(self._thresholds.health_probe_interval_seconds)

    async def _watch_loop(self) -> None:
        """사람이 모르면 조용히 멈추는 지점을 주기적으로 본다."""
        while not self._stopping.is_set():
            await asyncio.sleep(self._thresholds.health_probe_interval_seconds * 2)
            with contextlib.suppress(Exception):
                self.run_watches()

    def run_watches(self) -> dict[str, Any]:
        """예산 소진 · 가드 차단 급증 · 분류 실패율.

        전부 **전이에서만** 알린다. 예산이 90% 인 동안 매 주기 경고하면 이틀 뒤
        아무도 그 채널을 안 본다 — 안 보는 알림은 없는 알림이다.

        테스트가 직접 부를 수 있게 루프에서 분리했다.
        """
        if self._notifier is None:
            return {}

        findings: dict[str, Any] = {}
        warn_at = self._thresholds.cost_budget_burn_warn
        since = self._accountant.period_start()

        for row in self._store.tenant_budget_status(since):
            limit = row["budget_usd_per_month"]
            if not limit:
                continue
            burn = row["spent"] / limit
            # 세 구간 중 어디에 있는지를 상태로 본다. 값이 아니라 구간이 바뀔 때만 나간다.
            band = "exhausted" if burn >= 1.0 else ("warn" if burn >= warn_at else "ok")
            findings[row["tenant_id"]] = band
            if band == "ok":
                self._notifier.seed(f"budget:{row['tenant_id']}", band)
                continue
            self._notifier.observe(
                f"budget:{row['tenant_id']}", band,
                event="budget_exhausted" if band == "exhausted" else "budget_warn",
                tenant=row["tenant_id"], percent=round(burn * 100, 1),
            )

        # 가드 차단 급증 — 규칙을 잘못 켰거나 소비자가 잘못 붙였다는 신호다.
        blocks = self._store.recent_filter_event_count("block", since=self._now() - 3600)
        spike = blocks >= self._thresholds.guard_block_spike_per_hour
        findings["guard_blocks_last_hour"] = blocks
        self._notifier.observe(
            "guard:blocks", "spike" if spike else "normal",
            event="guard_blocks_spike", count=blocks,
        )

        # 분류 실패는 판정이 아니다. 실패율이 오르면 관리자가 알아야 한다.
        rate = self._store.classifier_failure_rate(since=self._now() - 3600)
        findings["classifier_failure_rate"] = rate
        bad = rate >= self._thresholds.classifier_failure_rate_warn
        self._notifier.observe(
            "guard:classifier", "degraded" if bad else "ok",
            event="classifier_error_rate", percent=round(rate * 100, 1),
        )
        return findings

    async def _retention_loop(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(Exception):
                self.run_retention()
            await asyncio.sleep(3600)

    def run_retention(self) -> dict[str, int]:
        """보존 정리 + 레이트 카운터 정리. 테스트가 직접 부를 수 있게 분리했다."""
        counts = self._store.purge_expired(
            raw_prompt_retention_days=self._config.guard_settings.raw_prompt_retention_days
        )
        counts["rate_counters"] = self._store.prune_rate_counters(
            int(self._now()) - 120
        )
        return counts

    # -- 관제 -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            lane: {
                "running": stats.running,
                "queued": self._store.count_queued(lane),
                "max_concurrent": self._config.lanes[lane].max_concurrent,
                # 잘린 사실을 숨기지 않는다 — 조용히 자르면 "전부 검토했다" 로 읽힌다.
                "scan_truncated": stats.scan_truncated,
                "scan_window": self._thresholds.scan_window_per_lane,
                "starvation_trips": stats.starvation_trips,
                "wait_reasons": dict(stats.wait_reasons),
            }
            for lane, stats in self._stats.items()
        }


def _round_robin_by_tenant(jobs: Sequence[Any]) -> list[Any]:
    """테넌트별로 한 건씩 번갈아 뽑는다.

    한 테넌트가 1,000건을 넣어도 다른 테넌트의 첫 잡이 1,001번째로 밀리지 않는다.
    테넌트 안에서는 들어온 순서(우선순위·나이)가 유지된다.
    """
    buckets: dict[str, list[Any]] = {}
    for job in jobs:
        buckets.setdefault(job.tenant_id, []).append(job)

    ordered: list[Any] = []
    while buckets:
        for tenant in list(buckets):
            ordered.append(buckets[tenant].pop(0))
            if not buckets[tenant]:
                del buckets[tenant]
    return ordered
