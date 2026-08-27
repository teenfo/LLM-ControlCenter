# 구성도

---

## 1. 서버 구조도 (Standard 프로파일)

```
   소비자 — 설치처 애플리케이션
   ┌──────────────┐  ┌──────────────┐
   │ 서비스 A      │  │ 서비스 B      │   각자 토큰 → 테넌트·서비스 확정
   └──────┬───────┘  └──────┬───────┘
          └────────┬────────┘
                   ▼
 ╔═══════════════════════════════════════════════════════════╗
 ║  컨트롤 플레인 호스트 — Docker Compose 1대                  ║
 ║   ┌─────────────────┐                                     ║
 ║   │ proxy    :443   │  TLS 종단 · /v1/platform/* 차단       ║
 ║   └────────┬────────┘                                     ║
 ║   ┌────────▼──────────────────────┐   ┌────────────────┐  ║
 ║   │ controlcenter          :8610  │───│ db             │  ║
 ║   │  API · 스케줄러 · 관제 UI      │   │ SQLite 또는 PG │  ║
 ║   └────────┬──────────────────────┘   └────────────────┘  ║
 ║            │                          ┌────────────────┐  ║
 ║            │                          │ /keys 마스터KEK│  ║
 ║            │                          └────────────────┘  ║
 ╚════════════╪══════════════════════════════════════════════╝
     ┌────────┼────────┬─────────────┬──────────────┐
     │ 원문 OK│        │             ┊ 가드 통과분만 ┊
     ▼        ▼        ▼             ▼              ▼
 ┌────────┐┌────────┐┌──────────┐ ┌──────────┐ ┌──────────┐
 │ node-a ││ node-b ││ 가드 분류 │ │ node-c   │ │ 클라우드  │
 │ Ollama ││ Ollama ││ 소형 모델 │ │ 임대 GPU │ │ 프로바이더│
 │internal││internal││ internal │ │ external │ │ external │
 └────────┘└────────┘└──────────┘ └──────────┘ └──────────┘
 └──── 설치처 소유 · 아무것도 설치하지 않는다 (등록형) ────┘
```

**읽는 법**: 실선은 원문이 갈 수 있는 경로, 점선(`┊`)은 가드를 통과한 것만 가는 경로.

**`node-c` 가 Ollama 인데도 점선인 것이 요지다** — 소프트웨어가 아니라 기계의 위치가 경계를 정한다.
임대 GPU 의 Ollama 는 `provider` 가 같아도 프롬프트가 남의 기계로 나간다.

```mermaid
flowchart TB
  subgraph CONSUMERS["소비자 — 설치처 애플리케이션"]
    C1["서비스 A · token"]
    C2["서비스 B · token"]
  end

  subgraph HOST["컨트롤 플레인 호스트 — Docker Compose 1대"]
    PROXY["proxy :443 — TLS 종단"]
    CC["controlcenter :8610<br/>API · 스케줄러 · 관제 UI"]
    DB[("db — SQLite 또는 Postgres")]
    KEYS[/"volume /keys — 마스터 KEK"/]
  end

  subgraph INT["내부 경계 — data_boundary: internal"]
    N1["node-a · Ollama"]
    N2["node-b · Ollama"]
    NG["가드 분류 전용 소형 모델"]
  end

  subgraph EXT["외부 경계 — data_boundary: external"]
    N3["node-c · 임대 GPU Ollama"]
    CLOUD["클라우드 프로바이더"]
  end

  C1 --> PROXY
  C2 --> PROXY
  PROXY --> CC
  CC --- DB
  CC --- KEYS
  CC -->|원문 가능| N1
  CC -->|원문 가능| N2
  CC -->|원문 가능| NG
  CC -.->|가드 통과분만| N3
  CC -.->|가드 통과분만| CLOUD
```

---

## 2. 신뢰 경계를 넘는 것 / 넘지 않는 것

**두 열을 섞지 않는다.** 앱이 코드로 강제하는 것과 설치처가 네트워크에서 해줘야 하는 것을
한 칸에 적으면, 설치처 숙제가 제품의 보증처럼 읽힌다 — 그러면 아무도 그 숙제를 안 한다.

