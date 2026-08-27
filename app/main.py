"""HTTP API — 라우터와 조립.

`build_app()` 은 **전부 주입식**이다. 테스트가 목 클러스터를 넣고, 데모 프로파일이
목 프로바이더를 넣고, 실제 설치가 진짜 노드를 넣는다 — 세 경로가 같은 코드를 지난다.

이 모듈은 **잡을 만들지 않는다.** 요청 순서(인증 → 가드 → 저장 → 배치 → 실행)는
`pipeline.Pipeline` 이 강제하고, 라우터는 HTTP 를 파이프라인 호출로 옮기기만 한다.
라우터가 `store.create_job()` 을 직접 부를 수 있으면 언젠가 누군가 가드를 건너뛴
경로를 만든다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import meta as meta_mod
from .auth import (
    ROLE_PLATFORM_ADMIN,
    ROLE_SERVICE,
    Principal,
    RateLimiter,
    authenticate,
    bearer_from_header,
    issue_token,
    limits_for,
    require_platform_admin,
    require_tenant_admin,
    rotate_token,
)
from .cluster import Cluster
from .config import EXTERNAL, INTERNAL, Config, ConfigError, validate_role_fields
from .cost import CostAccountant
from .crypto import KeyDestroyed, KeyVault, Sealed
from .evals import Evaluator
from .guard import Guard
from .i18n import ApiError, Translator, guard_pack_for, negotiate_locale
from .identity import new_salt
from .models import ModelRegistrar
from .pipeline import GUARD_ROLE, Pipeline, Submission, is_public_role
from .scheduler import Scheduler
from .store import PlatformScope, ScopeViolation, SqliteStore, StoreError, TenantScope

VERSION = "0.1.0"

#: 상태 조회는 제출과 다른 한도로 잰다. 대기 중인 소비자가 정상적으로 폴링하는 것을
#: 제출 한도로 막으면 안 되고, 그렇다고 무제한이면 큐가 길어질 때 컨트롤 플레인이
#: **클러스터 포화의 증상으로** 죽는다.
POLL_LIMIT_PER_MIN = 600

CLIENT_DIR = Path(__file__).resolve().parent.parent / "clients"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ── 조립 ────────────────────────────────────────────────────────────────────


@dataclass
class AppContext:
    """앱이 들고 있는 것 전부. 라우트 핸들러는 `request.app.state.ctx` 로 받는다."""

    config: Config
    store: SqliteStore
    cluster: Cluster
    guard: Guard
    pipeline: Pipeline
    translator: Translator
    vault: KeyVault
    limiter: RateLimiter
    accountant: CostAccountant
    evaluator: Evaluator
    registrar: ModelRegistrar
    scheduler: Scheduler | None = None
    version: str = VERSION
    #: 에어갭이면 클라우드 티어를 자동 비활성화하고 그 사실을 표시한다.
    #: **설정에 남아 있는데 조용히 실패하는 것이 최악이다.**
    airgap: bool = False
    now: Callable[[], float] = time.time
    static_dir: Path = STATIC_DIR
    client_dir: Path = CLIENT_DIR

    def limits_for_principal(self, tenant: Any, service: Any) -> dict[str, Any]:
        limits = limits_for(tenant, service)
        return {
            "rate_limit_tenant_per_min": limits.tenant,
            "rate_limit_service_per_min": limits.service,
            "rate_limit_end_user_per_min": limits.end_user,
            "status_poll_per_min": POLL_LIMIT_PER_MIN,
            "budget_usd_per_month_tenant": tenant["budget_usd_per_month"],
            "budget_usd_per_month_service": service["budget_usd_per_month"],
            "require_end_user": bool(service["require_end_user"]),
        }


def build_app(
    *,
    config: Config,
    store: SqliteStore,
    cluster: Cluster | None = None,
    guard: Guard | None = None,
    scheduler: Scheduler | None = None,
    pipeline: Pipeline | None = None,
    translator: Translator | None = None,
    vault: KeyVault | None = None,
    evaluator: Evaluator | None = None,
    registrar: ModelRegistrar | None = None,
    accountant: CostAccountant | None = None,
    airgap: bool = False,
    version: str = VERSION,
    now: Callable[[], float] = time.time,
    static_dir: Path | None = None,
    client_dir: Path | None = None,
    start_scheduler: bool = False,
) -> Starlette:
    """앱을 조립한다. 부품을 안 주면 기본값으로 만든다.

    **부품을 전부 주입 가능하게 두는 것이 데모 프로파일의 근거다** — 목 프로바이더를
    끼우면 GPU 없는 노트북 한 대로 클러스터 제품 전체를 시연할 수 있다.
    """
    translator = translator or Translator.from_dir(Path(__file__).resolve().parent.parent / "locales")
    vault = vault or KeyVault(None)
    accountant = accountant or CostAccountant(config.pricing, store, now=now)
    cluster = cluster or Cluster(config, store, accountant=accountant, now=now)
    guard = guard or Guard(config)
    evaluator = evaluator or Evaluator(config, store, guard, now=now)
    registrar = registrar or ModelRegistrar(config, cluster, store, now=now)
    pipeline = pipeline or Pipeline(
        config, store, cluster, guard,
        vault=vault, accountant=accountant, evaluator=evaluator, now=now,
    )

    # 2단 분류기를 여기서 꽂는다. 배선이 원형이라(가드 → 분류기 → 클러스터 → 파이프라인
    # → 가드) 생성자에서는 못 묶는다. **안 꽂으면 맥락 규칙이 조용히 아무것도 안 한다.**
    # 이미 꽂혀 있으면(테스트가 자기 분류기를 넣은 경우) 건드리지 않는다.
    if not guard.has_classifier and GUARD_ROLE in config.roles:
        guard.set_classifier(pipeline.make_classifier())

    ctx = AppContext(
        config=config, store=store, cluster=cluster, guard=guard, pipeline=pipeline,
        translator=translator, vault=vault, limiter=RateLimiter(store, now=now),
        accountant=accountant, evaluator=evaluator, registrar=registrar,
        scheduler=scheduler, version=version, airgap=airgap, now=now,
        static_dir=static_dir or STATIC_DIR, client_dir=client_dir or CLIENT_DIR,
    )

    routes = _routes(ctx)
    app = Starlette(
        routes=routes,
        exception_handlers={
            ApiError: _api_error_handler,
            ScopeViolation: _scope_violation_handler,
            StoreError: _store_error_handler,
            ConfigError: _config_error_handler,
            404: _not_found_handler,
            405: _method_handler,
            500: _internal_handler,
        },
    )
    app.state.ctx = ctx

    if start_scheduler and scheduler is not None:
        app.add_event_handler("startup", scheduler.start)
        app.add_event_handler("shutdown", scheduler.stop)

    return app


# ── 오류 ────────────────────────────────────────────────────────────────────


def _render(request: Request, error: ApiError) -> JSONResponse:
    """오류를 낸다. **기계용 코드와 사람용 메시지를 둘 다 싣는다.**

    분기는 코드로, 표시는 메시지로. 로케일을 바꿔도 `code` 와 `retryable` 은 안 바뀐다.
    """
    ctx: AppContext = request.app.state.ctx
    locale = _locale(request, getattr(request.state, "tenant_locale", None), ctx)
    body = ctx.translator.render_error(error, locale)
    return JSONResponse(body, status_code=error.status, headers={"Content-Language": locale})


async def _api_error_handler(request: Request, exc: Exception) -> Response:
    return _render(request, exc)  # type: ignore[arg-type]


async def _scope_violation_handler(request: Request, exc: Exception) -> Response:
    # 스코프 위반은 **버그이지 사용자 오류가 아니다.** 사유를 밖으로 흘리지 않는다.
    return _render(request, ApiError("internal", status=500))


async def _store_error_handler(request: Request, exc: Exception) -> Response:
    return _render(request, ApiError("internal", status=500))


async def _config_error_handler(request: Request, exc: Exception) -> Response:
    return _render(request, ApiError("invalid_field", status=400, params={"field": str(exc)}))


async def _not_found_handler(request: Request, exc: Exception) -> Response:
    return _render(request, ApiError("not_found", status=404))


async def _method_handler(request: Request, exc: Exception) -> Response:
    return _render(request, ApiError("method_not_allowed", status=405))


async def _internal_handler(request: Request, exc: Exception) -> Response:
    return _render(request, ApiError("internal", status=500))


# ── 요청 보조 ────────────────────────────────────────────────────────────────


def _locale(request: Request, tenant_locale: str | None, ctx: AppContext) -> str:
    """Accept-Language → 테넌트 기본값 → 플랫폼 기본값.

    다중 테넌트라 **테넌트마다 기본 로케일이 다를 수 있다.**
    """
    return negotiate_locale(
        ctx.translator.available,
        accept_language=request.headers.get("accept-language"),
        tenant_default=tenant_locale,
        platform_default=ctx.translator.default,
    )


def _principal(request: Request) -> Principal:
    ctx: AppContext = request.app.state.ctx
    principal = authenticate(
        ctx.store, bearer_from_header(request.headers.get("authorization")), now=ctx.now
    )
    tenant = ctx.store.get_tenant(principal.tenant_id)
    # 이후 오류 응답이 이 테넌트의 로케일로 렌더되도록 남긴다.
    request.state.tenant_locale = tenant["locale"] if tenant else None
    request.state.tenant = tenant
    return principal


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ApiError("invalid_json", status=400)
    if not isinstance(parsed, dict):
        raise ApiError("invalid_json", status=400)
    return parsed


def _need(body: Mapping[str, Any], field_name: str) -> Any:
    if field_name not in body or body[field_name] in (None, ""):
        raise ApiError("missing_field", status=400, params={"field": field_name})
    return body[field_name]


def _ok(request: Request, payload: Any, status: int = 200) -> JSONResponse:
    locale = getattr(request.state, "response_locale", None)
    headers = {"Content-Language": locale} if locale else {}
    return JSONResponse(payload, status_code=status, headers=headers)


def _submission_body(submission: Submission) -> dict[str, Any]:
    body: dict[str, Any] = {
        "job_id": submission.job_id,
        "status": submission.status,
        "role": submission.role,
        "attempts": submission.attempts,
        "guard_actions": dict(submission.guard_actions),
    }
    for key in ("response", "error", "error_code", "model", "node", "tier"):
        value = getattr(submission, key)
        if value is not None:
            body[key] = value
    if submission.pending:
        body["queue_position"] = submission.queue_position
        body["retry_after"] = submission.retry_after
        if submission.wait_reason:
            body["wait_reason"] = submission.wait_reason
    if submission.metadata:
        body["metadata"] = dict(submission.metadata)
    return body


def _submission_response(request: Request, submission: Submission) -> JSONResponse:
    response = _ok(request, _submission_body(submission))
    if submission.pending and submission.retry_after is not None:
        # 표준 헤더로도 실어 보낸다 — 본문을 안 읽는 클라이언트도 지키게.
        response.headers["Retry-After"] = str(int(submission.retry_after))
    return response


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ── 소비자 ──────────────────────────────────────────────────────────────────


async def healthz(request: Request) -> Response:
    """인증 없이 응답한다. 컨테이너 헬스체크와 로드밸런서가 쓴다.

    **DB 를 만지지 않는다** — DB 가 느릴 때 헬스체크까지 느려지면 오케스트레이터가
    멀쩡한 컨테이너를 죽인다.
    """
    ctx: AppContext = request.app.state.ctx
    return JSONResponse({"ok": True, "version": ctx.version, "api": meta_mod.API_VERSION})


async def generate(request: Request) -> Response:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    body = await _body(request)

    submission = await ctx.pipeline.submit(
        principal,
        role=str(_need(body, "role")),
        prompt=str(_need(body, "prompt")),
        system=body.get("system"),
        end_user=body.get("end_user"),
        priority=int(body.get("priority", 0) or 0),
        metadata=body.get("metadata") or {},
        wait=body.get("wait"),
    )
    return _submission_response(request, submission)


async def embed(request: Request) -> Response:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    body = await _body(request)

    raw = _need(body, "input")
    inputs = [raw] if isinstance(raw, str) else list(raw)
    if not all(isinstance(t, str) for t in inputs):
        raise ApiError("invalid_field", status=400, params={"field": "input"})

    result = await ctx.pipeline.embed(
        principal,
        role=str(_need(body, "role")),
        inputs=inputs,
        end_user=body.get("end_user"),
    )
    return _ok(request, result)


async def job_get(request: Request) -> Response:
    """작업 조회. **폴링 방어가 여기 걸린다.**"""
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    ctx.limiter.check_named(
        f"poll:{principal.tenant_id}:{principal.service_id}",
        POLL_LIMIT_PER_MIN,
        scope_label="status_poll",
    )
    wait = request.query_params.get("wait")
    submission = await ctx.pipeline.wait_for(
        principal.scope(),
        request.path_params["job_id"],
        seconds=float(wait) if wait else 0.0,
    )
    return _submission_response(request, submission)


async def job_cancel(request: Request) -> Response:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    submission = ctx.pipeline.cancel(
        principal.scope(), request.path_params["job_id"], actor=principal.service_id
    )
    return _ok(request, _submission_body(submission))


async def roles(request: Request) -> Response:
    """이 토큰이 쓸 수 있는 역할.

    **여기 보이는 것이 곧 쓸 수 있는 전부여야 한다** — 목록에 있는데 못 쓰거나 목록에
    없는데 쓰이면 계약이 거짓말이 된다. 그래서 `meta.visible_roles()` 하나만 쓴다.
    """
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    service = ctx.store.get_service(principal.scope(), principal.service_id)
    if service is None:
        raise ApiError("unauthorized", status=401)

    names = meta_mod.visible_roles(ctx.config, service["allow_roles_json"])
    return _ok(request, {
        "roles": [meta_mod.role_contract(ctx.config, n) for n in names],
        "limits": ctx.limits_for_principal(request.state.tenant, service),
    })


async def status(request: Request) -> Response:
    """클러스터 상태 요약. 소비자가 "왜 느린가" 를 스스로 답할 수 있게."""
    ctx: AppContext = request.app.state.ctx
    _principal(request)   # 인증만 한다 — 클러스터 상태는 테넌트에 무관하다
    lanes = ctx.scheduler.snapshot() if ctx.scheduler else {}
    nodes = ctx.cluster.snapshot()
    return _ok(request, {
        # 레인 통계에는 `scan_truncated` 가 들어 있다. 절단 사실을 숨기지 않는다 —
        # 조용히 자르면 "전부 검토했다" 로 읽힌다.
        "lanes": lanes,
        "nodes": {
            "total": len(nodes),
            "healthy": sum(1 for n in nodes if n["status"] == "healthy"),
            "draining": sum(1 for n in nodes if n["status"] == "draining"),
        },
        # 자동 복제를 하지 않으므로 사람이 판단할 재료를 준다.
        "single_homed_roles": ctx.cluster.single_homed_roles(),
        "airgap": ctx.airgap,
    })


# ── 계약 자기 서빙 ───────────────────────────────────────────────────────────


def _service_or_401(ctx: AppContext, principal: Principal) -> Any:
    service = ctx.store.get_service(principal.scope(), principal.service_id)
    if service is None:
        raise ApiError("unauthorized", status=401)
    return service


async def meta_endpoint(request: Request) -> Response:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    service = _service_or_401(ctx, principal)
    tenant = request.state.tenant

    return _ok(request, meta_mod.meta_document(
        ctx.config,
        allow_roles=service["allow_roles_json"],
        tenant_locale=tenant["locale"],
        locales=ctx.translator.available,
        base_url=_base_url(request),
        routes=request.app.routes,
        version=ctx.version,
        schema_version=ctx.store.schema_version,
        limits=ctx.limits_for_principal(tenant, service),
        guard_locale_pack=guard_pack_for(tenant["locale"]),
        airgap=ctx.airgap,
    ))


async def integration(request: Request) -> Response:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    service = _service_or_401(ctx, principal)
    tenant = request.state.tenant
    locale = _locale(request, tenant["locale"], ctx)

    text = meta_mod.integration_guide(
        ctx.config,
        allow_roles=service["allow_roles_json"],
        base_url=_base_url(request),
        routes=request.app.routes,
        limits=ctx.limits_for_principal(tenant, service),
        translator=ctx.translator,
        locale=locale,
    )
    return PlainTextResponse(
        text, media_type="text/markdown; charset=utf-8",
        headers={"Content-Language": locale},
    )


def _openapi_for(request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    service = _service_or_401(ctx, principal)
    return meta_mod.openapi_document(
        ctx.config,
        allow_roles=service["allow_roles_json"],
        base_url=_base_url(request),
        version=ctx.version,
    )


async def openapi_json(request: Request) -> Response:
    return _ok(request, _openapi_for(request))


async def openapi_yaml(request: Request) -> Response:
    import yaml

    return PlainTextResponse(
        yaml.safe_dump(_openapi_for(request), allow_unicode=True, sort_keys=False),
        media_type="application/yaml; charset=utf-8",
    )


async def client_index(request: Request) -> Response:
    """번들된 클라이언트·목 서버 목록.

    **설치처 개발자가 노드도 토큰도 없이 통합 코드를 완성할 수 있어야 한다.**
    붙이기 어려우면 우회로를 만들고, 우회로는 가드도 비용도 감사도 지나지 않는다.
    """
    ctx: AppContext = request.app.state.ctx
    files = sorted(p.name for p in ctx.client_dir.glob("*") if p.is_file()) if ctx.client_dir.is_dir() else []
    return _ok(request, {
        "files": [
            {"name": name, "url": f"{_base_url(request)}/v1/client/{name}"} for name in files
        ],
    })


async def client_file(request: Request) -> Response:
    ctx: AppContext = request.app.state.ctx
    name = request.path_params["name"]
    target = (ctx.client_dir / name).resolve()
    # 경로 탈출 방지 — 파일 이름을 그대로 붙이면 `../../keys/master.key` 가 열린다.
    if ctx.client_dir.resolve() not in target.parents or not target.is_file():
        raise ApiError("not_found", status=404)
    return PlainTextResponse(target.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")


# ── 테넌트 관리 ──────────────────────────────────────────────────────────────


def _tenant_admin(request: Request) -> tuple[AppContext, Principal, TenantScope]:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    require_tenant_admin(principal)
    return ctx, principal, principal.scope()


async def tenant_services(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    if request.method == "GET":
        return _ok(request, {"services": [
            {
                "id": row["id"], "name": row["name"], "status": row["status"],
                "allow_roles": json.loads(row["allow_roles_json"]),
                "rate_limit_per_min": row["rate_limit_per_min"],
                "budget_usd_per_month": row["budget_usd_per_month"],
                "require_end_user": bool(row["require_end_user"]),
                "end_user_rate_limit": row["end_user_rate_limit"],
                "created_at": row["created_at"],
            }
            for row in ctx.store.list_services(scope)
        ]})

    body = await _body(request)
    allow = list(body.get("allow_roles") or ["*"])
    unknown = [r for r in allow if r != "*" and r not in ctx.config.roles]
    if unknown:
        raise ApiError("unknown_role", status=404, params={"role": ", ".join(unknown)})
    hidden = [r for r in allow if r != "*" and not is_public_role(r)]
    if hidden:
        # 내부 역할은 소비자 토큰에 붙일 수 없다. 붙일 수 있으면 분류 경로가 열린다.
        raise ApiError("unknown_role", status=404, params={"role": ", ".join(hidden)})

    service_id = str(_need(body, "id"))
    ctx.store.create_service(
        scope, service_id, str(body.get("name") or service_id),
        allow_roles=allow,
        rate_limit_per_min=body.get("rate_limit_per_min"),
        budget_usd_per_month=body.get("budget_usd_per_month"),
        require_end_user=bool(body.get("require_end_user", False)),
        end_user_rate_limit=body.get("end_user_rate_limit"),
    )
    ctx.store.audit(
        principal.token_id, "create_service", tenant_id=scope.tenant_id,
        target=service_id, detail={"allow_roles": allow},
    )
    return _ok(request, {"id": service_id}, status=201)


async def tenant_tokens(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    if request.method == "GET":
        # **원값도 해시도 나가지 않는다.** 접두사만으로 어느 토큰인지 식별한다.
        return _ok(request, {"tokens": [
            {
                "id": row["id"], "service_id": row["service_id"], "prefix": row["prefix"],
                "role": row["role"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "last_used_at": row["last_used_at"],
                "revoked_at": row["revoked_at"], "note": row["note"],
            }
            for row in ctx.store.list_tokens(scope)
        ]})

    body = await _body(request)
    service_id = str(_need(body, "service_id"))
    if ctx.store.get_service(scope, service_id) is None:
        raise ApiError("not_found", status=404)

    role = str(body.get("role") or ROLE_SERVICE)
    if role == ROLE_PLATFORM_ADMIN:
        # 테넌트 관리자가 플랫폼 권한을 스스로 발급할 수 있으면 RBAC 가 사라진다.
        raise ApiError("forbidden_platform_admin", status=403)

    token_id, raw = issue_token(
        ctx.store, scope, service_id, role=role,
        expires_at=body.get("expires_at"), note=body.get("note"),
        actor=principal.token_id,
    )
    return _ok(request, {
        "id": token_id,
        "token": raw,
        "note": "이 값은 지금 한 번만 보입니다. 다시 볼 수 없습니다.",
    }, status=201)


async def tenant_token_rotate(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    body = await _body(request)
    new_id, raw = rotate_token(
        ctx.store, scope, request.path_params["token_id"],
        actor=principal.token_id,
        grace_seconds=float(body.get("grace_seconds", 0) or 0),
        now=ctx.now,
    )
    return _ok(request, {
        "id": new_id, "token": raw,
        "note": "이 값은 지금 한 번만 보입니다. 다시 볼 수 없습니다.",
    })


async def tenant_token_revoke(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    if not ctx.store.revoke_token(scope, request.path_params["token_id"]):
        raise ApiError("not_found", status=404)
    ctx.store.audit(
        principal.token_id, "revoke_token", tenant_id=scope.tenant_id,
        target=request.path_params["token_id"],
    )
    return _ok(request, {"revoked": True})


async def tenant_guard_rules(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    tenant = request.state.tenant
    pack = guard_pack_for(tenant["locale"])

    if request.method == "GET":
        effective = ctx.guard.rules_for(
            [pack] if pack else [], ctx.pipeline.tenant_guard_rules(scope)
        )
        return _ok(request, {
            "locale_pack": pack,
            "tenant_rules": ctx.store.list_tenant_guard_rules(scope),
            # 실제로 적용되는 값. 베이스라인과 병합된 결과다.
            "effective": [
                {
                    "id": r.id, "kind": r.kind, "label": r.label,
                    "locale_pack": r.locale_pack,
                    "action": {b: r.action_for_boundary(b) for b in (INTERNAL, EXTERNAL)},
                }
                for r in effective
            ],
        })

    body = await _body(request)
    rule = {
        "id": str(_need(body, "id")),
        "kind": str(body.get("kind") or "pattern"),
        "action": _need(body, "action"),
        "label": body.get("label") or "",
        "pattern": body.get("pattern"),
        "checksum": body.get("checksum"),
        "keep_tail": int(body.get("keep_tail", 0) or 0),
        "description": body.get("description"),
        "locale_pack": str(body.get("locale_pack") or "tenant"),
    }
    if rule["kind"] == "pattern" and not rule["pattern"]:
        raise ApiError("missing_field", status=400, params={"field": "pattern"})
    ctx.guard.validate_rule(rule)

    ctx.store.set_tenant_guard_rule(scope, rule, updated_by=principal.token_id)
    ctx.store.audit(
        principal.token_id, "set_guard_rule", tenant_id=scope.tenant_id,
        target=rule["id"], detail={"action": rule["action"], "kind": rule["kind"]},
    )
    return _ok(request, {"id": rule["id"]}, status=201)


async def tenant_guard_rule_delete(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    rule_id = request.path_params["rule_id"]
    if not ctx.store.clear_tenant_guard_rule(scope, rule_id):
        raise ApiError("not_found", status=404)
    ctx.store.audit(
        principal.token_id, "clear_guard_rule", tenant_id=scope.tenant_id, target=rule_id
    )
    return _ok(request, {"deleted": True})


async def tenant_guard_events(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    params = request.query_params
    rows = ctx.store.list_filter_events(
        scope,
        action=params.get("action"),
        unreviewed_only=params.get("unreviewed") == "1",
        limit=min(int(params.get("limit", 100)), 500),
    )
    return _ok(request, {"events": [
        # **매칭된 값은 애초에 저장하지 않는다.** 오프셋과 횟수만 나간다.
        {
            "id": row["id"], "ts": row["ts"], "rule_id": row["rule_id"],
            "stage": row["stage"], "action": row["action"], "boundary": row["boundary"],
            "match_count": row["match_count"], "job_id": row["job_id"],
            "service_id": row["service_id"], "reviewed": bool(row["reviewed"]),
            "verdict": row["verdict"],
        }
        for row in rows
    ]})


async def tenant_guard_review(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    body = await _body(request)
    verdict = str(_need(body, "verdict"))
    if verdict not in ("true_positive", "false_positive"):
        raise ApiError("invalid_field", status=400, params={"field": "verdict"})
    if not ctx.evaluator.review(scope, int(request.path_params["event_id"]), verdict):
        raise ApiError("not_found", status=404)
    return _ok(request, {"reviewed": True})


async def tenant_guard_promote(request: Request) -> Response:
    """`audit` → `block` 승격 가능 여부.

    판정만 하고 적용은 하지 않는다 — 실제 적용은 규칙 저장(PUT)이고, 그쪽이 이
    게이트를 다시 검사한다.
    """
    ctx, principal, scope = _tenant_admin(request)
    verdict = ctx.evaluator.can_promote(
        scope,
        request.path_params["rule_id"],
        request.query_params.get("to", "block"),
    )
    return _ok(request, {
        "allowed": verdict.allowed, "reason": verdict.reason,
        "false_positive_rate": verdict.rate, "reviewed": verdict.reviewed,
        "limit": verdict.limit,
    })


async def tenant_settings(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    tenant = request.state.tenant

    if request.method == "GET":
        return _ok(request, {
            "id": tenant["id"], "name": tenant["name"], "locale": tenant["locale"],
            "status": tenant["status"],
            "budget_usd_per_month": tenant["budget_usd_per_month"],
            "rate_limit_per_min": tenant["rate_limit_per_min"],
            "raw_prompt_retention_days": ctx.store.tenant_setting(
                scope, "raw_prompt_retention_days",
                ctx.config.guard_settings.raw_prompt_retention_days,
            ),
            # 원문 보관은 키가 있을 때만 가능하다. **평문 폴백은 없다.**
            "raw_prompt_storage": ctx.vault.enabled,
            "guard_locale_pack": guard_pack_for(tenant["locale"]),
            "available_locales": list(ctx.translator.available),
        })

    body = await _body(request)
    if "locale" in body:
        if body["locale"] not in ctx.translator.available:
            raise ApiError("invalid_field", status=400, params={"field": "locale"})
        ctx.store.set_tenant_locale(scope, str(body["locale"]))
    if "raw_prompt_retention_days" in body:
        days = int(body["raw_prompt_retention_days"])
        if days < 0:
            raise ApiError("invalid_field", status=400, params={"field": "raw_prompt_retention_days"})
        ctx.store.set_tenant_setting(scope, "raw_prompt_retention_days", days)
    ctx.store.audit(
        principal.token_id, "update_tenant_settings", tenant_id=scope.tenant_id,
        detail={k: body[k] for k in ("locale", "raw_prompt_retention_days") if k in body},
    )
    return _ok(request, {"updated": True})


async def tenant_overrides(request: Request) -> Response:
    """역할 오버라이드.

    오버라이드 수 > 0 이면 **실행 중 설정이 배포본과 다르다는 뜻**이므로 그대로 노출한다.
    """
    ctx, principal, scope = _tenant_admin(request)
    if request.method == "GET":
        return _ok(request, {"overrides": ctx.store.get_role_overrides(scope)})

    body = await _body(request)
    role = str(_need(body, "role"))
    if role not in ctx.config.roles or not is_public_role(role):
        raise ApiError("unknown_role", status=404, params={"role": role})

    if request.method == "DELETE":
        ctx.store.clear_role_override(scope, role)
        ctx.store.audit(
            principal.token_id, "clear_role_override", tenant_id=scope.tenant_id, target=role
        )
        return _ok(request, {"cleared": True})

    fields = dict(_need(body, "fields"))
    errors = validate_role_fields(fields)
    if errors:
        # `kind` 와 `system` 은 여기서 걸린다 — 전자는 동기 경로로 새어나가고,
        # 후자는 "프롬프트는 호출자 소유" 계약과 충돌한다.
        raise ApiError("invalid_field", status=400, params={"field": ", ".join(sorted(errors))})
    ctx.store.set_role_override(
        scope, role, fields, note=body.get("note"), updated_by=principal.token_id
    )
    ctx.store.audit(
        principal.token_id, "set_role_override", tenant_id=scope.tenant_id,
        target=role, detail={"fields": sorted(fields)},
    )
    return _ok(request, {"role": role, "fields": fields}, status=201)


async def tenant_jobs(request: Request) -> Response:
    """작업 목록. **마스킹본만 나간다.** 원문은 단건 API + 감사다."""
    ctx, principal, scope = _tenant_admin(request)
    params = request.query_params
    rows = ctx.store.list_jobs(
        scope, status=params.get("status"),
        end_user_hash=params.get("end_user_hash"),
        limit=min(int(params.get("limit", 50)), 200),
    )
    return _ok(request, {"jobs": [
        {
            "id": j.id, "service_id": j.service_id, "end_user_hash": j.end_user_hash,
            "role": j.role, "lane": j.lane, "status": j.status, "node": j.node,
            "model": j.model, "tier": j.tier, "attempts": j.attempts,
            "prompt_masked": j.prompt_masked, "response": j.response,
            "prompt_hash": j.prompt_hash, "system_hash": j.system_hash,
            "has_raw": j.prompt_cipher is not None,
            "allowed_boundaries": list(j.allowed_boundaries),
            "wait_reason": j.wait_reason, "error_code": j.error_code,
            "cost_usd": j.cost_usd, "input_tokens": j.input_tokens,
            "output_tokens": j.output_tokens,
            "created_at": j.created_at, "finished_at": j.finished_at,
        }
        for j in rows
    ]})


async def tenant_job_raw(request: Request) -> Response:
    """원문 단건 복호화.

    **열람 자체가 감사에 남는다.** 원문을 볼 수 있는 경로가 있다는 것과 아무도 모르게
    볼 수 있다는 것은 전혀 다른 이야기다.
    """
    ctx, principal, scope = _tenant_admin(request)
    job_id = request.path_params["job_id"]
    job = ctx.store.get_job(scope, job_id)
    if job is None:
        raise ApiError("job_not_found", status=404)
    if not job.prompt_cipher or not job.prompt_nonce or not ctx.vault.enabled:
        raise ApiError("raw_prompt_unavailable", status=404)

    tenant = request.state.tenant
    try:
        plaintext = ctx.vault.open(
            tenant["dek_wrapped"], Sealed(nonce=job.prompt_nonce, ciphertext=job.prompt_cipher)
        )
    except KeyDestroyed:
        # DEK 가 폐기됐다 — crypto-shredding 이후에는 백업의 암호문도 못 연다.
        raise ApiError("raw_prompt_unavailable", status=404)

    ctx.store.audit(
        principal.token_id, "read_raw_prompt", tenant_id=scope.tenant_id, target=job_id,
        detail={"role": job.role, "service_id": job.service_id},
    )
    return _ok(request, {"job_id": job_id, "prompt": plaintext})


async def tenant_usage(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    params = request.query_params
    since = float(params.get("since") or (ctx.now() - 30 * 86400))
    axis = params.get("by", "service_id")
    if axis not in ctx.store.USAGE_AXES:
        raise ApiError("invalid_field", status=400, params={"field": "by"})

    budget = ctx.accountant.budget_status(
        scope, limit=request.state.tenant["budget_usd_per_month"]
    )
    return _ok(request, {
        "since": since,
        "by": axis,
        "rows": ctx.store.usage_summary(scope, since=since, group_by=axis),
        "spend_usd": ctx.store.spend_since(scope, since),
        "budget": {
            "limit": budget.limit, "spent": budget.spent, "reserved": budget.reserved,
            "committed": budget.committed,
            # 한도가 없으면 `remaining` 은 무한대다. JSON 에 `Infinity` 를 실으면
            # 엄격한 파서가 응답 자체를 거부하므로 `null` 로 내보낸다.
            "remaining": None if budget.limit is None else budget.remaining,
            "burn_rate": budget.burn_rate,
            # 임계는 `thresholds.yaml` 한 곳에서 읽는다 — 문서와 코드가 갈리지 않게.
            "warn_at": ctx.config.thresholds.cost_budget_burn_warn,
        },
    })


async def tenant_audit(request: Request) -> Response:
    ctx, principal, scope = _tenant_admin(request)
    rows = ctx.store.list_audit(scope, limit=min(int(request.query_params.get("limit", 100)), 500))
    return _ok(request, {"audit": [dict(row) for row in rows]})


async def tenant_export(request: Request) -> Response:
    """내보내기 — **마스킹본 기준.** 암호문은 나가지 않는다."""
    ctx, principal, scope = _tenant_admin(request)
    payload = ctx.store.export_tenant(scope)
    ctx.store.audit(
        principal.token_id, "export_tenant", tenant_id=scope.tenant_id,
        detail={k: len(v) if isinstance(v, list) else 1 for k, v in payload.items()},
    )
    return _ok(request, payload)


async def tenant_purge_end_user(request: Request) -> Response:
    """엔드유저 파기.

    되돌릴 수 없으므로 확인값을 요구한다. **감사에는 언제·누가·무엇을만 남기고
    지워진 내용은 남기지 않는다** — 감사가 새 유출 경로가 되면 안 된다.
    """
    ctx, principal, scope = _tenant_admin(request)
    end_user_hash = request.path_params["end_user_hash"]
    body = await _body(request)
    if body.get("confirm") != end_user_hash:
        raise ApiError("confirmation_required", status=400)

    counts = ctx.store.purge_end_user(scope, end_user_hash)
    ctx.store.audit(
        principal.token_id, "purge_end_user", tenant_id=scope.tenant_id,
        target=end_user_hash, detail=counts,
    )
    return _ok(request, {"purged": counts})


# ── 플랫폼 관리 ──────────────────────────────────────────────────────────────


def _platform_admin(request: Request) -> tuple[AppContext, Principal]:
    ctx: AppContext = request.app.state.ctx
    principal = _principal(request)
    require_platform_admin(principal)
    return ctx, principal


async def platform_tenants(request: Request) -> Response:
    ctx, principal = _platform_admin(request)
    scope = PlatformScope(principal.token_id, request.query_params.get("reason", "platform console"))

    if request.method == "GET":
        return _ok(request, {"tenants": [
            {
                "id": row["id"], "name": row["name"], "locale": row["locale"],
                "status": row["status"],
                "budget_usd_per_month": row["budget_usd_per_month"],
                "rate_limit_per_min": row["rate_limit_per_min"],
                "has_dek": row["dek_wrapped"] is not None,
                "created_at": row["created_at"],
            }
            for row in ctx.store.list_tenants(scope)
        ]})

    body = await _body(request)
    tenant_id = str(_need(body, "id"))
    locale = str(body.get("locale") or ctx.translator.default)
    if locale not in ctx.translator.available:
        raise ApiError("invalid_field", status=400, params={"field": "locale"})

    ctx.store.create_tenant(
        tenant_id, str(body.get("name") or tenant_id), locale=locale,
        end_user_salt=new_salt(), dek_wrapped=ctx.vault.create_dek(),
        budget_usd_per_month=body.get("budget_usd_per_month"),
        rate_limit_per_min=body.get("rate_limit_per_min"),
    )
    ctx.store.audit(
        principal.token_id, "create_tenant", tenant_id=tenant_id,
        detail={"locale": locale, "guard_locale_pack": guard_pack_for(locale)},
    )
    return _ok(request, {
        "id": tenant_id,
        "locale": locale,
        # 팩을 안 켜면 그 나라 PII 는 안 잡힌다. 만들 때부터 무엇이 켜졌는지 알려준다.
        "guard_locale_pack": guard_pack_for(locale),
        "raw_prompt_storage": ctx.vault.enabled,
    }, status=201)


async def platform_tenant_purge(request: Request) -> Response:
    """테넌트 파기 + DEK 폐기.

    **DEK 폐기가 가장 강한 삭제다** — 백업에 암호문이 남아 있어도 복호화가 불가능하다
    (crypto-shredding). 7일 뒤 암호문을 지워도 30일 전 백업을 복원하면 되살아나는
    문제를 구조적으로 푸는 유일한 수단이다.
    """
    ctx, principal = _platform_admin(request)
    tenant_id = request.path_params["tenant_id"]
    body = await _body(request)
    if body.get("confirm") != tenant_id:
        raise ApiError("confirmation_required", status=400)

    reason = str(body.get("reason") or "tenant purge requested")
    scope = PlatformScope(principal.token_id, reason)
    counts = ctx.store.purge_tenant(scope, tenant_id)
    return _ok(request, {"purged": counts, "dek_destroyed": True})


async def platform_nodes(request: Request) -> Response:
    ctx, principal = _platform_admin(request)
    if request.method == "GET":
        return _ok(request, {"nodes": ctx.cluster.snapshot(), "airgap": ctx.airgap})

    body = await _body(request)
    state, reachable = await ctx.cluster.register_node(
        body, actor=principal.token_id, airgap=ctx.airgap
    )
    # 등록 즉시 프로브한다 — 설치 후에 조용히 안 붙는 것이 제품에서 가장 나쁜 경험이다.
    # `reachable` 과 `status` 는 다른 질문에 답한다. 갓 등록한 노드는 잘 붙어도
    # `unknown` 이다(헬스는 연속 2회 성공을 요구한다). 등록 화면이 봐야 할 것은 앞쪽이다.
    return _ok(request, {
        "name": state.name,
        "reachable": reachable,
        "status": state.status,
        "data_boundary": state.node.data_boundary,
        "models": sorted(state.models),
        "error": state.last_error,
    }, status=201)


async def platform_node_drain(request: Request) -> Response:
    ctx, principal = _platform_admin(request)
    node = request.path_params["node"]
    body = await _body(request)
    if body.get("undrain"):
        ctx.cluster.undrain(node)
        action = "undrain"
    else:
        # 즉시 차단이 아니다 — 신규만 막고 실행 중인 잡은 끝낸다.
        ctx.cluster.drain(node, force=bool(body.get("force")))
        action = "drain"
    ctx.store.audit(principal.token_id, f"{action}_node", target=node)
    return _ok(request, {"node": node, "action": action})


async def platform_models(request: Request) -> Response:
    ctx, principal = _platform_admin(request)
    if request.method == "GET":
        return _ok(request, {
            "requests": ctx.registrar.snapshot(),
            "pending": ctx.registrar.pending_count(),
            "missing": [
                {"node": r.node, "model": r.model} for r in ctx.registrar.detect_missing()
            ],
        })

    body = await _body(request)
    req = ctx.registrar.request_install(
        node=str(_need(body, "node")), model=str(_need(body, "model")),
        requested_by=principal.token_id,
    )
    return _ok(request, {"id": req.id, "status": req.status}, status=201)


async def platform_model_approve(request: Request) -> Response:
    """설치 승인·거부.

    **플랫폼 관리자 권한인 이유**: 승인은 그 노드 디스크에 수 GB 를 내려받는 상태
    변경이고, 노드는 테넌트 공유 자원이다.
    """
    ctx, principal = _platform_admin(request)
    request_id = request.path_params["request_id"]
    body = await _body(request)

    if body.get("reject"):
        req = ctx.registrar.reject(
            request_id, actor=principal.token_id, reason=str(body.get("reason") or "")
        )
    else:
        req = ctx.registrar.approve(request_id, actor=principal.token_id)
    return _ok(request, {"id": req.id, "status": req.status, "progress": req.progress})


async def platform_model_delete(request: Request) -> Response:
    """모델 삭제. **`force` 는 없다** — 다섯 차단 사유 중 하나라도 걸리면 거부한다."""
    ctx, principal = _platform_admin(request)
    node = request.path_params["node"]
    model = request.path_params["model"]
    blockers = ctx.registrar.deletion_blockers(node, model)
    if blockers:
        raise ApiError(
            "model_in_use", status=409,
            params={"model": model, "reason": ", ".join(blockers)},
        )
    await ctx.registrar.delete(node, model, actor=principal.token_id)
    return _ok(request, {"deleted": True, "node": node, "model": model})


async def platform_catalog(request: Request) -> Response:
    ctx, principal = _platform_admin(request)
    entries = ctx.registrar.catalog_search(request.query_params.get("q", ""))
    return _ok(request, {"catalog": [
        {
            "name": e.name, "provider": e.provider, "est_size_gb": e.est_size_gb,
            "purpose": e.purpose, "note": e.note,
        }
        for e in entries
    ]})


async def platform_overview(request: Request) -> Response:
    """전역 관제. **테넌트를 가로지르므로 그 사실이 감사에 남는다.**"""
    ctx, principal = _platform_admin(request)
    reason = request.query_params.get("reason", "platform console")
    scope = PlatformScope(principal.token_id, reason)
    since = float(request.query_params.get("since") or (ctx.now() - 30 * 86400))

    lanes = ctx.scheduler.snapshot() if ctx.scheduler else {}
    return _ok(request, {
        "version": ctx.version,
        "schema_version": ctx.store.schema_version,
        "airgap": ctx.airgap,
        "raw_prompt_storage": ctx.vault.enabled,
        "tenants": [
            {"id": row["id"], "name": row["name"], "status": row["status"]}
            for row in ctx.store.list_tenants(scope)
        ],
        "usage_by_tenant": [dict(row) for row in ctx.store.usage_across_tenants(scope, since=since)],
        "nodes": ctx.cluster.snapshot(),
        "lanes": lanes,
        # 1급 카드 둘 — 자동 복제를 안 하므로 사람이 판단할 재료를 준다.
        "single_homed_roles": ctx.cluster.single_homed_roles(),
        "waiting_by_reason": ctx.store.queued_wait_reasons(),
        "model_requests_pending": ctx.registrar.pending_count(),
        "thresholds": ctx.config.thresholds.__dict__,
    })


async def platform_guard_baseline(request: Request) -> Response:
    """베이스라인 규칙과 **켜진 로케일 팩**.

    안 켜진 필터는 없는 필터인데, 다국어에서는 켰다고 착각하기가 더 쉽다. 그래서
    어떤 팩이 어느 테넌트에서 켜져 있는지를 상시 노출한다.
    """
    ctx, principal = _platform_admin(request)
    scope = PlatformScope(principal.token_id, "guard baseline review")
    packs: dict[str, list[str]] = {}
    for row in ctx.store.list_tenants(scope):
        pack = guard_pack_for(row["locale"]) or "(없음)"
        packs.setdefault(pack, []).append(row["id"])

    all_packs = sorted({r.locale_pack for r in ctx.config.guard_rules})
    return _ok(request, {
        "baseline": [
            {
                "id": r.id, "kind": r.kind, "label": r.label, "locale_pack": r.locale_pack,
                "checksum": r.checksum,
                "action": {b: r.action_for_boundary(b) for b in (INTERNAL, EXTERNAL)},
            }
            for r in ctx.config.guard_rules
        ],
        "locale_packs": all_packs,
        "packs_in_use": packs,
        "packs_unused": [p for p in all_packs if p != "common" and p not in packs],
        "settings": ctx.config.guard_settings.__dict__,
    })


async def platform_evals(request: Request) -> Response:
    ctx, principal = _platform_admin(request)
    if request.method == "GET":
        return _ok(request, {
            "runs": [dict(row) for row in ctx.store.list_eval_runs(limit=50)],
        })
    results = ctx.evaluator.evaluate_rules(record=True)
    return _ok(request, {"results": [r.as_metrics() for r in results]})


async def metrics(request: Request) -> Response:
    """Prometheus/OpenMetrics. 13단계에서 채운다."""
    ctx, principal = _platform_admin(request)
    lines = [
        "# HELP llmcc_up 컨트롤 플레인 생존",
        "# TYPE llmcc_up gauge",
        "llmcc_up 1",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ── 라우트 ──────────────────────────────────────────────────────────────────


def _routes(ctx: AppContext) -> list[Any]:
    """라우트 정의.

    `name=` 이 곧 `meta.ROUTE_SUMMARIES` 의 열쇠다. 새 라우트를 추가하고 요약을
    안 달면 `test_meta.py` 가 실패한다 — **손으로 관리하는 표는 반드시 어긋나므로**
    어긋난 것을 사람이 아니라 테스트가 발견하게 만든다.
    """
    v = f"/{meta_mod.API_VERSION}"
    routes: list[Any] = [
        Route("/healthz", healthz, name="healthz"),
        # 계약 자기 서빙
        Route(f"{v}/meta", meta_endpoint, name="meta"),
        Route(f"{v}/integration", integration, name="integration"),
        Route(f"{v}/openapi.json", openapi_json, name="openapi_json"),
        Route(f"{v}/openapi.yaml", openapi_yaml, name="openapi_yaml"),
        Route(f"{v}/client", client_index, name="client_index"),
        Route(f"{v}/client/{{name}}", client_file, name="client_file"),
        # 소비자
        Route(f"{v}/generate", generate, methods=["POST"], name="generate"),
        Route(f"{v}/embed", embed, methods=["POST"], name="embed"),
        Route(f"{v}/jobs/{{job_id}}", job_get, name="job_get"),
        Route(f"{v}/jobs/{{job_id}}", job_cancel, methods=["DELETE"], name="job_cancel"),
        Route(f"{v}/roles", roles, name="roles"),
        Route(f"{v}/status", status, name="status"),
        # 테넌트 관리
        Route(f"{v}/admin/services", tenant_services, methods=["GET", "POST"], name="tenant_services"),
        Route(f"{v}/admin/tokens", tenant_tokens, methods=["GET", "POST"], name="tenant_tokens"),
        Route(f"{v}/admin/tokens/{{token_id}}/rotate", tenant_token_rotate,
              methods=["POST"], name="tenant_token_rotate"),
        Route(f"{v}/admin/tokens/{{token_id}}", tenant_token_revoke,
              methods=["DELETE"], name="tenant_token_revoke"),
        Route(f"{v}/admin/guard/rules", tenant_guard_rules,
              methods=["GET", "PUT"], name="tenant_guard_rules"),
        Route(f"{v}/admin/guard/rules/{{rule_id}}", tenant_guard_rule_delete,
              methods=["DELETE"], name="tenant_guard_rule_delete"),
        Route(f"{v}/admin/guard/events", tenant_guard_events, name="tenant_guard_events"),
        Route(f"{v}/admin/guard/events/{{event_id}}/review", tenant_guard_review,
              methods=["POST"], name="tenant_guard_review"),
        Route(f"{v}/admin/guard/rules/{{rule_id}}/promotion", tenant_guard_promote,
              name="tenant_guard_promote"),
        Route(f"{v}/admin/settings", tenant_settings, methods=["GET", "PUT"], name="tenant_settings"),
        Route(f"{v}/admin/overrides", tenant_overrides,
              methods=["GET", "PUT", "DELETE"], name="tenant_overrides"),
        Route(f"{v}/admin/jobs", tenant_jobs, name="tenant_jobs"),
        Route(f"{v}/admin/jobs/{{job_id}}/raw", tenant_job_raw, name="tenant_job_raw"),
        Route(f"{v}/admin/usage", tenant_usage, name="tenant_usage"),
        Route(f"{v}/admin/audit", tenant_audit, name="tenant_audit"),
        Route(f"{v}/admin/export", tenant_export, name="tenant_export"),
        Route(f"{v}/admin/end-users/{{end_user_hash}}", tenant_purge_end_user,
              methods=["DELETE"], name="tenant_purge_end_user"),
        # 플랫폼 관리
        Route(f"{v}/platform/tenants", platform_tenants,
              methods=["GET", "POST"], name="platform_tenants"),
        Route(f"{v}/platform/tenants/{{tenant_id}}", platform_tenant_purge,
              methods=["DELETE"], name="platform_tenant_purge"),
        Route(f"{v}/platform/nodes", platform_nodes,
              methods=["GET", "POST"], name="platform_nodes"),
        Route(f"{v}/platform/nodes/{{node}}/drain", platform_node_drain,
              methods=["POST"], name="platform_node_drain"),
        Route(f"{v}/platform/models", platform_models,
              methods=["GET", "POST"], name="platform_models"),
        Route(f"{v}/platform/models/{{request_id}}/approve", platform_model_approve,
              methods=["POST"], name="platform_model_approve"),
        Route(f"{v}/platform/nodes/{{node}}/models/{{model:path}}", platform_model_delete,
              methods=["DELETE"], name="platform_model_delete"),
        Route(f"{v}/platform/catalog", platform_catalog, name="platform_catalog"),
        Route(f"{v}/platform/overview", platform_overview, name="platform_overview"),
        Route(f"{v}/platform/guard/baseline", platform_guard_baseline,
              name="platform_guard_baseline"),
        Route(f"{v}/platform/evals", platform_evals,
              methods=["GET", "POST"], name="platform_evals"),
        Route("/metrics", metrics, name="metrics"),
    ]
    if ctx.static_dir.is_dir():
        # 관제 UI. 외부 CDN 을 쓰지 않으므로 전부 여기서 나간다.
        routes.append(Mount("/ui", StaticFiles(directory=ctx.static_dir, html=True), name="ui"))
    return routes
