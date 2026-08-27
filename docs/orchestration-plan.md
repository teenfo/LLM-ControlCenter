# 오케스트레이션 구현 계획 (2026-08-27)

[`design-audit.md`](design-audit.md) §4 의 판정 — **L1 스마트 라우팅: 채택,
L2 선언적 체인: 조건부, L3 동적 계획: 기각·보류** — 을 실행 계획으로 옮긴 문서다.
각 작업 항목에 ID · 대상 파일 · 수용 기준 · 상대 크기(S/M/L)를 달아 그대로
플랜(태스크 보드)으로 옮길 수 있게 썼다.

**전체 순서: Phase 0(선행 결함) → Phase 1(L1 라우팅) → [수요 확인 게이트] →
Phase 2(L2 체인).** Phase 0 없이 Phase 1 을 시작하지 않는다 — 라우팅은 가드
분류기와 같은 인프라를 재사용하므로, 그 인프라의 결함(audit.md H1·H2)이
그대로 라우팅의 결함이 된다.

---

## 0. 구현이 지켜야 할 불변식 — 수용 기준의 상위 조항

design-audit §4.0 의 5개 불변식을 **테스트 가능한 문장**으로 옮긴다. 모든 Phase 의
PR 은 이 다섯을 깨지 않음을 테스트로 증명해야 한다(기존
`tests/test_architecture.py` 방식).

| # | 불변식 | 테스트로 확인할 것 |
|---|---|---|
| I1 | 모든 홉이 가드를 지난다 | 라우팅 분류·체인 중간 산출물이 `pipeline` 관문 밖에서 잡을 만들지 않는다 (`store.create_job` 호출자는 `pipeline` 뿐 — 기존 아키텍처 테스트 확장) |
| I2 | 경계는 좁아지기만 한다 | 라우트/체인 단계가 역할의 `placement` · `internal_only` · 가드가 좁힌 `allowed_boundaries` 를 **넓히는 설정이 로드조차 되지 않는다** (설정 검증에서 거부) |
| I3 | 비용은 상한 선예약 | 라우팅 분류 비용은 가드 2단과 같은 방식으로 계상, 체인은 전 단계 합산 상한을 부모 잡에 예약 |
| I4 | 판정 호출은 마스킹본 · 내부 노드 | 라우터가 받는 텍스트는 `GuardResult.prompt_for(INTERNAL)`, 배치는 `allowed_boundaries=(INTERNAL,)` 고정 |
| I5 | 소비자 계약 불변 | `/v1/meta` · openapi 의 요청/응답 스키마가 변하지 않는다. 라우팅·체인 유무는 응답 모양에 나타나지 않는다 (`status` 의 진행 표시 제외) |

---

## Phase 0 — 선행 결함 해소

Phase 1 이 재사용하는 경로의 결함부터. 전부 audit.md 에서 확정된 항목이다.

| ID | 작업 | 파일 | 내용 | 크기 |
|---|---|---|---|---|
| P0-1 | 가드 겹침 스팬 병합 | `app/guard.py` `_apply()` | 치환 전 스팬 coalesce(겹침 시 더 강한 등급·넓은 스팬 채택). **실제 겹치는** 스팬 테스트 추가 | S |
| P0-2 | 정규식 캐시 키 수정 | `app/guard.py` `rules_for()` · `probe_rule()` | 캐시 키를 패턴 문자열로(또는 `(id, pattern)`), 상한 있는 LRU | S |
| P0-3 | 역할 기본 `system` 전달 | `app/scheduler.py` `_execute()` | `system = job.system_* or role.system` — 해시(`pipeline.py:307`)와 실전송을 일치시킨다. 라우팅 분류 프롬프트도 같은 경로를 쓴다 | S |
| P0-4 | 완료 이벤트 | `app/pipeline.py` `wait_for()` · `app/scheduler.py` | 잡 종결 시 `asyncio.Event` 발화, `wait_for` 는 이벤트 대기 + 폴링 폴백. **Phase 2 의 필수 선행**이고 Phase 1 과는 독립 — 병렬 진행 가능 | M |