| 방향 | 무엇이 | 제품이 강제하는 것 | 설치처 전제 |
|---|---|---|---|
| 밖 → 안 | 소비자 요청 | Bearer 토큰 · 3단 레이트리밋 · 본문 크기 상한 | TLS 종단 |
| 밖 → 안 | 테넌트 관리 면 `/v1/admin/*` | 테넌트 관리자 역할 검사 · 테넌트 스코프 | — (**공개돼야 한다.** 테넌트 관리자가 원격에서 쓴다) |
| 밖 → 안 | 플랫폼 관리 면 `/v1/platform/*` | 플랫폼 관리자 역할 검사 | **프록시에서 차단.** 번들의 [`tls/nginx.conf`](../tls/nginx.conf) 가 404 로 돌려준다 |
| 안 → 밖 | external 노드로 가는 프롬프트 | **가드 통과분만.** 역할 `placement` 에 external 티어가 없으면 경로 자체가 없다 | external 노드는 TLS·인증 필수 (등록에서 거절) |
| 안 ↔ 안 | 컨트롤 플레인 ↔ internal 노드 | 옵션 인증 헤더를 선언대로 붙인다 | **사설망.** Ollama 는 기본 무인증이다 |
| 안 → 밖 | 알림 | 상태 전이만. 비밀·프롬프트·응답 본문 미포함 | 웹훅 수신처 |

**`/v1/admin/*` 은 차단 대상이 아니다.** 이름이 admin 이라 막고 싶어지지만 그것은 테넌트
관리자의 API 다 — 막으면 테넌트가 자기 토큰도 못 만든다. 숨겨야 하는 것은 플랫폼 관리
면뿐이고, 그 차단은 **인증이 아니라 노출 축소**다. 역할 검사는 프록시가 없어도 이미 돈다.

프록시는 `--profile tls` 옵트인이다. **그 프로파일 없이 공개망에 붙이면 위 표의 오른쪽 열이
전부 비어 있는 상태로 도는 것**이고, `preflight` 가 그 사실을 경고한다.

**절대 경계를 안 넘는 것**: 가드 2단 분류(`internal_only` 강제), 원문 복호화(단건 + 감사),
`placement: [internal]` 역할의 프롬프트.

---

## 3. 요청 처리 흐름 — 순서가 계약이다

```
요청 (role · prompt · end_user)
  │
  ▼
① 인증 ──── 토큰 → 테넌트·서비스 확정 · 3단 레이트리밋 · end_user 해싱
  │
  ▼
② 가드 1단 (패턴 + 체크섬)  →  2단 (내부 노드 LLM 분류)
  │
  ├── block ──▶ 422 차단 ·············· 어떤 노드에도 안 가고 평문 저장도 안 됨
  │
  ▼ off · audit · partial · full
③ 저장 ───── 마스킹본 평문 + 원문 AES-GCM (KEK 없으면 암호문 자체를 안 만듦)
  │           프롬프트 해시 기록 (마스킹 후 + 테넌트 솔트)
  ▼
④ 배치 ───── 스냅샷 ∩ 현재 설정 · 티어 → 모델 친화 → 최소 부하 · 원자적 예약
  │
  ├── 후보 없음 ─────▶ 대기 (사유 기록) ──┐
  ├── 용량 불가 ─────▶ 즉시 실패          │ 재시도
  ▼ 배치 성공                             │
⑤ 실행 → 비용 정산 · usage 기록  ◀────────┘
  │
  ▼
⑥ 출력 가드 ─ 응답에 1단 패턴 (external 등급 · block 은 full 로 강등)
  │
  ▼
⑦ 저장 ───── 마스킹본 평문 + 응답 원문 AES-GCM (AAD 는 프롬프트와 다른 값)
```

