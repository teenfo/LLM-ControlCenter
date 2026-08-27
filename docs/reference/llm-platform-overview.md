# LLM 플랫폼 정리 — hosub-mcp 기반 재개발 레퍼런스

이 문서는 `hosub-mcp` 저장소의 **LLM 서브시스템 전체**를 한 곳에 정리한 것이다.
새 프로젝트를 이 코드베이스 위에서 디벨롭하기 위한 베이스 문서이므로,
"무엇이 있는가" 뿐 아니라 **"왜 그렇게 만들었고, 무엇을 하지 않기로 했는가"** 까지
담는다. 설계 판단의 근거가 없으면 재개발에서 같은 실수를 반복하게 된다.

- 대상 범위: `llm-gateway/` 전체 + 소비자 4종(hosub MCP·대시보드, TNM, trading, roxlogy)
- 범위 밖: 트레이딩 매매 로직·발굴 엔진 자체(LLM 소비 지점만 다룬다) → `docs/trading/`
- 원전: `docs/requests/llm-gateway-service.md`(설계서), `llm-gateway/README.md`(운영),
  `llm-gateway/docs/integration.md`(소비자 계약)

---

## 1. 한눈에 보기

```
 소비자                                게이트웨이 (단일 관문)              백엔드
┌──────────────────────┐         ┌─────────────────────────────┐    ┌──────────────┐
│ hosub MCP  :8700     │         │  llm-gateway  :8603 (Docker)│    │ 맥 스튜디오   │
│ 대시보드    :8701     │──HTTP──▶│  ├ 인증/레이트리밋           │───▶│ Ollama       │
│ TNM        :8602     │ Bearer  │  ├ 역할 해석(+런타임 override)│Tail│ :11434       │
│ trading    :8600     │         │  ├ 2레인 잡 큐 + 메모리 예산  │scale│ qwen2.5 계열  │
│ roxlogy(Vercel,집밖) │         │  ├ 재시도·영속화(SQLite)      │    │ bge-m3       │
└──────────────────────┘         │  └ 모델 설치 요청·승인·pull   │    └──────────────┘
                                 └─────────────────────────────┘
```

**한 문장**: 어떤 소비자도 Ollama를 직접 부르지 않는다. 모든 추론은 게이트웨이를 거쳐
같은 큐·레인·재시도·사용량 집계를 공유하고, 모델 교체는 `roles.yaml` 한 줄로 끝난다.

### 왜 서비스마다 워커를 두지 않았나

소비자마다 자기 큐·워커·재시도를 만들면 (a) 맥 메모리를 서로 모르고 밟고,
(b) 같은 재시도 로직이 4벌 생기고, (c) 모델 교체가 4개 레포의 PR이 된다.
게이트웨이의 잡 큐가 **공용 워커** 역할을 하므로 소비자는 HTTP 호출만 하면 된다.
컨테이너 추가 없음.

### 규모

| 구성 | LOC | 비고 |
|---|---:|---|
| `llm-gateway/app/` | ~3,930 | main 1399 · meta 697 · store 660 · config 550 · scheduler 500 · ollama 287 · notify 145 · catalog 103 · auth 49 |
| `llm-gateway/client/llmgw.py` | 338 | 소비자가 복사해 쓰는 단일 파일 클라이언트 |
| `llm-gateway/tests/` | 15 파일 | 실제 맥 없이 전부 통과(가짜 백엔드 주입) |
| `src/gateway.py` + `src/tools/llm.py` | 453 | hosub MCP 클라이언트 + MCP 도구 6종 |
| `static/pages/llm.js` + `llm-models.js` | 1,475 | 대시보드 UI 2페이지 |

---

## 2. 파일 지도

```
llm-gateway/
├── app/
│   ├── main.py         라우터 27개(공개 14 + 관리 13) · 인증 게이트 · 앱 조립(build_app 주입식)
│   ├── scheduler.py    2레인 루프 · 메모리 예산 · 모델 친화 · 기아 방지 · 재시도 · 모델 설치 루프
│   ├── store.py        SQLite 6테이블 · 크래시 복구 · 보존 정리 · ADD COLUMN 마이그레이션
│   ├── config.py       roles/services/catalog 로딩 · 런타임 오버라이드 병합 · 필드 검증
│   ├── meta.py         /v1/meta · OpenAPI 3.1 자동 생성(라우트에서 유도, 토큰별로 다름)
│   ├── ollama.py       백엔드 클라이언트(generate/embed/pull/tags/delete) · 재시도 가능 여부 판정
│   ├── catalog.py      모델 카탈로그(mtime 캐시)
│   ├── notify.py       Slack — 상태 전이에서만, 실패를 삼킴, 비밀 미포함
│   └── auth.py         Bearer 토큰 → 서비스 식별 + 슬라이딩 윈도 레이트리밋
├── config/
│   ├── roles.yaml      역할 = 모델 정책 (기본값)
│   ├── services.yaml   소비자 등록 (토큰 환경변수 이름 + allow_roles + rate limit + admin)
│   └── catalog.yaml    설치 후보 모델 큐레이션 목록 (git pull 로 갱신, 재빌드 불필요)
├── client/llmgw.py     동기/비동기 클라이언트 (소비자 레포로 복사)
├── tools/mock_gateway.py  게이트웨이 없이 개발하는 목 서버
├── docs/integration.md    소비자 계약 (게이트웨이가 /v1/integration 으로 직접 서빙)
├── static/docs.html + swagger-ui/   /v1/docs 브라우저 탐색기 (CDN 금지 → 벤더링)
├── compose.yml · Dockerfile · .env.example
└── tests/  15개

src/
├── gateway.py          hosub 쪽 게이트웨이 클라이언트 25함수 (관리 API 포함)
└── tools/llm.py        MCP 도구 6종

static/pages/
├── llm.js              백엔드 상태 · 역할 목록 · 테스트 실행 (조회)
└── llm-models.js       설치 모델 · 카탈로그 · 설치/삭제 · A/B 비교 (변경)

trading/app/llmgw.py · tnm/app/llmgw.py   client/llmgw.py 의 사본 (현재 3개 md5 동일)
deploy/llm-gateway.service · deploy/Caddyfile
```

---

## 3. 계층 구조

```
HTTP (Starlette)
  └ _auth()          Bearer → Service 식별 + rate limit         auth.py
     └ _require_admin()  admin: true 서비스만 (Caddy 404 와 이중)  main.py
        └ 역할 해석    roles.yaml + DB 오버라이드 병합            config.py
           └ 잡 생성   model/options/timeout 스냅샷              store.py
              └ 스케줄러  레인 선택 → 메모리 예산 → 실행/재시도    scheduler.py
                 └ 백엔드  Ollama /api/generate · /api/embed      ollama.py
```

**의존성 주입**이 앱 조립의 기본이다. `build_app(roles=, services=, store=, client=,
scheduler=)` 로 전부 갈아끼울 수 있어서 테스트가 실제 맥 없이 돈다.
`create_app()` 은 환경변수로 기본값을 채우는 얇은 팩토리일 뿐이다.