**수용 기준**: audit.md H1·H2 재현 케이스가 테스트로 고정되어 통과. 기존 753+
테스트 전부 통과. P0-3 은 "system 을 생략한 요청의 실제 전송 system == 해시된
system" 을 목 프로바이더의 call_log 로 검증.

---

## Phase 1 — L1 스마트 라우팅

### 목표 한 문장

역할 안에 라우팅 정책을 선언하면, 제출 시점에 내부 소형 모델이 프롬프트
(마스킹본)를 분류해 그 역할의 여러 모델 중 하나를 고른다. **소비자는 아무것도
바꾸지 않고, 설정하지 않은 역할은 아무것도 달라지지 않는다.**

### 1.1 설정 스키마 (P1-1 · M)

`config/roles.yaml` 에 선택 절 `routing` 을 추가하고 `config.py` 가 파싱한다:

```yaml
summarize:
  model: qwen2.5:7b            # 기본 = 분류 실패 시 폴백 (fail-to-default)
  routing:
    classifier: _route_classify  # 내부 전용 분류 역할 (없으면 기동 시 거부)
    routes:
      simple:                    # 라우트 키 — 분류기가 고르는 어휘
        model: qwen2.5:1.5b
        description: "한두 문단 요약, 사실 추출"   # 분류 프롬프트의 재료
      complex:
        model: qwen2.5:32b
        description: "장문 분석, 추론이 필요한 요약"
        tier_models: {external: <cloud-model>}     # 선택 — 티어별 덮어쓰기
```

- `Role` 에 `routing: RoleRouting | None` 필드 추가.
  `RoleRouting = (classifier: str, routes: Mapping[str, RouteSpec])`,
  `RouteSpec = (model, description, tier_models)`.
- **설정 검증(기동 시 거부)**: ① `classifier` 역할이 존재하고 `internal_only`
  ② `routes` 비어 있지 않음 ③ 각 라우트 모델이 카탈로그에 존재 ④ 라우트에
  `placement` · `internal_only` 키가 **아예 없음**(I2 — 경계·배치는 역할의 것,
  라우트는 모델만 바꾼다) ⑤ `description` 필수(분류 근거가 없으면 판정 불가 —
  가드 LLM 규칙의 `description` 필수와 같은 근거).
- 시드 설정에 `_route_classify` 역할 추가(= `_guard_classify` 와 같은 모델·레인
  공유 가능. 별도 역할로 두는 이유: 오버라이드·인증을 독립적으로 걸 수 있게).

파일: `app/config.py`(Role·파싱·검증), `config/roles.yaml`(주석 포함 시드),
`tests/test_config.py`.

### 1.2 라우터 배선 (P1-2 · M)

`pipeline.make_classifier()` 와 **같은 패턴**으로 `pipeline.make_router()` 를
만든다 — 이 대칭이 구현의 안전 근거다:

```
make_router(role) → async route(masked_text) -> str | None
  1. routing 없으면 None (호출 자체를 안 함)
  2. 분류 역할 인증 게이트 — evaluator.classifier_is_certified(model)
     (가드와 같은 게이트: 구조화 출력을 못 지키는 모델로 라우팅하지 않는다)
  3. cluster.place(role=분류역할, allowed_boundaries=(INTERNAL,), ...)  ← I4
  4. provider.generate(_routing_prompt(masked_text, routes))
  5. _parse_route(): 마지막 비어 있지 않은 줄 → 단어 경계 → 알려진 라우트
     키와 교집합 (가드의 _parse_classification 과 동일 기법 — 공용 함수로 추출)
  6. 실패(배치 불가·타임아웃·파싱 불능·미인증) → None
```

**None 의 의미는 항상 "기본 모델"이다.** 가드의 `on_classifier_error` 같은
정책 축을 만들지 않는다 — 라우팅 실패는 보안 사건이 아니라 최적화 기회의
상실이므로, 정책 없이 fail-to-default 가 맞다. 단, 실패는 집계한다(1.6).

파일: `app/pipeline.py`, `tests/test_pipeline.py`.