**②가 ③보다, ③이 ④보다 먼저인 것이 계약이다.** ②를 ③ 뒤로 옮기면 원문이 무방비로 DB 에 남고,
②를 ④ 뒤로 옮기면 이미 나간 뒤다. **⑥이 ⑦보다 먼저인 것도 같은 이유다** — 뒤집으면 응답
원문이 평문으로 DB 에 남는다. ⑥⑦ 은 `scheduler._succeed()` 한 함수 안에 있고, 거기가
`jobs.response` 를 쓰는 유일한 지점이다(architecture.md §7.1-1).

---

## 4. 테넌시 계층과 격리 경계

```mermaid
flowchart TB
  subgraph PLATFORM["플랫폼 — 설치 1건"]
    PA["플랫폼 관리자"]
    BASE["베이스라인 가드 규칙 — 하한"]
    KEK["마스터 KEK"]
    POOL["공유 노드 풀"]
  end

  subgraph T1["테넌트 acme"]
    TA1["테넌트 관리자"]
    DEK1["DEK — KEK로 래핑"]
    R1["가드 규칙 — 조이기만 가능"]
    SV1["서비스 acme-web"]
    EU1["end_user u_8f3a — 해시"]
  end

  subgraph T2["테넌트 globex"]
    TA2["테넌트 관리자"]
    DEK2["DEK"]
    DED["전용 노드 — tenant_affinity"]
  end

  PA --> BASE
  PA --> POOL
  KEK --> DEK1
  KEK --> DEK2
  BASE -->|완화 불가| R1
  TA1 --> SV1 --> EU1
  POOL -.->|공유| T1
  POOL -.->|공유| T2
  DED --> T2
```

`BASE → R1` 의 "완화 불가" 가 핵심이다. 테넌트가 플랫폼의 PII 차단을 끌 수 있으면
제품의 보증이 사라진다.

---

## 5. 배치 결정

```mermaid
flowchart TB
  JOB["대기 잡 — 레인별 상위 N건만 스캔"]
  ST0{"기아 방지<br/>임계 초과 대기?"}
  F1{"enabled · draining 아님?"}
  F2{"healthy? — unknown이면 통과"}
  F3{"테넌트가 쓸 수 있는 노드?"}
  F4{"데이터 경계 허용?"}
  F5{"모델 서빙 가능?"}
  F6{"노드 메모리 예산?"}
  F7{"비용 예산 — 예약분 포함?"}
  F8{"노드 동시성 슬롯이 비었나?"}
  F9{"직전 실패 노드가 아닌가?"}
  SEL["선택: 티어 순서 → 모델 친화 → 최소 부하"]
  RES["원자적 예약 — 슬롯 · 메모리 · 비용"]
  DROP["이 노드 탈락 → 다음 후보"]

  JOB --> ST0
  ST0 -->|예| SEL
  ST0 -->|아니오| F1
  F1 -->|예| F2 -->|예| F3 -->|예| F4 -->|예| F5 -->|예| F6 -->|예| F7 -->|예| F8 -->|예| F9 -->|예| SEL
  F1 -->|아니오| DROP
  F2 -->|아니오| DROP
  F3 -->|아니오| DROP
  F4 -->|아니오| DROP
  F5 -->|아니오| DROP
  F6 -->|아니오| DROP
  F7 -->|아니오| DROP
  F8 -->|아니오| DROP
  F9 -->|아니오| DROP
  SEL --> RES
```

---

## 6. 모듈 구성