기술 스택: Python 3.12-slim · Starlette · uvicorn · httpx · PyYAML · SQLite.
**의존성 4개**가 전부다(요구사항 파일 기준). 프레임워크를 얇게 유지한 것이 이 시스템의
이식성을 만든다.

---

## 4. 도메인 모델 4개

### 4.1 역할(Role) = 모델 정책

```yaml
classify_news:
  model: qwen2.5:14b      # 무엇으로 돌릴지
  lane: interactive       # 어느 레인에서
  timeout: 120            # 얼마나 기다릴지 (1~3600)
  options: {temperature: 0.1}
  kind: generate          # generate | embed
  system: "..."           # 선택 — 요청의 system 이 없을 때만 쓰는 기본값
```

**핵심 계약 두 줄:**
- **역할 이름이 계약이고, 모델은 정책이다.** 소비자는 모델명을 하드코딩하지 않는다.
- **프롬프트는 호출자 소유다.** 요청의 `system` 이 우선하고, 없을 때만 역할 기본값을 쓴다.
  덕분에 소비자가 자기 레포에서 프롬프트를 개선하는 데 게이트웨이 PR이 필요 없다.

현재 정의된 역할 9개:

| 역할 | 모델 | 레인 | 소유 | 비고 |
|---|---|---|---|---|
| `summarize` | qwen2.5:7b | interactive | hosub | 기본 system 있음 |
| `log_analyze` | qwen2.5:14b | interactive | hosub | 서버 로그 분석 |
| `translate` | qwen2.5:14b | interactive | hosub | |
| `code` | qwen2.5-coder:14b | interactive | hosub | |
| `general` | qwen2.5:32b | batch | hosub | trading 일지 요약이 사용 |
| `analyze_workout` | qwen2.5:14b | batch | roxlogy | 프롬프트 없음(호출자가 보냄) |
| `coach_feedback` | qwen2.5:32b | batch | roxlogy | 〃 |
| `classify_news` | qwen2.5:14b | interactive | TNM | 7b의 JSON 위반율 ~2%로 상향 |
| `embed` | bge-m3 | — | 공용 | `kind: embed` — 큐를 타지 않는 동기 경로 |

### 4.2 잡(Job) = 모든 요청

`/v1/embed` 를 제외한 모든 요청은 잡이다. 동기/비동기 구분이 없다 — `wait` 초까지
기다렸다가 안 끝나면 `pending` + `job_id` 를 준다. **응답 모양이 항상 같으므로 호출자가
분기할 필요가 없다.**

```jsonc
{
  "job_id": "a1b2c3d4e5f6",   // 항상 있다
  "status": "ok",             // ok | pending | failed | cancelled
  "response": "...",          // ok 일 때
  "error": null,              // failed 일 때
  "role": "analyze_workout", "model": "qwen2.5:14b", "lane": "batch",
  "attempts": 1,
  "metadata": {"session_id": 42},   // 요청에 담은 것이 그대로 되돌아온다
  "queue_position": null,           // pending 일 때 앞에 몇 개
  "created_at": "...", "started_at": "...", "finished_at": "..."
}
```

**잡은 자기완결형이다.** 생성 시점의 `model`·`options`·`timeout` 을 자기 행에 스냅샷한다.
역할 모델을 런타임에 바꿔도 큐에 있던 잡이 "옛 모델 + 새 옵션" 으로 도는 일이 없다.

### 4.3 서비스(Service) = 토큰 하나 = 소비자 하나

```yaml
tnm:
  token_env: LLMGW_TOKEN_TNM     # 토큰 값은 여기 두지 않는다 (환경변수 이름만)
  allow_roles: ["summarize", "classify_news", "translate", "embed"]
  rate_limit_per_min: 120
  admin: false                   # true 는 hosub 하나뿐
```

토큰 = 식별 = 권한 = 사용량 귀속. 세 가지를 한 값이 담당한다.

| 서비스 | allow_roles | rpm | admin |
|---|---|---:|---|
| `hosub` | `*` | 120 | ✅ |
| `roxlogy` | analyze_workout, coach_feedback, summarize | 30 | |
| `tnm` | summarize, classify_news, translate, embed | 120 | |
| `trading` | summarize, general | 60 | |

roxlogy만 30rpm인 이유: Vercel에서 돌아 고정 출발 IP가 없어 IP 허용목록을 만들 수 없다 —
토큰이 유일한 통제이므로 한도를 보수적으로 잡아 **토큰이 새도 소비량 상한이 걸리게** 했다.

### 4.4 모델 설치 요청(ModelRequest)

`pending → approved → pulling → ready` (또는 `rejected` / `failed`).
미설치 모델을 만나면 자동 생성된다. 상세는 8절.

---

## 5. HTTP 계약

### 5.1 공개 엔드포인트 (소비자용)

| 엔드포인트 | 설명 |
|---|---|
| `POST /v1/generate` | 생성. `wait` 0~300초(기본 30). 0이면 즉시 pending |
| `POST /v1/embed` | 임베딩. **유일하게 잡이 아닌 동기 경로**. 배치 최대 256건 |
| `GET /v1/jobs/{id}` | 잡 조회 (본인 서비스 것만) |
| `GET /v1/jobs?status=&limit=` | 잡 목록 |
| `DELETE /v1/jobs/{id}` | 취소 (대기 중인 것만) |
| `GET /v1/roles` | 이 토큰으로 쓸 수 있는 역할·모델·레인·타임아웃 |
| `GET /v1/status` | 백엔드 상태·레인 큐·메모리 예산·사용량·오버라이드 수 |
| `GET /v1/models/requests` · `POST` | 설치 요청 조회 / 승인·거부(admin만) |
| `GET /v1/integration` | 소비자 가이드 마크다운 — **계약의 최신본을 서빙** |
| `GET /v1/meta` | 기계가 읽는 계약 — 역할·한도·오류코드·엔드포인트·클라이언트 해시 |
| `GET /v1/openapi.json` · `.yaml` | OpenAPI 3.1 (토큰별 생성) |
| `GET /v1/client/llmgw.py` · `mock_gateway.py` | 클라이언트 원본 서빙 |
| `GET /v1/docs` | 브라우저 탐색기 (페이지만 무인증, 토큰은 화면 입력) |
| `GET /healthz` | 인증 불필요 |

요청 본문:

```jsonc
{
  "role": "analyze_workout",   // 필수
  "prompt": "...",             // 필수
  "system": "...",             // 선택 — 있으면 역할 기본 프롬프트를 덮는다
  "wait": 30,                  // 선택 — 0~300
  "priority": 0,               // 선택 — 클수록 먼저
  "metadata": {},              // 선택 — 그대로 되돌아온다
  "model": "..."               // ⚠️ 관리 전용. 소비자가 보내면 403
}
```

### 5.2 관리 엔드포인트 (`/v1/admin/*`)

**127.0.0.1:8603 으로만 닿는다.** Caddy가 공개 경로에서 404로 잘라내고, 앱 안에서도
`admin: true` 서비스만 통과시킨다 — 두 겹.