### 1.3 판정 시점 = 제출 시, 저장 = 잡 스냅샷 (P1-3 · M)

아키텍처 §5(안정성은 스냅샷)를 따른다 — **라우팅 판정은 제출 시 1회, 잡에
스냅샷으로 고정**된다. 디스패치 시 판정하면 재시도마다 모델이 바뀌어 재현성이
깨진다.

- `pipeline.submit()`: 가드 통과 후 `route = await router(masked_internal)`,
  잡 생성 시 `route` 저장.
- 스키마: `jobs` 에 `route TEXT NULL` — **ADD COLUMN 전용 마이그레이션 원칙
  그대로**(`_MIGRATIONS` 에 1행). NULL = 라우팅 없음/실패 = 기본 모델.
- `scheduler._try_dispatch()`: 역할 해석 직후 **효과 역할 치환** —

  ```python
  role = self._config.roles.get(job.role)
  if job.route and role.routing and (spec := role.routing.routes.get(job.route)):
      role = replace(role, model=spec.model,
                     tier_models={**role.tier_models, **spec.tier_models})
  ```

  `cluster.place()` 는 `role.model_for_tier(tier)` 를 그대로 쓰므로
  **cluster.py 는 한 줄도 바뀌지 않는다.** "모델은 정책" 이므로 라우팅은 역할
  값의 치환으로 완결된다 — 이것이 이 설계가 제품 원칙과 정합하다는 증명이다.
- 역할 오버라이드(audit.md H4 수정 후)와의 우선순위: 오버라이드 → 라우팅 순으로
  적용(오버라이드가 routing 절 자체를 바꿀 수 있다).

파일: `app/store.py`(마이그레이션·JobRow), `app/pipeline.py`,
`app/scheduler.py`, `tests/test_store.py` · `test_scheduler.py`.

### 1.4 가드 2단과 호출 합치기 — 선택 최적화 (P1-4 · M, 뒤로 미룰 수 있음)

2단 분류가 어차피 도는 요청이면 분류 프롬프트에 라우팅 질문을 함께 실어
증분 호출을 0으로 만든다. **1차 릴리스에서는 하지 않는다** — 프롬프트 하나에
두 판정을 섞으면 파싱·인증·실패 처리가 얽힌다. 별도 호출로 먼저 출시하고,
계측(1.6)으로 증분 비용이 실제 문제일 때만 합친다. 합칠 때의 형태: 출력을
`rules: ...` / `route: ...` 2줄로 강제하고 각각 기존 파서를 태운다.

### 1.5 비용·용량 (P1-5 · S)

- 라우팅 분류 호출은 가드 분류와 같은 **전용 레인**(`guard`)을 탄다 — 대형
  batch 잡 뒤에 줄 서면 제출 지연이 그만큼 늘어난다(capacity §3 의 근거 동일).
- 분류는 내부 노드 = 비용 0 이지만 **용량은 소비한다**: `capacity.md` §3 표에
  "라우팅 분류 +1회(합치기 전)" 행 추가, 라우팅 켠 역할의 호출량 기준으로 산정.
- `thresholds.yaml` 에 라우팅 관련 임계는 추가하지 않는다(실패는 메트릭으로만).

파일: `docs/capacity.md`, `app/observability.py`.

### 1.6 관측·UI (P1-6 · S)

- 메트릭: `llmcc_route_decisions{role, route}` 카운터 + `llmcc_route_failures{role}`.
  라벨은 역할·라우트 키만 — **테넌트 라벨 금지 원칙 유지.** 라우트 키는 설정
  어휘라 카디널리티 유한.
- 잡 상세(관제 UI · `GET /v1/admin/jobs/{id}`)에 `route` 표시 — "왜 이 모델로
  갔는가" 에 답한다.
- `/v1/meta` · openapi: **변경 없음**(I5). 라우팅은 소비자에게 보이지 않는다.

파일: `app/observability.py`, `app/main.py`(잡 상세 필드), `static/app.js`.

### 1.7 라우터 품질 측정 (P1-7 · M)

