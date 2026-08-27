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

import contextlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

SCHEMA_VERSION = 1

#: 한 문장에 넣을 파라미터 상한. SQLite 의 실제 상한은 빌드에 따라 999~32766 인데,
#: 넘으면 `too many SQL variables` 로 **문장 전체가** 실패한다. 보수적으로 잡는다 —
#: 쪼개는 비용은 무시할 만하고, 넘쳐서 실패하는 쪽은 파기가 안 되는 사고다.
SQL_VARIABLE_LIMIT = 500


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]

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

TERMINAL_STATUSES = frozenset({"ok", "failed", "cancelled", "blocked", "needs_review"})

#: 보존 정리가 지우는 상태. `TERMINAL_STATUSES` 에서 파생시켜 **목록을 두 벌 두지
#: 않는다** — 하드코딩된 목록에 `needs_review` 가 빠져 그 잡들이 영원히 쌓이고 있었다.
RETAINABLE_STATUSES = TERMINAL_STATUSES

#: 같은 관리자의 같은 **읽기 전용** 조회를 이 안에서는 한 줄로 합친다.
#: 관제 대시보드 폴링이 감사 테이블을 무한 증식시키던 것을 막는다.
AUDIT_COALESCE_SECONDS = 300.0

#: 감사 보존. 잡보다 길게 둔다(규제 대응) — 다만 상한은 있어야 한다.
AUDIT_RETENTION_DAYS = 365
#: 평가 이력 보존. 규칙 승격 판단의 근거이므로 잡보다 길다.
EVAL_RUN_RETENTION_DAYS = 180


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
    -- 경계 밖으로 나갈 때 쓰는 더 강하게 마스킹된 변형. NULL 이면 위와 같다.
    -- 가드는 경계별로 다른 등급을 적용할 수 있는데(안에서는 보되 밖으로는 가리고),
    -- 한 벌만 저장하면 그 구분이 디스패치 시점에 사라진다.
    prompt_external   TEXT,
    system_external   TEXT,
    -- 가드가 좁힌 허용 경계. 배치 필터가 노드 경계와 교집합을 낸다.
    allowed_boundaries_json TEXT NOT NULL DEFAULT '["internal","external"]',
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
-- 엔드유저 파기가 이 컬럼으로 UPDATE 한다. 없으면 풀스캔이고, 잡이 많은
-- 엔드유저일수록 — 즉 파기가 가장 중요한 경우일수록 — 느려진다.
CREATE INDEX IF NOT EXISTS idx_filter_job ON filter_events(job_id);
-- `/metrics` 가 스크레이프마다 전역 집계를 한다. `idx_filter_review` 는 tenant_id
-- 선두라 전역 집계에 못 쓰여 매번 풀스캔이었다.
CREATE INDEX IF NOT EXISTS idx_filter_global ON filter_events(action, stage, reviewed);

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

-- 등록된 노드 선언. **YAML 은 시드이고 여기가 권위다.**
--
-- 관제 UI 로 등록한 노드가 여기 없으면 컨테이너 재시작 한 번에 사라진다 —
-- 그 노드에서 돌던 잡은 복구 후 배치 불가가 되고, 관리자는 증설한 노드가
-- 왜 없어졌는지 알 수 없다. `node_health` 는 상태 전용이라 선언을 못 담는다.
--
-- 테넌트 스코프가 없다. 노드는 **플랫폼 자원**이고 등록도 플랫폼 권한이다.
CREATE TABLE IF NOT EXISTS nodes (
    name             TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    base_url         TEXT,
    api_key_env      TEXT,
    auth_header_env  TEXT,
    data_boundary    TEXT NOT NULL DEFAULT 'external',
    mem_budget_gb    REAL,
    max_concurrent   INTEGER NOT NULL DEFAULT 1,
    tags_json        TEXT NOT NULL DEFAULT '[]',
    models_json      TEXT NOT NULL DEFAULT '[]',
    tenant_affinity_json TEXT NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    metered_override INTEGER,
    registered_by    TEXT,
    created_at       REAL NOT NULL
);

-- 테넌트 관리자가 바꿀 수 있는 설정. 컬럼을 늘리는 대신 키·값으로 둔 이유는,
-- 설정 항목이 늘 때마다 ADD COLUMN 마이그레이션을 찍는 것이 과하기 때문이다.
-- **정책의 하한은 여기 없다** — 그건 플랫폼 소유라 YAML 에 있다.
CREATE TABLE IF NOT EXISTS tenant_settings (
    tenant_id  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, key)
);

