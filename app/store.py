"""영속화 — `Store` 프로토콜과 SQLite 구현.

이 모듈의 존재 이유 하나: **테넌트 경계 누수를 구조적으로 막는 것.**

한 번의 스코프 누락이 다른 조직의 프롬프트를 노출시킨다. 핸들러마다
`WHERE tenant_id = ?` 를 손으로 뿌리면 반드시 하나를 빠뜨리므로, 여기서는

  * 테넌트 데이터를 만지는 모든 메서드가 `TenantScope` 를 **첫 인자로 강제**하고
  * 테넌트 조건을 각 쿼리가 아니라 `_scoped_where()` 한 곳이 붙이며
  * 전 테넌트 조회는 `PlatformScope` 를 요구하는 `*_across_tenants` 로만 열고 감사에 남긴다.

스코프 없는 범용 `execute()` 는 공개 API 에 없다. 있으면 언젠가 누군가 그것을 쓴다.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

SCHEMA_VERSION = 1

#: 잡 상태.
#:   needs_review 는 크래시 복구 전용이다 — 과금 노드에서 돌던 잡을 자동 재큐하면
#:   같은 작업이 두 번 돌고 두 번 청구된다. 노드에 취소 의미론이 없으므로
#:   되돌리지 못하고 사람에게 드러내기만 한다.
JOB_STATUSES = (
    "queued",
    "running",
    "ok",
    "failed",
    "cancelled",
    "blocked",        # 가드가 차단
    "needs_review",   # 크래시 복구 — 이중 실행 가능성
)

TERMINAL_STATUSES = frozenset({"ok", "failed", "cancelled", "blocked"})


def _json(obj: Any) -> str:
    """JSON 직렬화.

    `ensure_ascii=False` 가 기본이 아닌 것이 함정이다 — 한글이 `\\uc6d4` 로 저장되면
    감사 로그를 사람이 못 읽고, 저장 용량도 문자당 3바이트에서 6바이트로 늘어난다.
    """
    return json.dumps(obj, ensure_ascii=False)


class StoreError(RuntimeError):
    pass


class ScopeViolation(StoreError):
    """테넌트 스코프 없이 테넌트 데이터를 만지려 했다. 버그이지 사용자 오류가 아니다."""


@dataclass(frozen=True)
class TenantScope:
    """한 테넌트의 데이터에만 닿을 수 있는 열쇠."""

    tenant_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ScopeViolation("빈 tenant_id 로는 아무것도 조회할 수 없다")


@dataclass(frozen=True)
class PlatformScope:
    """전 테넌트를 가로지르는 조회용. 사용처가 감사에 남는다.

    `reason` 을 필수로 둔 것은 의도적이다 — 왜 경계를 넘는지 적지 않고는 넘을 수 없다.
    """

    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not self.actor or not self.reason:
            raise ScopeViolation("전 테넌트 조회는 actor 와 reason 을 모두 요구한다")


# ── 스키마 ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    locale               TEXT NOT NULL DEFAULT 'ko-KR',
    status               TEXT NOT NULL DEFAULT 'active',
    dek_wrapped          BLOB,
    end_user_salt        BLOB NOT NULL,
    budget_usd_per_month REAL,
    rate_limit_per_min   INTEGER,
    created_at           REAL NOT NULL,
    purged_at            REAL
);

CREATE TABLE IF NOT EXISTS services (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    name                 TEXT NOT NULL,
    allow_roles_json     TEXT NOT NULL DEFAULT '["*"]',
    rate_limit_per_min   INTEGER,
    budget_usd_per_month REAL,
    require_end_user     INTEGER NOT NULL DEFAULT 0,
    end_user_rate_limit  INTEGER,
    status               TEXT NOT NULL DEFAULT 'active',
    created_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_services_tenant ON services(tenant_id);

CREATE TABLE IF NOT EXISTS tokens (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    service_id   TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'service',
    created_at   REAL NOT NULL,
    expires_at   REAL,
    revoked_at   REAL,
    last_used_at REAL,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant ON tokens(tenant_id);

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    service_id        TEXT NOT NULL,
    end_user_hash     TEXT,
    role              TEXT NOT NULL,
    lane              TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'generate',
    status            TEXT NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 0,

    -- 프롬프트: 마스킹본은 평문, 원문은 암호문. KEK 가 없으면 암호문 자체를 안 만든다.
    prompt_masked     TEXT,
    prompt_cipher     BLOB,
    prompt_nonce      BLOB,
    system_masked     TEXT,
    -- 해시: prompt_hash 는 마스킹 후 + 테넌트 솔트다.
    -- 원문 그대로 해싱하면 탐색 공간이 좁은 값(주민번호 등)을 전수조사로 복원할 수 있다.
    prompt_hash       TEXT,
    system_hash       TEXT,

    response          TEXT,
    error             TEXT,
    error_code        TEXT,

    -- 생성 시점 스냅샷 (재현성)
    placement_json    TEXT NOT NULL DEFAULT '[]',
    tier_models_json  TEXT NOT NULL DEFAULT '{}',
    options_json      TEXT NOT NULL DEFAULT '{}',
    timeout_s         INTEGER NOT NULL DEFAULT 120,
    max_prompt_chars  INTEGER,

    -- 디스패치 시점 결정 (노드 헬스가 런타임 상태라 불가피)
    node              TEXT,
    model             TEXT,
    tier              TEXT,
    last_failed_node  TEXT,

    attempts          INTEGER NOT NULL DEFAULT 0,
    wait_reason       TEXT,
    wait_since        REAL,

    cost_reserved_usd REAL NOT NULL DEFAULT 0.0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,

    metrics_json      TEXT,
    metadata_json     TEXT NOT NULL DEFAULT '{}',

    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status ON jobs(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, lane, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_retention ON jobs(status, finished_at);
CREATE INDEX IF NOT EXISTS idx_jobs_enduser ON jobs(tenant_id, end_user_hash);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    tenant_id     TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    end_user_hash TEXT,
    job_id        TEXT,
    role          TEXT NOT NULL,
    model         TEXT,
    node          TEXT,
    provider      TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    cost_usd      REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON usage(tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_retention ON usage(ts);

-- 가드 이벤트: 규칙 ID·매칭 횟수·오프셋만 남긴다.
-- 매칭된 값은 절대 남기지 않는다 — 감사 로그가 새 유출 경로가 되면 안 된다.
CREATE TABLE IF NOT EXISTS filter_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    tenant_id     TEXT NOT NULL,
    service_id    TEXT,
    job_id        TEXT,
    rule_id       TEXT NOT NULL,
    stage         TEXT NOT NULL,
    action        TEXT NOT NULL,
    match_count   INTEGER NOT NULL DEFAULT 0,
    offsets_json  TEXT,
    boundary      TEXT,
    reviewed      INTEGER NOT NULL DEFAULT 0,
    verdict       TEXT
);
CREATE INDEX IF NOT EXISTS idx_filter_tenant_ts ON filter_events(tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_filter_review ON filter_events(tenant_id, action, reviewed);

-- 노드는 공유 인프라라 테넌트 스코프가 아니다.
CREATE TABLE IF NOT EXISTS node_health (
    node                  TEXT PRIMARY KEY,
    status                TEXT NOT NULL DEFAULT 'unknown',
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    last_probe_at         REAL,
    models_json           TEXT NOT NULL DEFAULT '[]',
    loaded_model          TEXT,
    error                 TEXT
);

CREATE TABLE IF NOT EXISTS model_requests (
    id             TEXT PRIMARY KEY,
    node           TEXT NOT NULL,
    model          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    requested_by   TEXT,
    roles_json     TEXT NOT NULL DEFAULT '[]',
    est_size_gb    REAL NOT NULL DEFAULT 0.0,
    progress       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     REAL NOT NULL,
    decided_at     REAL,
    UNIQUE(node, model)
);

CREATE TABLE IF NOT EXISTS role_overrides (
    tenant_id   TEXT NOT NULL,
    role        TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    note        TEXT,
    updated_by  TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (tenant_id, role)
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    tenant_id   TEXT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    outcome     TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON admin_audit(tenant_id, ts);

-- 레이트리밋 카운터. 1초 버킷을 합산해 슬라이딩 윈도를 만든다.
--
-- 프로세스 메모리에 두지 않는 이유: API 워커를 N개 띄우면 각자 자기 카운터를 갖게 되어
-- 실효 한도가 N배가 된다. 한도가 조용히 곱해지는 것은 제품에서 버그다.
CREATE TABLE IF NOT EXISTS rate_counters (
    key    TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, bucket)
);
CREATE INDEX IF NOT EXISTS idx_rate_bucket ON rate_counters(bucket);
"""

