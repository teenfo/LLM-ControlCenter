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
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import logging

from .cluster import FAIL, WAIT, Cluster, Placement
from .config import EXTERNAL, Config
from .cost import CostAccountant
from .crypto import response_aad
from .guard import (
    OUTPUT_BOUNDARY,
    STAGE_OUTPUT,
    Guard,
    OutputResult,
    rules_from_rows,
)
from .i18n import guard_pack_for
from .models import ModelRegistrar
from .observability import log_event
from .roles import RoleResolver, resolver_for
from .providers import BackendError
from .store import SqliteStore, TenantScope

LANE_POLL_SECONDS = 0.5

#: 종료 시 진행 중인 실행을 기다리는 상한. 노드가 응답하지 않으면 잡 타임아웃까지
#: 걸릴 수 있는데, 종료가 그만큼 매달리면 오케스트레이터가 SIGKILL 을 보낸다 —
#: 그러면 우아한 종료를 시도한 의미가 없어진다.
DRAIN_SECONDS = 20.0

log = logging.getLogger("llmcc.scheduler")


@dataclass
class LaneStats:
    """관제 UI 가 읽는 레인 상태."""

    running: int = 0
    queued: int = 0
    #: 스캔 창에 잘린 잡이 있었는가. 조용히 자르지 않기 위한 표시.
    scan_truncated: bool = False
    starvation_trips: int = 0
    wait_reasons: dict[str, int] = field(default_factory=dict)


def _longest_outbound(job: Any) -> str:
    """이 잡이 노드로 보낼 수 있는 가장 긴 텍스트. 비용 예약의 입력 근거다.

    경계마다 마스킹 결과가 다르고(외부용이 더 많이 가려진다) 배치 전에는 어느 쪽으로
    갈지 모른다. **예약은 상한이므로 긴 쪽을 쓴다** — 남는 예약은 정산에서 풀린다.
    """
    internal = (job.prompt_masked or "") + (job.system_masked or "")
    external = (job.prompt_external or job.prompt_masked or "") + (
        job.system_external or job.system_masked or ""
    )
    return max(internal, external, key=len)


#: 백오프에 얹는 흔들림의 비율. 노드 하나가 죽으면 그 노드에 있던 잡이 **전부 같은
#: 시각에** 재시도한다 — 다음 노드가 그 순간 몰린 요청을 받고 같이 죽는 경로다.
JITTER_RATIO = 0.25


def _jitter(seed: str, delay: float) -> float:
    """잡마다 다른 지연을 준다. **난수가 아니라 잡 id 의 함수다.**

    난수를 쓰면 같은 잡의 준비 여부가 틱마다 달라져서, 아직 안 됐다가 됐다가
    한다. 잡 id 로 정하면 그 잡의 지연은 고정이고 잡들 사이에서는 흩어진다.
    """
    if delay <= 0:
        return 0.0
    spread = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return delay * JITTER_RATIO * spread


