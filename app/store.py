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
import hashlib
import json
import sqlite3
import time
import uuid

from .tokens import estimate_outbound_tokens
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

SCHEMA_VERSION = 1

#: 예산 롤링 창(일). 달력 월이 아니라 롤링인 이유는 설치처의 시간대·회계 월을
#: 모르는데 달력 월을 가정하면 월초에 예산이 통째로 리셋되는 절벽이 생기기 때문이다.
#:
#: **보존 정리가 이 창을 침범하면 안 된다.** 사용량 행이 창보다 먼저 지워지면
#: `spend_since` 가 조용히 과소 계상하고 예산이 남은 것처럼 보인다 — 두 값이
#: 우연히 둘 다 30일이라 지금까지 안 드러났을 뿐이다. 여기 두는 이유는 이 모듈이
#: 어느 앱 모듈도 임포트하지 않는 바닥 레이어이기 때문이다.
BUDGET_WINDOW_DAYS = 30

#: `claim_queued` 가 **의도적으로 안 읽는** 잡 컬럼.
#:
#: 스캔 창(`scan_window_per_lane`, 기본 50)만큼 매 틱 읽으므로 큰 값을 넣으면
#: 배치 결정과 무관한 바이트를 초당 수십 번 옮긴다. 실측으로 200KB 프롬프트
#: 51건이면 텍스트를 같이 읽는 것만 60ms 다. 여기 있는 것은 전부 **디스패치
#: 이전에는 쓸 일이 없는** 값이다:
#:
#: - 프롬프트 텍스트·암호문 — 스케줄러는 프롬프트를 안 본다. 비용 상한에 필요한
#:   토큰 수는 제출 시 재서 `input_tokens_estimate` 에 넣어 뒀다. 실행 직전
#:   `get_job` 이 본문을 꺼낸다.
#: - 응답 계열 — 아직 응답이 없는 `queued` 잡만 뽑으므로 언제나 NULL 이다.
#: - `error` — 재시도 판단은 `attempts`·`wait_*` 로 하고 메시지는 안 본다.
#:
#: **"안 읽으니까 뺀다" 는 검사받는다** — `test_the_scheduler_never_reads_a_blind_column`
#: 이 `app/scheduler.py` 를 파싱해 여기 적힌 컬럼을 읽는지 본다. 한때
#: 마스킹본이 여기 있었고 `_longest_outbound` 가 그것을 읽었다. 예외는 안 났다.
#: 큐를 지난 모든 잡의 입력 토큰이 예약에서 조용히 `0` 이 됐을 뿐이다.
SCHEDULER_BLIND_COLUMNS = frozenset({
    "prompt_masked",
    "prompt_external",
    "system_masked",
    "system_external",
    "prompt_cipher",
    "prompt_nonce",
    "response",
    "response_cipher",
    "response_nonce",
    "error",
})

#: 멱등성 키가 사는 시간(시). 업계 관행(24시간)을 따른다.
#:
#: **잡 보존(30일)과 같이 두면 안 된다.** 소비자가 한 달 뒤 같은 키를 다시 쓰면
#: 그때는 새 작업을 원하는 것이지 옛 응답을 원하는 것이 아니고, 그 사이에 프롬프트도
#: 모델도 바뀌었을 수 있다. 창을 넘긴 키는 지워서 다음 요청이 새 잡을 만들게 한다.
IDEMPOTENCY_TTL_HOURS = 24

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

#: 내보내기 한 종류당 행 상한. 512MB 프로파일에서 전량 적재가 프로세스를 죽인다.
#: 잘린 사실은 결과에 실어 **조용히 자르지 않는다.**
EXPORT_ROW_LIMIT = 50_000

#: 검토를 마친 가드 이벤트의 보존. 승격 게이트의 표본이므로 잡보다 길다.
REVIEWED_EVENT_RETENTION_DAYS = 180

#: 감사 보존. 잡보다 길게 둔다(규제 대응) — 다만 상한은 있어야 한다.
AUDIT_RETENTION_DAYS = 365

#: 보존 정리가 끊어 낸 자리의 해시. `meta` 에 산다.
AUDIT_ANCHOR_KEY = "audit_chain_anchor"

#: 마지막으로 밖으로 내보낸 시점의 팁. **재계산을 잡는 유일한 근거다.**
AUDIT_EXPORTED_TIP_KEY = "audit_exported_tip"

#: 체인의 첫 고리. 실제 해시가 아니라 **시작 표시**다 — 첫 행의 `prev_hash` 가 NULL 이면
#: 유일 인덱스가 안 걸리고(SQLite 에서 NULL 은 서로 다르다), 그러면 여러 워커가 각자
#: "내가 첫 행" 이라고 주장하는 포크가 생긴다.
AUDIT_GENESIS = "genesis"

#: 포크 경합에서 졌을 때 다시 시도할 횟수. 워커 수만큼만 지면 되므로 넉넉하다.
AUDIT_CHAIN_RETRIES = 8

#: 해시 입력의 필드 구분자. **JSON 이 만들 수 없는 문자여야 한다** — 구분자가 데이터에
#: 나타날 수 있으면 공격자가 필드 경계를 옮기면서 같은 해시를 유지할 수 있다.
#: `json.dumps` 는 0x20 미만 제어문자를 `\uXXXX` 로 이스케이프하므로 0x1f 는 안 나온다.
_AUDIT_FIELD_SEP = "\x1f"


def _chain_broken(row: Any, reason: str, checked: int, unchained: int) -> dict[str, Any]:
    """어긋난 자리 하나를 사람이 읽을 수 있게. **id 와 시각과 행위자를 같이 준다** —
    "체인이 깨졌습니다" 만으로는 아무도 다음 행동을 못 정한다."""
    return {
        "ok": False,
        "checked": checked,
        "unchained": unchained,
        "tip": None,
        "broken_at": {
            "id": row["id"], "ts": row["ts"],
            "actor": row["actor"], "action": row["action"],
        },
        "reason": reason,
    }