-- 테넌트가 추가·강화한 가드 규칙.
--
-- 베이스라인은 YAML(플랫폼 소유)이고 이 테이블은 그 위에 얹는 층이다. 완화 여부는
-- 스토어가 아니라 `guard.rules_for()` 가 판정한다 — 두 곳에서 판정하면 언젠가 갈린다.
CREATE TABLE IF NOT EXISTS tenant_guard_rules (
    tenant_id   TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'pattern',
    action_json TEXT NOT NULL,
    label       TEXT,
    pattern     TEXT,
    checksum    TEXT,
    keep_tail   INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    locale_pack TEXT NOT NULL DEFAULT 'common',
    updated_by  TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (tenant_id, rule_id)
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

-- 가드 정답셋. **합성 샘플만 담는다.**
--
-- 실제 트래픽에서 수확할 수 없는 이유가 있다: filter_events 는 설계상 매칭된 값을
-- 남기지 않으므로(감사가 유출 경로가 되면 안 되니까) 거기서 텍스트를 꺼낼 방법이 없다.
-- 그래서 정답셋은 사람이 만든 합성 샘플이고, 실제 트래픽의 오탐률은 검토 큐가 따로 잰다.
CREATE TABLE IF NOT EXISTS eval_fixtures (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT,              -- NULL 이면 번들 기본 세트(전 테넌트 공용)
    rule_id      TEXT NOT NULL,
    text         TEXT NOT NULL,
    expect_match INTEGER NOT NULL,  -- 1=양성(잡혀야 함) 0=음성(안 잡혀야 함)
    source       TEXT NOT NULL DEFAULT 'manual',
    note         TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fixtures_rule ON eval_fixtures(rule_id, tenant_id);

CREATE TABLE IF NOT EXISTS eval_runs (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    tenant_id   TEXT,
    kind        TEXT NOT NULL,      -- 'rules' | 'classifier'
    subject     TEXT NOT NULL,      -- 규칙 id 또는 모델 이름
    system_hash TEXT,               -- 어떤 프롬프트 버전에서 잰 값인가 (C8)
    passed      INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_eval_runs ON eval_runs(kind, subject, ts);

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
    prompt_external: str | None = None
    system_external: str | None = None
    allowed_boundaries: tuple[str, ...] = ("internal", "external")
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

    @contextlib.contextmanager
    def _tx(self):
        """여러 문장을 한 트랜잭션으로 묶는다. **실패하면 되돌린다.**

        이 저장소에는 `rollback` 이 한 곳도 없었다. 다중 문장 메서드가 중간에
        실패하면 앞선 쓰기가 **열린 트랜잭션에 남았다가 다음 무관한 commit 에
        섞여** 영속화된다 — 파기 요청이 절반만 처리되고 그 사실이 감사에도
        안 남는다는 뜻이다.

        `sqlite3` 의 컨텍스트 매니저를 쓰지 않는 이유: 그것은 커넥션 자체를
        컨텍스트로 쓰기 때문에 중첩이 어렵고, 여기서는 명시적으로 커밋 지점을
        드러내는 편이 읽기 쉽다.
        """
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

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
            "prompt_external": fields.pop("prompt_external", None),
            "system_external": fields.pop("system_external", None),
            "allowed_boundaries_json": _json(
                list(fields.pop("allowed_boundaries", ("internal", "external")))
            ),
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

    def job_status(self, scope: TenantScope, job_id: str) -> str | None:
        """상태 한 칸만. **대기 폴링이 매번 잡 전체를 읽지 않게 한다.**

        `get_job` 은 `SELECT *` 라 마스킹본·암호문·응답까지 끌어온다. 완료를
        기다리는 요청이 수십 건이면 그 전량이 초당 수백 번 오간다 — 정작 폴링이
        보는 것은 이 한 칸뿐이다.
        """
        where, params = self._scoped_where(scope, "id = ?")
        params.append(job_id)
        row = self._conn.execute(
            f"SELECT status FROM jobs WHERE {where}", params
        ).fetchone()
        return row["status"] if row else None

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

    def update_job(
        self,
        scope: TenantScope,
        job_id: str,
        *,
        expect_status: str | Sequence[str] | None = None,
        **fields: Any,
    ) -> bool:
        """잡을 갱신한다. `tenant_id` 는 절대 바꿀 수 없다 — 그것이 격리 그 자체다.

        `expect_status` 를 주면 **그 상태일 때만** 갱신한다(compare-and-set).
        문서가 지원한다는 다중 프로세스 구성(워커 N + 스케줄러 싱글턴)에서는
        API 워커의 취소와 스케줄러의 디스패치가 같은 잡을 두고 경합한다. 검사와
        갱신이 분리돼 있으면 **"취소됨" 을 응답받은 잡이 실행되고 과금까지 간다.**
        조건을 UPDATE 문 안에 넣으면 그 창이 없어진다.
        """
        if "tenant_id" in fields:
            raise ScopeViolation("잡의 tenant_id 는 변경할 수 없다")

        mapped = _map_job_fields(fields)
        if not mapped:
            return False

        extra = "id = ?"
        assignments = ", ".join(f"{k} = ?" for k in mapped)
        expected: list[Any] = []
        if expect_status is not None:
            expected = [expect_status] if isinstance(expect_status, str) else list(expect_status)
            extra += f" AND status IN ({','.join('?' * len(expected))})"

        where, params = self._scoped_where(scope, extra)
        params.append(job_id)
        params.extend(expected)
        cur = self._conn.execute(
            f"UPDATE jobs SET {assignments} WHERE {where}", [*mapped.values(), *params]
        )
        self._conn.commit()
        return cur.rowcount > 0

    def settle_job(
        self,
        scope: TenantScope,
        job_id: str,
        *,
        job_fields: Mapping[str, Any],
        usage_fields: Mapping[str, Any],
    ) -> None:
        """정산을 **한 트랜잭션으로** 끝낸다.

        예약 해제(`jobs`)와 지출 기록(`usage`)이 별도 커밋이면 그 사이의 크래시가
        예약은 풀고 지출은 잃는다 — **예산이 영구히 과소 계상되고**, 그 오차는
        아무 데도 안 남아서 누구도 발견하지 못한다. 예약을 둔 이유 자체가
        "완료 후에야 드러나는 초과" 를 막는 것인데 여기서 되살아난다.
        """
        mapped = _map_job_fields(dict(job_fields))
        with self._tx():
            if mapped:
                assignments = ", ".join(f"{k} = ?" for k in mapped)
                where, params = self._scoped_where(scope, "id = ?")
                params.append(job_id)
                self._conn.execute(
                    f"UPDATE jobs SET {assignments} WHERE {where}",
                    [*mapped.values(), *params],
                )
            self._insert_usage(scope, dict(usage_fields))

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
            "allowed_boundaries_json, node, model, tier, last_failed_node, attempts, "
            "wait_reason, wait_since, cost_reserved_usd, created_at "
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

        rows = list(self._conn.execute("SELECT id, node FROM jobs WHERE status='running'"))

        # **한 트랜잭션이다.** 절반만 복구된 상태로 기동하면 나머지는 `running` 인
        # 채 남아 영원히 아무도 안 건드린다 — 크래시 복구가 그 자체로 사고가 된다.
        with self._tx():
            for row in rows:
                if row["node"] in metered:
                    self._conn.execute(
                        "UPDATE jobs SET status='needs_review', "
                        "error_code='possible_double_execution', "
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

        return {"requeued": requeued, "needs_review": reviewed}

    # -- 사용량·가드 이벤트 ---------------------------------------------------

    def _insert_usage(self, scope: TenantScope, fields: Mapping[str, Any]) -> None:
        """커밋하지 않는다 — 호출자의 트랜잭션 안에서 쓰인다."""
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

    def record_usage(self, scope: TenantScope, **fields: Any) -> None:
        with self._tx():
            self._insert_usage(scope, fields)

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

    def reserved_cost(self, scope: TenantScope, *, service_id: str | None = None) -> float:
        """아직 정산되지 않은 예약 비용의 합.

        예약을 잡 행에 두는 이유는 내구성이다 — 프로세스 메모리에 두면 재기동 시
        예약이 사라져 예산 확인이 이미 디스패치된 잡을 못 본다.
        """
        extra = "cost_reserved_usd > 0 AND status IN ('queued','running')"
        params_extra: list[Any] = []
        if service_id:
            extra += " AND service_id = ?"
            params_extra.append(service_id)

        where, params = self._scoped_where(scope, extra)
        params.extend(params_extra)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(cost_reserved_usd), 0.0) AS total FROM jobs WHERE {where}",
            params,
        ).fetchone()
        return float(row["total"])

    #: 사용량을 묶을 수 있는 축. **화이트리스트인 이유는 SQL 주입 때문**이다 —
    #: 그룹 컬럼은 파라미터로 못 넘기므로 문자열로 붙여야 하고, 그러면 검증이 유일한 방어다.
    USAGE_AXES = ("service_id", "end_user_hash", "role", "model", "node", "provider")

    def usage_summary(
        self, scope: TenantScope, *, since: float, group_by: str = "service_id"
    ) -> list[dict[str, Any]]:
        """`서비스 × 엔드유저 × 역할 × 노드` 축의 집계."""
        if group_by not in self.USAGE_AXES:
            raise StoreError(f"알 수 없는 집계 축: {group_by}")

        where, params = self._scoped_where(scope, "ts >= ?")
        params.append(since)
        rows = self._conn.execute(
            f"SELECT {group_by} AS key, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cost_usd) AS cost_usd, AVG(duration_ms) AS avg_duration_ms, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok "
            f"FROM usage WHERE {where} GROUP BY {group_by} ORDER BY calls DESC",
            params,
        )
        return [
            {
                "key": row["key"],
                "calls": row["calls"],
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "cost_usd": round(row["cost_usd"] or 0.0, 6),
                "avg_duration_ms": int(row["avg_duration_ms"] or 0),
                "ok": row["ok"],
                "success_rate": round((row["ok"] or 0) / row["calls"], 4) if row["calls"] else 0.0,
            }
            for row in rows
        ]

    # -- 관제 집계 (테넌트를 가로지르지만 **어느 테넌트인지는 안 나온다**) ------
    #
    # 메트릭은 설치처 전체가 보는 대시보드로 흘러간다. 테넌트별 숫자가 거기 뜨면
    # 그것도 정보 유출이므로, 이 집계들은 합계만 돌려준다. 테넌트별 값은 인증이
    # 걸린 관제 API 에만 있다.

    def job_counts(self) -> dict[str, int]:
        return {
            row["status"]: row["n"]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            )
        }

    def filter_event_counts(self) -> dict[tuple[str, str], int]:
        return {
            (row["action"], row["stage"]): row["n"]
            for row in self._conn.execute(
                "SELECT action, stage, COUNT(*) AS n FROM filter_events GROUP BY action, stage"
            )
        }

    def unreviewed_filter_event_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM filter_events WHERE action='audit' AND reviewed=0"
        ).fetchone()
        return int(row["n"])

    def total_spend_since(self, since: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM usage WHERE ts >= ?", (since,)
        ).fetchone()
        return float(row["total"])

    def tenant_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tenants WHERE purged_at IS NULL"
        ).fetchone()
        return int(row["n"])

    def recent_job_errors(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """최근 실패. **프롬프트도 응답도 담지 않는다** — 코드와 사유뿐이다.

        진단 번들에 들어가는 값이고, 그 번들은 설치처가 지원 채널로 그대로 보낸다.
        """
        rows = self._conn.execute(
            "SELECT id, role, lane, node, model, error_code, error, attempts, finished_at "
            "FROM jobs WHERE error_code IS NOT NULL ORDER BY finished_at DESC LIMIT ?",
            (int(limit),),
        )
        return [
            {
                "job_id": row["id"], "role": row["role"], "lane": row["lane"],
                "node": row["node"], "model": row["model"],
                "error_code": row["error_code"],
                # 백엔드 오류 문자열에 응답 조각이 섞여 올 수 있어 길이를 자른다.
                "error": (row["error"] or "")[:200],
                "attempts": row["attempts"], "finished_at": row["finished_at"],
            }
            for row in rows
        ]

    def tenant_budget_status(self, since: float) -> list[dict[str, Any]]:
        """테넌트별 한도와 소진액. 예산 알림의 재료다.

        전 테넌트를 가로지르지만 `PlatformScope` 를 요구하지 않는다 — 이건 사람이
        조회하는 경로가 아니라 **알림 루프가 쓰는 내부 집계**이고, 결과는 알림
        본문(테넌트 id 와 퍼센트)으로만 나간다. 조회 감사를 매 주기 남기면
        감사 로그가 알림 루프로 가득 찬다.
        """
        rows = self._conn.execute(
            "SELECT t.id AS tenant_id, t.budget_usd_per_month, "
            "COALESCE((SELECT SUM(cost_usd) FROM usage u "
            "          WHERE u.tenant_id = t.id AND u.ts >= ?), 0.0) AS spent "
            "FROM tenants t WHERE t.purged_at IS NULL",
            (since,),
        )
        return [
            {
                "tenant_id": row["tenant_id"],
                "budget_usd_per_month": row["budget_usd_per_month"],
                "spent": float(row["spent"]),
            }
            for row in rows
        ]

    def recent_filter_event_count(self, action: str, *, since: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM filter_events WHERE action = ? AND ts >= ?",
            (action, since),
        ).fetchone()
        return int(row["n"])

    def classifier_failure_rate(self, *, since: float) -> float:
        """최근 창에서 2단 분류가 실패한 비율.

        분모는 **맥락 규칙이 걸린 요청** 이 아니라 그 창의 전체 잡이다. 분류를
        시도하지 않은 잡까지 세면 실패율이 희석돼 경보가 안 울린다 — 그래서
        `llm` 단계 이벤트가 하나라도 있는 잡만 분모에 넣는다.
        """
        row = self._conn.execute(
            "SELECT "
            " COUNT(DISTINCT CASE WHEN stage='llm' THEN job_id END) AS attempted, "
            " COUNT(DISTINCT CASE WHEN stage='llm' AND rule_id='_classifier_failed' "
            "        THEN job_id END) AS failed "
            "FROM filter_events WHERE ts >= ?",
            (since,),
        ).fetchone()
        attempted = int(row["attempted"] or 0)
        return (int(row["failed"] or 0) / attempted) if attempted else 0.0

    def queued_wait_reasons(self) -> dict[str, int]:
        """대기 사유별 잡 수. 관제 UI 의 1급 카드다.

        "노드 정비로 대기 12건" 을 보여주지 못하면 관리자는 큐가 왜 안 줄어드는지
        알 수 없고, 알 수 없으면 노드를 늘리는 잘못된 대응을 한다.
        """
        rows = self._conn.execute(
            "SELECT COALESCE(wait_reason, 'none') AS reason, COUNT(*) AS n "
            "FROM jobs WHERE status='queued' GROUP BY reason"
        )
        return {row["reason"]: row["n"] for row in rows}

    def export_tenant(self, scope: TenantScope) -> dict[str, Any]:
        """내보내기 — **마스킹본 기준.**

        `prompt_cipher` · `prompt_nonce` 는 담지 않는다. 내보내기 파일이 원문을 나르면
        보관 기간과 접근 감사가 그 파일 밖에서 모두 무력화된다.
        """
        where, params = self._scoped_where(scope)
        jobs = [
            {
                k: row[k]
                for k in row.keys()
                if k not in ("prompt_cipher", "prompt_nonce")
            }
            for row in self._conn.execute(f"SELECT * FROM jobs WHERE {where}", params)
        ]
        where, params = self._scoped_where(scope)
        usage = [dict(row) for row in self._conn.execute(f"SELECT * FROM usage WHERE {where}", params)]
        where, params = self._scoped_where(scope)
        events = [
            dict(row)
            for row in self._conn.execute(f"SELECT * FROM filter_events WHERE {where}", params)
        ]
        where, params = self._scoped_where(scope)
        audit = [
            dict(row)
            for row in self._conn.execute(f"SELECT * FROM admin_audit WHERE {where}", params)
        ]

        tenant = self.get_tenant(scope.tenant_id)
        return {
            "tenant": {
                "id": scope.tenant_id,
                "name": tenant["name"] if tenant else None,
                "locale": tenant["locale"] if tenant else None,
            },
            "exported_at": self._now(),
            "services": [dict(row) for row in self.list_services(scope)],
            "settings": self.tenant_settings(scope),
            "guard_rules": self.list_tenant_guard_rules(scope),
            "role_overrides": self.get_role_overrides(scope),
            "jobs": jobs,
            "usage": usage,
            "filter_events": events,
            "audit": audit,
        }

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

    def review_filter_event(
        self, scope: TenantScope, event_id: int, verdict: str
    ) -> bool:
        """오탐 검토 큐의 판정. `verdict` 는 'true_positive' | 'false_positive'.

        **값이 아니라 판정만 남긴다.** 검토자는 원문을 UI 에서 보지만 그 텍스트는
        여기 들어오지 않는다 — 들어오면 감사 테이블이 곧 PII 저장소가 된다.
        """
        if verdict not in ("true_positive", "false_positive"):
            raise StoreError(f"알 수 없는 판정: {verdict}")

        where, params = self._scoped_where(scope, "id = ?")
        params.append(event_id)
        cur = self._conn.execute(
            f"UPDATE filter_events SET reviewed = 1, verdict = ? WHERE {where}",
            [verdict, *params],
        )
        self._conn.commit()
        return cur.rowcount > 0

    def review_stats(self, scope: TenantScope, rule_id: str | None = None) -> dict[str, dict[str, int]]:
        """규칙별 검토 집계 — 실제 트래픽 기준 오탐률의 근거."""
        extra = "reviewed = 1"
        params_extra: list[Any] = []
        if rule_id:
            extra += " AND rule_id = ?"
            params_extra.append(rule_id)

        where, params = self._scoped_where(scope, extra)
        params.extend(params_extra)

        stats: dict[str, dict[str, int]] = {}
        for row in self._conn.execute(
            f"SELECT rule_id, verdict, COUNT(*) AS n FROM filter_events "
            f"WHERE {where} GROUP BY rule_id, verdict",
            params,
        ):
            stats.setdefault(row["rule_id"], {})[row["verdict"]] = row["n"]
        return stats

    # -- 정답셋 · 평가 이력 -----------------------------------------------------

    def add_fixture(
        self,
        rule_id: str,
        text: str,
        expect_match: bool,
        *,
        scope: TenantScope | None = None,
        source: str = "manual",
        note: str | None = None,
    ) -> str:
        """정답셋 샘플 하나. `scope` 가 없으면 번들 기본 세트(전 테넌트 공용)다."""
        fixture_id = uuid.uuid4().hex[:16]
        self._conn.execute(
            "INSERT INTO eval_fixtures(id, tenant_id, rule_id, text, expect_match, "
            "source, note, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                fixture_id, scope.tenant_id if scope else None, rule_id, text,
                int(expect_match), source, note, self._now(),
            ),
        )
        self._conn.commit()
        return fixture_id

    def list_fixtures(
        self, *, scope: TenantScope | None = None, rule_id: str | None = None
    ) -> list[sqlite3.Row]:
        """번들 기본 세트 + (스코프가 있으면) 그 테넌트가 추가한 것."""
        conditions = ["(tenant_id IS NULL" + (" OR tenant_id = ?)" if scope else ")")]
        params: list[Any] = [scope.tenant_id] if scope else []
        if rule_id:
            conditions.append("rule_id = ?")
            params.append(rule_id)

        return list(
            self._conn.execute(
                f"SELECT * FROM eval_fixtures WHERE {' AND '.join(conditions)} "
                f"ORDER BY rule_id, created_at",
                params,
            )
        )

    def delete_fixture(self, scope: TenantScope, fixture_id: str) -> bool:
        """테넌트가 추가한 것만 지울 수 있다. 번들 기본 세트는 못 지운다."""
        where, params = self._scoped_where(scope, "id = ?")
        params.append(fixture_id)
        cur = self._conn.execute(f"DELETE FROM eval_fixtures WHERE {where}", params)
        self._conn.commit()
        return cur.rowcount > 0

    def record_eval_run(
        self,
        kind: str,
        subject: str,
        *,
        passed: int,
        total: int,
        metrics: Mapping[str, Any] | None = None,
        tenant_id: str | None = None,
        system_hash: str | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex[:16]
        self._conn.execute(
            "INSERT INTO eval_runs(id, ts, tenant_id, kind, subject, system_hash, "
            "passed, total, metrics_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, self._now(), tenant_id, kind, subject, system_hash,
             passed, total, _json(dict(metrics or {}))),
        )
        self._conn.commit()
        return run_id

    def latest_eval_run(self, kind: str, subject: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM eval_runs WHERE kind = ? AND subject = ? "
            "ORDER BY ts DESC LIMIT 1",
            (kind, subject),
        ).fetchone()

    def list_eval_runs(self, *, kind: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if kind:
            return list(
                self._conn.execute(
                    "SELECT * FROM eval_runs WHERE kind = ? ORDER BY ts DESC LIMIT ?",
                    (kind, int(limit)),
                )
            )
        return list(
            self._conn.execute("SELECT * FROM eval_runs ORDER BY ts DESC LIMIT ?", (int(limit),))
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

    # -- 노드 선언 -------------------------------------------------------------

    def save_node(self, declaration: Mapping[str, Any], *, actor: str = "") -> None:
        """노드 선언을 영속화한다. **재기동해도 살아남아야 한다.**"""
        self._conn.execute(
            "INSERT INTO nodes(name, provider, base_url, api_key_env, auth_header_env, "
            "data_boundary, mem_budget_gb, max_concurrent, tags_json, models_json, "
            "tenant_affinity_json, enabled, metered_override, registered_by, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "provider=excluded.provider, base_url=excluded.base_url, "
            "api_key_env=excluded.api_key_env, auth_header_env=excluded.auth_header_env, "
            "data_boundary=excluded.data_boundary, mem_budget_gb=excluded.mem_budget_gb, "
            "max_concurrent=excluded.max_concurrent, tags_json=excluded.tags_json, "
            "models_json=excluded.models_json, "
            "tenant_affinity_json=excluded.tenant_affinity_json, "
            "enabled=excluded.enabled, metered_override=excluded.metered_override",
            (
                declaration["name"], declaration["provider"], declaration.get("base_url"),
                declaration.get("api_key_env"), declaration.get("auth_header_env"),
                declaration.get("data_boundary", "external"),
                declaration.get("mem_budget_gb"),
                int(declaration.get("max_concurrent", 1)),
                _json(list(declaration.get("tags") or ())),
                _json(list(declaration.get("models") or ())),
                _json(list(declaration.get("tenant_affinity") or ())),
                int(bool(declaration.get("enabled", True))),
                declaration.get("metered_override"),
                actor, self._now(),
            ),
        )
        self._conn.commit()

    def list_nodes(self) -> list[dict[str, Any]]:
        """등록된 노드 선언 전부. 기동 시 클러스터가 이것으로 자기를 채운다."""
        return [
            {
                "name": row["name"],
                "provider": row["provider"],
                "base_url": row["base_url"],
                "api_key_env": row["api_key_env"],
                "auth_header_env": row["auth_header_env"],
                "data_boundary": row["data_boundary"],
                "mem_budget_gb": row["mem_budget_gb"],
                "max_concurrent": row["max_concurrent"],
                "tags": json.loads(row["tags_json"]),
                "models": json.loads(row["models_json"]),
                "tenant_affinity": json.loads(row["tenant_affinity_json"]),
                "enabled": bool(row["enabled"]),
                "metered_override": row["metered_override"],
            }
            for row in self._conn.execute("SELECT * FROM nodes ORDER BY name")
        ]

    def delete_node(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM nodes WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    # -- 테넌트 설정 -----------------------------------------------------------

    def set_tenant_setting(self, scope: TenantScope, key: str, value: Any) -> None:
        self._scoped_where(scope)
        self._conn.execute(
            "INSERT INTO tenant_settings(tenant_id, key, value_json, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(tenant_id, key) DO UPDATE SET "
            "value_json=excluded.value_json, updated_at=excluded.updated_at",
            (scope.tenant_id, key, _json(value), self._now()),
        )
        self._conn.commit()

    def tenant_setting(self, scope: TenantScope, key: str, default: Any = None) -> Any:
        where, params = self._scoped_where(scope, "key = ?")
        params.append(key)
        row = self._conn.execute(
            f"SELECT value_json FROM tenant_settings WHERE {where}", params
        ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def tenant_settings(self, scope: TenantScope) -> dict[str, Any]:
        where, params = self._scoped_where(scope)
        return {
            row["key"]: json.loads(row["value_json"])
            for row in self._conn.execute(
                f"SELECT key, value_json FROM tenant_settings WHERE {where}", params
            )
        }

    #: 플랫폼 설정을 담는 예약 테넌트 id. **별도 테이블을 만들지 않는 이유**는
    #: 스토어의 테넌트 초크포인트를 우회하는 경로를 새로 뚫지 않기 위해서다 —
    #: 우회로가 생기면 언젠가 쓰인다.
    PLATFORM_SETTINGS_TENANT = "_platform"

    def set_platform_setting(self, key: str, value: Any) -> None:
        """플랫폼 전역 설정. **테넌트가 만질 수 없다** — 스코프가 다르기 때문이다."""
        self.set_tenant_setting(TenantScope(self.PLATFORM_SETTINGS_TENANT), key, value)

    def platform_setting(self, key: str, default: Any = None) -> Any:
        return self.tenant_setting(
            TenantScope(self.PLATFORM_SETTINGS_TENANT), key, default
        )

    def set_tenant_locale(self, scope: TenantScope, locale: str) -> None:
        """테넌트 기본 로케일. 이 값이 가드 로케일 팩까지 정한다 — 단순 번역이 아니다."""
        self._scoped_where(scope)
        self._conn.execute(
            "UPDATE tenants SET locale = ? WHERE id = ?", (locale, scope.tenant_id)
        )
        self._conn.commit()

    def adopt_tenant_dek(self, scope: TenantScope, wrapped: bytes) -> bytes | None:
        """DEK 가 없는 테넌트에 하나 붙이고 **실제로 저장된 것**을 돌려준다.

        KEK 없이 기동한 설치처(원문 보관 비활성)에서 만들어진 테넌트는 `dek_wrapped`
        가 NULL 이다. 나중에 관리자가 KEK 를 넣으면 금고는 켜지는데 그 테넌트의 키만
        없어서, **그 테넌트의 모든 요청이 봉인 단계에서 죽는다.** 설치 순서 하나로
        멀쩡하던 테넌트가 통째로 멈추면 안 된다.

        돌려준 값으로 봉인해야 한다 — 두 요청이 동시에 들어오면 UPDATE 는 하나만
        이긴다. 진 쪽이 자기가 만든 DEK 로 봉인하면 **그 암호문은 아무도 못 연다.**

        파기된 테넌트에는 붙이지 않는다. 붙이면 crypto-shredding 이후에 새 암호문이
        다시 쌓이기 시작한다 — 지웠다고 믿은 것이 되살아나는 셈이다.
        """
        self._scoped_where(scope)
        cursor = self._conn.execute(
            "UPDATE tenants SET dek_wrapped = ? "
            "WHERE id = ? AND dek_wrapped IS NULL AND purged_at IS NULL",
            (wrapped, scope.tenant_id),
        )
        adopted = cursor.rowcount == 1
        self._conn.commit()
        if adopted:
            # 테넌트가 키를 갖게 된 시점은 남긴다 — 이 앞뒤로 원문 보관 여부가 갈린다.
            self.audit(
                "system", "adopt_tenant_dek", tenant_id=scope.tenant_id,
                detail={"reason": "kek_enabled_after_tenant_creation"},
            )
        row = self._conn.execute(
            "SELECT dek_wrapped FROM tenants WHERE id = ? AND purged_at IS NULL",
            (scope.tenant_id,),
        ).fetchone()
        return row["dek_wrapped"] if row else None

    # -- 테넌트 가드 규칙 ------------------------------------------------------

    def set_tenant_guard_rule(
        self, scope: TenantScope, rule: Mapping[str, Any], *, updated_by: str = ""
    ) -> None:
        """테넌트 규칙을 저장한다. **완화 여부는 여기서 판정하지 않는다.**

        판정을 스토어와 가드 두 곳에 두면 언젠가 둘이 갈리고, 갈리는 순간 어느 쪽이
        진짜 정책인지 아무도 모르게 된다. 스토어는 보관만 하고 `guard.rules_for()` 가
        베이스라인과 병합하면서 강한 쪽을 채택한다.
        """
        self._scoped_where(scope)
        self._conn.execute(
            "INSERT INTO tenant_guard_rules(tenant_id, rule_id, kind, action_json, label, "
            "pattern, checksum, keep_tail, description, locale_pack, updated_by, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id, rule_id) DO UPDATE SET "
            "kind=excluded.kind, action_json=excluded.action_json, label=excluded.label, "
            "pattern=excluded.pattern, checksum=excluded.checksum, keep_tail=excluded.keep_tail, "
            "description=excluded.description, locale_pack=excluded.locale_pack, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (
                scope.tenant_id, str(rule["id"]), str(rule.get("kind", "pattern")),
                _json(rule["action"]), rule.get("label"), rule.get("pattern"),
                rule.get("checksum"), int(rule.get("keep_tail", 0)),
                rule.get("description"), str(rule.get("locale_pack", "common")),
                updated_by, self._now(),
            ),
        )
        self._conn.commit()

    def list_tenant_guard_rules(self, scope: TenantScope) -> list[dict[str, Any]]:
        where, params = self._scoped_where(scope)
        rows = self._conn.execute(
            f"SELECT * FROM tenant_guard_rules WHERE {where} ORDER BY rule_id", params
        )
        return [
            {
                "id": row["rule_id"],
                "kind": row["kind"],
                "action": json.loads(row["action_json"]),
                "label": row["label"] or "",
                "pattern": row["pattern"],
                "checksum": row["checksum"],
                "keep_tail": row["keep_tail"],
                "description": row["description"],
                "locale_pack": row["locale_pack"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def clear_tenant_guard_rule(self, scope: TenantScope, rule_id: str) -> bool:
        where, params = self._scoped_where(scope, "rule_id = ?")
        params.append(rule_id)
        cur = self._conn.execute(f"DELETE FROM tenant_guard_rules WHERE {where}", params)
        self._conn.commit()
        return cur.rowcount > 0

    # -- 감사 ----------------------------------------------------------------

    def audit(
        self, actor: str, action: str, *, tenant_id: str | None = None,
        target: str | None = None, detail: Mapping[str, Any] | None = None,
        outcome: str = "ok", coalesce_seconds: float = 0.0,
    ) -> None:
        """감사 한 줄.

        `coalesce_seconds` 는 **읽기 전용 조회에만** 쓴다. 관제 대시보드가
        폴링하면 같은 관리자의 같은 조회가 초 단위로 반복되는데, 그것을 한 줄씩
        남기면 감사 테이블이 대시보드를 열어 둔 시간에 비례해 자란다 —
        그리고 그 안에서 **진짜 봐야 할 한 줄을 찾을 수 없게 된다.**

        경계를 넘은 사실 자체는 지운 적이 없다. 같은 사람이 같은 조회를 N 번 한
        것을 한 줄 + 횟수로 적을 뿐이다. **변경은 절대 합치지 않는다** — 파기
        두 번과 파기 한 번은 다른 사건이다.
        """
        now = self._now()
        if coalesce_seconds > 0:
            recent = self._conn.execute(
                "SELECT id, detail_json FROM admin_audit "
                "WHERE actor = ? AND action = ? AND ts >= ? "
                "AND tenant_id IS ? AND target IS ? "
                "ORDER BY ts DESC LIMIT 1",
                (actor, action, now - coalesce_seconds, tenant_id, target),
            ).fetchone()
            if recent is not None:
                merged = dict(detail or {})
                try:
                    previous = json.loads(recent["detail_json"] or "{}")
                except ValueError:
                    previous = {}
                merged["repeats"] = int(previous.get("repeats", 1)) + 1
                with self._tx():
                    self._conn.execute(
                        "UPDATE admin_audit SET ts = ?, detail_json = ? WHERE id = ?",
                        (now, _json(merged), recent["id"]),
                    )
                return

        with self._tx():
            self._conn.execute(
                "INSERT INTO admin_audit(ts, tenant_id, actor, action, target, "
                "detail_json, outcome) VALUES(?,?,?,?,?,?,?)",
                (now, tenant_id, actor, action, target,
                 _json(dict(detail or {})), outcome),
            )

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
            coalesce_seconds=AUDIT_COALESCE_SECONDS,
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
        self.audit(
            scope.actor, "list_tenants", detail={"reason": scope.reason},
            coalesce_seconds=AUDIT_COALESCE_SECONDS,
        )
        return list(
            self._conn.execute(
                # 호출부가 읽는 컬럼을 전부 담는다. 빠뜨리면 sqlite3.Row 키 오류로
                # 500 이 되고, 목록 조회는 성공 경로 테스트가 없으면 안 드러난다.
                "SELECT id, name, locale, status, budget_usd_per_month, "
                "rate_limit_per_min, dek_wrapped, created_at "
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

    # -- 모델 설치 요청 (공유 인프라 — 테넌트 스코프 아님) ------------------------

    def create_model_request(
        self, node: str, model: str, *, requested_by: str = "",
        roles: Sequence[str] = (), est_size_gb: float = 0.0,
        status: str = "pending",
    ) -> str:
        """(모델, 노드) 쌍당 요청 하나. 같은 모델을 3대에 얹으려면 요청 3건이다."""
        request_id = uuid.uuid4().hex[:16]
        self._conn.execute(
            "INSERT INTO model_requests(id, node, model, status, requested_by, roles_json, "
            "est_size_gb, created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node, model) DO NOTHING",
            (request_id, node, model, status, requested_by, _json(list(roles)),
             est_size_gb, self._now()),
        )
        self._conn.commit()
        row = self.get_model_request(node, model)
        return row["id"] if row else request_id

    def get_model_request(self, node: str, model: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM model_requests WHERE node = ? AND model = ?", (node, model)
        ).fetchone()

    def get_model_request_by_id(self, request_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM model_requests WHERE id = ?", (request_id,)
        ).fetchone()

    def list_model_requests(self, *, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return list(
                self._conn.execute(
                    "SELECT * FROM model_requests WHERE status = ? ORDER BY created_at",
                    (status,),
                )
            )
        return list(self._conn.execute("SELECT * FROM model_requests ORDER BY created_at"))

    def update_model_request(self, request_id: str, **fields: Any) -> bool:
        allowed = {"status", "progress", "error", "decided_at", "est_size_gb"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return False
        assignments = ", ".join(f"{k} = ?" for k in payload)
        cur = self._conn.execute(
            f"UPDATE model_requests SET {assignments} WHERE id = ?",
            [*payload.values(), request_id],
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_model_request(self, node: str, model: str) -> bool:
        """삭제 시 요청 행 자체를 지운다.

        `ready` 로 두면 다음 탐지에서 되살아나고, `rejected` 로 두면 이후 잡이
        "설치가 거부됨" 이라는 **거짓 사유**로 하드 실패한다.
        """
        cur = self._conn.execute(
            "DELETE FROM model_requests WHERE node = ? AND model = ?", (node, model)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def infra_job_counts(self, *, node: str | None = None, model: str | None = None) -> dict[str, int]:
        """상태별 잡 수. **집계만 돌려주므로 테넌트 내용을 노출하지 않는다.**

        모델 삭제 차단 판정처럼 인프라 결정에 쓰인다. 개수는 테넌트 데이터가 아니므로
        스코프를 요구하지 않되, 프롬프트나 응답은 절대 여기로 나가지 않는다.
        """
        conditions, params = [], []
        if node:
            conditions.append("node = ?")
            params.append(node)
        if model:
            conditions.append("model = ?")
            params.append(model)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        return {
            row["status"]: row["n"]
            for row in self._conn.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs {where} GROUP BY status", params
            )
        }

    def queued_roles(self) -> dict[str, int]:
        """대기 중인 잡의 역할별 개수. 삭제 차단이 "이 역할의 대기 잡" 을 세는 데 쓴다."""
        return {
            row["role"]: row["n"]
            for row in self._conn.execute(
                "SELECT role, COUNT(*) AS n FROM jobs WHERE status='queued' GROUP BY role"
            )
        }

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

    def oldest_rate_bucket(self, key: str, since: int) -> int | None:
        """윈도 안에서 가장 오래된 버킷. 429 의 `retry_after` 근거다."""
        row = self._conn.execute(
            "SELECT MIN(bucket) AS oldest FROM rate_counters WHERE key = ? AND bucket >= ?",
            (key, since),
        ).fetchone()
        return int(row["oldest"]) if row and row["oldest"] is not None else None

    def prune_rate_counters(self, before_bucket: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM rate_counters WHERE bucket < ?", (before_bucket,)
        )
        self._conn.commit()
        return cur.rowcount

    # -- 보존 정리 ------------------------------------------------------------

    RAW_RETENTION_KEY = "raw_prompt_retention_days"

    def effective_raw_retention_days(self, tenant_id: str, platform_max: int) -> int:
        """이 테넌트에 실제로 적용되는 원문 보관 일수.

        **테넌트는 짧게만 정할 수 있다.** 가드 규칙과 같은 방향이다 — 플랫폼이 정한
        상한을 테넌트가 늘릴 수 있으면 플랫폼의 거버넌스 약속이 사라진다.
        조용히 자르지 않도록 설정 API 가 두 숫자를 함께 보여준다.
        """
        row = self._conn.execute(
            "SELECT value_json FROM tenant_settings WHERE tenant_id = ? AND key = ?",
            (tenant_id, self.RAW_RETENTION_KEY),
        ).fetchone()
        if row is None:
            return platform_max
        try:
            requested = int(json.loads(row["value_json"]))
        except (TypeError, ValueError):
            return platform_max
        return max(0, min(requested, platform_max))

    def purge_expired(
        self, *, job_retention_days: int = 30, raw_prompt_retention_days: int = 7
    ) -> dict[str, int]:
        """보존 기간이 지난 것을 정리한다.

        원문 암호문과 잡 본체를 **다른 주기로** 지운다 — 마스킹본은 프롬프트 개선의
        재료라 오래 두되, 원문은 짧게 두는 것이 거버넌스의 요구다.

        원문 주기는 **테넌트마다 다르다.** 설정 화면에 있는 값이 실제로 아무것도 안
        하면 관리자는 설정했다고 믿는 채로 보관 기간을 어긴다 — 그 쪽이 설정이 아예
        없는 것보다 나쁘다. `raw_prompt_retention_days` 는 그 상한이다.
        """
        now = self._now()
        job_cutoff = now - job_retention_days * 86400

        # 테넌트별 주기를 하나로 묶어 한 번씩만 실행한다. 테넌트가 많아도 쿼리는
        # 서로 다른 주기의 수만큼만 늘어난다.
        by_days: dict[int, list[str]] = {}
        for row in self._conn.execute("SELECT id FROM tenants"):
            days = self.effective_raw_retention_days(row["id"], raw_prompt_retention_days)
            by_days.setdefault(days, []).append(row["id"])

        cipher = 0
        with self._tx():
            for days, tenant_ids in by_days.items():
                for chunk in _chunks(tenant_ids, SQL_VARIABLE_LIMIT):
                    placeholders = ",".join("?" * len(chunk))
                    cipher += self._conn.execute(
                        "UPDATE jobs SET prompt_cipher = NULL, prompt_nonce = NULL "
                        f"WHERE prompt_cipher IS NOT NULL AND tenant_id IN ({placeholders}) "
                        "AND created_at < ?",
                        (*chunk, now - days * 86400),
                    ).rowcount

            # 테넌트 행이 이미 지워진 고아 암호문은 플랫폼 상한으로 정리한다.
            cipher += self._conn.execute(
                "UPDATE jobs SET prompt_cipher = NULL, prompt_nonce = NULL "
                "WHERE prompt_cipher IS NOT NULL AND created_at < ? "
                "AND tenant_id NOT IN (SELECT id FROM tenants)",
                (now - raw_prompt_retention_days * 86400,),
            ).rowcount

            # **상태 목록을 손으로 적지 않는다.** `needs_review` 가 이 목록에
            # 없어서 그 잡들이 영원히 안 지워지고 있었다 — 새 종결 상태를
            # 추가할 때마다 여기를 함께 고쳐야 한다는 것은 규율이고, 규율은 깨진다.
            statuses = sorted(RETAINABLE_STATUSES)
            placeholders = ",".join("?" * len(statuses))
            jobs = self._conn.execute(
                f"DELETE FROM jobs WHERE status IN ({placeholders}) "
                "AND finished_at IS NOT NULL AND finished_at < ?",
                (*statuses, job_cutoff),
            ).rowcount
            usage = self._conn.execute("DELETE FROM usage WHERE ts < ?", (job_cutoff,)).rowcount
            events = self._conn.execute(
                "DELETE FROM filter_events WHERE ts < ?", (job_cutoff,)
            ).rowcount

            # **감사와 평가 이력도 자란다.** 플랫폼 개요 화면이 호출마다 감사를
            # 남기므로 대시보드를 열어 두기만 해도 무한 증식한다. 감사는 잡보다
            # 오래 보관하되(규제 대응) 상한은 있어야 한다.
            audits = self._conn.execute(
                "DELETE FROM admin_audit WHERE ts < ?",
                (now - AUDIT_RETENTION_DAYS * 86400,),
            ).rowcount
            evals = self._conn.execute(
                "DELETE FROM eval_runs WHERE ts < ?",
                (now - EVAL_RUN_RETENTION_DAYS * 86400,),
            ).rowcount

        return {
            "prompt_cipher": cipher, "jobs": jobs, "usage": usage,
            "filter_events": events, "admin_audit": audits, "eval_runs": evals,
        }

    # -- 파기 (C6) ------------------------------------------------------------

    def purge_end_user(
        self, scope: TenantScope, end_user_hash: str, *, actor: str = "tenant_admin"
    ) -> dict[str, int]:
        """엔드유저 파기. **그 사용자의 데이터만 지운다.**

        `filter_events` 는 남긴다. 값을 저장하지 않는 테이블이라 지워진 잡과의
        연결이 끊긴 시점에 더는 그 사람의 데이터가 아니고, 지우면 가드 품질 통계가
        파기 요청 한 건에 왜곡된다 — 오탐률이 규칙을 켜고 끄는 근거이므로
        그 왜곡이 곧 필터 정책의 왜곡이 된다.
        """
        where, params = self._scoped_where(scope, "end_user_hash = ?")
        params.append(end_user_hash)

        # 지우기 **전에** 잡 id 를 모은다. 지운 뒤에는 어떤 이벤트가 이 사람 것이었는지
        # 알 방법이 없다.
        job_ids = [
            row["id"] for row in self._conn.execute(f"SELECT id FROM jobs WHERE {where}", params)
        ]

        # **한 트랜잭션이다.** 중간에 실패해 절반만 지워지면 그것은 파기가 아니고,
        # 요청자에게는 파기됐다고 답한 뒤다.
        with self._tx():
            jobs = self._conn.execute(f"DELETE FROM jobs WHERE {where}", params).rowcount
            usage = self._conn.execute(f"DELETE FROM usage WHERE {where}", params).rowcount

            # 이벤트 자체는 남기되 job_id 는 끊는다. 지워진 잡을 가리키는 식별자가 남으면
            # 그 사람의 요청 하나하나를 다시 묶어 셀 수 있고, 그건 파기가 아니다.
            #
            # **IN 절을 쪼갠다.** SQLite 의 파라미터 상한(빌드에 따라 999~32766)을
            # 넘으면 `too many SQL variables` 로 통째로 실패한다 — 잡이 많은
            # 엔드유저일수록, 즉 파기가 가장 중요한 경우일수록 실패한다.
            events = 0
            for chunk in _chunks(job_ids, SQL_VARIABLE_LIMIT):
                placeholders = ",".join("?" * len(chunk))
                events += self._conn.execute(
                    f"UPDATE filter_events SET job_id = NULL WHERE job_id IN ({placeholders})",
                    chunk,
                ).rowcount

        # 무엇을 지웠는지가 아니라 **언제·누가·얼마나** 지웠는지만 남긴다.
        # 감사가 새 유출 경로가 되면 나머지 노력이 무의미해진다.
        self.audit(
            actor, "purge_end_user", tenant_id=scope.tenant_id,
            detail={"jobs": jobs, "usage": usage},
        )
        return {"jobs": jobs, "usage": usage, "filter_events_unlinked": events}

    def purge_tenant(self, scope: PlatformScope, tenant_id: str) -> dict[str, int]:
        """테넌트 파기.

        행을 지우는 것과 별개로, **DEK 폐기가 가장 강한 삭제다** — 백업에 남은 암호문도
        복호화할 수 없게 된다(crypto-shredding). 그 처리는 crypto 레이어의 몫이고
        여기서는 래핑된 DEK 를 지운다.
        """
        if not isinstance(scope, PlatformScope):
            raise ScopeViolation("테넌트 파기는 PlatformScope 를 요구한다")

        counts: dict[str, int] = {}
        # **한 트랜잭션이다.** 테이블 아홉 개를 지우다 중간에 실패하면 절반만
        # 파기된 테넌트가 남고, DEK 는 아직 살아 있어 crypto-shredding 도 안 된다.
        with self._tx():
            for table in (
                "jobs", "usage", "filter_events", "role_overrides", "tenant_guard_rules",
                "tenant_settings", "eval_fixtures", "tokens", "services",
            ):
                counts[table] = self._conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,)
                ).rowcount

            self._conn.execute(
                "UPDATE tenants SET status='purged', dek_wrapped=NULL, purged_at=? WHERE id=?",
                (self._now(), tenant_id),
            )

        self.audit(
            scope.actor, "purge_tenant", tenant_id=tenant_id,
            detail={"reason": scope.reason, **counts},
        )
        return counts

    def close(self) -> None:
        self._conn.close()


# ── 행 ↔ 객체 ───────────────────────────────────────────────────────────────

_JOB_FIELD_MAP = {
    "allowed_boundaries": ("allowed_boundaries_json", lambda v: _json(list(v))),
    "placement": ("placement_json", lambda v: _json(list(v))),
    "tier_models": ("tier_models_json", lambda v: _json(dict(v))),
    "options": ("options_json", lambda v: _json(dict(v))),
    "metadata": ("metadata_json", lambda v: _json(dict(v))),
    "metrics": ("metrics_json", lambda v: _json(dict(v)) if v is not None else None),
}

_JOB_DIRECT_FIELDS = frozenset(
    {
        "status", "priority", "prompt_masked", "prompt_cipher", "prompt_nonce",
        "system_masked", "prompt_external", "system_external",
        "prompt_hash", "system_hash", "response", "error", "error_code",
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
        prompt_external=get("prompt_external"),
        system_external=get("system_external"),
        allowed_boundaries=tuple(
            loads("allowed_boundaries_json", ["internal", "external"])
        ),
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
