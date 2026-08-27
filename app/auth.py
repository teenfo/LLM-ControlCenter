"""인증 · 권한 · 레이트리밋.

이전 시스템은 토큰을 `services.yaml` 에 선언하고 `.env` 에 값을 넣은 뒤 재기동했다.
설치형 제품에서는 성립하지 않는다 — 소비자를 추가할 때마다 설치처가 YAML 을 고치고
서비스를 재기동해야 한다면, 그것은 기능이 아니라 장애다. 그래서 토큰 수명주기
(발급·회전·만료·폐기)가 1급 기능이다.

권한은 2단이다:

  플랫폼 관리자 — 테넌트 생성·정지, 노드 등록, 베이스라인 가드 규칙, 전역 관제
  테넌트 관리자 — 자기 서비스·토큰·가드 규칙(조이기만)·예산·엔드유저

**테넌트 관리자는 플랫폼 베이스라인 가드 규칙을 완화할 수 없다.** 플랫폼이 정한 PII
차단을 테넌트가 끌 수 있으면 제품의 보증이 사라진다. 그 강제는 guard.py 가 하고,
여기서는 권한 판정만 한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .i18n import ApiError
from .store import SqliteStore, TenantScope

ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_SERVICE = "service"

ROLES = (ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_SERVICE)

TOKEN_PREFIX = "lcc"
_PREFIX_LEN = 8
_SECRET_BYTES = 32

#: 레이트리밋 윈도(초). 1초 버킷을 이만큼 합산한다.
RATE_WINDOW_SECONDS = 60


# ── 주체 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Principal:
    """인증된 호출자. 이 객체가 곧 테넌트 스코프의 근거다."""

    tenant_id: str
    service_id: str
    token_id: str
    role: str

    @property
    def is_platform_admin(self) -> bool:
        return self.role == ROLE_PLATFORM_ADMIN

    @property
    def is_tenant_admin(self) -> bool:
        # 플랫폼 관리자는 테넌트 관리자가 할 수 있는 일을 전부 할 수 있다.
        return self.role in (ROLE_TENANT_ADMIN, ROLE_PLATFORM_ADMIN)

    def scope(self) -> TenantScope:
        return TenantScope(self.tenant_id)


# ── 토큰 ────────────────────────────────────────────────────────────────────


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """새 토큰을 만든다. 반환은 (원값, 접두어, 해시).

    **원값은 여기서만 존재한다.** 저장은 해시만 하고, 발급 화면에서 1회 보여준 뒤
    복구할 방법이 없다. 잃어버리면 회전시키는 것이 유일한 경로다.
    """
    prefix = secrets.token_hex(_PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    raw = f"{TOKEN_PREFIX}_{prefix}_{secret}"
    return raw, prefix, _hash_token(raw)


def issue_token(
    store: SqliteStore,
    scope: TenantScope,
    service_id: str,
    *,
    role: str = ROLE_SERVICE,
    expires_at: float | None = None,
    note: str | None = None,
    actor: str = "",
) -> tuple[str, str]:
    """토큰을 발급한다. 반환은 (token_id, 원값) — 원값은 이때가 마지막이다."""
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할: {role}")

    raw, prefix, token_hash = generate_token()
    token_id = store.create_token(
        scope, service_id, token_hash, prefix,
        role=role, expires_at=expires_at, note=note,
    )
    store.audit(
        actor or "tenant_admin", "issue_token", tenant_id=scope.tenant_id,
        target=token_id, detail={"service_id": service_id, "role": role, "prefix": prefix},
    )
    return token_id, raw


def rotate_token(
    store: SqliteStore,
    scope: TenantScope,
    token_id: str,
    *,
    actor: str = "",
    grace_seconds: float = 0.0,
    now: Callable[[], float] = time.time,
) -> tuple[str, str]:
    """토큰을 회전한다. 새 토큰을 발급하고 옛 토큰에 만료를 건다.

    `grace_seconds` 를 주면 옛 토큰이 그만큼 더 살아 있다 — 소비자가 배포하는 동안
    끊기지 않게 하는 창이다. 0 이면 즉시 폐기다.
    """
    rows = [t for t in store.list_tokens(scope) if t["id"] == token_id]
    if not rows:
        raise ApiError("unauthorized", status=404)
    old = rows[0]

    new_id, raw = issue_token(
        store, scope, old["service_id"], role=old["role"],
        note=f"rotated from {token_id}", actor=actor,
    )

    if grace_seconds > 0:
        store.set_token_expiry(scope, token_id, now() + grace_seconds)
    else:
        store.revoke_token(scope, token_id)

    store.audit(
        actor or "tenant_admin", "rotate_token", tenant_id=scope.tenant_id,
        target=token_id, detail={"new_token_id": new_id, "grace_seconds": grace_seconds},
    )
    return new_id, raw


def authenticate(
    store: SqliteStore,
    raw_token: str | None,
    *,
    now: Callable[[], float] = time.time,
) -> Principal:
    """Bearer 토큰을 주체로 바꾼다.

    해시로 찾은 뒤 **상수 시간 비교로 재확인**한다. 인덱스 조회만으로도 동작하지만,
    타이밍으로 유효 토큰의 존재 여부를 알아내는 경로를 남기지 않기 위해서다.
    """
    if not raw_token:
        raise ApiError("unauthorized", status=401)

    token_hash = _hash_token(raw_token)
    row = store.find_token(token_hash)
    if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
        raise ApiError("unauthorized", status=401)

    if row["expires_at"] is not None and row["expires_at"] < now():
        raise ApiError("unauthorized", status=401)

    tenant = store.get_tenant(row["tenant_id"])
    if tenant is None or tenant["status"] != "active":
        # 정지·파기된 테넌트의 토큰은 존재하지 않는 것과 같다.
        raise ApiError("unauthorized", status=401)

    store.touch_token(row["id"])
    return Principal(
        tenant_id=row["tenant_id"],
        service_id=row["service_id"],
        token_id=row["id"],
        role=row["role"],
    )


def bearer_from_header(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


# ── 권한 ────────────────────────────────────────────────────────────────────


def require_platform_admin(principal: Principal) -> None:
    if not principal.is_platform_admin:
        raise ApiError("forbidden_platform_admin", status=403)


def require_tenant_admin(principal: Principal) -> None:
    if not principal.is_tenant_admin:
        raise ApiError("forbidden_admin", status=403)


def check_role_allowed(allow_roles: Sequence[str] | str, role_name: str) -> None:
    """서비스가 이 역할을 쓸 수 있는가.

    `GET /v1/roles` 가 보여주는 것이 곧 쓸 수 있는 전부여야 한다 —
    목록에 있는데 못 쓰거나, 목록에 없는데 쓰이는 경우가 생기면 계약이 거짓말이 된다.
    """
    allowed = json.loads(allow_roles) if isinstance(allow_roles, str) else list(allow_roles)
    if "*" in allowed or role_name in allowed:
        return
    raise ApiError("forbidden_role", status=403, params={"role": role_name})


# ── 3단 레이트리밋 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RateLimits:
    """테넌트 → 서비스 → 엔드유저. `None` 은 그 단계에 제한이 없다는 뜻."""

    tenant: int | None = None
    service: int | None = None
    end_user: int | None = None


class RateLimiter:
    """1초 버킷을 합산하는 슬라이딩 윈도.

    카운터를 스토어에 두는 이유는 워커 다중화 때문이다 — 프로세스 메모리에 두면
    API 워커를 N개 띄웠을 때 실효 한도가 N배가 된다. 한도가 조용히 곱해지는 것은
    설정한 사람의 의도를 배반한다.

    예산이 테넌트·서비스·엔드유저 3단인데 레이트리밋만 2단이면 비대칭이다 —
    한 테넌트가 서비스를 여러 개 만들어 입구를 독점할 수 있다.
    """

    def __init__(self, store: SqliteStore, *, now: Callable[[], float] = time.time):
        self._store = store
        self._now = now

    def check_and_consume(
        self,
        principal: Principal,
        limits: RateLimits,
        *,
        end_user_hash: str | None = None,
    ) -> None:
        """세 단계를 순서대로 검사하고 통과하면 전부 증가시킨다.

        **어느 단계에서 걸렸는지 알려준다.** 안 그러면 소비자가 자기 서비스 한도를
        늘려도 안 풀리는 이유를 알 수 없다(테넌트 총량에 걸린 경우).
        """
        now = self._now()
        bucket = int(now)
        since = bucket - RATE_WINDOW_SECONDS + 1

        checks: list[tuple[str, str, int]] = []
        if limits.tenant is not None:
            checks.append(("tenant", f"t:{principal.tenant_id}", limits.tenant))
        if limits.service is not None:
            checks.append(("service", f"s:{principal.tenant_id}:{principal.service_id}", limits.service))
        if limits.end_user is not None and end_user_hash:
            checks.append(
                ("end_user", f"u:{principal.tenant_id}:{end_user_hash}", limits.end_user)
            )

        # 넓은 단계부터 검사한다 — 테넌트 총량에 걸렸는데 서비스 한도를 탓하지 않게.
        for scope_name, key, limit in checks:
            if self._store.rate_count(key, since) >= limit:
                raise ApiError(
                    "rate_limited", status=429, retryable=True,
                    params={"scope": scope_name, "limit": limit},
                )

        for _, key, _ in checks:
            self._store.bump_rate_counter(key, bucket)

    def check_named(self, key: str, limit: int, *, scope_label: str) -> None:
        """이름 붙은 별도 한도. 상태 조회(폴링)처럼 제출과 다르게 재야 하는 경로용.

        폴링을 제출과 같은 한도에 넣으면 둘 중 하나를 잘못 잡게 된다 — 제출 기준으로
        맞추면 정상적인 대기 폴링이 429 를 맞고, 폴링 기준으로 맞추면 제출 한도가
        무의미해진다. 그래서 넉넉한 별도 창을 준다.
        """
        bucket = int(self._now())
        since = bucket - RATE_WINDOW_SECONDS + 1
        if self._store.rate_count(key, since) >= limit:
            raise ApiError(
                "rate_limited", status=429, retryable=True,
                params={"scope": scope_label, "limit": limit},
            )
        self._store.bump_rate_counter(key, bucket)

    def prune(self) -> int:
        """윈도를 벗어난 버킷을 정리한다. 스케줄러의 보존 루프가 주기적으로 부른다."""
        return self._store.prune_rate_counters(
            int(self._now()) - RATE_WINDOW_SECONDS * 2
        )


def limits_for(
    tenant_row, service_row, *, default_tenant: int | None = None
) -> RateLimits:
    """테넌트·서비스 행에서 3단 한도를 뽑는다."""
    return RateLimits(
        tenant=(tenant_row["rate_limit_per_min"] if tenant_row else None) or default_tenant,
        service=service_row["rate_limit_per_min"] if service_row else None,
        end_user=service_row["end_user_rate_limit"] if service_row else None,
    )
