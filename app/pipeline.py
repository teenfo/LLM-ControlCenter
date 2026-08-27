"""요청 파이프라인 — 순서가 계약이다.

    ① 인증 → ② 가드 → ③ 저장 → ④ 배치 → ⑤ 실행

②를 ③ 뒤로 옮기면 원문이 무방비로 DB 에 남고, ②를 ④ 뒤로 옮기면 이미 나간 뒤다.

**이 모듈이 잡을 만드는 유일한 경로다.** 라우터가 `store.create_job()` 을 직접 부를 수
있으면 언젠가 누군가 가드를 건너뛴 경로를 만든다. 스토어의 테넌트 초크포인트와 같은 이유로,
순서를 규율이 아니라 구조로 만든다.

동기 임베딩도 같은 관문을 지난다. **큐만 우회하고 가드·배치·경계·비용은 우회하지 않는다.**
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .auth import Principal, RateLimiter, check_role_allowed, limits_for
from .cluster import PLACED, WAIT, Cluster
from .config import EXTERNAL, INTERNAL, Config, GuardRule, Role
from .cost import CostAccountant
from .crypto import KeyVault
from .evals import Evaluator
from .guard import Guard, GuardResult
from .i18n import ApiError, guard_pack_for
from .identity import hash_end_user, hash_prompt, hash_system, looks_like_pii
from .roles import RoleResolver, resolver_for
from .store import TERMINAL_STATUSES, SqliteStore, TenantScope


def prompt_aad(tenant_id: str, job_id: str) -> str:
    """원문 암호문을 묶을 레코드 식별자. **봉인과 해제가 같은 값을 써야 한다.**

    한 곳에서 만든다 — 두 곳에서 조립하면 한쪽이 바뀌는 순간 그 테넌트의
    원문이 통째로 안 열린다.
    """
    return f"job:{tenant_id}:{job_id}"

DEFAULT_WAIT_SECONDS = 30.0
MAX_WAIT_SECONDS = 300.0
POLL_INTERVAL = 0.05
#: 폴 간격을 매번 이 배로 늘린다. 짧은 잡은 여전히 첫 폴에서 잡히고, 긴 잡은
#: 확인 빈도가 빠르게 떨어진다.
POLL_BACKOFF = 1.6
#: 그래도 이 이상 벌어지지는 않는다 — 완료 후 응답까지의 지연 상한이다.
MAX_POLL_INTERVAL = 0.5

#: 2단 분류를 수행하는 역할. `internal_only` 이므로 경계 밖으로 나갈 수 없다.
GUARD_ROLE = "_guard_classify"

#: 소비자에게 보이지 않는 역할의 접두사. 가드 분류처럼 시스템이 자기 자신을 위해
#: 쓰는 역할이며, 토큰의 `allow_roles` 에 `*` 가 있어도 노출되지 않는다.
INTERNAL_ROLE_PREFIX = "_"

#: 종결 상태. **스토어와 같은 정의를 쓴다** — 두 벌로 두면 갈리고, 실제로
#: 보존 정리 쪽 목록에서 `needs_review` 가 빠져 그 잡들이 영원히 쌓였다.
_TERMINAL = TERMINAL_STATUSES

#: 2단 분류의 시도·실패를 세기 위한 예약 규칙 id. 실제 규칙이 아니라 계수기다.
#: 밑줄로 시작하므로 테넌트가 같은 id 의 규칙을 만들어도 겹치지 않는다.
CLASSIFIER_OK_RULE = "_classifier_ok"
CLASSIFIER_FAILED_RULE = "_classifier_failed"


def is_public_role(name: str) -> bool:
    return not name.startswith(INTERNAL_ROLE_PREFIX)


@dataclass(frozen=True)
class Submission:
    """제출 결과. 완료됐든 대기 중이든 **모양이 같다.**

    동기/비동기를 `status` 한 필드로 흡수하면 호출자 코드에서 분기가 사라진다.
    """

    job_id: str
    status: str
    response: str | None = None
    error: str | None = None
    error_code: str | None = None
    role: str = ""
    model: str | None = None
    node: str | None = None
    tier: str | None = None
    attempts: int = 0
    #: 이 테넌트의 같은 레인 대기 잡 중 앞에 있는 수. **전역 큐 깊이가 아니다** —
    #: 남의 테넌트가 얼마나 밀어 넣었는지는 이 테넌트가 알 일이 아니다.
    queue_position: int | None = None
    retry_after: float | None = None
    wait_reason: str | None = None
    guard_actions: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.status == "pending"


class Pipeline:
    """잡을 만드는 유일한 경로."""

    def __init__(
        self,
        config: Config,
        store: SqliteStore,
        cluster: Cluster,
        guard: Guard,
        *,
        vault: KeyVault | None = None,
        limiter: RateLimiter | None = None,
        accountant: CostAccountant | None = None,
        evaluator: Evaluator | None = None,
        resolver: RoleResolver | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        # 역할 해석은 한 곳에서만 한다 — 파이프라인과 스케줄러가 각자 읽으면
        # 한쪽만 오버라이드를 반영하는 조합이 생긴다.
        self._roles = resolver_for(config, store, resolver)
        self._store = store
        self._cluster = cluster
        self._guard = guard
        self._vault = vault or KeyVault(None)
        self._limiter = limiter or RateLimiter(store, now=now)
        self._accountant = accountant or CostAccountant(config.pricing, store, now=now)
        self._evaluator = evaluator
        self._now = now

    # -- ① 인증 이후의 권한·한도 -----------------------------------------------

    def _authorize(
        self, principal: Principal, role_name: str, end_user: str | None
    ) -> tuple[Role, Any, Any, str | None]:
        tenant = self._store.get_tenant(principal.tenant_id)
        if tenant is None or tenant["status"] != "active":
            raise ApiError("tenant_inactive", status=403)

        service = self._store.get_service(principal.scope(), principal.service_id)
        if service is None or service["status"] != "active":
            raise ApiError("unauthorized", status=401)

        role = self._roles.get(principal.tenant_id, role_name)
        if role is None or not is_public_role(role_name):
            # 내부 역할은 소비자에게 **존재하지 않는다.** `allow_roles` 검사보다 먼저
            # 걸러야 403 과 404 로 존재 여부가 새지 않는다.
            raise ApiError("unknown_role", status=404, params={"role": role_name})

        check_role_allowed(service["allow_roles_json"], role_name)

        if service["require_end_user"] and not end_user:
            raise ApiError("end_user_required", status=400)

        end_user_hash = hash_end_user(end_user, tenant["end_user_salt"])

        self._limiter.check_and_consume(
            principal, limits_for(tenant, service), end_user_hash=end_user_hash
        )
        return role, tenant, service, end_user_hash

    # -- ② 가드 ---------------------------------------------------------------

    def tenant_guard_rules(self, scope: TenantScope) -> tuple[GuardRule, ...]:
        """테넌트가 추가한 규칙.

        **완화는 여기서 막지 않는다** — `guard.rules_for()` 가 베이스라인과 병합하며
        강한 쪽을 채택한다. 판정이 두 곳에 있으면 언젠가 갈린다.
        """
        rules = []
        for raw in self._store.list_tenant_guard_rules(scope):
            rules.append(
                GuardRule(
                    id=raw["id"],
                    kind=raw["kind"],
                    action=raw["action"],
                    label=raw["label"],
                    pattern=raw["pattern"],
                    checksum=raw["checksum"],
                    keep_tail=raw["keep_tail"],
                    description=raw["description"],
                    locale_pack=raw["locale_pack"],
                )
            )
        return tuple(rules)

    def _candidate_boundaries(self, role: Role) -> tuple[str, ...]:
        """가드가 등급을 매길 경계.

        가드는 배치보다 **먼저** 돌기 때문에 어느 노드로 갈지 아직 모른다. 그래서
        갈 수 있는 모든 경계에 대해 등급을 매기고, 차단된 경계를 잡의
        `allowed_boundaries` 에서 뺀다 — 그 뒤는 배치 필터가 처리한다.
        """
        if role.internal_only:
            return (INTERNAL,)
        return (INTERNAL, EXTERNAL)

    async def _inspect(
        self, role: Role, tenant: Any, prompt: str, system: str | None
    ) -> GuardResult:
        pack = guard_pack_for(tenant["locale"])
        return await self._guard.inspect(
            prompt,
            system=system,
            locales=[pack] if pack else [],
            tenant_rules=self.tenant_guard_rules(TenantScope(tenant["id"])),
            candidate_boundaries=self._candidate_boundaries(role),
            # 분류기가 자기 자신을 다시 분류하면 무한 재귀다.
            allow_classifier=role.name != GUARD_ROLE,
        )

    def _record_guard_events(
        self,
        scope: TenantScope,
        result: GuardResult,
        *,
        job_id: str | None,
        service_id: str,
    ) -> None:
        """탐지 사실만 남긴다. **매칭된 값은 어디에도 남기지 않는다.**

        경계마다 등급이 다를 수 있으므로 경계별로 한 행씩 남긴다 — 한 행으로 뭉치면
        "내부에선 통과, 외부에선 차단" 이라는 판정이 감사에서 사라진다.
        """
        for detection in result.detections:
            for boundary, action in sorted(detection.actions.items()):
                self._store.record_filter_event(
                    scope,
                    rule_id=detection.rule_id,
                    stage=detection.stage,
                    action=action,
                    match_count=detection.match_count,
                    offsets=detection.spans + detection.system_spans,
                    job_id=job_id,
                    service_id=service_id,
                    boundary=boundary,
                )

        if result.classifier_attempted:
            # **분류 실패는 판정이 아니다.** `on_classifier_error` 정책을 타되,
            # 그 사건 자체를 별도로 집계한다 — 실패율이 오르면 사람이 알아야 하고,
            # 시도 건수를 안 세면 실패율의 분모가 없다.
            self._store.record_filter_event(
                scope,
                rule_id=CLASSIFIER_FAILED_RULE if result.classifier_failed else CLASSIFIER_OK_RULE,
                stage="llm",
                action=self._config.guard_settings.on_classifier_error
                if result.classifier_failed
                else "audit",
                job_id=job_id,
                service_id=service_id,
            )

    @staticmethod
    def _guard_actions(result: GuardResult) -> dict[str, str]:
        """소비자에게 돌려줄 요약. 규칙 id 와 등급만 — 값은 절대 싣지 않는다."""
        return {
            d.rule_id: d.actions.get(INTERNAL, d.actions.get(EXTERNAL, "audit"))
            for d in result.detections
        }

    def _blocked(self, result: GuardResult) -> ApiError:
        return ApiError(
            "guard_blocked",
            status=422,
            params={"rules": ", ".join(result.blocked_rules)},
        )

    # -- ③ 저장 ---------------------------------------------------------------

    def _seal(
        self, scope: TenantScope, tenant: Any, plaintext: str, *, job_id: str
    ) -> Any:
        """원문 암호화. **KEK 가 없으면 암호문 자체를 안 만든다.**

        평문 폴백 경로를 두면 키 설정을 깜빡한 설치처에 원문이 평문으로 쌓인다.

        반대 순서도 실제로 일어난다 — KEK 없이 시작해 테넌트를 만들고 나중에 키를
        넣는 설치처다. 그때 이 테넌트만 DEK 가 없어서 **모든 요청이 여기서 죽었다.**
        키가 생긴 시점에 붙여 준다(`adopt_tenant_dek`). 그래도 못 붙으면(파기된
        테넌트) 원문 없이 간다 — 요청을 죽이는 쪽이 아니라 안 남기는 쪽이 안전하다.
        """
        if not self._vault.enabled:
            return None
        wrapped = tenant["dek_wrapped"]
        if not wrapped:
            wrapped = self._store.adopt_tenant_dek(scope, self._vault.create_dek())
            if not wrapped:
                return None
        # **암호문을 그 행에 묶는다.** DB 에 쓸 수 있는 공격자가 잡 A 의 암호문을
        # 잡 B 의 행에 옮겨 넣어도 열리지 않게 — 같은 테넌트 안에서는 DEK 가
        # 하나라서 키만으로는 안 막힌다.
        return self._vault.seal(wrapped, plaintext, aad=prompt_aad(scope.tenant_id, job_id))

    def _create_job(
        self,
        scope: TenantScope,
        *,
        principal: Principal,
        role_config: Role,
        tenant: Any,
        end_user_hash: str | None,
        verdict: GuardResult,
        raw_prompt: str,
        system: str | None,
        priority: int,
        metadata: Mapping[str, Any] | None,
        status: str = "queued",
    ) -> str:
        # **잡 id 를 먼저 만든다.** 봉인이 그 id 를 태그에 묶어야 하므로 저장
        # 시점에 받아서는 늦다.
        job_id = uuid.uuid4().hex[:16]
        sealed = self._seal(scope, tenant, raw_prompt, job_id=job_id)
        masked = verdict.storable_prompt
        external = verdict.prompt_for(EXTERNAL)
        system_internal = verdict.system_for(INTERNAL)
        system_external = verdict.system_for(EXTERNAL)

        return self._store.create_job(
            scope,
            id=job_id,
            service_id=principal.service_id,
            end_user_hash=end_user_hash,
            role=role_config.name,
            lane=role_config.lane,
            kind=role_config.kind,
            status=status,
            priority=priority,
            prompt_masked=masked,
            # 외부용 변형은 **다를 때만** 저장한다. 같은 값을 두 벌 두면 저장이 두 배가
            # 되고, 다를 때 저장하지 않으면 실행 시점에 구분이 사라진다.
            prompt_external=external if external != masked else None,
            system_masked=system_internal,
            system_external=(
                system_external if system_external != system_internal else None
            ),
            prompt_cipher=sealed.ciphertext if sealed else None,
            prompt_nonce=sealed.nonce if sealed else None,
            # 해시는 **마스킹 후 + 테넌트 솔트**다. 원문을 그대로 해싱하면 주민번호처럼
            # 탐색 공간이 좁은 값은 전수조사로 복원되고, 해시가 새 유출 경로가 된다.
            prompt_hash=hash_prompt(masked, tenant["end_user_salt"]),
            # `system_hash` 는 솔트가 없다 — 테넌트를 가로질러 "같은 프롬프트 전략을
            # 쓰는가" 를 비교해야 하고, system 프롬프트는 저엔트로피가 아니다.
            system_hash=hash_system(system),
            allowed_boundaries=sorted(verdict.allowed_boundaries),
            placement=role_config.placement,
            tier_models=role_config.tier_models,
            options=role_config.options,
            timeout_s=role_config.timeout,
            max_prompt_chars=role_config.max_prompt_chars,
            metadata=dict(metadata or {}),
        )

    # -- 제출 -----------------------------------------------------------------

    async def submit(
        self,
        principal: Principal,
        *,
        role: str,
        prompt: str,
        system: str | None = None,
        end_user: str | None = None,
        priority: int = 0,
        metadata: Mapping[str, Any] | None = None,
        wait: float | None = None,
    ) -> Submission:
        """생성 요청. **이 함수의 본문 순서가 곧 계약이다.**"""
        scope = principal.scope()

        # ① 인증 이후 — 권한·한도
        role_config, tenant, service, end_user_hash = self._authorize(
            principal, role, end_user
        )
        if role_config.is_embed:
            # 임베딩은 동기 경로다. 큐에 넣으면 소비자가 영원히 폴링한다.
            raise ApiError("wrong_kind", status=400, params={"role": role, "kind": "embed"})

        self._check_size(prompt, role_config)

        # **프롬프트는 호출자 소유다** — 요청의 `system` 이 우선하고, 없을 때만
        # 역할 기본값을 쓴다(B0). 그 해석을 **여기서** 끝낸다.
        #
        # 이 줄이 없었을 때: 가드도 저장도 요청의 `system` 만 보았고, 그래서
        # 역할 기본 system 이 **추론에 전혀 전달되지 않았다.** 그런데
        # `system_hash` 는 기본값을 보낸 것처럼 해싱해서, 프롬프트 드리프트
        # 추적까지 "쓰고 있다" 고 거짓 보고했다.
        #
        # 기본값도 가드를 지난다 — 관리자가 역할 system 에 실수로 넣은 PII 가
        # 필터를 우회하면 안 된다.
        effective_system = system or role_config.system

        # ② 가드 — 저장보다, 배치보다 먼저.
        verdict = await self._inspect(role_config, tenant, prompt, effective_system)

        if verdict.blocked:
            # 차단된 프롬프트는 어떤 노드에도 가지 않고 평문으로 저장되지도 않는다.
            self._record_guard_events(
                scope, verdict, job_id=None, service_id=principal.service_id
            )
            raise self._blocked(verdict)

        # ③ 저장
        job_id = self._create_job(
            scope,
            principal=principal, role_config=role_config, tenant=tenant,
            end_user_hash=end_user_hash, verdict=verdict, raw_prompt=prompt,
            system=effective_system, priority=priority, metadata=metadata,
        )
        self._record_guard_events(
            scope, verdict, job_id=job_id, service_id=principal.service_id
        )
        self._flag_end_user_shape(principal, end_user, job_id)

        # ④·⑤ 배치와 실행은 스케줄러가 한다. 여기서는 기다리기만 한다.
        result = await self.wait_for(scope, job_id, seconds=wait)
        return _with_guard(result, self._guard_actions(verdict))

    def _check_size(self, text: str, role: Role) -> None:
        if len(text) > role.max_prompt_chars:
            raise ApiError(
                "payload_too_large",
                status=413,
                params={"size": len(text), "limit": role.max_prompt_chars},
            )

    def _flag_end_user_shape(
        self, principal: Principal, end_user: str | None, job_id: str
    ) -> None:
        """`end_user` 가 개인정보처럼 생겼으면 남긴다.

        차단하지는 않는다 — 이미 해싱돼 저장되므로 거부할 실익이 없고, 거부하면
        소비자가 우회로를 만든다. 다만 계약 오해의 신호이므로 관리자가 알아야 한다.
        **감사에는 모양 이름만 남기고 값은 남기지 않는다.**
        """
        shape = looks_like_pii(end_user) if end_user else None
        if shape:
            self._store.audit(
                principal.service_id,
                "end_user_looks_like_pii",
                tenant_id=principal.tenant_id,
                target=job_id,
                detail={"shape": shape},
                outcome="warn",
            )

    async def wait_for(
        self, scope: TenantScope, job_id: str, *, seconds: float | None = None
    ) -> Submission:
        """완료를 기다린다. 다 못 기다리면 `pending` 으로 돌려준다.

        **응답 모양이 항상 같으므로 호출자가 분기할 필요가 없다.** 이 통합 대기가
        폴링을 줄이는 유일한 장치다(E6-a) — 없으면 소비자는 1초 폴링을 한다.
        """
        budget = (
            DEFAULT_WAIT_SECONDS
            if seconds is None
            else max(0.0, min(float(seconds), MAX_WAIT_SECONDS))
        )
        deadline = self._now() + budget
        # 폴 횟수로도 상한을 건다. 시계와 `asyncio.sleep` 이 어긋나면(주입된 시계,
        # 멈춘 단조 시계) 시각 비교만으로는 **루프가 영원히 안 끝난다** — 연결 하나가
        # 워커를 영구히 붙잡는 것은 대기 상한을 둔 이유 자체를 무효로 만든다.
        remaining_polls = int(budget / POLL_INTERVAL) + 1
        interval = POLL_INTERVAL

        while True:
            # **상태 한 칸만 읽는다.** `get_job` 은 `SELECT *` 라 마스킹본·암호문·
            # 응답까지 끌어오는데, 폴링이 보는 것은 이 한 칸뿐이다. 대기가 수십 건
            # 겹치면 그 전량이 초당 수백 번 오가며 이벤트 루프를 점유한다.
            status = self._store.job_status(scope, job_id)
            if status is None:
                raise ApiError("job_not_found", status=404)
            if status in _TERMINAL or self._now() >= deadline or remaining_polls <= 0:
                job = self._store.get_job(scope, job_id)
                if job is None:
                    raise ApiError("job_not_found", status=404)
                return self._to_submission(scope, job)

            remaining_polls -= 1
            await asyncio.sleep(interval)
            # **간격을 늘린다.** 첫 폴은 빨라야 짧은 잡이 즉시 돌아오고, 오래 걸리는
            # 잡을 20회/초로 확인할 이유는 없다. 고정 50ms 는 그 둘을 같은 값으로
            # 다루느라 긴 대기에 부하를 몰아준다.
            interval = min(interval * POLL_BACKOFF, MAX_POLL_INTERVAL)

    def cancel(self, scope: TenantScope, job_id: str, *, actor: str) -> Submission:
        """대기 중인 잡을 취소한다.

        **실행 중인 잡은 취소하지 못한다** — 노드에도 클라우드에도 취소 의미론이 없어서
        "취소했다" 고 답하면 거짓말이 된다. 예약은 반드시 푼다.
        """
        job = self._store.get_job(scope, job_id)
        if job is None:
            raise ApiError("job_not_found", status=404)
        if job.status == "running":
            raise ApiError("job_running", status=409)
        if job.status in _TERMINAL:
            return self._to_submission(scope, job)

        # **검사와 갱신을 한 문장으로.** 위의 상태 확인과 이 UPDATE 사이에
        # 스케줄러가 같은 잡을 디스패치할 수 있다 — 문서가 지원한다는 다중
        # 프로세스 구성에서는 그 창이 실제로 열린다. 그러면 소비자는 "취소됨" 을
        # 응답받고 그 잡은 실행되어 과금까지 간다.
        if not self._store.update_job(
            scope, job_id, expect_status=("queued", "pending"),
            status="cancelled", error_code="cancelled", finished_at=self._now(),
        ):
            # 그 사이에 상태가 바뀌었다. 지금 상태를 그대로 돌려준다 —
            # 취소했다고 답하는 것이 거짓말이 되는 경우다.
            current = self._store.get_job(scope, job_id)
            if current is None:
                raise ApiError("job_not_found", status=404)
            if current.status == "running":
                raise ApiError("job_running", status=409)
            return self._to_submission(scope, current)
        self._accountant.release_reservation(scope, job_id)
        self._store.audit(
            actor, "cancel_job", tenant_id=scope.tenant_id, target=job_id
        )
        refreshed = self._store.get_job(scope, job_id)
        return self._to_submission(scope, refreshed if refreshed else job)

    def _to_submission(self, scope: TenantScope, job: Any) -> Submission:
        pending = job.status not in _TERMINAL
        return Submission(
            job_id=job.id,
            status="pending" if pending else job.status,
            response=job.response,
            error=job.error,
            error_code=job.error_code,
            role=job.role,
            model=job.model,
            node=job.node,
            tier=job.tier,
            attempts=job.attempts,
            queue_position=self._queue_position(scope, job) if pending else None,
            retry_after=self._retry_after(job) if pending else None,
            wait_reason=job.wait_reason if pending else None,
            metadata=job.metadata,
        )

    def _queue_position(self, scope: TenantScope, job: Any) -> int:
        """이 테넌트의 같은 레인 대기 잡 중 앞에 있는 수.

        전역 깊이를 돌려주면 **다른 테넌트가 얼마나 밀어 넣었는지가 새어 나간다.**
        약한 정보지만 다중 테넌트 제품에서 굳이 열어 줄 이유가 없다.
        """
        ahead = 0
        for other in self._store.list_jobs(scope, status="queued", limit=500):
            if other.lane != job.lane or other.id == job.id:
                continue
            if (other.priority, -other.created_at) > (job.priority, -job.created_at):
                ahead += 1
        return ahead

    def _retry_after(self, job: Any) -> float:
        """큐 위치 기반 적응형 백오프.

        **폴링이 이 시스템의 유일한 파국 경로다**(E6-a) — 클러스터가 포화되면 큐가 늘고,
        큐가 늘면 대기 잡이 늘고, 대기 잡이 늘면 폴링이 늘어 컨트롤 플레인이 자기 부하가
        아니라 **클러스터 포화의 증상으로** 죽는다. 고정 간격을 주면 폴링 총량이 대기
        잡 수에 정비례해 늘어난다.

        전역 깊이를 쓰되 레인 처리율로 나눈 스칼라만 내보낸다 — 대기 시간의 추정치이지
        큐의 내용이 아니다.
        """
        depth = self._store.count_queued(job.lane)
        lane = self._config.lanes.get(job.lane)
        rate = max(1, lane.max_concurrent if lane else 1)
        return round(max(2.0, min(60.0, depth / rate)), 1)

    # -- 동기 임베딩 -----------------------------------------------------------

    async def embed(
        self,
        principal: Principal,
        *,
        role: str,
        inputs: Sequence[str],
        end_user: str | None = None,
    ) -> dict[str, Any]:
        """큐 밖 동기 경로.

        **큐만 우회하고 가드·배치·경계·비용은 우회하지 않는다.** 임베딩 입력도
        프롬프트이고, 어느 노드로 보낼지도 정해야 하며, metered 노드면 돈이 든다.

        입력을 **한 건씩 따로** 검사한다. 이어 붙여 한 번에 검사하면 마스킹이 길이를
        바꿔서 되쪼갤 수 없고, 소비자는 N개를 넣고 1개를 돌려받는다.
        """
        scope = principal.scope()
        role_config, tenant, service, end_user_hash = self._authorize(
            principal, role, end_user
        )
        if not role_config.is_embed:
            raise ApiError(
                "wrong_kind", status=400, params={"role": role, "kind": role_config.kind}
            )
        if not inputs:
            raise ApiError("empty_input", status=400)
        for text in inputs:
            self._check_size(text, role_config)

        verdicts = [await self._inspect(role_config, tenant, text, None) for text in inputs]

        allowed = frozenset(self._candidate_boundaries(role_config))
        blocked_rules: list[str] = []
        for verdict in verdicts:
            allowed &= verdict.allowed_boundaries
            blocked_rules.extend(verdict.blocked_rules)

        merged = GuardResult(
            allowed_boundaries=allowed,
            prompts={b: "" for b in allowed},
            systems={},
            detections=tuple(d for v in verdicts for d in v.detections),
            blocked_rules=tuple(dict.fromkeys(blocked_rules)),
            classifier_failed=any(v.classifier_failed for v in verdicts),
        )
        if not allowed:
            self._record_guard_events(
                scope, merged, job_id=None, service_id=principal.service_id
            )
            raise self._blocked(merged)

        # 잡 행을 만든다 — 큐에 넣지 않으려고 `running` 으로 시작한다. 스케줄러는
        # `queued` 만 집어가므로 이 잡은 큐를 지나지 않는다. 행이 필요한 이유는
        # **비용 예약이 잡 행에 걸려 있기 때문**이다(`place()` 가 거기에 쓴다).
        joined = "\n".join(inputs)
        store_verdict = GuardResult(
            allowed_boundaries=allowed,
            prompts={b: "\n".join(v.prompt_for(b) for v in verdicts) for b in allowed},
            systems={},
            detections=merged.detections,
        )
        job_id = self._create_job(
            scope,
            principal=principal, role_config=role_config, tenant=tenant,
            end_user_hash=end_user_hash, verdict=store_verdict, raw_prompt=joined,
            system=None, priority=0, metadata={"inputs": len(inputs)},
            status="running",
        )
        self._record_guard_events(
            scope, merged, job_id=job_id, service_id=principal.service_id
        )

        started = self._now()
        result = self._cluster.place(
            job_id=job_id,
            tenant_id=principal.tenant_id,
            service_id=principal.service_id,
            role=role_config,
            placement_snapshot=role_config.placement,
            prompt=joined,
            tenant_budget=tenant["budget_usd_per_month"],
            service_budget=service["budget_usd_per_month"],
            allowed_boundaries=allowed,
        )
        if result.outcome != PLACED or result.placement is None:
            # 동기 경로에는 큐가 없으므로 대기시킬 곳이 없다. 행정적 부재는
            # 재시도 가능(503), 용량 불가는 재시도해도 같다(422).
            retryable = result.outcome == WAIT
            self._store.update_job(
                scope, job_id, status="failed",
                error_code=result.code or "no_placement", error=result.reason,
                finished_at=self._now(),
            )
            self._accountant.release_reservation(scope, job_id)
            raise ApiError(
                result.code or "no_placement",
                status=503 if retryable else 422,
                retryable=retryable,
                params={"reason": result.reason or ""},
            )

        chosen = result.placement
        state = self._cluster.state(chosen.node)
        outbound = state is not None and state.node.data_boundary == EXTERNAL
        texts = [
            v.prompt_for(EXTERNAL) if outbound else v.prompt_for(INTERNAL)
            for v in verdicts
        ]

        try:
            embedding = await self._cluster.provider_for(chosen.node).embed(
                model=chosen.model, inputs=texts, timeout=role_config.timeout
            )
            self._cluster.record_success(chosen.node)
        except Exception as exc:
            self._cluster.record_failure(chosen.node, str(exc))
            self._store.update_job(
                scope, job_id, status="failed", error=str(exc),
                error_code=getattr(exc, "code", "backend_unavailable"),
                finished_at=self._now(),
            )
            self._accountant.release_reservation(scope, job_id)
            raise
        finally:
            self._cluster.release(chosen)

        self._store.update_job(scope, job_id, status="ok", finished_at=self._now())
        self._accountant.settle(
            scope, job_id,
            provider=chosen.provider, model=chosen.model,
            input_tokens=embedding.input_tokens, output_tokens=0,
            node=chosen.node, role=role_config.name, service_id=principal.service_id,
            status="ok", duration_ms=int((self._now() - started) * 1000),
            end_user_hash=end_user_hash,
        )

        return {
            "job_id": job_id,
            "model": chosen.model,
            "node": chosen.node,
            "tier": chosen.tier,
            "vectors": [list(v) for v in embedding.vectors],
            "input_tokens": embedding.input_tokens,
            "guard_actions": self._guard_actions(merged),
        }

    # -- 2단 분류기 배선 -------------------------------------------------------

    def make_classifier(self) -> Callable[[str, Sequence[GuardRule]], Awaitable[set[str]]]:
        """가드 2단 분류기를 클러스터에 연결한다.

        `_guard_classify` 역할은 `internal_only` 라서 배치 필터가 경계 밖 노드를 무조건
        탈락시킨다 — **분류기가 원문을 밖으로 보내는 경로가 구조적으로 없다.** 여기에
        `allowed_boundaries=(INTERNAL,)` 을 한 번 더 거는 것은 중복이 아니라, 역할
        설정이 잘못 바뀌어도 이 호출만은 안 새게 하는 두 번째 자물쇠다.

        실패는 예외로 던진다 — `Guard` 가 그것을 `classifier_failed` 로 받아
        `on_classifier_error` 정책을 태운다. **분류 실패는 "민감하지 않음" 판정이 아니다.**
        """
        # 가드 역할은 **플랫폼 소유다.** 테넌트 오버라이드를 태우지 않는다 —
        # 테넌트가 자기 분류기의 타임아웃이나 배치를 바꿀 수 있으면 안 된다.
        role = self._config.roles.get(GUARD_ROLE)

        async def classify(text: str, rules: Sequence[GuardRule]) -> set[str]:
            if role is None:
                raise RuntimeError(f"분류 역할 {GUARD_ROLE} 이 설정에 없다")
            if not rules:
                return set()

            if self._evaluator is not None and not self._evaluator.classifier_is_certified(
                role.model
            ):
                # 구조화 출력 준수율을 통과하지 못한 모델로 보안 판정을 하지 않는다.
                raise RuntimeError(f"분류 모델 {role.model} 이 인증되지 않았다")

            result = self._cluster.place(
                job_id=f"guard-{uuid.uuid4().hex[:12]}",
                tenant_id="_platform",
                service_id="_guard",
                role=role,
                placement_snapshot=role.placement,
                prompt=text,
                allowed_boundaries=(INTERNAL,),
            )
            if result.outcome != PLACED or result.placement is None:
                raise RuntimeError(
                    f"분류를 실행할 내부 노드가 없다: {result.reason or result.code}"
                )

            chosen = result.placement
            try:
                generated = await self._cluster.provider_for(chosen.node).generate(
                    model=chosen.model,
                    prompt=_classification_prompt(text, rules),
                    system=role.system,
                    options=role.options,
                    timeout=role.timeout,
                )
            finally:
                self._cluster.release(chosen)
            return _parse_classification(generated.text, rules)

        return classify


def _with_guard(submission: Submission, actions: Mapping[str, str]) -> Submission:
    return Submission(
        **{
            **{
                f: getattr(submission, f)
                for f in submission.__dataclass_fields__
                if f != "guard_actions"
            },
            "guard_actions": dict(actions),
        }
    )


def _classification_prompt(text: str, rules: Sequence[GuardRule]) -> str:
    catalog = "\n".join(
        f"- {rule.id}: {rule.description or rule.label or rule.id}" for rule in rules
    )
    return (
        "다음 맥락 중 입력에 해당하는 것의 id 만 쉼표로 나열한다. "
        "해당 없으면 NONE 만 출력한다. 다른 말을 덧붙이지 않는다.\n\n"
        f"[맥락]\n{catalog}\n\n[입력]\n{text}\n\n[출력]"
    )


def _parse_classification(raw: str, rules: Sequence[GuardRule]) -> set[str]:
    """모델 출력을 결정론적으로 흡수한다.

    프롬프트에 "id 만 쓰라" 고 적어도 모델은 벗어난다(§15-8 — hybrid thinking 계열은
    구조화 출력을 아예 깨뜨린다). 그래서 두 겹으로 흡수한다.

    **① 마지막 비어 있지 않은 줄만 본다.** 모델은 답을 마지막에 둔다. 출력 전체를
    훑으면 프롬프트의 맥락 카탈로그를 그대로 되읊은 출력에서 **모든 규칙이 걸린다** —
    오탐이 쏟아지면 관리자가 규칙을 꺼버리고, 안 켜진 필터는 없는 필터다.

    **② 그 줄에서는 단어 경계로 자른다.** `- deal` `deal, hr` `답: deal` 이 전부
    같게 읽힌다. 불릿·번호·따옴표에 판정이 흔들리면 안 된다.

    알려진 id 와 교집합을 취하므로 **모델이 없는 규칙을 지어내도 결과에 안 들어온다.**

    형식을 아예 못 지키는 모델은 여기서 조용히 빈 집합이 된다. 그 경우를 막는 것은
    이 함수가 아니라 `Evaluator.certify_classifier()` 의 등록 게이트다.
    """
    known = {rule.id for rule in rules}
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return set()
    return set(re.findall(r"[A-Za-z0-9_]+", lines[-1])) & known