| 엔드포인트 | 설명 |
|---|---|
| `GET·POST·DELETE /v1/admin/roles` | 역할 오버라이드 조회/저장/해제(+ `yaml_snippet` 제공) |
| `GET·DELETE /v1/admin/models` | 설치 모델 목록(용량·사용 역할·최근 사용·삭제 차단 사유) / 삭제 |
| `POST /v1/admin/models/install` | 설치 지시 |
| `GET /v1/admin/catalog?q=` | 내장 카탈로그 검색 |
| `POST·GET /v1/admin/compare[/{id}]` | 모델 A/B |
| `GET /v1/admin/services` | 소비자 등록 현황 + **마스킹된** 토큰 |
| `GET /v1/admin/services/{name}/token` | 토큰 전체 값 **1건**. 열람이 감사에 남는다 |
| `GET /v1/admin/audit` | 관리 작업 감사 로그 |

### 5.3 한도·상수

| 상수 | 값 | 위치 |
|---|---|---|
| `DEFAULT_WAIT` / `MAX_WAIT` | 30 / 300초 | main.py |
| `MAX_PROMPT_CHARS` | 200,000 (역할별 `max_prompt_chars` 로 축소 가능) | main.py |
| `MAX_EMBED_BATCH` | 256 | main.py |
| 역할 `timeout` 상한 | 3600초 | config.py |
| 오버라이드 가능 필드 | `model` `lane` `timeout` `options` `max_prompt_chars` | config.py |
| 오버라이드 **불가** | `kind` `system` | 아래 참조 |

`kind` 를 못 바꾸는 이유: `embed` 로 바꾸면 그 역할이 큐와 메모리 예산을 우회하는
동기 경로로 넘어간다. `system` 을 못 바꾸는 이유: "프롬프트는 호출자 소유" 와 충돌한다.

### 5.4 오류 처리 계약

| 코드 | 의미 | 대응 |
|---|---|---|
| 401 / 403 | 토큰 없음·틀림 / 역할 권한 없음 | 재시도 무의미 |
| 404 / 413 | 모르는 역할 / 입력 초과 | 재시도 무의미 |
| 429 | 레이트리밋 | 간격 늘려 재시도 |
| 503 | 백엔드 불가·모델 설치 대기 | 나중에 재시도 |
| 5xx | 게이트웨이 오류 | 백오프 후 재시도 |

> ⚠️ **`error` 문자열로 분기하지 말 것.** 검증·권한 오류는 기계 코드가 오지만
> 모델 미설치·백엔드 장애·실패한 잡에서는 사람이 읽는 문장이 온다. 분기는
> **HTTP 상태와 `retryable`** 로 한다. `/v1/meta` 의 `error_codes` 가 전체 목록이되,
> **enum 으로 쓰지 않는다**(엄격한 검증기가 진짜 응답을 거부하기 때문).

클라이언트 예외도 같은 축으로 나뉜다: `AuthError`·`RoleError`(재시도 무의미) /
`JobFailed`(잡 종료) / `JobTimeout`(잡은 계속 돌 수 있음) / `GatewayError`(재시도 가치 있음).

---

## 6. 데이터 스키마 (SQLite 6테이블)

| 테이블 | 목적 | 핵심 컬럼 |
|---|---|---|
| `jobs` | 잡 영속화 | id, service, role, model, lane, status, priority, prompt, system, response, error, metadata_json, attempts, **options_json·timeout_s·metrics_json**, created/started/finished_at |
| `usage` | 사용량 귀속 | ts, service, role, model, eval_count, duration_ms, status |
| `model_requests` | 설치 요청 | model(PK), status, requested_by, roles_json, est_size_gb, progress, error, decided_at |
| `role_overrides` | 런타임 역할 교체 | role(PK), origin(yaml\|db), **fields_json**, note, updated_by/at |
| `admin_audit` | 관리 작업 감사 | ts, actor, action, target, detail_json, outcome |
| `ab_runs` | 모델 A/B | prompt, system, options_json, model_a/b, jobs_json, status |

설계 포인트:

- **크래시 복구**: 기동 시 `running` 으로 남은 잡을 `queued` 로 되돌린다.
- **`role_overrides.fields_json`**: 개별 컬럼이 아니라 JSON. 덮어쓸 수 있는 필드를 늘려도
  스키마가 안 바뀐다. 검증은 SQL이 아니라 `config.validate_role_fields` 가 한다.
- **마이그레이션은 ADD COLUMN 전용**: `PRAGMA` 로 확인 후 `ALTER TABLE`. 추가 전용·NULL
  기본값만 허용(재작성·삭제 금지) — SQLite의 ADD COLUMN은 메타데이터 연산이라 WAL 라이브
  DB에서 안전하다.
- **감사 2벌**: 게이트웨이의 `admin_audit` 과 대시보드 감사 로그는 **다른 질문에 답한다**
  (MCP·curl로도 관리 API에 들어올 수 있으므로).
- **보존 30일**: 완료 잡·사용량 자동 정리. 안 하면 DB가 무한히 커진다.
  → 결과가 중요하면 **소비자 쪽에 저장**해야 한다(계약에 명시).

---

## 7. 스케줄러 — 이 시스템의 심장

```
interactive : 동시 1  — 작은 모델·짧은 작업
batch       : 동시 1  — 큰 모델·긴 작업(야간 분석)
```

### 7.1 왜 레인을 나눴나

동시성 1 하나만 두면 3분짜리 batch 잡이 2초짜리 대화형 요청을 막는다(head-of-line
blocking). **우선순위는 큐 순서만 바꿀 뿐 실행 중인 잡을 비우지 못한다.** 그래서 레인을
물리적으로 나눴다.

### 7.2 잡 선택 알고리즘 (`_pick`)

```
1. 예산 초과 모델      → 즉시 실패시킨다 (예산을 비워도 안 들어가면 영원히 못 돈다.
                          조용히 쌓아두지 않고 설정 실수를 빨리 드러낸다)
2. 미설치 모델 잡      → 건너뛴다 (레인을 막지 않는 것이 핵심. 설치 요청은 _models_loop 담당)
3. 메모리 예산 확인    → 다른 레인 사용량을 뺀 잔여로 들어갈 수 있는 잡만
4. 기아 방지          → 300초 이상 기다린 잡이 있으면 무조건 그것부터
5. 모델 친화          → 현재 로드된 모델과 같은 모델 우선 (모델 전환·재로드 감소)
6. 그 외              → priority DESC, created_at ASC
```

`_available` 이 `None`(=아직 모름)이면 아무 잡도 건너뛰지 않는다 — 백엔드가 잠깐 죽었다고
큐 전체를 멈추면 안 되기 때문.

### 7.3 재시도

`MAX_RETRIES=3` → 최초 1회 + 재시도 3회, 백오프 2→4→8초. **재시도 가능 여부는 백엔드
클라이언트가 판정한다**(`BackendError.retryable`). 컨텍스트 초과처럼 다시 해도 같은
결과인 것은 재시도하지 않는다.