def audit_row_hash(
    prev_hash: str,
    *,
    ts: float,
    tenant_id: str | None,
    actor: str,
    action: str,
    target: str | None,
    detail_json: str,
    outcome: str,
) -> str:
    """이 행의 해시. **앞 고리를 포함하므로 한 행만 고쳐도 뒤가 전부 어긋난다.**

    `id` 는 안 넣는다. AUTOINCREMENT 라 삽입 전에는 모르고, 넣으려면 삽입 뒤 UPDATE 를
    해야 하는데 그것은 "쓰고 나면 안 고친다" 는 이 테이블의 성질과 정면으로 어긋난다.
    행의 위치는 `prev_hash` 연결이 이미 정한다.
    """
    payload = _AUDIT_FIELD_SEP.join(
        (
            prev_hash,
            f"{ts:.6f}",
            tenant_id or "",
            actor,
            action,
            target or "",
            detail_json,
            outcome,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
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

    -- 응답: 프롬프트와 **같은 모양**이다. 마스킹본은 평문, 원문은 암호문.
    --
    -- 입력만 거르고 출력을 안 거르면 제품의 한 문장("나가는 프롬프트에서 개인정보를
    -- 걸러낸다")이 절반만 참이다. 요약·추출 작업의 산출물 자체가 개인정보이거나,
    -- 모델이 마스킹되지 않은 문맥을 재구성하는 경로가 실재한다.
    response          TEXT,
    response_cipher   BLOB,
    response_nonce    BLOB,
    error             TEXT,
    error_code        TEXT,

    -- 생성 시점 스냅샷 (재현성)
    placement_json    TEXT NOT NULL DEFAULT '[]',
    tier_models_json  TEXT NOT NULL DEFAULT '{}',
    options_json      TEXT NOT NULL DEFAULT '{}',
    timeout_s         INTEGER NOT NULL DEFAULT 120,
    max_prompt_chars  INTEGER,

    -- 제출 시 잰 입력 토큰 상한(경계별 마스킹본 중 큰 쪽).
    --
    -- 값이 아니라 **위치**가 요점이다. 스케줄러는 매 틱 스캔 창만큼 잡을 훑는데,
    -- 추정에 텍스트가 필요하면 그 창의 프롬프트 전량을 매 틱 읽어야 한다
    -- (200KB 프롬프트 51건에서 60ms, 실측). 필요한 것은 숫자 한 칸이다.
    input_tokens_estimate INTEGER,

    -- 디스패치 시점 결정 (노드 헬스가 런타임 상태라 불가피)
    node              TEXT,
    model             TEXT,
    tier              TEXT,
    last_failed_node  TEXT,

    attempts          INTEGER NOT NULL DEFAULT 0,
    wait_reason       TEXT,
    wait_since        REAL,

    -- 멱등성 키. 소비자가 준 값이며 **(테넌트, 서비스) 안에서만 유일하다.**
    -- 네트워크 단절 뒤 재시도하는 소비자가 같은 작업을 두 번 만들지 않게 한다 —
    -- metered 경로면 그 중복이 곧 이중 과금이다.
    idempotency_key   TEXT,

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

-- 관리 감사. `prev_hash`·`row_hash` 가 순차 해시 체인을 만든다.
--
-- **이 체인이 만드는 성질은 "조작하면 드러난다" 이지 "조작할 수 없다" 가 아니다.**
-- DB 에 쓸 수 있는 공격자는 체인 전체를 다시 계산할 수 있다. 그래서 이 컬럼들만으로
-- 무결성을 주장하면 과장이고, 진짜 무결성은 **밖으로 내보낸 사본**에서 나온다
-- (`export_audit_chain`). 재계산은 그 사본과의 대조에서 걸린다.
--
-- 두 컬럼이 NULL 인 행은 체인 도입 **이전**에 쓰인 것이다. 소급해 채우지 않는다 —
-- 소급 계산은 "그때 이 값이었다" 를 증명하지 못하고, 증명하지 못하는 것을 증명한 것처럼
-- 보이게 만드는 쪽이 아예 없는 것보다 나쁘다.
CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    tenant_id   TEXT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    outcome     TEXT NOT NULL DEFAULT 'ok',
    prev_hash   TEXT,
    row_hash    TEXT
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

-- 노드 점유 장부. 슬롯 하나 = 행 하나다.
--
-- 레이트리밋 카운터와 **같은 이유로** 프로세스 메모리에 두지 않는다. 예약이 워커
-- 메모리에 있으면 워커 N개는 장부 N개고, `max_concurrent=1` 노드에 N건이 동시에
-- 올라간다. 한도가 조용히 곱해지는 것은 제품에서 버그다.
--
-- `expires_at` 이 이 설계의 핵심이다. 만료가 없으면 DB 장부는 인메모리 장부보다
-- **나쁘다** — 인메모리 예약은 워커가 죽으면 같이 사라지지만, 행은 남아서 노드가
-- 영원히 가득 찬 것처럼 보인다. 만료 시각은 역할의 `timeout` 에서 나온다: 요청
-- 자신이 그 시각을 넘겨 살아 있을 수 없으므로 예약도 그 시각을 넘길 이유가 없다.
CREATE TABLE IF NOT EXISTS node_leases (
    id          TEXT PRIMARY KEY,
    node        TEXT NOT NULL,
    mem_gb      REAL NOT NULL DEFAULT 0,
    acquired_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_leases ON node_leases(node, expires_at);
"""

#: ADD COLUMN 전용 마이그레이션. 추가·NULL 기본값만 허용하고 재작성·삭제는 금지한다.
#: SQLite 의 ADD COLUMN 은 메타데이터 연산이라 WAL 라이브 DB 에서 안전하다.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (테이블, 컬럼, 타입+기본값)  — 예: ("jobs", "foo", "TEXT")
    #
    # 출력 축. 기존 DB 에는 응답이 평문 그대로 들어 있고, 그것을 소급해 마스킹하지
    # 않는다 — 마스킹은 원문을 지우는 연산이라 되돌릴 수 없고, 봉인할 원문도 이미
    # 없다. 새 응답부터 적용되고 옛 응답은 잡 보존 기간이 지나면 사라진다.
    ("jobs", "response_cipher", "BLOB"),
    ("jobs", "response_nonce", "BLOB"),
    ("jobs", "idempotency_key", "TEXT"),
    # 감사 해시 체인. 옛 행은 NULL 로 남는다 — 소급 계산은 "그때 이 값이었다" 를
    # 증명하지 못하면서 증명한 것처럼 보이게 만든다.
    ("admin_audit", "prev_hash", "TEXT"),
    ("admin_audit", "row_hash", "TEXT"),
    # 라우팅 판정의 스냅샷. NULL = 라우팅을 안 켰거나 분류가 실패했다 = 기본 모델.
    # 두 경우를 구분하는 컬럼을 따로 두지 않는 이유: 둘 다 결과가 같고, 왜 실패했는지는
    # 메트릭(route_failures)이 답한다.
    ("jobs", "route", "TEXT"),
    # 제출 시 잰 입력 토큰 상한. 스케줄러가 매 틱 프롬프트를 다시 읽지 않게 하려고
    # 둔다 — 옛 행은 NULL 이고 `_backfill_input_token_estimates` 가 채운다.
    ("jobs", "input_tokens_estimate", "INTEGER"),
)

#: 컬럼이 생긴 **뒤에** 만들어야 하는 인덱스. `_SCHEMA` 에 두면 옛 DB 에서
#: 컬럼보다 먼저 실행돼 죽는다 — 스키마는 마이그레이션보다 앞서 돌기 때문이다.
_POST_MIGRATION_INDEXES: tuple[str, ...] = (
    # **멱등성의 강제 지점.** 애플리케이션에서 "먼저 조회하고 없으면 삽입" 하면
    # 두 워커가 동시에 조회를 통과한다 — 다중 워커가 지원 구성이므로 그 창은
    # 실제로 열린다. 유일성은 DB 가 지켜야 프로세스를 넘는다.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
    "ON jobs(tenant_id, service_id, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    # **체인이 갈라지는 것을 DB 가 막는다.** 같은 이유, 같은 수법이다.
    #
    # 감사 기록은 "팁을 읽고 거기 이어 붙인다" 인데, 두 워커가 같은 팁을 나란히
    # 읽는 창이 실재한다(다중 워커가 지원 구성이다). 그러면 같은 `prev_hash` 를 가진
    # 행이 둘 생기고 체인이 포크된다 — 그 상태는 변조와 구분되지 않아서, 검증이
    # 정상 운영을 사고로 신고하게 된다.
    #
    # 유일 인덱스를 걸면 진 쪽이 삽입에 실패하고, 팁을 다시 읽어 잇는다.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_chain "
    "ON admin_audit(prev_hash) WHERE prev_hash IS NOT NULL",
)

#: 원문을 담은 컬럼. **내보내기와 백업에서 함께 빠진다.**
#:
#: 손으로 두 곳에 적으면 새 암호문 컬럼을 추가할 때 한쪽을 빠뜨린다 — 그리고
#: 빠뜨린 쪽이 원문을 파일로 내보내는 쪽이면, 보관 기간과 접근 감사가 그 파일
#: 밖에서 통째로 무력화된다. 실제로 출력 축을 넣으면서 두 컬럼이 늘었다.
CIPHER_COLUMNS: frozenset[str] = frozenset(
    {"prompt_cipher", "prompt_nonce", "response_cipher", "response_nonce"}
)

#: "원문이 아직 남아 있는 행" 조건. 보존 정리가 프롬프트만 보고 있으면 응답 원문이
#: 보관 기간을 넘겨 살아남는다 — 지워진 줄 알았던 원문이 남는 것이 가장 나쁜 실패다.
_HAS_CIPHER = "(prompt_cipher IS NOT NULL OR response_cipher IS NOT NULL)"


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
    idempotency_key: str | None = None
    #: 제출 시점에 고정된 라우팅 판정. 재시도해도 안 바뀐다 — 디스패치마다 다시
    #: 판정하면 같은 잡이 재시도마다 다른 모델을 타고 재현성이 사라진다.
    route: str | None = None
    response: str | None = None
    response_cipher: bytes | None = None
    response_nonce: bytes | None = None
    error: str | None = None
    error_code: str | None = None
    placement: tuple[str, ...] = ()
    tier_models: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: int = 120
    max_prompt_chars: int | None = None
    #: 제출 시 잰 입력 토큰 상한. 스케줄러가 비용을 예약할 때 읽는 값이다.
    #: 업그레이드 이전 행은 마이그레이션이 채운다.
    input_tokens_estimate: int = 0
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

    **다중 워커는 지원 구성이다** — API 워커 N 개 + 스케줄러 싱글턴이 같은 호스트에서
    이 파일을 공유한다. 계약 전문은 `docs/architecture.md` §11 에 있고, 이 저장소가
    지는 몫은 넷이다:

    1. 잡 상태 전이는 `update_job(..., expect_status=...)` **CAS 로만** 한다.
       진 쪽은 갱신하지 않고 `False` 를 받는다.
    2. 노드 용량은 `node_leases` 가 지킨다. `try_acquire_node_lease()` 가 쓰기
       트랜잭션 안에서 용량을 재확인하며 삽입한다 — 진 쪽은 `False` 를 받는다.
    3. 다중 문장 쓰기는 `_tx()` 안에서만 — 실패하면 되돌린다.
    4. 테넌트 조건은 `_scoped_where()` 한 곳에서만 붙는다.

    **이 계약은 다중 프로세스에서 검증됐다** — `tests/test_multiprocess.py` 가 진짜
    프로세스로 CAS·롤백·슬롯 장부·리스 만료를 잰다. 한동안 이 독스트링은 검증 없이
    지원을 단언했고, 그것이 설계 감사에서 지적된 결함이었다.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._now = now
        #: `_scheduler_columns` 의 결과. 스키마는 기동 시 한 번 정해지므로 매 틱
        #: `PRAGMA` 를 때릴 이유가 없다.
        self._scheduler_columns_cache: "tuple[str, ...] | None" = None
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
        for statement in _POST_MIGRATION_INDEXES:
            self._conn.execute(statement)
        self._backfill_input_token_estimates()
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def _backfill_input_token_estimates(self) -> None:
        """업그레이드 이전 잡의 입력 토큰 추정치를 채운다.

        **아직 디스패치될 수 있는 잡만** 채운다. 끝난 잡은 이 값을 아무도 안 읽고,
        보존 기간이 지나면 사라진다 — 전량을 다시 훑는 것은 업그레이드를 느리게 할
        뿐이다. 반대로 `queued`·`running` 인 잡을 안 채우면 그 잡들의 입력 토큰이
        예약에서 `0` 으로 계상된다. 그 조용한 `0` 이 이 컬럼을 만든 이유다.

        비어 있어도(둘 다 NULL) `0` 을 써 둔다 — NULL 로 두면 "안 잰 것" 과
        "재 보니 0" 이 다시 구분되지 않는다.
        """
        rows = self._conn.execute(
            "SELECT id, prompt_masked, system_masked, prompt_external, system_external "
            "FROM jobs WHERE input_tokens_estimate IS NULL "
            "AND status IN ('queued', 'running')"
        ).fetchall()
        if not rows:
            return
        self._conn.executemany(
            "UPDATE jobs SET input_tokens_estimate = ? WHERE id = ?",
            [
                (
                    estimate_outbound_tokens(
                        row["prompt_masked"], row["system_masked"],
                        row["prompt_external"], row["system_external"],
                    ),
                    row["id"],
                )
                for row in rows
            ],
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
        """마지막 사용 시각. **커밋한다.**

        커밋 없는 쓰기는 열린 트랜잭션으로 남아 다음 커밋까지 WAL 쓰기 락을
        붙잡는다. 요청마다 일어나는 쓰기라서, 다중 프로세스 구성에서는 그
        붙잡음이 스케줄러의 쓰기를 기다리게 만든다.
        """
        with self._tx():
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
            "idempotency_key": fields.pop("idempotency_key", None),
            "route": fields.pop("route", None),
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

        # **여기서 한 번만 잰다.** 호출자가 넘기는 값이 아니라 저장되는 텍스트에서
        # 유도하므로 둘이 어긋날 수 없다 — 파이프라인이 깜박해도 채워진다.
        row["input_tokens_estimate"] = estimate_outbound_tokens(
            row["prompt_masked"], row["system_masked"],
            row["prompt_external"], row["system_external"],
        )

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

    def queue_position(
        self, scope: TenantScope, *, lane: str, priority: int,
        created_at: float, job_id: str,
    ) -> int:
        """이 잡 앞에 몇 건이 있는가. **행을 끌어오지 않고 센다.**

        예전에는 대기 잡을 최대 500건 `SELECT *` 로 가져와 파이썬에서 셌다. 그
        쿼리는 마스킹본·암호문·응답까지 끌어오는데 폴링이 보는 것은 개수 하나뿐이고,
        그래서 **폴 한 번의 원가가 큐 깊이에 비례했다** — 실측으로 깊이 0 에서
        0.72ms, 1000 에서 28ms 였다.

        하필 그것이 이 시스템의 유일한 파국 경로를 **증폭한다**: 클러스터가 포화되면
        큐가 늘고, 큐가 늘면 대기 잡이 늘고, 대기 잡이 늘면 폴링이 느는데, 그 폴링이
        큐 깊이에 비례해 비싸진다. 적응형 `retry_after` 가 damp 하려던 바로 그
        되먹임에 gain 을 얹고 있었던 셈이다.

        정렬 기준은 `priority DESC, created_at ASC` — 스케줄러가 꺼내는 순서와 같다.
        `idx_jobs_queue` 가 그 순서 그대로라 이 COUNT 는 인덱스만 훑는다.

        상한도 사라졌다. 500건에서 자르면 그 너머는 전부 "500번째" 로 보였다.
        """
        where, params = self._scoped_where(
            scope,
            "status = 'queued' AND lane = ? AND id != ? "
            "AND (priority > ? OR (priority = ? AND created_at < ?))",
        )
        params.extend([lane, job_id, priority, priority, created_at])
        row = self._conn.execute(
            f"SELECT COUNT(*) AS ahead FROM jobs WHERE {where}", params
        ).fetchone()
        return int(row["ahead"])

    def route_counts(self) -> list[tuple[str, str | None, int]]:
        """(역할, 라우트, 건수). `route` 가 `None` 인 행도 담는다.

        **인메모리 카운터를 두지 않는 이유는 슬롯 장부와 같다** — 워커마다 하나씩
        생기고 재기동이면 사라진다. 잡 행이 이미 판정을 들고 있으므로 세기만 하면 된다.

        라우팅을 켠 역할의 `route IS NULL` 이 곧 라우팅 실패다. 둘을 구분하는 컬럼을
        따로 두지 않은 대가인데, 어느 역할이 라우팅을 켰는지는 설정이 알므로
        호출자가 그 둘을 가른다.

        **테넌트를 가로지른다.** 메트릭에 테넌트 라벨을 안 붙이는 원칙이라 합계만 낸다.
        """
        return [
            (row["role"], row["route"], int(row["n"]))
            for row in self._conn.execute(
                "SELECT role, route, COUNT(*) AS n FROM jobs GROUP BY role, route"
            )
        ]

    # -- 노드 점유 ------------------------------------------------------------
    #
    # **테넌트 스코프가 없는 것이 맞다.** 노드는 플랫폼 자원이고 그 용량은 테넌트를
    # 가로질러 공유된다. `_scoped_where` 를 안 쓰는 소수의 경로 중 하나다.

    def node_occupancy(self, now: float) -> dict[str, tuple[int, float]]:
        """노드별 (점유 슬롯, 예약 메모리 GB). 만료된 리스는 세지 않는다.

        후보를 거르고 순위를 매기는 데 쓰는 **스냅샷**이다. 이 값으로 확정하면 안
        된다 — 읽고 나서 쓰기까지 사이에 다른 워커가 들어온다. 확정은
        `try_acquire_node_lease()` 가 한 트랜잭션 안에서 다시 확인하며 한다.
        """
        rows = self._conn.execute(
            "SELECT node, COUNT(*) AS n, COALESCE(SUM(mem_gb), 0) AS mem "
            "FROM node_leases WHERE expires_at > ? GROUP BY node",
            (now,),
        ).fetchall()
        return {row["node"]: (int(row["n"]), float(row["mem"])) for row in rows}

    def try_acquire_node_lease(
        self,
        *,
        lease_id: str,
        node: str,
        mem_gb: float,
        now: float,
        ttl_seconds: float,
        max_concurrent: int,
        mem_budget_gb: float | None,
    ) -> bool:
        """슬롯을 잡는다. **용량 재확인과 삽입이 한 트랜잭션 안이다.**

        "먼저 세어 보고 자리가 있으면 넣는다" 를 애플리케이션에서 하면 다중 워커에서
        반드시 진다 — 두 워커가 나란히 카운트를 통과하는 창이 실재한다. 그래서
        재확인을 쓰기 트랜잭션 안으로 넣는다. SQLite 는 라이터를 직렬화하므로 그
        안에서 본 카운트는 삽입 시점의 사실이다.

        진 쪽은 `False` 를 받는다. 예외가 아니다 — 경합에서 지는 것은 정상 경로이고,
        호출자는 그 노드를 후보에서 빼고 다음으로 간다.
        """
        with self._tx():
            # 만료 수확을 여기서 한다. 별도 청소 루프에 맡기면 그 루프가 안 도는
            # 구성(스케줄러 없이 API 워커만)에서 장부가 영원히 새 것처럼 안 보인다.
            self._conn.execute("DELETE FROM node_leases WHERE expires_at <= ?", (now,))

            # **자기 자신은 안 센다.** 삽입이 `INSERT OR REPLACE` 라 같은 `lease_id`
            # 로 다시 잡는 것은 점유를 늘리지 않는다. 안 빼면 워커가 죽고 남긴 자기
            # 리스가 그 잡의 재배치를 막아, `max_concurrent=1` 노드에서 잡이 만료될
            # 때까지 자기 자리를 못 되찾는다.
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(mem_gb), 0) AS mem "
                "FROM node_leases WHERE node = ? AND id != ?",
                (node, lease_id),
            ).fetchone()
            if int(row["n"]) >= max_concurrent:
                return False
            if mem_budget_gb is not None and float(row["mem"]) + mem_gb > mem_budget_gb:
                return False

            self._conn.execute(
                "INSERT OR REPLACE INTO node_leases"
                "(id, node, mem_gb, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (lease_id, node, float(mem_gb), now, now + float(ttl_seconds)),
            )
        return True

    def release_node_lease(self, lease_id: str) -> None:
        """슬롯을 돌려준다. 없는 리스를 놓아도 조용히 넘어간다 — 만료가 먼저
        수확했을 수 있고, 그것은 오류가 아니다."""
        self._conn.execute("DELETE FROM node_leases WHERE id = ?", (lease_id,))
        self._conn.commit()

    def job_by_idempotency_key(
        self, scope: TenantScope, service_id: str, key: str
    ) -> JobRow | None:
        """이 키로 이미 만든 잡. **서비스까지 스코프에 넣는다.**

        키는 소비자가 정한 값이라 테넌트만으로 가르면 한 테넌트의 두 서비스가
        `retry-1` 같은 흔한 값에서 부딪힌다 — 그러면 A 서비스가 B 서비스의 응답을
        받는다. 유일성 인덱스도 같은 세 칸으로 걸려 있다.
        """
        where, params = self._scoped_where(scope, "service_id = ? AND idempotency_key = ?")
        params.extend([service_id, key])
        row = self._conn.execute(
            f"SELECT * FROM jobs WHERE {where}", params
        ).fetchone()
        return _row_to_job(row) if row else None

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

        **이름과 달리 claim 하지 않는다.** 상태 전이 없는 순수 SELECT 다 — 여러
        프로세스가 같은 행을 함께 읽는다. 실제 직렬화는 `_try_dispatch` 가
        `update_job(expect_status="queued")` 로 이겨야 실행에 들어가는 데서 나온다.
        읽기는 겹쳐도 실행은 하나다.

        이름을 안 바꾼 이유는 호출부가 스케줄러 한 곳뿐이고 그쪽 주석이 CAS 를
        설명하기 때문이다. 이름이 계약을 과장한다는 사실을 여기 적어 둔다 —
        진짜 claim(SELECT ... FOR UPDATE 상당)을 기대하고 이 함수를 다른 곳에서
        쓰면 그 자리에서 중복 실행이 난다.

        스케줄러는 전 테넌트를 가로질러 봐야 하므로 테넌트 스코프가 없다.
        무거운 컬럼은 `SCHEDULER_BLIND_COLUMNS` 로만 뺀다 — **빼는 쪽을 이름으로
        적는다**(`_scheduler_columns` 참고).
        """
        rows = self._conn.execute(
            f"SELECT {', '.join(self._scheduler_columns())} "
            "FROM jobs WHERE status = 'queued' AND lane = ? "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (lane, int(limit)),
        )
        return [_row_to_job(r) for r in rows]

    def _scheduler_columns(self) -> tuple[str, ...]:
        """`claim_queued` 가 읽을 컬럼 — **스키마 전체에서 눈감을 것만 뺀다.**

        원래 이 자리에는 컬럼을 하나하나 나열한 SELECT 가 있었다. 그러다
        `route` 컬럼이 추가됐고 이 목록에는 안 들어갔다. `_row_to_job` 은 없는
        컬럼을 조용히 기본값으로 채우므로(`get()`), 스케줄러는 **모든 잡의
        `route` 를 `None` 으로 읽었다** — 라우팅 판정이 잡에는 박히는데 노드까지
        안 갔다. 같은 사고가 이미 한 번 더 있었다: `prompt_masked` 계열이 빠져
        있어서 `_longest_outbound` 가 언제나 빈 문자열을 냈고, 큐를 지난 모든 잡의
        **입력 토큰이 비용 예약에서 빠졌다.**

        둘 다 예외가 아니라 기본값으로 나타났다. 열거식 SELECT 의 실패는 언제나
        이 모양이다 — 안 뽑은 컬럼과 NULL 인 컬럼이 구분되지 않는다.

        그래서 방향을 뒤집는다. 새 컬럼은 **가만히 둬도 들어온다**(안전한 쪽으로
        틀린다). 빼려면 `SCHEDULER_BLIND_COLUMNS` 에 이름을 적어야 하고, 거기 적힌
        컬럼을 스케줄러가 읽으면 `test_the_scheduler_never_reads_a_blind_column`
        이 실패한다.

        `PRAGMA` 로 읽으므로 마이그레이션으로 늘어난 컬럼도 자동으로 따라온다.
        """
        if self._scheduler_columns_cache is None:
            columns = tuple(
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(jobs)")
                if str(row["name"]) not in SCHEDULER_BLIND_COLUMNS
            )
            self._scheduler_columns_cache = columns
        return self._scheduler_columns_cache

    def count_queued(self, lane: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND lane = ?", (lane,)
        ).fetchone()
        return int(row["n"])

    # -- 크래시 복구 ----------------------------------------------------------

    def recover_running_jobs(
        self, metered_nodes: Iterable[str], *, max_retries: int | None = None
    ) -> dict[str, int]:
        """기동 시 `running` 으로 남은 잡을 정리한다.

        단일 백엔드 시절에는 전부 `queued` 로 되돌리면 됐다. 클러스터에서는 다르다 —
        **컨트롤 플레인이 재시작하는 동안 노드는 여전히 추론을 돌리고 있다.**
        재큐되어 다른 노드에 배치되면 같은 작업이 두 번 돌고, 과금 노드면 두 번 청구된다.
        노드에도 클라우드에도 이걸 되돌릴 취소 의미론이 없다.

        그래서 과금 노드에서 돌던 잡은 자동 재큐하지 않고 `needs_review` 로 둔다.
        막지는 못하고 **드러내기만** 한다.
        """
        metered = set(metered_nodes)
        requeued = reviewed = exhausted = 0

        rows = list(
            self._conn.execute("SELECT id, node, attempts FROM jobs WHERE status='running'")
        )

        # **한 트랜잭션이다.** 절반만 복구된 상태로 기동하면 나머지는 `running` 인
        # 채 남아 영원히 아무도 안 건드린다 — 크래시 복구가 그 자체로 사고가 된다.
        with self._tx():
            for row in rows:
                # **재큐도 시도다.** 이 경로가 max_retries 를 안 보면, 기동할
                # 때마다 죽는 잡이 영원히 재큐된다 — 크래시 루프에 빠진 노드가
                # 있으면 그 잡들이 매 기동 재시도되며 계속 자원을 먹는다.
                if max_retries is not None and row["attempts"] + 1 > max_retries:
                    self._conn.execute(
                        "UPDATE jobs SET status='failed', error_code='max_retries', "
                        "wait_reason='crash_recovery_exhausted', finished_at=? WHERE id=?",
                        (self._now(), row["id"]),
                    )
                    exhausted += 1
                elif row["node"] in metered:
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

        return {"requeued": requeued, "needs_review": reviewed, "exhausted": exhausted}

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

    def token_rate(
        self, scope: TenantScope | None = None, *, window_seconds: float = 300.0
    ) -> dict[str, float]:
        """최근 창의 **분당 토큰 처리율**(TPM). `scope` 가 없으면 전 테넌트 합계.

        ### 이것은 한도가 아니다

        레이트리밋은 건/분이고 예산은 달러다. 무료(internal) 경로는 달러가 0 이라
        **200KB 프롬프트 1건과 1KB 1건이 같은 1건**이고, 대형 프롬프트를 던지는
        테넌트가 건수 한도를 지키면서 클러스터를 잠식할 수 있다.

        그렇다고 지금 토큰 상한을 걸 근거는 없다 — 설치처의 분포를 모른다.
        **값을 모르는 채 건 한도는 오탐 규칙과 같은 운명을 맞는다**(관리자가
        꺼버린다). 그래서 먼저 재서 보여주고, 실측 분포를 본 뒤에 상한을 건다.

        창을 두는 이유: 순간값은 튀고 전체 평균은 어제 일을 오늘로 끌고 온다.
        """
        clause = "ts >= ?"
        since = self._now() - window_seconds
        if scope is None:
            where, params = clause, [since]
        else:
            where, params = self._scoped_where(scope, clause)
            params.append(since)

        row = self._conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COUNT(*) AS calls "
            f"FROM usage WHERE {where}",
            params,
        ).fetchone()

        minutes = max(window_seconds / 60.0, 1e-9)
        inbound = float(row["input_tokens"])
        outbound = float(row["output_tokens"])
        return {
            "window_seconds": window_seconds,
            "input_tokens_per_minute": round(inbound / minutes, 2),
            "output_tokens_per_minute": round(outbound / minutes, 2),
            "tokens_per_minute": round((inbound + outbound) / minutes, 2),
            "calls_per_minute": round(row["calls"] / minutes, 2),
        }

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

        `CIPHER_COLUMNS` 는 담지 않는다 — 프롬프트 원문도 응답 원문도. 내보내기
        파일이 원문을 나르면 보관 기간과 접근 감사가 그 파일 밖에서 모두 무력화된다.
        제외 목록을 여기 손으로 적지 않는 이유는 백업도 같은 목록을 쓰기 때문이다.
        """
        # **상한을 둔다.** 잡 수십만 건인 테넌트를 내보내면 전량이 메모리에 올라오고
        # JSON 직렬화가 그것을 한 번 더 복제한다 — 컨트롤 플레인 512MB 프로파일에서
        # 내보내기 한 번이 프로세스를 죽인다.
        #
        # 잘린 사실은 결과에 싣는다(아래 `truncated`). 조용히 자르면 설치처는
        # 그것을 전량으로 믿고 원본을 지운다.
        where, params = self._scoped_where(scope)
        jobs = [
            {
                k: row[k]
                for k in row.keys()
                if k not in CIPHER_COLUMNS
            }
            for row in self._conn.execute(
                f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC LIMIT ?",
                [*params, EXPORT_ROW_LIMIT + 1],
            )
        ]
        def capped(table: str, order: str) -> list[dict[str, Any]]:
            clause, values = self._scoped_where(scope)
            return [
                dict(row)
                for row in self._conn.execute(
                    f"SELECT * FROM {table} WHERE {clause} ORDER BY {order} DESC LIMIT ?",
                    [*values, EXPORT_ROW_LIMIT + 1],
                )
            ]

        usage = capped("usage", "ts")
        events = capped("filter_events", "ts")
        audit = capped("admin_audit", "ts")

        # 어느 한 종류라도 상한을 넘겼으면 그 사실을 싣는다.
        truncated = {
            name: len(rows) > EXPORT_ROW_LIMIT
            for name, rows in (
                ("jobs", jobs), ("usage", usage),
                ("filter_events", events), ("audit", audit),
            )
        }
        jobs = jobs[:EXPORT_ROW_LIMIT]
        usage = usage[:EXPORT_ROW_LIMIT]
        events = events[:EXPORT_ROW_LIMIT]
        audit = audit[:EXPORT_ROW_LIMIT]

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
            # **조용히 자르지 않는다.** 잘린 것을 전량으로 믿고 원본을 지우면
            # 그 데이터는 되돌릴 수 없다.
            "truncated": truncated,
            "row_limit": EXPORT_ROW_LIMIT,
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

    # -- KEK 회전 -------------------------------------------------------------

    def wrapped_deks(self) -> dict[str, bytes]:
        """살아 있는 테넌트의 래핑된 DEK 전부. **회전이 훑는 목록이다.**

        `dek_wrapped IS NULL` 인 행은 안 담는다. 그런 테넌트는 둘 중 하나인데
        둘 다 회전의 대상이 아니다 — KEK 없이 기동해 원문 보관이 꺼져 있거나,
        파기돼 DEK 가 지워졌거나. 목록에 담으면 회전이 "풀 수 없는 DEK 를 만났다"
        고 멈추고, 그 멈춤은 사고가 아닌데 사고처럼 보인다.

        **테넌트 스코프가 없다.** 회전은 플랫폼 운영이고 전 테넌트를 가로지른다.
        """
        return {
            row["id"]: row["dek_wrapped"]
            for row in self._conn.execute(
                "SELECT id, dek_wrapped FROM tenants "
                "WHERE purged_at IS NULL AND dek_wrapped IS NOT NULL"
            )
        }

    def replace_wrapped_deks(self, rewrapped: Mapping[str, bytes], *, actor: str) -> int:
        """새 래핑을 **한 트랜잭션으로** 쓴다.

        절반만 쓰이면 그 테넌트들은 **어느 키로도 안 열린다** — 옛 키는 새로 감싼
        쪽을, 새 키는 아직 옛 것인 쪽을 못 푼다. 이 저장소에서 원자성이 데이터
        손실과 직결되는 몇 안 되는 자리다.

        감사도 같은 커밋 안이다. 회전이 됐는데 기록이 없거나 그 반대면, 유출 대응을
        되짚는 사람이 무엇이 사실인지 판단할 근거를 잃는다. `audit()` 이 자기
        트랜잭션을 커밋하므로 마지막에 두면 래핑 교체와 기록이 함께 나간다.
        """
        with self._tx():
            for tenant_id, wrapped in rewrapped.items():
                self._conn.execute(
                    "UPDATE tenants SET dek_wrapped = ? "
                    "WHERE id = ? AND purged_at IS NULL",
                    (wrapped, tenant_id),
                )
            self.audit(
                actor, "rotate_master_kek",
                detail={"tenants": sorted(rewrapped)},
            )
        return len(rewrapped)

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

        ### 해시 체인

        각 행은 앞 행의 해시를 품는다. 한 행만 고쳐도 그 뒤가 전부 어긋나므로
        **조작이 드러난다** — 조작을 막지는 못한다. 그 구분은 `admin_audit` 스키마
        주석과 `docs/runbook-audit-integrity.md` 에 있다.

        **합치기는 그 행이 체인의 끝일 때만 한다.** 합치기는 행을 고치는 연산이라
        중간 행에 하면 뒤따르는 모든 해시가 어긋나고, 그것은 변조와 구분되지 않는다.
        끝이 아니면(그 사이 다른 사건이 기록됐다는 뜻이다) 새 행을 쓴다 — 그 편이
        정직하기도 하다. 폭주를 막으려던 목적은 대부분의 경우 그대로 달성된다.
        """
        now = self._now()
        detail_json = _json(dict(detail or {}))

        for _ in range(AUDIT_CHAIN_RETRIES):
            tip = self._conn.execute(
                "SELECT id, detail_json, prev_hash, row_hash FROM admin_audit "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = (tip["row_hash"] if tip else None) or AUDIT_GENESIS

            merge_into = None
            if coalesce_seconds > 0 and tip is not None and tip["row_hash"]:
                # 후보를 **팁 하나로 한정한다.** 예전에는 창 안의 가장 최근 행을
                # 찾았는데, 그 행이 끝이 아니면 고치는 순간 체인이 끊긴다.
                merge_into = self._conn.execute(
                    "SELECT id, detail_json, prev_hash FROM admin_audit "
                    "WHERE id = ? AND actor = ? AND action = ? AND ts >= ? "
                    "AND tenant_id IS ? AND target IS ?",
                    (tip["id"], actor, action, now - coalesce_seconds,
                     tenant_id, target),
                ).fetchone()

            if merge_into is not None:
                try:
                    previous = json.loads(merge_into["detail_json"] or "{}")
                except ValueError:
                    previous = {}
                merged = dict(detail or {})
                merged["repeats"] = int(previous.get("repeats", 1)) + 1
                merged_json = _json(merged)
                # 앞 고리는 그대로다 — 이 행의 자리는 안 바뀌고 내용만 바뀐다.
                chained = merge_into["prev_hash"] or AUDIT_GENESIS
                with self._tx():
                    self._conn.execute(
                        "UPDATE admin_audit SET ts = ?, detail_json = ?, row_hash = ? "
                        "WHERE id = ?",
                        (
                            now, merged_json,
                            audit_row_hash(
                                chained, ts=now, tenant_id=tenant_id, actor=actor,
                                action=action, target=target,
                                detail_json=merged_json, outcome=outcome,
                            ),
                            merge_into["id"],
                        ),
                    )
                return

            row_hash = audit_row_hash(
                prev_hash, ts=now, tenant_id=tenant_id, actor=actor, action=action,
                target=target, detail_json=detail_json, outcome=outcome,
            )
            try:
                with self._tx():
                    self._conn.execute(
                        "INSERT INTO admin_audit(ts, tenant_id, actor, action, target, "
                        "detail_json, outcome, prev_hash, row_hash) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (now, tenant_id, actor, action, target, detail_json, outcome,
                         prev_hash, row_hash),
                    )
                return
            except sqlite3.IntegrityError:
                # 다른 워커가 같은 팁에 먼저 이었다. 유일 인덱스가 포크를 막아 줬으니
                # 새 팁을 읽고 그 뒤에 붙는다. **감사 기록을 잃지 않는 것이 우선이다.**
                continue

        raise RuntimeError("감사 체인 경합이 계속됩니다 — 기록하지 못했습니다")

    def list_audit(self, scope: TenantScope, *, limit: int = 100) -> list[sqlite3.Row]:
        where, params = self._scoped_where(scope)
        return list(
            self._conn.execute(
                f"SELECT * FROM admin_audit WHERE {where} ORDER BY ts DESC LIMIT ?",
                [*params, int(limit)],
            )
        )

    # -- 감사 무결성 ----------------------------------------------------------
    #
    # **이 절이 만드는 성질을 정확히 적는다: 조작하면 드러난다.**
    #
    # 조작을 막지는 못한다. DB 에 쓸 수 있는 공격자는 고친 행부터 끝까지 다시 계산해
    # 검증을 통과시킬 수 있다. 그래서 체인만으로 컴플라이언스를 주장하면 과장이고,
    # 진짜 무결성은 **밖으로 내보낸 사본**에서 나온다 — 재계산은 그 사본의 팁과
    # 대조할 때 걸린다. `export_audit_chain` 이 그 사본을 만든다.

    def verify_audit_chain(self, *, limit: int | None = None) -> dict[str, Any]:
        """체인을 처음부터 다시 계산해 **첫 번째 어긋난 자리**를 지목한다.

        전부를 세지 않고 첫 자리만 내는 이유: 한 행이 어긋나면 그 뒤는 전부 어긋나므로
        건수는 정보가 아니다. 사람이 알아야 할 것은 **어디서부터**다.

        해시가 NULL 인 행은 체인 도입 이전 구간이다. 어긋난 것이 아니라 **보증 범위
        밖**이므로 그렇게 센다 — 섞으면 옛 설치처가 업그레이드하자마자 "변조됨" 을 본다.

        `limit` 은 **앞에서부터** 자른다. 뒤 N 개만 보는 선택지는 없다 — 체인은 알려진
        앵커에서 앞으로만 검증할 수 있고, 중간부터 시작하면 그 시작점의 해시를 그냥
        믿는 것이 되어 검증이 아니게 된다. 원가는 행당 약 4.4µs 다(실측, 20만 행에
        875ms). 진단용이므로 전량을 본다.
        """
        anchor = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (AUDIT_ANCHOR_KEY,)
        ).fetchone()
        expected = anchor["value"] if anchor else AUDIT_GENESIS

        sql = (
            "SELECT id, ts, tenant_id, actor, action, target, detail_json, outcome, "
            "prev_hash, row_hash FROM admin_audit ORDER BY id"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"

        unchained = 0
        checked = 0
        tip = None
        for row in self._conn.execute(sql):
            if row["row_hash"] is None:
                unchained += 1
                continue
            recomputed = audit_row_hash(
                row["prev_hash"] or AUDIT_GENESIS,
                ts=row["ts"], tenant_id=row["tenant_id"], actor=row["actor"],
                action=row["action"], target=row["target"],
                detail_json=row["detail_json"], outcome=row["outcome"],
            )
            if row["prev_hash"] != expected:
                return _chain_broken(row, "앞 고리가 끊겼습니다", checked, unchained)
            if recomputed != row["row_hash"]:
                return _chain_broken(row, "행 내용이 해시와 다릅니다", checked, unchained)
            expected = row["row_hash"]
            tip = row["row_hash"]
            checked += 1

        return {
            "ok": True, "checked": checked, "unchained": unchained,
            "tip": tip, "broken_at": None, "reason": None,
        }

    def audit_chain_tip(self) -> str | None:
        row = self._conn.execute(
            "SELECT row_hash FROM admin_audit WHERE row_hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["row_hash"] if row else None

    def export_audit_chain(self, *, since_id: int = 0) -> list[dict[str, Any]]:
        """내보낼 행들. **밖에 사본이 있어야 체인이 의미를 갖는다.**

        `since_id` 로 증분 내보내기를 한다 — 매번 전량을 내보내면 1년치가 쌓인 뒤에는
        아무도 안 돌리고, 안 돌리는 절차는 없는 절차다.
        """
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT id, ts, tenant_id, actor, action, target, detail_json, "
                "outcome, prev_hash, row_hash FROM admin_audit "
                "WHERE id > ? ORDER BY id",
                (int(since_id),),
            )
        ]

    def record_audit_export(self, *, tip: str | None, last_id: int) -> None:
        """무엇을 어디까지 내보냈는지. 다음 검증이 이것과 대조한다."""
        with self._tx():
            for key, value in (
                (AUDIT_EXPORTED_TIP_KEY, _json({"tip": tip, "last_id": last_id})),
            ):
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def last_audit_export(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (AUDIT_EXPORTED_TIP_KEY,)
        ).fetchone()
        if row is None:
            return {"tip": None, "last_id": 0}
        try:
            return json.loads(row["value"])
        except ValueError:
            return {"tip": None, "last_id": 0}

    def audit_export_still_agrees(self) -> bool | None:
        """내보낸 시점의 팁이 **지금도 체인 안에 있는가.**

        `None` 은 내보낸 적이 없다는 뜻이다(판정 불가). `False` 면 그 해시가 사라졌다 —
        누군가 그 지점 이후를 다시 계산했다는 뜻이고, **체인 검증만으로는 절대 못 잡는
        사건이다.** 이것이 내보내기를 체인과 한 묶음으로 내는 이유 그 자체다.
        """
        exported = self.last_audit_export()
        if not exported.get("tip"):
            return None
        found = self._conn.execute(
            "SELECT 1 FROM admin_audit WHERE row_hash = ?", (exported["tip"],)
        ).fetchone()
        return found is not None

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
        with self._tx():
            self._conn.execute(
                "INSERT INTO rate_counters(key, bucket, count) VALUES(?,?,1) "
                "ON CONFLICT(key, bucket) DO UPDATE SET count = count + 1",
                (key, bucket),
            )

    def consume_rate_slots(
        self, checks: Sequence[tuple[str, str, int]], bucket: int, since_bucket: int
    ) -> str | None:
        """세 단계를 **한 트랜잭션 안에서** 전부 검사하고 전부 증가시킨다.

        걸린 단계의 이름을 돌려주고, 통과하면 `None`.

        검사와 증가가 분리돼 있으면 동시 요청이 둘 다 통과한 뒤 둘 다 증가해서
        한도를 조금 넘긴다. 그리고 단계마다 따로 소비하면 3단계에서 걸렸을 때
        1·2단계는 이미 늘어난 채로 남아 **안 받은 요청이 한도를 먹는다.**
        전부 아니면 전무여야 한다.

        SQLite 는 단일 라이터라 이 구간이 직렬화된다.
        """
        with self._tx():
            for scope_name, key, limit in checks:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(count), 0) AS n FROM rate_counters "
                    "WHERE key = ? AND bucket >= ?",
                    (key, since_bucket),
                ).fetchone()
                if int(row["n"]) >= limit:
                    return scope_name
            for _, key, _ in checks:
                self._conn.execute(
                    "INSERT INTO rate_counters(key, bucket, count) VALUES(?,?,1) "
                    "ON CONFLICT(key, bucket) DO UPDATE SET count = count + 1",
                    (key, bucket),
                )
        return None

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
                        "UPDATE jobs SET prompt_cipher = NULL, prompt_nonce = NULL, "
                        "response_cipher = NULL, response_nonce = NULL "
                        f"WHERE {_HAS_CIPHER} AND tenant_id IN ({placeholders}) "
                        "AND created_at < ?",
                        (*chunk, now - days * 86400),
                    ).rowcount

            # 테넌트 행이 이미 지워진 고아 암호문은 플랫폼 상한으로 정리한다.
            cipher += self._conn.execute(
                "UPDATE jobs SET prompt_cipher = NULL, prompt_nonce = NULL, "
                "response_cipher = NULL, response_nonce = NULL "
                f"WHERE {_HAS_CIPHER} AND created_at < ? "
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
            # **예산 창 안의 사용량은 보존 설정보다 우선한다.**
            #
            # 잡 보존을 14일로 줄이면 30일 롤링 예산이 `spend_since` 에서 절반을
            # 잃고, 예산이 남은 것처럼 보인다. 지금까지 둘 다 30일이라 우연히
            # 안 드러났을 뿐이고, 한쪽 상수만 바뀌면 조용히 깨진다.
            usage_cutoff = min(job_cutoff, now - BUDGET_WINDOW_DAYS * 86400)
            usage = self._conn.execute(
                "DELETE FROM usage WHERE ts < ?", (usage_cutoff,)
            ).rowcount

            # **창을 넘긴 멱등성 키를 놓아준다.** 잡 행은 보존 기간까지 남지만
            # 키는 24시간짜리다 — 안 놓아주면 유일성 인덱스가 한 달 전 키를 붙들고
            # 있어서, 같은 키를 다시 쓴 소비자가 한 달 전 응답을 받는다.
            self._conn.execute(
                "UPDATE jobs SET idempotency_key = NULL "
                "WHERE idempotency_key IS NOT NULL AND created_at < ?",
                (now - IDEMPOTENCY_TTL_HOURS * 3600,),
            )

            # **검토를 마친 가드 이벤트는 오래 둔다.** 그것이 승격 게이트의
            # 표본이다 — 잡과 같은 주기로 지우면 승격 가능하던 규칙이 표본 부족으로
            # 되돌아가고, 관리자는 어제 되던 것이 왜 안 되는지 알 수 없다.
            events = self._conn.execute(
                "DELETE FROM filter_events WHERE ts < ? AND reviewed = 0",
                (job_cutoff,),
            ).rowcount
            events += self._conn.execute(
                "DELETE FROM filter_events WHERE ts < ? AND reviewed = 1",
                (now - REVIEWED_EVENT_RETENTION_DAYS * 86400,),
            ).rowcount

            # **감사와 평가 이력도 자란다.** 플랫폼 개요 화면이 호출마다 감사를
            # 남기므로 대시보드를 열어 두기만 해도 무한 증식한다. 감사는 잡보다
            # 오래 보관하되(규제 대응) 상한은 있어야 한다.
            #
            # **정리는 체인을 끊는다 — 끊긴 자리를 앵커로 남긴다.**
            #
            # 옛 행을 지우면 살아남은 첫 행의 `prev_hash` 가 가리키는 행이 없어진다.
            # 그대로 두면 검증이 매년 보존 경계에서 "끊겼다" 고 신고하고, **정상 운영이
            # 사고로 보이는 검증은 곧 꺼진다.** 그래서 지우기 직전 마지막 행의 해시를
            # 적어 두고, 검증은 거기서부터 잇는다.
            audit_cutoff = now - AUDIT_RETENTION_DAYS * 86400
            doomed_tip = self._conn.execute(
                "SELECT row_hash FROM admin_audit WHERE ts < ? AND row_hash IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (audit_cutoff,),
            ).fetchone()
            audits = self._conn.execute(
                "DELETE FROM admin_audit WHERE ts < ?", (audit_cutoff,)
            ).rowcount
            if doomed_tip is not None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (AUDIT_ANCHOR_KEY, doomed_tip["row_hash"]),
                )
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
        "prompt_hash", "system_hash",
        "response", "response_cipher", "response_nonce", "error", "error_code",
        "timeout_s", "max_prompt_chars", "node", "model", "tier", "last_failed_node",
        "attempts", "wait_reason", "wait_since", "cost_reserved_usd", "cost_usd",
        "idempotency_key",
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
        idempotency_key=get("idempotency_key"),
        route=get("route"),
        response=get("response"),
        response_cipher=get("response_cipher"),
        response_nonce=get("response_nonce"),
        error=get("error"),
        error_code=get("error_code"),
        placement=tuple(loads("placement_json", [])),
        tier_models=loads("tier_models_json", {}),
        options=loads("options_json", {}),
        timeout_s=get("timeout_s", 120) or 120,
        max_prompt_chars=get("max_prompt_chars"),
        input_tokens_estimate=get("input_tokens_estimate", 0) or 0,
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