def _loop_failure(loop: str, exc: BaseException) -> None:
    """배경 루프의 실패를 **구조화 로그로 남긴다.**

    루프를 죽이지 않는 것과 실패를 감추는 것은 다르다. `suppress(Exception)` 만
    두면 헬스 프로브가 매 주기 터져도 화면에는 "노드 상태 unknown" 만 보이고,
    아무도 왜인지 모른다 — "조용한 실패를 시끄럽게" 원칙의 정반대다.

    본문·비밀은 안 담는다. 담기는 것은 어느 루프가 어떤 예외로 죽었는가뿐이다.
    """
    log_event(
        log, "배경 루프 실패",
        loop=loop, error_type=type(exc).__name__, error=str(exc)[:200],
    )


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
        resolver: RoleResolver | None = None,
        guard: Guard | None = None,
        vault: Any = None,
    ) -> None:
        self._config = config
        self._roles = resolver_for(config, store, resolver)
        self._store = store
        self._cluster = cluster
        # **출력 축은 끌 수 있는 스위치가 아니다.** 가드를 안 넘겨받아도 설정으로
        # 하나 만든다 — `None` 이면 검사를 건너뛰는 경로를 두면, 배선 하나를
        # 빠뜨린 조립이 응답 필터가 통째로 꺼진 채로 조용히 돈다.
        #
        # 분류기 없이 만들어도 된다. 출력은 1단만 쓴다.
        self._guard = guard or Guard(config)
        # 금고는 없을 수 있다. **KEK 가 없으면 암호문 자체를 안 만든다** — 그것이
        # 원문 보관 비활성화의 정의이고, 평문 폴백 경로는 존재하지 않는다.
        self._vault = vault
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
        #: 진행 중인 `_execute`. 루프 태스크와 달리 **취소하면 안 되는** 것들이다 —
        #: 노드는 이미 추론을 돌리고 있고, 여기서 끊으면 그 잡이 `running` 인 채
        #: 남아 다음 기동의 크래시 복구 경로를 탄다. 정상 종료가 크래시처럼 보인다.
        self._inflight: set[asyncio.Task[Any]] = set()
        self._stopping = asyncio.Event()

    # -- 수명주기 --------------------------------------------------------------

    async def start(self) -> None:
        """레인 루프와 배경 루프를 띄운다.

        기동 시 크래시 복구를 먼저 돌린다 — 과금 노드에서 돌던 잡은 자동 재큐하지 않고
        `needs_review` 로 남겨 이중 청구를 드러낸다.
        """
        recovered = self._store.recover_running_jobs(
            self._cluster.metered_nodes(),
            max_retries=self._thresholds.max_retries,
        )
        if recovered["needs_review"]:
            self._notify("crash_recovery_needs_review", recovered)

        self._stopping.clear()
        for lane in self._config.lanes:
            self._tasks.append(asyncio.create_task(self._lane_loop(lane), name=f"lane:{lane}"))
        self._tasks.append(asyncio.create_task(self._health_loop(), name="health"))
        self._tasks.append(asyncio.create_task(self._models_loop(), name="models"))
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))
        self._tasks.append(asyncio.create_task(self._watch_loop(), name="watch"))

    async def stop(self, *, drain_seconds: float = DRAIN_SECONDS) -> None:
        """루프를 멈추고 **진행 중인 실행은 끝나기를 기다린다.**

        루프만 취소하고 나가면 in-flight `_execute` 가 미정리 상태로 파괴된다.
        그 잡들은 DB 에 `running` 으로 남고, 다음 기동의 크래시 복구가 그것을
        재큐하거나(중복 실행) `needs_review` 로 세운다 — **정상 종료가 크래시
        복구 경로를 타는 것**이고, 배포할 때마다 그렇게 된다.

        기다리는 데 상한을 둔다. 노드가 응답하지 않으면 잡의 타임아웃까지
        걸릴 수 있는데, 종료가 그만큼 매달리면 오케스트레이터가 SIGKILL 을 보낸다.
        """
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        if self._inflight:
            _done, pending = await asyncio.wait(
                tuple(self._inflight), timeout=drain_seconds
            )
            for task in pending:
                # 상한을 넘겼다. 취소하되 **왜 그랬는지가 보이게** 남긴다 —
                # 조용히 버리면 다음 기동의 needs_review 를 아무도 설명 못 한다.
                task.cancel()
                self._notify("crash_recovery_needs_review", {"requeued": 0, "needs_review": 1})
            self._inflight.clear()

    # -- 레인 루프 -------------------------------------------------------------

    async def _lane_loop(self, lane: str) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick(lane)
            except Exception as exc:
                # 한 틱의 실패가 레인을 죽이면 안 된다 — 다만 **조용히는 아니다.**
                _loop_failure(f"lane:{lane}", exc)
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
        return now - job.wait_since >= delay + _jitter(job.id, delay)

    # -- 디스패치 --------------------------------------------------------------

    async def _try_dispatch(self, job: Any, lane: str) -> bool:
        scope = TenantScope(job.tenant_id)
        # **현재 설정**을 읽는다. 배치 티어는 스냅샷 ∩ 현재이고, 그 "현재" 에는
        # 테넌트 오버라이드가 포함된다 — 안 그러면 오버라이드로 좁힌 배치가
        # 디스패치에서 무시된다.
        role = self._roles.get(job.tenant_id, job.role)
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
            # **실제로 나갈 텍스트를 넘긴다.** 여기가 `0` 이었고, 그래서 큐를 지난
            # 모든 잡의 입력 토큰이 비용 예약에서 빠졌다 — 긴 프롬프트가 과금 노드로
            # 나가도 예약은 출력 토큰만 잡았고, 예산 초과가 정산 뒤에야 드러났다.
            #
            # 어느 경계로 갈지는 아직 모르므로 **둘 중 긴 쪽**을 쓴다. 예약은 상한이라
            # 넉넉한 쪽으로 틀리는 것이 맞고, 남는 예약은 정산에서 풀린다.
            prompt=_longest_outbound(job),
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

        # **`queued` 일 때만 running 으로 넘긴다.** 배치를 결정하는 동안 API 워커가
        # 그 잡을 취소했을 수 있다 — 그때 그대로 실행하면 소비자는 "취소됨" 을
        # 응답받은 잡의 과금 청구서를 받는다.
        if not self._store.update_job(
            scope, job.id, expect_status="queued",
            status="running", node=placement.node, model=placement.model,
            tier=placement.tier, started_at=self._now(),
            wait_reason=None, wait_since=None,
        ):
            self._cluster.release(placement)
            self._accountant.release_reservation(scope, job.id)
            return False

        self._lane_running[lane] += 1
        self._stats[lane].running = self._lane_running[lane]
        # **참조를 붙잡는다.** `create_task` 의 반환을 버리면 이벤트 루프는 태스크를
        # 약한 참조로만 들고 있어서, GC 가 실행 도중에 가져갈 수 있다. 그러면 잡은
        # `running` 인 채 남고 `_execute` 의 `finally` 가 안 돌아 슬롯·메모리·비용
        # 예약이 영영 안 풀린다 — 부하가 없는데 큐가 쌓이는 그 증상이다.
        task = asyncio.create_task(
            self._execute(scope, job.id, lane, placement), name=f"exec:{job.id}"
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
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
            role = self._roles.get(job.tenant_id, job.role) or self._config.roles[job.role]

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

    def _record_output_events(
        self, scope: TenantScope, verdict: OutputResult, *, job: Any
    ) -> None:
        """탐지 사실만 남긴다. **매칭된 값은 어디에도 남기지 않는다.**

        `stage` 를 입력과 구분한다 — 뭉치면 "규칙이 입력에서 걸렸는가 출력에서
        걸렸는가" 가 사라지고, 그 둘은 관리자에게 전혀 다른 사건이다. 입력 히트는
        소비자가 보낸 것이고, **출력 히트는 모델이 만들어 낸 것**이다. 후자가 늘면
        고칠 곳은 규칙이 아니라 프롬프트다.
        """
        for detection in verdict.detections:
            self._store.record_filter_event(
                scope,
                rule_id=detection.rule_id,
                stage=STAGE_OUTPUT,
                action=detection.actions.get(OUTPUT_BOUNDARY, "audit"),
                match_count=detection.match_count,
                offsets=detection.spans,
                job_id=job.id,
                service_id=job.service_id,
                boundary=OUTPUT_BOUNDARY,
            )

    async def _inspect_output(self, scope: TenantScope, text: str) -> OutputResult:
        """응답에 입력과 같은 1단 규칙을 적용한다.

        테넌트의 로케일 팩과 추가 규칙을 함께 읽는다. 안 읽으면 그 테넌트의 규칙이
        **입력에서는 강하고 출력에서는 약해지고**, 그 비대칭은 어디에도 안 드러난다.
        규칙을 조인다고 믿는 관리자가 실제로는 절반만 조이고 있는 셈이다.
        """
        tenant = self._store.get_tenant(scope.tenant_id)
        pack = guard_pack_for(tenant["locale"]) if tenant else None
        return await self._guard.inspect_output(
            text,
            locales=[pack] if pack else [],
            tenant_rules=rules_from_rows(self._store.list_tenant_guard_rules(scope)),
        )

    def _seal_response(self, scope: TenantScope, job_id: str, text: str) -> Any:
        """응답 원문 봉인. **KEK 가 없으면 암호문 자체를 안 만든다.**

        AAD 가 프롬프트와 **다르다**(`response_aad` vs `prompt_aad`). 같은 값으로
        묶으면 응답 암호문을 프롬프트 컬럼에 옮겨 심어도 열린다 — 관리자가 원문
        열람을 눌렀을 때 감사에는 "프롬프트를 봤다" 고 남고 화면에는 응답이 뜬다.
        """
        if self._vault is None or not getattr(self._vault, "enabled", False):
            return None
        tenant = self._store.get_tenant(scope.tenant_id)
        wrapped = tenant["dek_wrapped"] if tenant else None
        if not wrapped:
            return None
        try:
            return self._vault.seal(
                wrapped, text, aad=response_aad(scope.tenant_id, job_id)
            )
        except Exception:
            # 봉인 실패가 잡을 죽이지 않는다. 마스킹본은 이미 만들어졌고, 원문을
            # 못 남기는 것이 응답을 통째로 잃는 것보다 낫다.
            return None

    async def _succeed(
        self, scope: TenantScope, job: Any, placement: Placement, lane: str,
        started: float, *, text: str, input_tokens: int, output_tokens: int,
        metrics: Mapping[str, Any],
    ) -> None:
        self._cluster.record_success(placement.node)
        duration_ms = int((self._now() - started) * 1000)

        # **출력 축의 초크포인트다.** `jobs.response` 를 쓰는 곳은 여기 하나뿐이고,
        # 그래서 응답 필터는 새 배관이 아니라 이 함수 안의 한 단계다. 입력에서
        # `pipeline.py` 가 잡 생성의 유일한 경로인 것과 같은 구조다.
        verdict = await self._inspect_output(scope, text)
        sealed = self._seal_response(scope, job.id, text) if text else None

        self._store.update_job(
            scope, job.id,
            status="ok", response=verdict.masked,
            response_cipher=sealed.ciphertext if sealed else None,
            response_nonce=sealed.nonce if sealed else None,
            finished_at=self._now(), metrics=dict(metrics),
        )
        self._record_output_events(scope, verdict, job=job)
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
        # **"이 노드에서 안 된다" 와 "어디서도 안 된다" 는 다르다.**
        #
        # `ModelNotFound` 는 `retryable=False` 다 — 같은 노드에 다시 보내도 결과가
        # 같으니 맞는 판정이다. 그런데 재시도가 **재배치를 동반**하므로 다음 시도는
        # 다른 노드로 간다. 기동 직후에는 인벤토리가 비어 있어 배치 필터가 노드를
        # 통과시키는데(모른다는 이유로 막으면 전부 대기한다), 그렇게 잘못 간 잡이
        # 여기서 재배치 없이 죽는다 — 그 모델을 가진 노드가 멀쩡히 놀고 있는데도.
        #
        # 모델 설치 요청 경로는 `_fail` 이 그대로 태운다. 여기서는 다른 노드를
        # 한 번 더 시도할 뿐이고, 전부 없으면 결국 그 경로로 간다.
        rebindable = exc.code == "model_not_installed"
        retriable = (exc.retryable or rebindable) and attempts <= self._thresholds.max_retries

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
            try:
                await self._cluster.probe_all()
            except Exception as exc:
                _loop_failure("health", exc)
            await asyncio.sleep(interval)

    async def _models_loop(self) -> None:
        """미설치 탐지 · 승인된 모델 설치. 레지스트러가 없으면 아무것도 안 한다."""
        while not self._stopping.is_set():
            if self._registrar is not None:
                try:
                    self._registrar.detect_missing()
                    await self._registrar.process_approved()
                except Exception as exc:
                    _loop_failure("models", exc)
            await asyncio.sleep(self._thresholds.health_probe_interval_seconds)

    async def _watch_loop(self) -> None:
        """사람이 모르면 조용히 멈추는 지점을 주기적으로 본다."""
        while not self._stopping.is_set():
            await asyncio.sleep(self._thresholds.health_probe_interval_seconds * 2)
            try:
                self.run_watches()
            except Exception as exc:
                _loop_failure("watch", exc)

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
        # **상태마다 다른 이벤트를 준다** — 노드 헬스가 offline/recovered 를
        # 나눠 쓰는 것과 같은 이유다. 하나로 쓰면 회복 전이가 "급증" 제목으로
        # 나가고, 첫 관측(재기동 직후)마다 "0건 급증" 이 나간다.
        self._notifier.observe(
            "guard:blocks", "spike" if spike else "normal",
            event="guard_blocks_spike" if spike else "guard_blocks_normal",
            count=blocks,
        )

        # 분류 실패는 판정이 아니다. 실패율이 오르면 관리자가 알아야 한다.
        rate = self._store.classifier_failure_rate(since=self._now() - 3600)
        findings["classifier_failure_rate"] = rate
        bad = rate >= self._thresholds.classifier_failure_rate_warn
        self._notifier.observe(
            "guard:classifier", "degraded" if bad else "ok",
            event="classifier_error_rate" if bad else "classifier_error_normal",
            percent=round(rate * 100, 1),
        )
        return findings

    async def _retention_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.run_retention()
            except Exception as exc:
                _loop_failure("retention", exc)
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