### 7.4 메모리 예산

두 레인에서 동시에 도는 모델 크기 합이 `MEM_BUDGET_GB`(기본 40, 48GB 맥 기준)를 넘으면
시작을 미룬다. 크기는 `roles.yaml` 의 `model_sizes_gb` → `catalog.yaml` → 태그의 파라미터
수 추정 순으로 정한다.

### 7.5 배경 루프 3개

| 루프 | 주기 | 하는 일 |
|---|---|---|
| `_lane_loop` × 2 | 0.5초 폴링 | 잡 선택 → claim → 실행 → 완료/재시도 |
| `_models_loop` | 30초 | 맥 보유 모델 갱신 · 미설치 triage · 승인된 모델 pull · Slack 알림 |
| `_retention_loop` | — | 30일 지난 잡·사용량 정리 |

---

## 8. 모델 생애주기

### 8.1 자동 요청 → 승인 → 자동 설치

```
소비자가 미설치 모델 역할 호출
  → 스케줄러가 그 잡만 레인에서 건너뜀 (다른 잡은 계속 돈다)
  → model_requests 에 pending 생성 + Slack 알림
  → 승인 (대시보드 카드 / MCP llm_decide_model / POST /v1/models/requests)
  → 게이트웨이가 맥의 /api/pull 직접 호출 (SSH 불필요) — progress 0~100
  → ready → 대기하던 잡이 자동으로 이어서 실행
```

거부(`rejected`)나 실패(`failed`)면 그 모델을 기다리던 잡은 **무한 대기 대신 명확한 오류**로
끝난다(`DEAD_STATUSES`). 거부한 모델은 다시 물어보지 않는다.

승인해도 설치되는 것은 `roles.yaml` 의 역할이 참조하는 모델뿐이다 — **임의 모델을 요청할
수 있는 경로가 아니다.** 끄려면 `AUTO_INSTALL_MODELS=0`.

### 8.2 직접 찾아 설치

대시보드 **LLM 모델** 페이지. `catalog.yaml` 의 큐레이션 목록에서 고르거나 이름 직접 입력.

- **ollama.com 을 스크레이핑하지 않는다** — 검색 API가 없어 HTML 파싱에 기대야 하고,
  그러면 남의 사이트 개편에 게이트웨이가 끌려 죽는다.
- **새 pull 코드를 만들지 않았다** — 기존 승인 파이프라인에 `approved` 로 밀어 넣으면
  모델 루프가 2초 안에 집어간다. 진행률·재시도·실패 처리를 그대로 재사용.
- **크기 게이트**: 추정 크기가 `MEM_BUDGET_GB` 를 넘으면 **설치 전에** 거부한다.
  안 그러면 21GB를 잘 받아 놓고 실행할 때마다 잡이 죽는다.

### 8.3 삭제 — `force` 는 없다

다섯 중 하나라도 걸리면 409:

| 차단 사유 | 왜 |
|---|---|
| 그 모델을 쓰는 역할이 있다 | 지워도 다음 요청에서 곧바로 재설치 대기 — 역할을 먼저 바꿔야 한다 |
| 대기 중인 잡이 있다 | 그 잡들이 통째로 멈춘다 |
| 실행 중이다 | 진행 중인 추론이 깨진다 |
| 설치가 진행 중이다 | 부분 파일이 남는다 |
| 임베딩 역할이 쓴다 | 임베딩은 동기 경로라 소비자가 **즉시 503** 을 받는다 |

삭제 시 설치 요청 행 자체를 지운다. `ready` 로 두면 다음 탐지에서 되살아나고,
`rejected` 로 두면 이후 잡이 "설치가 거부됨" 이라는 **거짓 사유**로 하드 실패한다.

### 8.4 A/B 비교

- **새 실행 경로를 만들지 않았다** — 각 측이 "워밍업 1회 + 측정 1회" 의 평범한 잡 2개.
  재시도·영속성·메모리 가드를 그대로 쓴다.
- **정직한 지표는 tok/s**(`eval_count / eval_duration`) — 모델 로드를 제외한다.
  `total_duration` 만 보면 "콜드 32b vs 웜 7b" 비교가 무의미해진다.
- **변수는 하나** — 양쪽 동일 옵션(temperature 0, seed 0)·동일 system. 역할 옵션 상속 안 함.
- **실사용 잡을 밀지 않는다** — 우선순위 −1 + 동시 1건.
- **`keep_alive` 30초** — 21GB 두 개를 기본값(10분)으로 붙잡으면 맥 전체가 느려진다.

### 8.5 역할 모델 런타임 교체

`roles.yaml` 은 **기본값**, 대시보드 오버라이드가 그 위에 얹힌다. 모델 교체에 PR·머지·배포가
필요 없다.

- 잘못된 오버라이드 행이 **기동을 막지 않는다** — 건너뛰고 `/v1/status.overrides.invalid` 로 알린다.
- **드리프트가 보인다** — `overrides.count > 0` 이면 프로덕션이 레포와 다르다는 뜻.
  `GET /v1/admin/roles` 의 `yaml_snippet` 을 `roles.yaml` 에 반영하면 0으로 돌아온다.
- **롤백 레버**: 오버라이드는 코드가 아니라 데이터다.
  `DELETE FROM role_overrides;` + 재기동 = 순수 `roles.yaml` 동작으로 즉시 복귀.
  ⚠️ 뒤집어 말하면 **DB를 백업본으로 되돌리면 모델 선택도 조용히 되돌아간다.**

---

## 9. 인증·보안 경계

### 9.1 계층

```
Caddy      /llm/v1/* 만 프록시 · /v1/admin/* 은 404 · /healthz 비공개 · CORS 안 염
  ↓
_auth      Bearer → Service (역인덱스 조회 후 hmac.compare_digest 상수시간 재확인)
           실패 시 클라이언트 IP 와 함께 로그 (토큰 값은 절대 남기지 않는다)
  ↓
RateLimiter  서비스별 분당 슬라이딩 윈도
  ↓
allow_roles  역할 단위 권한 — GET /v1/roles 가 보여주는 것이 곧 쓸 수 있는 전부
  ↓
_require_admin  admin: true 만 (프록시만 믿지 않는다 — 설정 실수·직접 접근 대비)
```

Caddy 설정에서 **순서가 의미를 갖는다**: 관리 API 404 규칙이 반드시 프록시 규칙보다
앞에 있어야 한다.

### 9.2 위험 범위 (명시적으로 계산해 둔 것)

게이트웨이 토큰이 새면 피해는 **맥 GPU 소모**까지다 — MCP 토큰(=root 권한)과 달리 서버가
장악되지 않는다. `allow_roles` 로 역할이, `rate_limit_per_min` 으로 소비량이 제한된다.
유출 의심 시 `.env` 토큰만 갈아끼우고 재시작.

반면 `/v1/admin/*` 토큰 하나가 새면 "맥의 모델 전부 삭제 + 수십 GB 다운로드" 까지 가능하다.
그래서 토큰 게이트만으로 부족하다고 보고 **공개 경로에서 아예 존재하지 않게** 만들었다.