가드 정답셋 체계(`evals.py`)를 재사용한다: 라우트 정답이 붙은 픽스처
(`텍스트 → 기대 라우트`)를 넣고, 라우팅 정확도를 측정·표시한다. 인증 게이트는
1.2 에서 이미 걸리므로(분류 역할 인증), 여기서는 **정확도 가시화**만 — 라우팅이
계속 틀리면 관리자가 routes 의 `description` 을 고치는 운영 루프를 만든다.
승격 게이트 같은 강제는 두지 않는다(오탐의 대가가 보안이 아니라 비용이므로).

파일: `app/evals.py`(kind 추가), `tests/test_evals.py`.

### Phase 1 테스트 목록 (필수)

1. routing 없는 역할 = 기존과 바이트 단위 동일 동작 (회귀 없음이 1번 테스트)
2. 분류 성공 → 잡의 `route` 저장 → 디스패치가 라우트 모델 사용 (목 call_log 검증)
3. 분류 실패(배치 불가 / 타임아웃 / 쓰레기 출력 / 미인증) 4종 → 전부 기본 모델
4. 라우터 입력이 마스킹본임을 검증 — PII 원문이 라우팅 분류 프롬프트에 없음 (I4)
5. 라우트가 placement 를 못 넓힘 — `placement`/`internal_only` 키 든 설정 로드 거부 (I2)
6. 재시도 시 라우트 불변 (스냅샷) · 오버라이드와의 우선순위
7. `/v1/meta` 불변 (I5) · 메트릭 라벨에 테넌트 없음
8. 아키텍처 테스트: 라우터의 잡 생성 경로 없음 (I1 — cluster.place 직접 호출만 허용, 가드 분류기와 동일 예외)

**Phase 1 완료 기준**: 위 8종 + 기존 전체 통과, Demo 프로파일에서 목 분류기로
라우팅 시연 가능(README 데모 표에 1행 추가), capacity·architecture 문서 갱신
(모듈 표 드리프트 테스트 있음 — `docs/architecture.md` §9 에 routing 언급 추가).

**총 크기: M~L** (P0 제외 순수 Phase 1 = 신규 코드 대략 가드 2단 배선과 동급.
P1-1~P1-3 이 코어이고 순차 의존, P1-5~P1-7 은 병렬 가능.)

---

## [게이트] Phase 2 진입 조건

Phase 2 는 아래 **전부**를 만족하기 전에 시작하지 않는다:

1. **수요 실재** — map-reduce 형(장문 분해·통합) 요구가 실제 테넌트에서 나옴
2. P0-4(완료 이벤트) 머지 — 없으면 체인 지연이 단계 수 × 폴링 간격으로 곱해진다
3. audit.md M11(update_job CAS) 해소 — 부모/자식 잡 상태 전이의 원자성 전제
4. design-audit G1(출력 검사) 설계 확정 — 중간 산출물 검사가 같은 메커니즘을 쓴다

---

## Phase 2 — L2 선언적 체인 (조건부 — 게이트 통과 후 상세 설계)

Phase 1 과 달리 여기는 **방향만 고정하고 상세는 게이트 통과 후** 잡는다.
지금 확정하는 것은 형태 제약과 작업 분해뿐이다.

### 형태 제약 (지금 확정)

- **선형 체인 + map 1단**만 허용한다. 임의 DAG 금지 — 스케줄러가 단순하게
  남고, 경계 교집합·비용 상한이 선언 시점에 계산 가능해진다.
- 체인은 역할 `kind: chain` + `steps:` 목록으로 선언한다. 각 step 은 기존
  역할을 참조한다(새 실행 단위를 만들지 않는다 — 역할 재사용).
- 체인의 허용 경계 = **전 단계 역할의 placement 교집합**, 설정 로드 시 계산해
  비면 거부(I2). 실행 중 축소는 기존 `effective_placement` 메커니즘.
- 부모 잡 1개 + 단계별 자식 잡. 소비자는 부모 잡만 본다(I5). `status` 에
  `step: k/n` 만 추가.