```mermaid
flowchart TB
  MAIN["main.py — 라우터 · build_app 주입식 조립"]
  AUTH["auth.py — 토큰 수명주기 · RBAC 2단 · 3단 레이트리밋"]
  IDENT["identity.py — 엔드유저 해싱"]
  GUARD["guard.py — 2단 필터 · 등급 · 계층"]
  CRYPTO["crypto.py — KEK / DEK"]
  STORE["store.py — Store 프로토콜 · 테넌트 스코프 초크포인트"]
  SCHED["scheduler.py — 레인 루프 · 기아 방지 · 테넌트 공정성"]
  CLUSTER["cluster.py — 레지스트리 · 헬스 · 원자적 예약 · 배치"]
  COST["cost.py — 예약 / 정산"]
  MODELS["models.py — 설치 요청 · 승인 · pull · 삭제 차단"]
  EVALS["evals.py — 정답셋 · 회귀 평가 · 승격 게이트"]
  PROV["providers/ — base · ollama · anthropic · mock"]
  I18N["i18n.py — 문자열 카탈로그 · 로케일 협상"]
  CONF["config.py — nodes · roles · pricing · lanes · guard · thresholds"]

  MAIN --> AUTH --> IDENT
  MAIN --> GUARD --> CRYPTO
  GUARD --> CLUSTER
  GUARD --> EVALS
  MAIN --> STORE
  MAIN --> I18N
  MAIN --> SCHED --> CLUSTER --> PROV
  CLUSTER --> COST
  CLUSTER --> MODELS
  SCHED --> STORE
  CONF --> CLUSTER
  CONF --> SCHED
  CONF --> GUARD
```

`guard.py → cluster.py` 화살표가 중요하다 — 가드 2단 분류도 배치를 거쳐 내부 노드에서 돈다.
동기 임베딩 경로도 같은 `cluster.py` 를 호출해서 배치·경계·비용을 우회하지 못하게 한다.

---

## 7. 장애 반경

**이 표의 각 행은 희망이 아니라 테스트다.** 예전 판은 서술문이라 구현이 표를 배신해도
드러나지 않았고, 실제로 배신하고 있었다 — "재기동 시 복구" 는 등록 노드가 영속화되지
않던 시절에는 성립하지 않았다. 재기동하면 노드가 사라져 복구된 잡이 갈 곳이 없었다.

그래서 각 행을 **검증 가능한 한 문장**으로 다시 쓰고, 그것을 고정하는 테스트를 함께 적는다.
표의 모든 행이 실재하는 테스트를 지목하는지는 `test_the_blast_radius_table_names_real_tests`
가 검사한다 — **표가 코드와 어긋나면 테스트가 실패한다.**

| 죽는 것 | 보증 (검증 가능한 문장) | 고정하는 테스트 |
|---|---|---|
| 컨트롤 플레인 | 재기동 후 **등록 노드 선언이 남아 있고**, `running` 이던 잡은 재큐되거나 `needs_review` 로 남는다 | `test_registered_nodes_survive_a_restart` · `test_running_jobs_are_recovered_not_lost` |
| internal 노드 1대 | 그 노드가 단독 호밍한 모델의 잡만 대기하고, 다른 모델의 잡은 남은 노드에서 계속 돈다 | `test_one_node_down_only_stalls_what_it_alone_hosted` |
| **internal 전멸** | 가드 2단 분류가 **판정을 만들지 않고** `on_classifier_error` 정책을 탄다 — "민감하지 않음" 으로 새지 않는다 | `test_no_internal_node_falls_through_to_the_classifier_error_policy` |
| external / 클라우드 | `placement` 가 internal 을 포함한 역할은 영향을 받지 않는다 | `test_losing_external_does_not_stall_internal_roles` |
| 노드망 전체 | `placement: [internal]` 역할의 잡은 **external 로 넘어가지 않고** 대기한다 | `test_internal_only_jobs_wait_rather_than_cross_the_boundary` |
| metered 노드 + 컨트롤 플레인 | 그 잡은 자동 재큐되지 않고 `needs_review` 로 남는다 — 이중 과금을 막는다 | `test_metered_jobs_are_not_requeued_after_a_crash` |

마지막에서 두 번째 행이 요지다 — **노드를 2대로 늘려도 노드망은 여전히 공통 의존성 1개다.**
그리고 그때의 올바른 동작은 폴백이 아니라 **대기**다. 경계를 넘어 계속 도는 것은
가용성이 아니라 유출이다.

**여기 없는 것**: 컨트롤 플레인 자체의 이중화. Starter·Standard 에서 SPOF 인 것은
의도된 선택이고 Scale 프로파일에서 해소한다. 한 호스트 안의 다중 워커는 지원 구성이지만
(architecture.md §11) 그것은 처리량이지 가용성이 아니다 — 호스트가 죽으면 워커도 전부 죽는다.