#: ADD COLUMN 전용 마이그레이션. 추가·NULL 기본값만 허용하고 재작성·삭제는 금지한다.
#: SQLite 의 ADD COLUMN 은 메타데이터 연산이라 WAL 라이브 DB 에서 안전하다.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (테이블, 컬럼, 타입+기본값)  — 예: ("jobs", "foo", "TEXT")
)


# ── 값 객체 ─────────────────────────────────────────────────────────────────


@dataclass
class JobRow:
    """잡 한 건. DB 행을 그대로 담되 JSON 컬럼은 파싱해서 준다."""

    id: str
    tenant_id: str
    service_id: str
    role: str
    lane: str
    status: str
    kind: str = "generate"
    end_user_hash: str | None = None
    priority: int = 0
    prompt_masked: str | None = None
    prompt_cipher: bytes | None = None
    prompt_nonce: bytes | None = None
    system_masked: str | None = None
    prompt_hash: str | None = None
    system_hash: str | None = None
    response: str | None = None
    error: str | None = None
    error_code: str | None = None
    placement: tuple[str, ...] = ()
    tier_models: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: int = 120
    max_prompt_chars: int | None = None
    node: str | None = None
    model: str | None = None
    tier: str | None = None
    last_failed_node: str | None = None
    attempts: int = 0
    wait_reason: str | None = None
    wait_since: float | None = None
    cost_reserved_usd: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    metrics: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