### 9.3 토큰 노출 경로는 하나뿐

`GET /v1/admin/services/{name}/token` — 한 번에 한 서비스, 목록에는 절대 안 실리고,
열람이 게이트웨이와 대시보드 **양쪽 감사에 남는다**(값은 어디에도 남기지 않는다).

**발급·회전·폐기 기능은 없다.** 새 소비자는 `services.yaml` PR → `.env` → 재기동.
(새 프로젝트에서 가장 먼저 검토할 부채 — 15절)

### 9.4 소비자 쪽 규칙 (집 밖 소비자)

- 토큰은 **서버 사이드에만**. `NEXT_PUBLIC_` 접두어 금지. 브라우저는 게이트웨이를 직접
  부르지 않고 자기 서비스 라우트를 부른다(그래서 CORS를 열지 않았다).
- Vercel 환경변수는 **Production 에만**. Preview까지 켜면 모든 PR 프리뷰가 토큰을 갖는다.
- 폴링은 **2초 이상**(roxlogy 한도 30rpm = 2초).
- 서버리스는 절대 기다리지 않는다 — `wait=0` 으로 던지고 `job_id` 저장 후 즉시 응답,
  브라우저가 폴링.

---

## 10. 운영·관측

### 10.1 배포

```bash
cp .env.example .env && docker compose up -d --build   # 개발
sudo systemctl restart llm-gateway   # 단독 재기동 (이미지 재빌드 없음)
sudo systemctl reload  llm-gateway   # 코드 반영 재빌드 (compose up -d --build)
```

**systemd로 감싼 이유는 수명주기 분리다** — 잡 큐를 들고 있으므로 다른 서비스 배포에
끌려 재시작되면 안 된다. `hosub-mcp` 자동 업데이트 타이머는 게이트웨이를 건드리지 않되,
`llm-gateway/` 코드가 바뀌면 **로그로 알려준다**(조용히 어긋나지 않도록).

`stop` 은 `compose stop` 이다(`down` 이 아닌 이유: `llm-net` 네트워크를 유지해야 다른
컨테이너 소비자가 안 깨진다). 도커 로그는 10MB×3 회전 — 공개 경로라 스캐너 트래픽이
로그를 채우고 루트 디스크를 잠식할 수 있다.

### 10.2 Slack 알림 — 3가지만

**사람이 모르면 조용히 멈추는 지점**에만 보낸다: 모델 설치 승인 대기 / 맥 백엔드
오프라인·복구 / 설치 완료·실패.

- 알림 실패가 파이프라인을 죽이지 않는다(예외를 삼키고 로그만).
- **상태 전이에서만** 보낸다(30초마다 알리면 아무도 안 본다).
- 기동 시 "복구됨" 을 보내지 않는다(재시작마다 시끄러워진다).
- 비밀·프롬프트·응답 본문은 담지 않는다.

### 10.3 관측 지표

| 어디서 | 무엇 |
|---|---|
| `GET /v1/status` | 백엔드 온라인·보유 모델·레인별 실행/대기·메모리 사용·사용량·오버라이드 수 |
| `usage` 테이블 | 서비스×역할×모델별 호출·토큰·지연·성공률 |
| `jobs.metrics_json` | Ollama의 load/prompt_eval/eval 세부 시간 (A/B의 tok/s 근거) |
| `admin_audit` | 누가 무엇을 바꿨는지 |
| 대시보드 LLM 페이지 | 위 전부의 UI |

---

## 11. 소비자 통합 방식 — 배울 점이 가장 많은 부분

### 11.1 게이트웨이가 자기 계약을 서빙한다

```
GET /v1/integration        사람이 읽는 통합 가이드 (마크다운)
GET /v1/meta               기계가 읽는 계약 (역할·한도·오류코드·엔드포인트·클라이언트 해시)
GET /v1/openapi.json|.yaml OpenAPI 3.1
GET /v1/client/llmgw.py    파이썬 클라이언트 원본
GET /v1/client/mock_gateway.py  개발용 목 서버 원본
GET /v1/docs               브라우저 탐색기 (swagger-ui 벤더링, CDN 금지)
```

**저장소 접근이 없는 소비자도 curl 한 번으로 최신 계약을 가져간다.** Dockerfile이
`docs/`·`client/`·`tools/`·`static/` 을 이미지에 넣어 컨테이너를 자립적으로 만든다.

### 11.2 문서가 조용히 거짓이 되는 경로를 구조로 막았다

- OpenAPI·`/v1/meta` 는 **정적 파일이 아니라 매번 생성**된다. 역할은 런타임에 바뀌고,
  한도는 스케줄러 인스턴스에서 읽고, 허용 역할은 토큰마다 다르다.
- **엔드포인트 목록도 손으로 적지 않는다** — `app.routes` 를 순회해 재고를 만들고,
  사람이 쓰는 것은 *요약*뿐이다. 라우트를 추가하고 요약을 안 달면 **테스트가 실패한다.**
- **토큰마다 결과가 다르다** — `role` enum에 그 서비스가 쓸 수 있는 역할만 넣으므로
  생성된 클라이언트는 못 쓰는 역할을 애초에 노출하지 않는다. 관리 엔드포인트는 admin
  토큰일 때만 `x-admin-endpoints` 로 나온다(공개 경로에서 404라 `paths` 에 있으면 거짓말).
- README/integration.md의 **표는 손으로 관리한다 → 실제로 어긋났다**(`/v1/meta` 계열이
  한동안 빠져 있었다). 그래서 권위를 `/v1/meta.endpoints` 로 옮기고 표에 그렇게 적어 뒀다.

### 11.3 클라이언트 한 파일 + 목 서버

`client/llmgw.py`(338줄, 의존성 httpx 하나)를 소비자 레포에 복사한다. 동기 `LLMGateway` /
비동기 `AsyncLLMGateway` 의 API가 동일하다.

```python
gw = LLMGateway()                                   # 환경변수 LLMGW_URL / LLMGW_TOKEN
text = gw.run("summarize", 문서)                     # 끝까지 기다려 텍스트만
job  = gw.generate("analyze_workout", 데이터, system=내프롬프트, wait=0, metadata={...})
done = gw.wait_for(job.job_id)                      # 폴링(기본 2초 간격)
vecs = gw.embed([t.title for t in items])           # 배치로
```

- **버전 문자열 대신 해시로 최신 여부를 확인한다** — `sha256sum lib/llmgw.py` vs
  `/v1/meta` 의 `client.files.python.sha256`. (현재 3사본 md5 동일 = 드리프트 없음)
- `/v1/meta` 의 `client.files.python` 에는 `entrypoints`·`exceptions`·`notes` 도 온다 —
  **OpenAPI로는 표현되지 않는 것들**(폴링 루프, `wait+15` 타임아웃 자동 확대, 미지 필드
  무시, `cancel()` 이 오류를 삼켜 `False` 를 주는 것).