### 작업 분해 (게이트 통과 시 상세화)

| ID | 작업 | 핵심 결정 사항 | 크기 |
|---|---|---|---|
| P2-1 | 역할 스키마 `kind: chain` + steps 파싱·검증 | 경계 교집합 선계산, step 역할 존재 검증, map 단계의 분할 함수(문자 수 기준 청크) | M |
| P2-2 | 잡 모델 확장 | `parent_job_id` · `step_index` ADD COLUMN, 부모 상태 머신(자식 전부 ok → 부모 ok), CAS 전제 | M |
| P2-3 | 중간 산출물의 가드 재통과 | 단계 산출물 → 다음 단계 입력 시 `pipeline` 관문 재진입(I1). 산출물 저장은 G1 의 응답 정책을 따름 | M |
| P2-4 | 비용 체인 상한 | 부모에 Σ(단계 상한) 선예약, 단계 정산마다 부모 예약 차감 조정 | M |
| P2-5 | 스케줄러 단계 전이 | 자식 완료 이벤트(P0-4) → 다음 단계 자식 생성. 실패 = 체인 실패, 재시도는 실패 단계부터 | L |
| P2-6 | API·UI | 부모 잡 status 에 step 진행, 관제 UI 체인 뷰 | S |
| P2-7 | 테스트 | 경계 교집합 강제, 부분 실패·재기동 복구(부모/자식 정합), 비용 상한, fan-out 통합 | L |

**총 크기: L** — Phase 1 의 2~3배. 이것이 "조건부" 인 이유다.

---

## 하지 않는 것 (재확인 — design-audit §4.3)

| 항목 | 판정 | 한 줄 근거 |
|---|---|---|
| 서버측 프롬프트 재작성·증강 | **기각** | "프롬프트는 호출자 소유" 계약 위반. 표준 지시문은 역할 `system` 의 몫 |
| LLM 동적 계획(에이전트 루프) | **보류** | 경계·비용 사전 예약(I2·I3)과 원리적 충돌. 제품은 루프의 각 호출을 통제하는 게이트웨이로 남는다 |
| 임의 DAG | **기각** | 선형+map 으로 실수요를 덮고, 스케줄러 복잡도가 통제 범위에 남는다 |

---

## 리스크와 완충

| 리스크 | 완충 |
|---|---|
| 라우팅 분류 지연이 제출 경로에 추가된다 (~0.5s) | 전용 레인 + 소형 모델 전제. 계측 후 문제면 P1-4(호출 합치기). 라우팅은 역할 단위 옵트인이라 지연에 민감한 역할은 안 켜면 된다 |
| 분류기 어휘(routes 키)를 모델이 못 지킴 | 가드와 같은 인증 게이트 + 결정론 파싱 + fail-to-default. 최악의 경우에도 기본 모델로 동작 — 기존보다 나빠질 수 없다 |
| 스키마 마이그레이션 | ADD COLUMN 전용 원칙 유지 — `route` · `parent_job_id` · `step_index` 전부 NULL 허용 추가 컬럼. 구버전이 신버전 DB 를 읽는 전진 호환 유지 |
| 라우팅이 비용 통제를 우회 | 불가 — 라우트는 모델만 바꾸고 예산·배치 필터는 기존 경로 그대로 탄다. 라우트 모델이 metered 면 기존 예약·정산이 그대로 적용 |
| Phase 2 를 시작해 놓고 수요가 없음 | 게이트 1번(수요 실재)이 문서화된 진입 조건 — "만들었으니 쓰라" 를 구조적으로 방지 |

## 마일스톤 요약

```
M0  Phase 0 (P0-1 ~ P0-3 필수, P0-4 병렬)          — 선행 결함 해소
M1  P1-1 → P1-2 → P1-3 (순차, 코어)                — 라우팅 동작
M2  P1-5 · P1-6 · P1-7 (병렬) + 테스트 8종 + 문서   — 라우팅 출시 가능
─── 게이트: 수요 · P0-4 · M11 · G1 ───
M3  P2-1 ~ P2-7                                    — 체인 (조건부)
```