# ── 프로토콜 ────────────────────────────────────────────────────────────────


class Store(Protocol):
    """영속화 계약.

    SQLite 는 이 프로토콜의 한 구현일 뿐이다. Postgres 구현을 추가하고 잡 claim 을
    원자적 UPDATE 로 바꾸면 컨트롤 플레인 다중 인스턴스로 가는 길이 열린다.
    """

    def create_job(self, scope: TenantScope, **fields: Any) -> str: ...
    def get_job(self, scope: TenantScope, job_id: str) -> JobRow | None: ...
    def list_jobs(self, scope: TenantScope, **filters: Any) -> list[JobRow]: ...
    def record_usage(self, scope: TenantScope, **fields: Any) -> None: ...
    def audit(self, actor: str, action: str, **fields: Any) -> None: ...
    def close(self) -> None: ...


# ── SQLite 구현 ─────────────────────────────────────────────────────────────


class SqliteStore:
    """SQLite 구현. WAL 모드로 다중 리더 + 단일 라이터를 지원한다.

    API 워커를 여러 개 띄우고 스케줄러를 싱글턴 별도 프로세스로 분리해도
    이 저장소를 공유할 수 있다.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._now = now
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    # -- 스키마 --------------------------------------------------------------

    def _migrate(self) -> None:
        """ADD COLUMN 전용 마이그레이션. 추가·NULL 기본값만, 재작성·삭제 금지."""
        for table, column, ddl in _MIGRATIONS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    # -- 스코프 초크포인트 ----------------------------------------------------

    @staticmethod
    def _scoped_where(scope: TenantScope, extra: str = "") -> tuple[str, list[Any]]:
        """테넌트 조건을 붙이는 **유일한** 지점.

        각 쿼리가 스스로 tenant_id 를 적지 않는다 — 적게 하면 언젠가 하나를 빠뜨린다.
        """
        if not isinstance(scope, TenantScope):
            raise ScopeViolation(f"TenantScope 가 필요하다 (받은 값: {type(scope).__name__})")
        clause = "tenant_id = ?"
        params: list[Any] = [scope.tenant_id]
        if extra:
            clause += f" AND {extra}"
        return clause, params

    # -- 테넌트·서비스·토큰 ---------------------------------------------------

    def create_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        locale: str = "ko-KR",
        end_user_salt: bytes,
        dek_wrapped: bytes | None = None,
        budget_usd_per_month: float | None = None,
        rate_limit_per_min: int | None = None,
    ) -> str:
        self._conn.execute(
            "INSERT INTO tenants(id, name, locale, end_user_salt, dek_wrapped, "
            "budget_usd_per_month, rate_limit_per_min, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                tenant_id, name, locale, end_user_salt, dek_wrapped,
                budget_usd_per_month, rate_limit_per_min, self._now(),
            ),
        )
        self._conn.commit()
        return tenant_id

    def get_tenant(self, tenant_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tenants WHERE id = ? AND purged_at IS NULL", (tenant_id,)
        ).fetchone()

    def create_service(
        self,
        scope: TenantScope,
        service_id: str,
        name: str,
        *,
        allow_roles: Sequence[str] = ("*",),
        rate_limit_per_min: int | None = None,
        budget_usd_per_month: float | None = None,
        require_end_user: bool = False,
        end_user_rate_limit: int | None = None,
    ) -> str:
        self._scoped_where(scope)  # 스코프 검증
        self._conn.execute(
            "INSERT INTO services(id, tenant_id, name, allow_roles_json, rate_limit_per_min, "
            "budget_usd_per_month, require_end_user, end_user_rate_limit, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                service_id, scope.tenant_id, name, _json(list(allow_roles)),
                rate_limit_per_min, budget_usd_per_month, int(require_end_user),
                end_user_rate_limit, self._now(),
            ),
        )
        self._conn.commit()
        return service_id

    def get_service(self, scope: TenantScope, service_id: str) -> sqlite3.Row | None:
        where, params = self._scoped_where(scope, "id = ?")
        params.append(service_id)
        return self._conn.execute(f"SELECT * FROM services WHERE {where}", params).fetchone()

    def list_services(self, scope: TenantScope) -> list[sqlite3.Row]:
        where, params = self._scoped_where(scope)
        return list(
            self._conn.execute(f"SELECT * FROM services WHERE {where} ORDER BY name", params)
        )

    def create_token(
        self,
        scope: TenantScope,
        service_id: str,
        token_hash: str,
        prefix: str,
        *,
        role: str = "service",
        expires_at: float | None = None,
        note: str | None = None,
    ) -> str:
        self._scoped_where(scope)
        token_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO tokens(id, tenant_id, service_id, token_hash, prefix, role, "
            "created_at, expires_at, note) VALUES(?,?,?,?,?,?,?,?,?)",
            (token_id, scope.tenant_id, service_id, token_hash, prefix, role,
             self._now(), expires_at, note),
        )
        self._conn.commit()
        return token_id

    def find_token(self, token_hash: str) -> sqlite3.Row | None:
        """해시로 토큰을 찾는다. 인증 경로라 테넌트 스코프 **이전**이다 —
        이 조회의 결과가 곧 스코프를 정한다."""
        return self._conn.execute(
            "SELECT * FROM tokens WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)
        ).fetchone()

    def touch_token(self, token_id: str) -> None:
        self._conn.execute(
            "UPDATE tokens SET last_used_at = ? WHERE id = ?", (self._now(), token_id)
        )

    def set_token_expiry(
        self, scope: TenantScope, token_id: str, expires_at: float | None
    ) -> bool:
        """토큰 만료 시각을 세운다. 회전 시 유예 창을 주는 데 쓴다."""
        where, params = self._scoped_where(scope, "id = ?")
        params.append(token_id)
        cur = self._conn.execute(
            f"UPDATE tokens SET expires_at = ? WHERE {where}", [expires_at, *params]
        )
        self._conn.commit()
        return cur.rowcount > 0

    def revoke_token(self, scope: TenantScope, token_id: str) -> bool:
        where, params = self._scoped_where(scope, "id = ? AND revoked_at IS NULL")
        params.append(token_id)
        cur = self._conn.execute(
            f"UPDATE tokens SET revoked_at = ? WHERE {where}", [self._now(), *params]
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_tokens(self, scope: TenantScope) -> list[sqlite3.Row]:
        """토큰 목록. **해시만 저장하므로 원값은 어디에도 없다** — 발급 시 1회 표시가 전부다."""
        where, params = self._scoped_where(scope)
        return list(
            self._conn.execute(
                f"SELECT id, service_id, prefix, role, created_at, expires_at, revoked_at, "
                f"last_used_at, note FROM tokens WHERE {where} ORDER BY created_at DESC",
                params,
            )
        )

    # -- 잡 ------------------------------------------------------------------

    def create_job(self, scope: TenantScope, **fields: Any) -> str:
        self._scoped_where(scope)
        job_id = fields.pop("id", None) or uuid.uuid4().hex[:16]

        row = {
            "id": job_id,
            "tenant_id": scope.tenant_id,
            "service_id": fields.pop("service_id"),
            "end_user_hash": fields.pop("end_user_hash", None),
            "role": fields.pop("role"),
            "lane": fields.pop("lane"),
            "kind": fields.pop("kind", "generate"),
            "status": fields.pop("status", "queued"),
            "priority": int(fields.pop("priority", 0)),
            "prompt_masked": fields.pop("prompt_masked", None),
            "prompt_cipher": fields.pop("prompt_cipher", None),
            "prompt_nonce": fields.pop("prompt_nonce", None),
            "system_masked": fields.pop("system_masked", None),
            "prompt_hash": fields.pop("prompt_hash", None),
            "system_hash": fields.pop("system_hash", None),
            "placement_json": _json(list(fields.pop("placement", ()))),
            "tier_models_json": _json(dict(fields.pop("tier_models", {}))),
            "options_json": _json(dict(fields.pop("options", {}))),
            "timeout_s": int(fields.pop("timeout_s", 120)),
            "max_prompt_chars": fields.pop("max_prompt_chars", None),
            "metadata_json": _json(dict(fields.pop("metadata", {}))),
            "error": fields.pop("error", None),
            "error_code": fields.pop("error_code", None),
            "created_at": fields.pop("created_at", None) or self._now(),
        }
        if fields:
            raise StoreError(f"알 수 없는 잡 필드: {sorted(fields)}")

        columns = ", ".join(row)
        placeholders = ", ".join("?" * len(row))
        self._conn.execute(
            f"INSERT INTO jobs({columns}) VALUES({placeholders})", list(row.values())
        )
        self._conn.commit()
        return job_id

    def get_job(self, scope: TenantScope, job_id: str) -> JobRow | None:
        where, params = self._scoped_where(scope, "id = ?")
        params.append(job_id)
        row = self._conn.execute(f"SELECT * FROM jobs WHERE {where}", params).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self,
        scope: TenantScope,
        *,
        status: str | None = None,
        end_user_hash: str | None = None,
        limit: int = 50,
    ) -> list[JobRow]:
        conditions = []
        extra_params: list[Any] = []
        if status:
            conditions.append("status = ?")
            extra_params.append(status)
        if end_user_hash:
            conditions.append("end_user_hash = ?")
            extra_params.append(end_user_hash)

        where, params = self._scoped_where(scope, " AND ".join(conditions))
        params.extend(extra_params)
        rows = self._conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC LIMIT ?",
            [*params, int(limit)],
        )
        return [_row_to_job(r) for r in rows]

    def update_job(self, scope: TenantScope, job_id: str, **fields: Any) -> bool:
        """잡을 갱신한다. `tenant_id` 는 절대 바꿀 수 없다 — 그것이 격리 그 자체다."""
        if "tenant_id" in fields:
            raise ScopeViolation("잡의 tenant_id 는 변경할 수 없다")

        mapped = _map_job_fields(fields)
        if not mapped:
            return False

        assignments = ", ".join(f"{k} = ?" for k in mapped)
        where, params = self._scoped_where(scope, "id = ?")
        params.append(job_id)
        cur = self._conn.execute(
            f"UPDATE jobs SET {assignments} WHERE {where}", [*mapped.values(), *params]
        )
        self._conn.commit()
        return cur.rowcount > 0

    def claim_queued(
        self, lane: str, *, limit: int
    ) -> list[JobRow]:
        """스케줄러용 — 레인의 대기 잡을 우선순위 순으로 가져온다.

        스케줄러는 전 테넌트를 가로질러 봐야 하므로 테넌트 스코프가 없다.
        대신 **프롬프트 본문을 읽지 않는다** — 배치 결정에 필요한 것은 정책과 메타데이터뿐이다.
        """
        rows = self._conn.execute(
            "SELECT id, tenant_id, service_id, end_user_hash, role, lane, kind, status, "
            "priority, placement_json, tier_models_json, options_json, timeout_s, "
            "node, model, tier, last_failed_node, attempts, wait_reason, wait_since, "
            "cost_reserved_usd, created_at "
            "FROM jobs WHERE status = 'queued' AND lane = ? "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (lane, int(limit)),
        )
        return [_row_to_job(r) for r in rows]

    def count_queued(self, lane: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND lane = ?", (lane,)
        ).fetchone()
        return int(row["n"])

    # -- 크래시 복구 ----------------------------------------------------------

    def recover_running_jobs(self, metered_nodes: Iterable[str]) -> dict[str, int]:
        """기동 시 `running` 으로 남은 잡을 정리한다.

        단일 백엔드 시절에는 전부 `queued` 로 되돌리면 됐다. 클러스터에서는 다르다 —
        **컨트롤 플레인이 재시작하는 동안 노드는 여전히 추론을 돌리고 있다.**
        재큐되어 다른 노드에 배치되면 같은 작업이 두 번 돌고, 과금 노드면 두 번 청구된다.
        노드에도 클라우드에도 이걸 되돌릴 취소 의미론이 없다.

        그래서 과금 노드에서 돌던 잡은 자동 재큐하지 않고 `needs_review` 로 둔다.
        막지는 못하고 **드러내기만** 한다.
        """
        metered = set(metered_nodes)
        requeued = reviewed = 0

        for row in list(self._conn.execute("SELECT id, node FROM jobs WHERE status='running'")):
            if row["node"] in metered:
                self._conn.execute(
                    "UPDATE jobs SET status='needs_review', error_code='possible_double_execution', "
                    "wait_reason='crash_recovery_metered', finished_at=? WHERE id=?",
                    (self._now(), row["id"]),
                )
                reviewed += 1
            else:
                self._conn.execute(
                    "UPDATE jobs SET status='queued', attempts=attempts+1, "
                    "last_failed_node=node, node=NULL, started_at=NULL WHERE id=?",
                    (row["id"],),
                )
                requeued += 1

        self._conn.commit()
        return {"requeued": requeued, "needs_review": reviewed}

    # -- 사용량·가드 이벤트 ---------------------------------------------------

    def record_usage(self, scope: TenantScope, **fields: Any) -> None:
        self._scoped_where(scope)
        self._conn.execute(
            "INSERT INTO usage(ts, tenant_id, service_id, end_user_hash, job_id, role, model, "
            "node, provider, input_tokens, output_tokens, duration_ms, status, cost_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fields.get("ts") or self._now(),
                scope.tenant_id,
                fields.get("service_id", ""),
                fields.get("end_user_hash"),
                fields.get("job_id"),
                fields.get("role", ""),
                fields.get("model"),
                fields.get("node"),
                fields.get("provider"),
                int(fields.get("input_tokens", 0)),
                int(fields.get("output_tokens", 0)),
                int(fields.get("duration_ms", 0)),
                fields.get("status", "ok"),
                float(fields.get("cost_usd", 0.0)),
            ),
        )
        self._conn.commit()

    def spend_since(self, scope: TenantScope, since: float, *, service_id: str | None = None) -> float:
        """기간 내 누적 비용. 예산 확인의 근거."""
        extra = "ts >= ?"
        params_extra: list[Any] = [since]
        if service_id:
            extra += " AND service_id = ?"
            params_extra.append(service_id)

        where, params = self._scoped_where(scope, extra)
        params.extend(params_extra)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM usage WHERE {where}", params
        ).fetchone()
        return float(row["total"])

    def record_filter_event(
        self,
        scope: TenantScope,
        *,
        rule_id: str,
        stage: str,
        action: str,
        match_count: int = 0,
        offsets: Sequence[tuple[int, int]] | None = None,
        job_id: str | None = None,
        service_id: str | None = None,
        boundary: str | None = None,
    ) -> None:
        """가드 이벤트를 남긴다.

        **매칭된 값은 인자로 받지 않는다.** 받을 수 있게 두면 언젠가 누군가 넣는다.
        감사 로그가 새 유출 경로가 되면 가드의 나머지 노력이 무의미해진다.
        """
        self._scoped_where(scope)
        self._conn.execute(
            "INSERT INTO filter_events(ts, tenant_id, service_id, job_id, rule_id, stage, "
            "action, match_count, offsets_json, boundary) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self._now(), scope.tenant_id, service_id, job_id, rule_id, stage,
                action, int(match_count),
                _json([list(o) for o in offsets]) if offsets else None,
                boundary,
            ),
        )
        self._conn.commit()

    def list_filter_events(
        self, scope: TenantScope, *, action: str | None = None,
        unreviewed_only: bool = False, limit: int = 100,
    ) -> list[sqlite3.Row]:
        conditions = []
        extra: list[Any] = []
        if action:
            conditions.append("action = ?")
            extra.append(action)
        if unreviewed_only:
            conditions.append("reviewed = 0")

        where, params = self._scoped_where(scope, " AND ".join(conditions))
        params.extend(extra)
        return list(
            self._conn.execute(
                f"SELECT * FROM filter_events WHERE {where} ORDER BY ts DESC LIMIT ?",
                [*params, int(limit)],
            )
        )

    # -- 역할 오버라이드 ------------------------------------------------------

    def set_role_override(
        self, scope: TenantScope, role: str, fields: Mapping[str, Any],
        *, note: str | None = None, updated_by: str = "",
    ) -> None:
        self._scoped_where(scope)
        self._conn.execute(
            "INSERT INTO role_overrides(tenant_id, role, fields_json, note, updated_by, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id, role) DO UPDATE SET "
            "fields_json=excluded.fields_json, note=excluded.note, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (scope.tenant_id, role, _json(dict(fields)), note, updated_by, self._now()),
        )
        self._conn.commit()

    def get_role_overrides(self, scope: TenantScope) -> dict[str, dict[str, Any]]:
        where, params = self._scoped_where(scope)
        return {
            row["role"]: json.loads(row["fields_json"])
            for row in self._conn.execute(
                f"SELECT role, fields_json FROM role_overrides WHERE {where}", params
            )
        }

    def clear_role_override(self, scope: TenantScope, role: str) -> bool:
        where, params = self._scoped_where(scope, "role = ?")
        params.append(role)
        cur = self._conn.execute(f"DELETE FROM role_overrides WHERE {where}", params)
        self._conn.commit()
        return cur.rowcount > 0

    # -- 감사 ----------------------------------------------------------------

    def audit(
        self, actor: str, action: str, *, tenant_id: str | None = None,
        target: str | None = None, detail: Mapping[str, Any] | None = None,
        outcome: str = "ok",
    ) -> None:
        self._conn.execute(
            "INSERT INTO admin_audit(ts, tenant_id, actor, action, target, detail_json, outcome) "
            "VALUES(?,?,?,?,?,?,?)",
            (self._now(), tenant_id, actor, action, target,
             _json(dict(detail or {})), outcome),
        )
        self._conn.commit()

    def list_audit(self, scope: TenantScope, *, limit: int = 100) -> list[sqlite3.Row]:
        where, params = self._scoped_where(scope)
        return list(
            self._conn.execute(
                f"SELECT * FROM admin_audit WHERE {where} ORDER BY ts DESC LIMIT ?",
                [*params, int(limit)],
            )
        )

    # -- 전 테넌트 조회 (명시적 · 감사 남김) -------------------------------------

    def usage_across_tenants(
        self, scope: PlatformScope, *, since: float
    ) -> list[sqlite3.Row]:
        """플랫폼 관리자용 전 테넌트 사용량.

        경계를 넘는 유일한 조회 경로이며, 넘은 사실이 감사에 남는다.
        """
        if not isinstance(scope, PlatformScope):
            raise ScopeViolation("전 테넌트 조회는 PlatformScope 를 요구한다")
        self.audit(
            scope.actor, "usage_across_tenants",
            detail={"reason": scope.reason, "since": since},
        )
        return list(
            self._conn.execute(
                "SELECT tenant_id, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd, "
                "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok "
                "FROM usage WHERE ts >= ? GROUP BY tenant_id ORDER BY cost_usd DESC",
                (since,),
            )
        )

    def list_tenants(self, scope: PlatformScope) -> list[sqlite3.Row]:
        if not isinstance(scope, PlatformScope):
            raise ScopeViolation("테넌트 목록은 PlatformScope 를 요구한다")
        self.audit(scope.actor, "list_tenants", detail={"reason": scope.reason})
        return list(
            self._conn.execute(
                "SELECT id, name, locale, status, budget_usd_per_month, created_at "
                "FROM tenants WHERE purged_at IS NULL ORDER BY created_at"
            )
        )

    # -- 노드 헬스·모델 요청 (공유 인프라 — 테넌트 스코프 아님) --------------------

    def upsert_node_health(self, node: str, **fields: Any) -> None:
        allowed = {
            "status", "consecutive_failures", "consecutive_successes",
            "last_probe_at", "loaded_model", "error",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if "models" in fields:
            payload["models_json"] = _json(list(fields["models"]))

        self._conn.execute("INSERT OR IGNORE INTO node_health(node) VALUES(?)", (node,))
        if payload:
            assignments = ", ".join(f"{k} = ?" for k in payload)
            self._conn.execute(
                f"UPDATE node_health SET {assignments} WHERE node = ?",
                [*payload.values(), node],
            )
        self._conn.commit()

    def get_node_health(self, node: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM node_health WHERE node = ?", (node,)
        ).fetchone()

    def all_node_health(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM node_health ORDER BY node"))

    # -- 레이트리밋 카운터 -----------------------------------------------------

    def bump_rate_counter(self, key: str, bucket: int) -> None:
        self._conn.execute(
            "INSERT INTO rate_counters(key, bucket, count) VALUES(?,?,1) "
            "ON CONFLICT(key, bucket) DO UPDATE SET count = count + 1",
            (key, bucket),
        )
        self._conn.commit()

    def rate_count(self, key: str, since_bucket: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS n FROM rate_counters "
            "WHERE key = ? AND bucket >= ?",
            (key, since_bucket),
        ).fetchone()
        return int(row["n"])

    def prune_rate_counters(self, before_bucket: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM rate_counters WHERE bucket < ?", (before_bucket,)
        )
        self._conn.commit()
        return cur.rowcount

    # -- 보존 정리 ------------------------------------------------------------

    def purge_expired(
        self, *, job_retention_days: int = 30, raw_prompt_retention_days: int = 7
    ) -> dict[str, int]:
        """보존 기간이 지난 것을 정리한다.

        원문 암호문과 잡 본체를 **다른 주기로** 지운다 — 마스킹본은 프롬프트 개선의
        재료라 오래 두되, 원문은 짧게 두는 것이 거버넌스의 요구다.
        """
        now = self._now()
        cipher_cutoff = now - raw_prompt_retention_days * 86400
        job_cutoff = now - job_retention_days * 86400

        cipher = self._conn.execute(
            "UPDATE jobs SET prompt_cipher = NULL, prompt_nonce = NULL "
            "WHERE prompt_cipher IS NOT NULL AND created_at < ?",
            (cipher_cutoff,),
        ).rowcount
        jobs = self._conn.execute(
            "DELETE FROM jobs WHERE status IN "
            "('ok','failed','cancelled','blocked') AND finished_at IS NOT NULL AND finished_at < ?",
            (job_cutoff,),
        ).rowcount
        usage = self._conn.execute("DELETE FROM usage WHERE ts < ?", (job_cutoff,)).rowcount
        events = self._conn.execute(
            "DELETE FROM filter_events WHERE ts < ?", (job_cutoff,)
        ).rowcount

        self._conn.commit()
        return {"prompt_cipher": cipher, "jobs": jobs, "usage": usage, "filter_events": events}

    # -- 파기 (C6) ------------------------------------------------------------

    def purge_end_user(self, scope: TenantScope, end_user_hash: str) -> dict[str, int]:
        """엔드유저 파기. 그 사용자의 데이터만 지운다."""
        where, params = self._scoped_where(scope, "end_user_hash = ?")
        params.append(end_user_hash)

        jobs = self._conn.execute(f"DELETE FROM jobs WHERE {where}", params).rowcount
        usage = self._conn.execute(f"DELETE FROM usage WHERE {where}", params).rowcount
        self._conn.commit()

        # 무엇을 지웠는지가 아니라 언제·누가·얼마나 지웠는지만 남긴다.
        self.audit(
            "tenant_admin", "purge_end_user", tenant_id=scope.tenant_id,
            detail={"jobs": jobs, "usage": usage},
        )
        return {"jobs": jobs, "usage": usage}

    def purge_tenant(self, scope: PlatformScope, tenant_id: str) -> dict[str, int]:
        """테넌트 파기.

        행을 지우는 것과 별개로, **DEK 폐기가 가장 강한 삭제다** — 백업에 남은 암호문도
        복호화할 수 없게 된다(crypto-shredding). 그 처리는 crypto 레이어의 몫이고
        여기서는 래핑된 DEK 를 지운다.
        """
        if not isinstance(scope, PlatformScope):
            raise ScopeViolation("테넌트 파기는 PlatformScope 를 요구한다")

        counts: dict[str, int] = {}
        for table in ("jobs", "usage", "filter_events", "role_overrides", "tokens", "services"):
            counts[table] = self._conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,)
            ).rowcount

        self._conn.execute(
            "UPDATE tenants SET status='purged', dek_wrapped=NULL, purged_at=? WHERE id=?",
            (self._now(), tenant_id),
        )
        self._conn.commit()

        self.audit(
            scope.actor, "purge_tenant", tenant_id=tenant_id,
            detail={"reason": scope.reason, **counts},
        )
        return counts

    def close(self) -> None:
        self._conn.close()


# ── 행 ↔ 객체 ───────────────────────────────────────────────────────────────

_JOB_FIELD_MAP = {
    "placement": ("placement_json", lambda v: _json(list(v))),
    "tier_models": ("tier_models_json", lambda v: _json(dict(v))),
    "options": ("options_json", lambda v: _json(dict(v))),
    "metadata": ("metadata_json", lambda v: _json(dict(v))),
    "metrics": ("metrics_json", lambda v: _json(dict(v)) if v is not None else None),
}

_JOB_DIRECT_FIELDS = frozenset(
    {
        "status", "priority", "prompt_masked", "prompt_cipher", "prompt_nonce",
        "system_masked", "prompt_hash", "system_hash", "response", "error", "error_code",
        "timeout_s", "max_prompt_chars", "node", "model", "tier", "last_failed_node",
        "attempts", "wait_reason", "wait_since", "cost_reserved_usd", "cost_usd",
        "input_tokens", "output_tokens", "started_at", "finished_at", "lane", "end_user_hash",
    }
)


def _map_job_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _JOB_FIELD_MAP:
            column, encode = _JOB_FIELD_MAP[key]
            mapped[column] = encode(value)
        elif key in _JOB_DIRECT_FIELDS:
            mapped[key] = value
        else:
            raise StoreError(f"갱신할 수 없는 잡 필드: {key}")
    return mapped


def _row_to_job(row: sqlite3.Row) -> JobRow:
    keys = set(row.keys())

    def get(name: str, default: Any = None) -> Any:
        return row[name] if name in keys else default

    def loads(name: str, default: Any) -> Any:
        raw = get(name)
        return json.loads(raw) if raw else default

    return JobRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        service_id=get("service_id", ""),
        end_user_hash=get("end_user_hash"),
        role=row["role"],
        lane=row["lane"],
        kind=get("kind", "generate"),
        status=row["status"],
        priority=get("priority", 0) or 0,
        prompt_masked=get("prompt_masked"),
        prompt_cipher=get("prompt_cipher"),
        prompt_nonce=get("prompt_nonce"),
        system_masked=get("system_masked"),
        prompt_hash=get("prompt_hash"),
        system_hash=get("system_hash"),
        response=get("response"),
        error=get("error"),
        error_code=get("error_code"),
        placement=tuple(loads("placement_json", [])),
        tier_models=loads("tier_models_json", {}),
        options=loads("options_json", {}),
        timeout_s=get("timeout_s", 120) or 120,
        max_prompt_chars=get("max_prompt_chars"),
        node=get("node"),
        model=get("model"),
        tier=get("tier"),
        last_failed_node=get("last_failed_node"),
        attempts=get("attempts", 0) or 0,
        wait_reason=get("wait_reason"),
        wait_since=get("wait_since"),
        cost_reserved_usd=get("cost_reserved_usd", 0.0) or 0.0,
        cost_usd=get("cost_usd", 0.0) or 0.0,
        input_tokens=get("input_tokens", 0) or 0,
        output_tokens=get("output_tokens", 0) or 0,
        metrics=loads("metrics_json", None),
        metadata=loads("metadata_json", {}),
        created_at=get("created_at", 0.0) or 0.0,
        started_at=get("started_at"),
        finished_at=get("finished_at"),
    )