- `tools/mock_gateway.py` 로 **맥도 도커도 토큰도 없이** 통합 코드를 완성할 수 있다.
  역할 목록을 실제 `roles.yaml` 에서 읽으므로 역할 이름이 어긋나지 않고, 응답 형태는
  회귀 테스트(`test_client_contract.py`)로 고정돼 있다.

---

## 12. 실제 소비자 4종

### 12.1 hosub MCP 도구 (`src/tools/llm.py`)

Claude(claude.ai·모바일)에서 대화로 LLM을 쓰는 경로. 도구 6종:

| 도구 | 위험도 | 설명 |
|---|---|---|
| `llm_list_roles` `llm_status` | Low | 역할·모델, 백엔드/대기열 상태 |
| `llm_generate` `llm_job` | Low | 실행 / pending 결과 수령 |
| `llm_model_requests` | Low | 설치 요청 목록 |
| `llm_decide_model` | **Medium** | 승인/거부 — `confirm=true` 없이는 실행 안 됨 |

`llm_decide_model` 만 Medium인 이유: 승인은 **맥 디스크에 수 GB를 내려받는 상태 변경**이다.
승인 흐름은 저장소 공통 정책(Medium/High는 confirm 게이트)을 그대로 탄다.

도구 응답에 **다음 행동을 안내하는 `hint`** 를 붙이는 패턴이 일관돼 있다
(pending이면 "llm_job(job_id=…)로 확인, 모델 미설치일 수도 있음").

`src/gateway.py` 는 예외를 던지지 않고 `status`/`error` dict를 돌려준다 — 호출부(MCP 도구·
대시보드)가 그대로 반환하기 때문. `unconfigured` 상태를 따로 두어 설정 누락을 오류와 구분한다.

### 12.2 대시보드 (`src/dashboard.py` + `static/pages/`)

`/api/llm/*` 20개 라우트(17경로)가 게이트웨이 API를 세션 인증 뒤로 감싼다. UI 2페이지:

- **`llm.js`** — 백엔드 상태·역할 목록·테스트 실행 (조회 성격, 갱신 주기 짧음)
- **`llm-models.js`** — 설치 모델·카탈로그·설치/삭제·A/B 비교 (변경 성격)

**나눈 이유가 명시돼 있다**: 한 페이지에 카드 5개는 너무 많고, 조회와 변경은 갱신 주기도
성격도 다르다.

### 12.3 TNM — 뉴스·공시 분류 (LLM을 가장 무겁게 쓰는 곳)

```
수집 → 종목 귀속 → raw_items → 임베딩(dedup) → 분류(LLM) → 결정론 점수
```

- `app/ollama.py` — 분류는 `classify_news`(잡 큐 + pending 폴링), 임베딩은 `embed`(동기).
- `app/pipeline/classify.py` — 시스템 프롬프트에서 **매매의견·목표주가·원문에 없는 수치를
  금지**하고, `reason` 의 수치는 원문 대조로 사후 검사한다.
- **실패를 두 종류로 나눈 것이 핵심 계약이다:**

  | 예외 | 의미 | 처리 |
  |---|---|---|
  | `OllamaUnavailable` | 백엔드/게이트웨이 불가 | 항목을 **버리지 않고 보류** → 백오프 후 자동 재처리 |
  | `SchemaError` | 응답이 JSON 스키마 위반 | 2회 재시도(총 3시도) 후 `llm_failed` 적재(원문 보존) → 프롬프트·모델 개선 후 **재큐** |

- **모델 출력을 결정론적으로 흡수한다**: 프롬프트에 "그대로 쓰라"고 적어도 계속 벗어나므로
  (실측 175건/7일) `CATEGORY_ALIASES` 로 정본화한다. 애매한 것은 전부 `기타`(가중치 0.2)로 —
  **과대평가는 누락보다 비싸다.**
- **enum을 넓혀 손실을 막은 사례**: `impact_horizon` 에 `unclear` 를 추가했다. 분류 실패
  원인 1위가 `'unclear'` 247건이었는데, `impact_direction` 에는 "모르겠다"가 있고 horizon에는
  없어서 항목이 통째로 버려졌다. 게다가 그 필드는 예측력이 확인되지 않았다(12.4).
- 모든 호출을 `tnm_llm_calls` 에 기록(입력해시·모델·지연·시도·결과).
- **임베딩이 LLM 물량을 줄이는 게이트다**: 유사도 ≥0.92 duplicate → LLM 생략,
  ≥0.85 follow_up → 수행하되 감점.

### 12.4 trading — LLM은 문장만 쓰고, 판정은 하지 않는다

- `app/journal.py` — 일일 매매일지 요약(`general` 역할). **수치·판정은 전부 결정론 코드가
  만들고 LLM은 관찰 사실 목록을 문장으로 엮기만 한다**(원자료 해석을 맡기지 않는다).
  요약 실패가 일지 생성을 막지 않는다. 프록시 타임아웃(180초)보다 짧은 150초로 잡아
  먼저 포기하고 사유를 남긴다. 완료 표시를 디스크에 남겨 **재배포마다 요약이 다시 도는
  결함**을 막았다.
- `app/signals/priority.py` — 명시적으로 "LLM·난수 개입 없음".
- `app/research/newsimpact.py` — **LLM 출력 자체를 측정 대상으로 세운 코드.**
  `impact_horizon`·`impact_direction` 이 실제 수익률과 상관이 있는가를 소급 측정한다.
  horizon은 이미 "1일 초과수익이 0과 구분되는 버킷 0/3" 으로 예측력 미확인 판정.
  → **상관이 없으면 LLM 스키마에서 뺀다**(프롬프트가 짧아지고 재시도가 준다).

  이 저장소의 측정 거버넌스가 LLM에도 그대로 적용된 사례다: LLM이 만든 필드라고 해서
  믿지 않고, 사전에 못박은 기준으로 사후 판정한다.

### 12.5 roxlogy — 집 밖 소비자 (다른 레포)

Vercel에서 돌며 Caddy `/llm/v1/*` 를 거쳐 들어온다. 프롬프트는 자기 레포 소유.
`wait=0` + 브라우저 폴링 패턴. 이 저장소에는 코드가 없고 **계약만 있다** —
게이트웨이가 이 관계를 지탱하는 방식(문서·클라이언트·목 서버 서빙)이 11절 그대로다.

---

## 13. 설계 원칙 — 새 프로젝트에 그대로 가져갈 것

1. **단일 관문.** 공유 자원(GPU·메모리)을 쓰는 경로는 하나로 모은다. 소비자마다 워커를
   만들면 큐·재시도·사용량이 N벌 생기고 서로를 모른다.
2. **이름이 계약, 구현이 정책.** 역할 이름은 고정, 그 뒤의 모델은 런타임에 바뀐다.
   소비자가 구현 세부(모델명)에 결합하지 않게 계약 문서에 못박는다.
3. **작업 단위는 자기완결형.** 실행 시점이 아니라 **생성 시점**의 설정을 스냅샷한다.
   설정 변경이 진행 중인 작업을 오염시키지 않는다.
4. **응답 모양을 하나로.** 동기/비동기를 `status` 한 필드로 흡수하면 호출자 코드에서
   분기가 사라진다.
