"""계약 자기 서빙 — 라우트 재고 · `/v1/meta` · OpenAPI · 통합 가이드.

두 가지 원칙이 이 모듈의 전부다.

**① 손으로 관리하는 표는 반드시 어긋난다.** 엔드포인트 목록을 문서에 적어 두면 라우트를
추가할 때 문서를 고치는 것을 잊는다 — 원칙만 인용하고 장치를 안 만들면 같은 실수를
반복한다. 그래서 재고는 앱의 라우트 테이블을 **순회해서** 만들고, 요약이 빠진 라우트가
있으면 테스트가 실패한다(`missing_summaries()`).

**② 계약은 토큰마다 다르다.** `role` enum 에는 그 서비스가 실제로 쓸 수 있는 역할만
넣는다. 다중 테넌트에서는 여기에 경계가 하나 더 걸린다 — **다른 테넌트의 역할 이름이
OpenAPI 에 새면 그것도 정보 유출이다.** 그래서 정적 파일이 아니라 매번 생성한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .config import Config
from .i18n import Translator
from .pipeline import DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, is_public_role

API_VERSION = "v1"

#: 이 목록은 **분기용이 아니다.** 소비자는 HTTP 상태와 `retryable` 로 분기해야 하고,
#: 이 목록은 사람이 무엇을 만날 수 있는지 알기 위한 것이다. OpenAPI 에서도 enum 으로
#: 쓰지 않는다 — 엄격한 검증기가 새 코드가 붙은 진짜 응답을 거부하게 된다.
ERROR_CODES: tuple[tuple[str, int, bool], ...] = (
    ("unauthorized", 401, False),
    ("forbidden_role", 403, False),
    ("forbidden_admin", 403, False),
    ("forbidden_platform_admin", 403, False),
    ("tenant_inactive", 403, False),
    ("unknown_role", 404, False),
    ("job_not_found", 404, False),
    ("not_found", 404, False),
    ("method_not_allowed", 405, False),
    ("job_running", 409, False),
    ("payload_too_large", 413, False),
    ("guard_blocked", 422, False),
    ("capacity_impossible", 422, False),
    ("wrong_kind", 400, False),
    ("empty_input", 400, False),
    ("invalid_json", 400, False),
    ("missing_field", 400, False),
    ("invalid_field", 400, False),
    ("end_user_required", 400, False),
    ("rate_limited", 429, True),
    ("budget_exceeded", 429, False),
    ("no_placement", 503, True),
    ("backend_unavailable", 503, True),
    ("node_unreachable", 503, True),
    ("model_not_installed", 503, True),
    ("administrative_wait_timeout", 504, True),
    ("internal", 500, False),
)


@dataclass(frozen=True)
class RouteInfo:
    """재고 한 줄. `summary` 는 라우트 등록 시점에 함께 적는다."""

    path: str
    methods: tuple[str, ...]
    name: str
    summary: str
    audience: str  # consumer | tenant_admin | platform_admin | public
    #: 사람이 읽는 통합 가이드에 실을지. 관리 API 는 UI 가 쓰므로 기본은 False.
    in_guide: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "methods": list(self.methods),
            "summary": self.summary,
            "audience": self.audience,
        }


#: 라우트 요약 대장. `main.build_app()` 이 라우트를 만들 때 이름으로 참조한다.
#: **여기에 없는 라우트를 추가하면 `missing_summaries()` 가 잡아낸다.**
ROUTE_SUMMARIES: Mapping[str, tuple[str, str, bool]] = {
    # name: (summary, audience, in_guide)
    "healthz": ("컨테이너·로드밸런서용 생존 확인. 인증이 필요 없다.", "public", True),
    "meta": ("기계가 읽는 계약 — 역할·한도·오류 코드·엔드포인트. 토큰마다 다르다.", "consumer", True),
    "integration": ("사람이 읽는 통합 가이드(마크다운).", "consumer", True),
    "openapi_json": ("OpenAPI 3.1 (JSON). 이 토큰이 쓸 수 있는 역할만 담는다.", "consumer", True),
    "openapi_yaml": ("OpenAPI 3.1 (YAML).", "consumer", False),
    "client_index": ("번들된 클라이언트·목 서버 원본 목록.", "consumer", True),
    "client_file": ("단일 파일 클라이언트 또는 목 서버 원본을 그대로 내려준다.", "consumer", False),
    "generate": ("생성 요청. `wait` 로 동기·비동기를 한 엔드포인트로 흡수한다.", "consumer", True),
    "embed": ("임베딩. 동기지만 가드·배치·경계·비용은 생성과 같은 관문을 지난다.", "consumer", True),
    "job_get": ("작업 조회. 대기 중이면 적응형 `retry_after` 가 함께 온다.", "consumer", True),
    "job_cancel": ("대기 중인 작업 취소. 실행 중인 작업은 취소할 수 없다.", "consumer", True),
    "roles": ("이 토큰이 쓸 수 있는 역할과 각 역할의 한도.", "consumer", True),
    "status": ("클러스터 상태 요약 — 레인·큐·노드 헬스.", "consumer", True),
    # 테넌트 관리
    "tenant_services": ("자기 테넌트의 서비스 목록·생성.", "tenant_admin", False),
    "tenant_tokens": ("서비스 토큰 발급·목록. 발급 값은 이때 한 번만 보인다.", "tenant_admin", False),
    "tenant_token_rotate": ("토큰 회전. 유예 기간 동안 구 토큰도 함께 동작한다.", "tenant_admin", False),
    "tenant_token_revoke": ("토큰 폐기.", "tenant_admin", False),
    "tenant_guard_rules": ("테넌트 가드 규칙 조회·저장. 베이스라인을 완화할 수는 없다.", "tenant_admin", False),
    "tenant_guard_rule_delete": ("테넌트 가드 규칙 삭제.", "tenant_admin", False),
    "tenant_guard_events": ("가드 탐지 이력과 오탐 검토 큐.", "tenant_admin", False),
    "tenant_guard_review": ("오탐 검토 판정. 정답셋 승격의 재료가 된다.", "tenant_admin", False),
    "tenant_guard_promote": ("`audit` → `block` 승격 가능 여부 판정.", "tenant_admin", False),
    "tenant_settings": ("테넌트 설정 — 기본 로케일·원문 보관 기간·예산.", "tenant_admin", False),
    "tenant_overrides": ("역할 오버라이드 조회·설정·해제.", "tenant_admin", False),
    "tenant_jobs": ("자기 테넌트의 작업 목록. 마스킹본만 보인다.", "tenant_admin", False),
    "tenant_job_raw": ("원문 단건 복호화. **열람 자체가 감사에 남는다.**", "tenant_admin", False),
    "tenant_usage": ("사용량 집계 — 서비스·엔드유저·역할·노드 축.", "tenant_admin", False),
    "tenant_audit": ("자기 테넌트의 감사 로그.", "tenant_admin", False),
    "tenant_export": ("내보내기 — 작업·사용량·감사·설정(마스킹본 기준).", "tenant_admin", False),
    "tenant_purge_end_user": ("엔드유저 파기. 그 엔드유저의 데이터만 지운다.", "tenant_admin", False),
    # 플랫폼 관리
    "platform_tenants": ("테넌트 목록·생성.", "platform_admin", False),
    "platform_tenant_purge": ("테넌트 파기 + DEK 폐기(crypto-shredding). 되돌릴 수 없다.", "platform_admin", False),
    "platform_nodes": ("노드 목록·등록. 등록 즉시 프로브한다.", "platform_admin", False),
    "platform_node_drain": ("노드 드레이닝·복귀. 즉시 차단이 아니라 신규만 막는다.", "platform_admin", False),
    "platform_models": ("모델 설치 요청 목록.", "platform_admin", False),
    "platform_model_approve": ("모델 설치 승인·거부. 공유 노드 디스크를 쓰므로 플랫폼 권한이다.", "platform_admin", False),
    "platform_model_delete": ("모델 삭제. 차단 사유가 하나라도 있으면 거부된다(`force` 없음).", "platform_admin", False),
    "platform_catalog": ("번들 카탈로그 검색. 외부 레지스트리를 스크레이핑하지 않는다.", "platform_admin", False),
    "platform_overview": ("전역 관제 — 테넌트별 소비·노드 그리드·단일 호밍 경고.", "platform_admin", False),
    "platform_guard_baseline": ("베이스라인 가드 규칙과 로케일 팩 현황.", "platform_admin", False),
    "platform_evals": ("가드 회귀 평가 실행·이력.", "platform_admin", False),
    "metrics": ("Prometheus/OpenMetrics 노출.", "platform_admin", False),
    "ui": ("관제 UI 정적 자산.", "public", False),
}


def inventory(routes: Iterable[Any]) -> tuple[RouteInfo, ...]:
    """앱의 라우트 테이블을 순회해 재고를 만든다.

    **목록을 손으로 적지 않는 것이 요점이다.** 라우트를 추가했는데 요약을 안 달면
    `summary` 가 비고, `missing_summaries()` 가 그것을 테스트 실패로 바꾼다.
    """
    found: list[RouteInfo] = []
    for route in routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        name = getattr(route, "name", "") or ""
        methods = tuple(
            sorted(m for m in (getattr(route, "methods", None) or ()) if m != "HEAD")
        )
        summary, audience, in_guide = ROUTE_SUMMARIES.get(name, ("", "unknown", False))
        found.append(
            RouteInfo(
                path=path, methods=methods or ("GET",), name=name,
                summary=summary, audience=audience, in_guide=in_guide,
            )
        )
    return tuple(sorted(found, key=lambda r: (r.audience, r.path)))


def missing_summaries(routes: Iterable[Any]) -> tuple[str, ...]:
    """요약이 없는 라우트의 경로. **비어 있지 않으면 테스트가 실패해야 한다.**"""
    return tuple(
        f"{r.path} ({r.name or 'unnamed'})" for r in inventory(routes) if not r.summary
    )


def orphan_summaries(routes: Iterable[Any]) -> tuple[str, ...]:
    """라우트가 사라졌는데 남아 있는 요약. 표가 반대 방향으로도 어긋나지 않게 한다."""
    live = {getattr(route, "name", "") for route in routes}
    return tuple(sorted(name for name in ROUTE_SUMMARIES if name not in live))


# ── 토큰별 계약 ──────────────────────────────────────────────────────────────


def visible_roles(config: Config, allow_roles: Sequence[str] | str) -> tuple[str, ...]:
    """이 토큰이 실제로 쓸 수 있는 역할.

    `GET /v1/roles` · OpenAPI enum · 통합 가이드가 **모두 이 함수 하나를 쓴다** —
    세 곳이 각자 계산하면 언젠가 갈리고, 갈리는 순간 계약이 거짓말이 된다.
    """
    allowed = json.loads(allow_roles) if isinstance(allow_roles, str) else list(allow_roles)
    wildcard = "*" in allowed
    return tuple(
        name
        for name in sorted(config.roles)
        if is_public_role(name) and (wildcard or name in allowed)
    )


def role_contract(config: Config, name: str) -> dict[str, Any]:
    """소비자에게 보여줄 역할 정보.

    **모델 이름·배치 티어·system 프롬프트는 넣지 않는다.** 역할 이름이 계약이고 모델은
    정책이다 — 모델명을 노출하면 소비자가 그것에 의존하기 시작하고, 그 순간 정책을
    바꿀 수 없게 된다. 배치 티어는 인프라 구조라 노출할 이유가 없다.
    """
    role = config.roles[name]
    return {
        "name": name,
        "kind": role.kind,
        "lane": role.lane,
        "timeout_seconds": role.timeout,
        "max_prompt_chars": role.max_prompt_chars,
        "has_default_system": role.system is not None,
    }


def meta_document(
    config: Config,
    *,
    allow_roles: Sequence[str] | str,
    tenant_locale: str,
    locales: Sequence[str],
    base_url: str,
    routes: Iterable[Any],
    version: str,
    schema_version: int,
    limits: Mapping[str, Any],
    guard_locale_pack: str | None,
    airgap: bool,
) -> dict[str, Any]:
    """`GET /v1/meta` 본문. 정적 파일이 아니라 매번 생성한다."""
    names = visible_roles(config, allow_roles)
    return {
        "product": "llm-controlcenter",
        "version": version,
        "api_version": API_VERSION,
        "schema_version": schema_version,
        "base_url": base_url,
        "airgap": airgap,
        "roles": [role_contract(config, n) for n in names],
        "limits": dict(limits),
        "wait": {
            "default_seconds": DEFAULT_WAIT_SECONDS,
            "max_seconds": MAX_WAIT_SECONDS,
            "note": (
                "wait 를 쓰면 폴링이 사라진다. wait=0 + 짧은 폴링은 대기 잡 수에 비례해 "
                "컨트롤 플레인 부하를 늘리므로 권장하지 않는다."
            ),
        },
        "error_handling": {
            "branch_on": ["http_status", "retryable"],
            "never_branch_on": ["message"],
            "note": (
                "message 는 로케일에 따라 바뀐다. code 와 retryable 은 바뀌지 않는다. "
                "error_codes 는 참고 목록이며 enum 으로 검증하지 않는다."
            ),
            "error_codes": [
                {"code": code, "status": status, "retryable": retryable}
                for code, status, retryable in ERROR_CODES
            ],
        },
        "i18n": {
            "available_locales": list(locales),
            "tenant_default": tenant_locale,
            "guard_locale_pack": guard_locale_pack,
            "negotiation": ["Accept-Language", "tenant default", "platform default"],
        },
        "endpoints": [
            r.as_dict() for r in inventory(routes) if r.audience in ("consumer", "public")
        ],
    }


# ── OpenAPI ─────────────────────────────────────────────────────────────────

_ERROR_SCHEMA = {
    "type": "object",
    "required": ["code", "message", "retryable"],
    "properties": {
        "code": {"type": "string", "description": "기계용. 로케일과 무관하게 고정."},
        "message": {"type": "string", "description": "사람용. 로케일에 따라 바뀐다."},
        "retryable": {"type": "boolean"},
        "params": {"type": "object", "additionalProperties": True},
    },
}


def openapi_document(
    config: Config,
    *,
    allow_roles: Sequence[str] | str,
    base_url: str,
    version: str,
) -> dict[str, Any]:
    """토큰별 OpenAPI 3.1.

    `role` enum 에 **이 토큰이 쓸 수 있는 역할만** 넣는다. 다른 테넌트가 쓰는 역할
    이름이 여기 새면 그것도 정보 유출이다.
    """
    names = visible_roles(config, allow_roles)
    generate = [n for n in names if config.roles[n].kind == "generate"]
    embed = [n for n in names if config.roles[n].kind == "embed"]

    def role_enum(subset: Sequence[str]) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string"}
        if subset:
            schema["enum"] = list(subset)
        return schema

    errors = {
        str(status): {
            "description": "오류",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
        }
        for status in sorted({s for _, s, _ in ERROR_CODES})
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "LLM ControlCenter API",
            "version": version,
            "description": (
                "역할 이름이 계약이고 모델은 정책이다. 분기는 HTTP 상태와 `retryable` 로 한다."
            ),
        },
        "servers": [{"url": base_url}],
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": {
                "Error": _ERROR_SCHEMA,
                "Submission": {
                    "type": "object",
                    "required": ["job_id", "status"],
                    "properties": {
                        "job_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "description": "ok | pending | failed | blocked | cancelled | needs_review",
                        },
                        "response": {"type": ["string", "null"]},
                        "error": {"type": ["string", "null"]},
                        "error_code": {"type": ["string", "null"]},
                        "role": {"type": "string"},
                        "model": {"type": ["string", "null"]},
                        "node": {"type": ["string", "null"]},
                        "tier": {"type": ["string", "null"]},
                        "attempts": {"type": "integer"},
                        "queue_position": {"type": ["integer", "null"]},
                        "retry_after": {"type": ["number", "null"]},
                        "wait_reason": {"type": ["string", "null"]},
                        "guard_actions": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                },
            },
        },
        "paths": {
            f"/{API_VERSION}/generate": {
                "post": {
                    "operationId": "generate",
                    "summary": ROUTE_SUMMARIES["generate"][0],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["role", "prompt"],
                                    "properties": {
                                        "role": role_enum(generate),
                                        "prompt": {"type": "string"},
                                        "system": {
                                            "type": "string",
                                            "description": "프롬프트는 호출자 소유다. 주면 역할 기본값을 대체한다.",
                                        },
                                        "end_user": {
                                            "type": "string",
                                            "description": "불투명 식별자. 서버가 테넌트 솔트로 해싱한다 — 이메일을 넣지 말 것.",
                                        },
                                        "priority": {"type": "integer", "default": 0},
                                        "wait": {
                                            "type": "number",
                                            "default": DEFAULT_WAIT_SECONDS,
                                            "maximum": MAX_WAIT_SECONDS,
                                        },
                                        "metadata": {"type": "object", "additionalProperties": True},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "완료 또는 대기 중",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Submission"}
                                }
                            },
                        },
                        **errors,
                    },
                }
            },
            f"/{API_VERSION}/embed": {
                "post": {
                    "operationId": "embed",
                    "summary": ROUTE_SUMMARIES["embed"][0],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["role", "input"],
                                    "properties": {
                                        "role": role_enum(embed),
                                        "input": {
                                            "oneOf": [
                                                {"type": "string"},
                                                {"type": "array", "items": {"type": "string"}},
                                            ]
                                        },
                                        "end_user": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "벡터"}, **errors},
                }
            },
            f"/{API_VERSION}/jobs/{{job_id}}": {
                "get": {
                    "operationId": "getJob",
                    "summary": ROUTE_SUMMARIES["job_get"][0],
                    "parameters": [
                        {"name": "job_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "wait", "in": "query", "required": False,
                         "schema": {"type": "number"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "작업",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Submission"}
                                }
                            },
                        },
                        **errors,
                    },
                },
                "delete": {
                    "operationId": "cancelJob",
                    "summary": ROUTE_SUMMARIES["job_cancel"][0],
                    "parameters": [
                        {"name": "job_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "취소됨"}, **errors},
                },
            },
            f"/{API_VERSION}/roles": {
                "get": {
                    "operationId": "listRoles",
                    "summary": ROUTE_SUMMARIES["roles"][0],
                    "responses": {"200": {"description": "역할 목록"}, **errors},
                }
            },
            f"/{API_VERSION}/status": {
                "get": {
                    "operationId": "getStatus",
                    "summary": ROUTE_SUMMARIES["status"][0],
                    "responses": {"200": {"description": "클러스터 상태"}, **errors},
                }
            },
            "/healthz": {
                "get": {
                    "operationId": "healthz",
                    "summary": ROUTE_SUMMARIES["healthz"][0],
                    "security": [],
                    "responses": {"200": {"description": "살아 있음"}},
                }
            },
        },
    }


def integration_guide(
    config: Config,
    *,
    allow_roles: Sequence[str] | str,
    base_url: str,
    routes: Iterable[Any],
    limits: Mapping[str, Any],
    translator: Translator,
    locale: str,
) -> str:
    """사람이 읽는 통합 가이드.

    설치처 개발자가 붙이기 어려우면 우회로를 만든다. **가장 비싼 실패는 소비자가
    이 게이트웨이를 건너뛰고 노드를 직접 부르는 것이다** — 그 순간 가드도 비용도
    감사도 전부 사라진다. 그래서 진입 장벽을 서비스가 치운다.
    """
    names = visible_roles(config, allow_roles)
    lines: list[str] = [
        "# LLM ControlCenter 통합 가이드",
        "",
        f"기준 주소: `{base_url}`",
        "",
        "## 1. 인증",
        "",
        "```",
        "Authorization: Bearer <토큰>",
        "```",
        "",
        "토큰은 발급 시 **한 번만** 표시된다. 다시 볼 수 없으므로 그때 보관해야 하고,",
        "잃어버리면 회전(rotate)해서 새로 받는다.",
        "",
        "## 2. 역할",
        "",
        "**역할 이름이 계약이고 모델은 정책이다.** 모델명을 하드코딩하지 않는다 —",
        "어느 모델로 돌지·어느 기계에서 돌지·돈이 드는지는 관리자가 역할 뒤에서 바꾼다.",
        "",
        "| 역할 | 종류 | 타임아웃 | 최대 입력 |",
        "|---|---|---:|---:|",
    ]
    for name in names:
        role = config.roles[name]
        lines.append(
            f"| `{name}` | {role.kind} | {role.timeout}s | {role.max_prompt_chars:,}자 |"
        )
    if not names:
        lines.append("| _(이 토큰에 허용된 역할이 없다)_ | | | |")

    generate = next((n for n in names if config.roles[n].kind == "generate"), "summarize")
    lines += [
        "",
        "## 3. 첫 호출",
        "",
        "```bash",
        f"curl -X POST {base_url}/{API_VERSION}/generate \\",
        '  -H "Authorization: Bearer $TOKEN" \\',
        '  -H "Content-Type: application/json" \\',
        f"""  -d '{{"role": "{generate}", "prompt": "요약할 내용", "end_user": "u_8f3a91", "wait": 30}}'""",
        "```",
        "",
        "## 4. `wait` — 동기와 비동기를 한 엔드포인트로",
        "",
        f"`wait` 초까지 기다렸다가 못 끝나면 `status: \"pending\"` 으로 돌려준다"
        f"(기본 {DEFAULT_WAIT_SECONDS:g}초, 최대 {MAX_WAIT_SECONDS:g}초).",
        "**응답 모양은 완료든 대기든 같으므로 호출자 코드에 분기가 필요 없다.**",
        "",
        "대기 응답에는 `retry_after` 가 실린다. 큐 위치에 따라 서버가 계산한 값이므로",
        "**그 값을 지켜야 한다** — 고정 간격 폴링은 큐가 길어질수록 컨트롤 플레인을 때린다.",
        "완료 콜백(웹훅)은 제공하지 않는다.",
        "",
        "## 5. 오류 처리",
        "",
        "**분기는 HTTP 상태와 `retryable` 로 한다.** `message` 로 분기하지 않는다 —",
        "메시지는 로케일에 따라 바뀌고, 한국어로 분기한 코드는 영어 환경에서 조용히 실패한다.",
        "",
        "```json",
        json.dumps(
            {
                "code": "rate_limited",
                "message": translator.t("error.rate_limited", locale, scope="tenant", limit=60),
                "retryable": True,
                "params": {"scope": "tenant", "limit": 60},
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "`429` 의 `params.scope` 는 **어느 단계**(tenant · service · end_user)에서 걸렸는지 알려준다.",
        "자기 서비스 한도를 올려도 안 풀리는 이유가 테넌트 총량인 경우가 있기 때문이다.",
        "",
        "## 6. 개인정보 필터",
        "",
        "보내는 프롬프트는 저장·전송 전에 검사된다. 규칙 등급에 따라 마스킹되거나 차단되며,",
        "차단이면 `422 guard_blocked` 와 **규칙 ID만** 돌아온다(원문은 응답에 실리지 않는다).",
        "탐지가 있었지만 통과한 경우 응답의 `guard_actions` 에 규칙별 등급이 담긴다.",
        "",
        "`end_user` 는 **불투명 식별자**여야 한다. 서버가 테넌트 솔트로 해싱하므로 이메일을",
        "넣어도 DB 에 이메일이 남지는 않지만, 그건 사고를 줄이는 장치이지 계약이 아니다.",
        "",
        "## 7. 현재 한도",
        "",
    ]
    for key, value in sorted(limits.items()):
        lines.append(f"- `{key}`: {value}")

    lines += [
        "",
        "## 8. 계약 자기 서빙",
        "",
        "| 엔드포인트 | 내용 |",
        "|---|---|",
    ]
    for route in inventory(routes):
        if route.in_guide:
            lines.append(f"| `{' '.join(route.methods)} {route.path}` | {route.summary} |")

    lines += [
        "",
        "`/v1/meta` 와 `/v1/openapi.json` 은 **이 토큰 기준으로 매번 생성된다** —",
        "역할 목록은 런타임에 바뀌고, 허용 역할은 토큰마다 다르다.",
        "",
        "## 9. 노드도 토큰도 없이 개발하기",
        "",
        "`/v1/client/` 에 단일 파일 클라이언트와 목 서버가 있다. 목 서버는 역할 목록을",
        "실제 설정에서 읽으므로 역할 이름이 어긋나지 않는다.",
        "",
    ]
    return "\n".join(lines)