5. **레인 분리 > 우선순위.** 우선순위는 큐 순서만 바꾼다. 긴 작업이 짧은 작업을 막지
   않게 하려면 실행 슬롯 자체를 나눠야 한다.
6. **막힌 것은 건너뛰고, 못 도는 것은 즉시 실패시킨다.** 미설치 모델 잡은 레인을 막지
   않게 건너뛰고, 예산을 비워도 안 들어가는 잡은 조용히 쌓아두지 말고 이유를 붙여 죽인다.
7. **기아 방지를 최적화보다 위에.** 모델 친화(성능)보다 300초 대기(공정성)가 우선.
8. **문서를 코드에서 생성한다.** 손으로 관리하는 표는 반드시 어긋난다 — 실제로 어긋났다.
   라우트 재고를 코드에서 유도하고, 요약 누락을 테스트로 잡는다.
9. **계약을 서비스가 직접 서빙한다.** 저장소 접근이 없는 소비자도 최신 계약을 받는다.
   계약이 두 곳에 있으면 반드시 어긋난다.
10. **개발 진입 장벽을 서비스가 치운다.** 목 서버 + 단일 파일 클라이언트를 서비스가
    함께 배포한다. 소비자가 "붙이기 어려워서" 우회로를 만드는 것이 가장 비싼 실패다.
11. **알림은 사람이 개입해야 하는 순간에만, 상태 전이에서만.** 그 외에는 침묵.
    알림 실패가 파이프라인을 죽이면 안 된다.
12. **위험 범위를 명시적으로 계산한다.** "토큰이 새면 어디까지 당하나" 를 문서에 적어 두면
    한도·경계 설계가 감이 아니라 계산이 된다.
13. **롤백 레버를 데이터로 만든다.** 오버라이드는 코드가 아니라 DB 행이라 한 줄로 되돌린다.
    단 그 대가(백업 복원이 모델 선택도 되돌림)도 함께 적어 둔다.
14. **LLM 출력을 믿지 말고 측정한다.** 프롬프트로 안 되는 것은 결정론 코드로 흡수하고,
    쓰이지 않거나 예측력 없는 필드는 스키마에서 뺀다.
15. **LLM에게 판정을 시키지 않는다.** 수치·판정은 결정론 코드, LLM은 분류·요약·서술.

---

## 14. 명시적으로 하지 않기로 한 것

| 항목 | 왜 |
|---|---|
| **스트리밍** | 1차 제외(설계서 9절). 잡 모델과 충돌하고, 실사용(배치 분석·분류)이 요구하지 않았다 |
| **완료 콜백** | 설계서 6.1절. 게이트웨이가 소비자에게 아웃바운드를 거는 순간 인증·SSRF 문제가 생긴다. 폴링으로 충분했다 |
| **ollama.com 스크레이핑** | 검색 API가 없다. HTML 파싱은 남의 사이트 개편에 끌려 죽는다 → 큐레이션 카탈로그 |
| **모델 강제 삭제(`force`)** | 5가지 차단 사유가 전부 실제 고장으로 이어진다 |
| **토큰 발급·회전·폐기 API** | 현재 없음. `services.yaml` PR → `.env` → 재기동 (부채로 인지) |
| **소비자의 모델 직접 지정** | `model` 필드는 관리 전용. 열면 "역할=모델 정책" 계약이 무너지고 메모리 추정도 흔들린다 |
| **CORS 개방** | 브라우저가 게이트웨이를 직접 부르지 않게 하려고 일부러 안 열었다 |
| **외부 CDN** | 저장소 정책. swagger-ui를 이미지에 벤더링 |

---

## 15. 알려진 한계·부채 (재개발에서 먼저 볼 것)

1. **단일 장애점.** 게이트웨이가 죽으면 4개 소비자의 LLM 기능이 전부 멈춘다.
   설계서 11.1에 "감수 범위" 로 명시했지만, 새 프로젝트가 더 크면 재검토 대상이다.
2. **단일 백엔드.** 맥 스튜디오 Ollama 하나. 맥이 자면 백엔드가 사라진다
   (`pmset -a sleep 0` 필요). 다중 백엔드·클라우드 폴백 경로가 없다.
3. **토큰 수명주기 부재.** 발급·회전·폐기가 수작업이다. 소비자가 늘면 가장 먼저 아프다.
4. **손으로 관리하는 표.** README·integration.md의 엔드포인트 표는 여전히 수동이다
   (권위는 `/v1/meta` 로 옮겼지만 표 자체는 남아 있다).
5. **클라이언트 사본 3벌.** `client/llmgw.py` 가 trading·tnm에 복사돼 있다. 현재는 md5가
   같지만 구조적으로는 드리프트가 가능하다(해시 비교가 유일한 방어).
6. **DB 백업 복원이 모델 선택을 조용히 되돌린다.** 오버라이드가 데이터라서 생기는 대가.
7. **레인 동시성이 각 1로 고정.** 설정 가능한 값이 아니다. 백엔드가 커지면 병목.
8. **qwen3 이후 계열의 thinking + JSON.** hybrid thinking 모델은 structured output이
   깨지는 미해결 이슈가 있다(ollama#10929, #10538). `classify_news` 같은 스키마 준수
   역할에 올릴 때는 `/no_think` 또는 thinking off + **JSON 준수율 사전 검증** 필요.
9. **완료 잡 30일 보존.** 결과가 중요하면 소비자가 저장해야 한다 — 계약에 적혀 있지만
   소비자가 안 지키면 조용히 잃는다.
10. **임베딩이 메모리 예산을 우회한다.** `kind: embed` 는 큐 밖 동기 경로라 가드가 없다.
    "임베딩 모델은 작아야 한다" 는 규약에만 의존한다.

---

## 16. 테스트 전략

```bash
../.venv/bin/python -m pytest        # llm-gateway — 실제 맥 없이 전부 통과
.venv/bin/pytest                     # 저장소 전체
```

가짜 백엔드·가짜 store를 주입해서 돌린다(`build_app` 이 전부 주입식인 이유).

| 파일 | 무엇을 고정하나 |
|---|---|
| `test_api.py` | 엔드포인트 계약 |
| `test_scheduler.py` | head-of-line blocking 방지, 모델 전환 최소화, 재시도/백오프, 재시작 후 이어받기, 미설치 모델이 레인을 막지 않는지, 승인 후 자동 재개 |
| `test_store.py` | 영속화·크래시 복구·보존 |
| `test_config.py` `test_overrides.py` | 역할 로딩·오버라이드 검증·잘못된 행이 기동을 막지 않는지 |
| `test_model_ops.py` | 설치/삭제 차단 사유 |
| `test_compare.py` | A/B |
| `test_meta.py` | **라우트 추가 시 요약 누락을 실패시킨다** |
| `test_client_contract.py` `test_client_serving.py` | 클라이언트 계약·서빙 |
| `test_admin_services.py` `test_notify.py` `test_docs_explorer.py` `test_models.py` | 관리·알림·탐색기 |

소비자 쪽: `tests/test_gateway.py`(hosub 클라이언트), `tnm/tests/test_classify.py`·
`test_embed_dedup.py`, `trading/tests/test_journal.py`·`test_news_impact.py`.

---

## 17. 새 프로젝트로 디벨롭할 때 — 체크리스트

### 그대로 가져갈 것 (검증된 것)

- 잡 모델(`status` 하나로 동기/비동기 흡수) + 생성 시점 스냅샷
- 역할 = 정책 / 프롬프트 = 호출자 소유 분리
- 2레인 + 메모리 예산 + 기아 방지 + 모델 친화 스케줄링
- 미설치 모델 → 건너뛰기 → 승인 → 자동 설치 → 자동 재개 파이프라인
- 계약 자기 서빙(`/v1/integration` `/v1/meta` `/v1/openapi` `/v1/client/*`)
- 목 서버 + 단일 파일 클라이언트 동봉
- 상태 전이 기반 알림, 실패를 삼키는 알림 계층
- 관리 API 이중 차단(프록시 404 + 앱 권한)
- 라우트 재고 자동 생성 + 요약 누락 테스트

### 먼저 갈아야 할 것

| 항목 | 방향 |
|---|---|
| 단일 백엔드 | 백엔드 풀 + 라우팅(모델별·부하별). `OllamaClient` 를 인터페이스로 추상화 |
| 단일 장애점 | 게이트웨이 다중 인스턴스 → 그러면 SQLite를 Postgres로, 잡 claim을 원자적 UPDATE로 |
| 토큰 수명주기 | 발급·회전·만료·폐기 API + 감사. 지금은 `.env` + 재기동 |
| 레인 고정 | 레인 수·동시성을 설정값으로. 백엔드 풀과 함께 재설계 |
| 스트리밍 | 대화형 UX가 필요하면 1차 제외 결정을 뒤집어야 한다. 잡 모델과 공존시키는 설계 필요 |
| 프로바이더 | 지금은 Ollama 전용. 클라우드 LLM(Anthropic 등) 폴백을 넣으려면 역할에 프로바이더 축 추가 |

### 새로 정해야 할 것

- **비용 축**: 지금 usage는 호출·토큰·지연만 센다. 유료 프로바이더가 들어오면 비용 집계와
  서비스별 예산·상한이 필요하다(`rate_limit_per_min` 만으로는 부족).
- **프롬프트 버전 관리**: "프롬프트는 호출자 소유" 는 좋았지만, 프롬프트 변경과 품질 변화의
  상관을 재려면 소비자 쪽에 버전·해시 기록이 필요하다(TNM의 `input_hash` 가 힌트).
- **평가(eval) 경로**: A/B 비교는 "속도" 를 잰다. **품질**을 재는 경로는 없다.
  TNM의 스키마 준수율·`llm_failed` 비율이 사실상 유일한 품질 지표다.
- **멀티테넌시 경계**: 지금은 서비스 4개·신뢰 관계 1개(전부 본인 소유). 외부 사용자가
  들어오면 잡 격리·프롬프트 보관·삭제 요구가 새 요구사항이 된다.

---

## 18. 부록

### 18.1 포트

| 포트 | 서비스 |
|---|---|
| 8600 | trading |
| 8602 | tnm |
| **8603** | **llm-gateway** |
| 8700 | hosub MCP |
| 8701 | 대시보드 |
| 11434 | 맥 Ollama (Tailscale) |

### 18.2 환경변수

```bash
OLLAMA_URL=http://100.69.201.28:11434   # 맥 Tailscale IP (LAN IP는 DHCP라 안 씀)
MEM_BUDGET_GB=40                        # 48GB 맥 기준
MAX_RETRIES=3                           # 최초 1회 + 재시도 3회, 백오프 2→4→8초
AUTO_INSTALL_MODELS=1                   # 0이면 자동 설치 요청 끔
MODELS_REFRESH_SECONDS=30
JOB_RETENTION_DAYS=30
LOG_LEVEL=INFO
SLACK_WEBHOOK_URL=                      # 비우면 알림 꺼짐(오류 아님)
LLMGW_TOKEN_HOSUB= / _ROXLOGY / _TNM / _TRADING
# 컨테이너 내부: LLMGW_CONFIG_DIR=/app/config  LLMGW_DB=/data/llmgw.db
# 소비자 쪽:     LLMGW_URL  LLMGW_TOKEN
```

### 18.3 자주 쓰는 명령

```bash
# 상태
curl -s localhost:8603/healthz
curl -sH "Authorization: Bearer $LLMGW_TOKEN_HOSUB" localhost:8603/v1/status | jq

# 계약 받기 (저장소 없이)
curl -H "Authorization: Bearer $T" $LLMGW_URL/v1/integration
curl -H "Authorization: Bearer $T" $LLMGW_URL/v1/client/llmgw.py -o llmgw.py

# 내 클라이언트 사본이 최신인가
sha256sum lib/llmgw.py
curl -sH "Authorization: Bearer $T" $LLMGW_URL/v1/meta \
  | jq -r '.client.files.python | "\(.sha256) \(.bytes)"'

# 오버라이드 전량 롤백 (모델 선택을 roles.yaml 기준으로 되돌린다)
sqlite3 /data/llm-gateway/llmgw.db "DELETE FROM role_overrides;"
sudo systemctl restart llm-gateway

# 개발
python llm-gateway/tools/mock_gateway.py --delay 20 --fail-rate 0.5 --deny code
```

### 18.4 용어

| 용어 | 뜻 |
|---|---|
| 역할(role) | 모델·레인·타임아웃·옵션의 이름 붙은 묶음. 소비자와의 계약 단위 |
| 레인(lane) | 실행 슬롯 그룹. `interactive`(짧음) / `batch`(김). 각 동시 1 |
| 잡(job) | 생성 시점 설정을 스냅샷한 실행 단위. 영속·재시도·취소 가능 |
| 오버라이드 | `roles.yaml` 기본값 위에 얹히는 DB 행. 재배포 없이 모델 교체 |
| 모델 요청 | 미설치 모델 감지 시 자동 생성되는 승인 대기 항목 |
| 서비스 | 토큰 하나로 식별되는 소비자. 권한·한도·사용량의 단위 |
| kind | `generate`(잡 큐) / `embed`(동기, 큐 밖) |

---

## 19. 원전 문서

| 문서 | 내용 |
|---|---|
| `docs/requests/llm-gateway-service.md` | 설계서 — 결정과 그 근거, 철회한 것 포함 |
| `llm-gateway/README.md` | 운영 — 배포·모델 관리·A/B·Slack |
| `llm-gateway/docs/integration.md` | 소비자 계약 (= `GET /v1/integration`) |
| `llm-gateway/docs/mac-setup.md` | 맥 준비 — 외부 바인딩·슬립 방지·Tailscale 키 만료 |
| `docs/SETUP.md` 8절 | 서비스 등록·재기동 경로 분리 |
| `docs/trading/README.md` | TNM·trading이 LLM을 쓰는 맥락 |
