# 기능 명세 — 지금 구현되어 있는 것

이 문서는 **현재 동작의 설명서**입니다. 기능 점검과 고도화 대상 선정에 쓰라고 만들었습니다.

문서 셋의 역할이 다릅니다. 섞어 읽으면 안 됩니다.

| 문서 | 답하는 질문 |
|---|---|
| **이 문서** | 이 제품이 지금 **무엇을 하는가** |
| [architecture.md](architecture.md) | **왜** 그렇게 만들었는가 |
| [design-decisions.md](design-decisions.md) | 감사 지적에 대한 **판정과 해제 조건** |
| [plan.md](plan.md) | 착수 시점의 계획 (역사, 현재 명세 아님) |
| [README](../README.md) 부채 표 | **하지 않는 것** |

각 항목의 근거는 여기서 다시 논증하지 않고 위 문서를 가리킵니다.

## 읽는 법

```
  ### AUTH-1  기능 이름
  정의   무엇을 하는가
  표면   소비자가 닿는 지점 (라우트 · CLI · 설정 키 · UI)
  구현   코드 위치 (파일:함수)
  계약   지켜지는 불변식 — 어기면 테스트가 실패한다
  고정   그 계약을 고정하는 테스트 이름
  상태   구현됨 | 부분 | 미구현
```

- ID 는 도메인 접두사 + 번호입니다. 순번을 안 쓰는 이유는 중간에 기능이 끼면 전부 밀리기 때문입니다.
- `상태` 는 세 값만 씁니다: **구현됨 · 부분 · 미구현**. "다음 라운드" 같은 상대 시점 표현은 쓰지 않습니다 — 적은 날에만 참인 문장입니다.
- `부분` · `미구현` 항목은 [§ 고도화 후보](#-고도화-후보) 에 모아 판정 ID 와 함께 다시 나옵니다.

이 문서가 코드와 어긋나면 `tests/test_feature_spec.py` 가 실패합니다. 손으로 관리하는 표는 반드시 어긋나기 때문입니다(architecture.md §13-8).

---

## 1. 신원 · 인증 · 권한

주 모듈: `auth.py` `identity.py` `roles.py`

### AUTH-1  서비스 토큰 발급
정의   테넌트 관리자가 자기 서비스용 토큰을 발급한다. 발급 값은 그 응답에서 한 번만 보인다.
표면   `POST /v1/admin/tokens` · `GET /v1/admin/tokens` · 관제 UI 토큰 탭
구현   `app/auth.py:issue_token` `generate_token` `require_can_issue`
계약   원문 토큰은 DB 에 저장되지 않는다(해시만) · 목록 조회는 비밀을 노출하지 않는다
       발급 권한 판정은 한 곳에만 있다 — 회전을 발급 우회로로 쓸 수 없다
고정   `test_auth.py::test_raw_token_is_never_stored`
       `test_auth.py::test_token_listing_does_not_expose_the_secret`
       `test_auth.py::test_the_issue_rule_lives_in_one_place`
상태   구현됨

### AUTH-2  토큰 회전과 폐기
정의   토큰을 새로 내주고 옛 토큰은 유예 기간 동안만 함께 살려 둔다. 폐기는 즉시 끊는다.
표면   `POST /v1/admin/tokens/{token_id}/rotate` · `DELETE /v1/admin/tokens/{token_id}`
구현   `app/auth.py:rotate_token`
계약   유예 창 안에서는 구 토큰도 인증된다 · 창이 지나면 죽는다
       플랫폼 토큰 회전이 새 플랫폼 토큰을 찍는 경로가 되지 않는다
고정   `test_auth.py::test_rotation_grace_window_keeps_old_alive`
       `test_auth.py::test_rotation_issues_new_and_kills_old`
       `test_auth.py::test_rotating_a_platform_token_is_not_a_way_to_mint_one`
상태   구현됨

### AUTH-3  3단 레이트리밋
정의   테넌트 → 서비스 → 엔드유저 세 단계로 초당 요청 수를 제한한다. 각 단계는 독립이다.
표면   모든 소비자 라우트 · `config/thresholds.yaml` · `PUT /v1/admin/settings`
구현   `app/auth.py:RateLimits` `RateLimiter` `limits_for`
계약   카운터는 프로세스 메모리가 아니라 스토어에 있다 — 워커를 N개 띄워도 실효 한도가 N배가 되지 않는다
       테넌트 상한은 서비스를 더 만들어 우회할 수 없다 · 거부된 요청은 할당량을 소비하지 않는다
       오류 응답이 어느 단계에서 걸렸는지와 언제 다시 오라는지를 말한다
고정   `test_auth.py::test_counter_lives_in_the_store_not_process_memory`
       `test_auth.py::test_tenant_ceiling_cannot_be_bypassed_with_more_services`
       `test_auth.py::test_rejected_request_does_not_consume_quota`
       `test_auth.py::test_a_rate_limited_reply_says_when_to_come_back`
상태   구현됨

### AUTH-4  권한 등급
정의   플랫폼 관리자 · 테넌트 관리자 · 서비스 토큰 세 등급. 상위가 하위를 포함하되 역방향은 없다.
표면   `/v1/platform/*` (플랫폼) · `/v1/admin/*` (테넌트) · 나머지 소비자 라우트
구현   `app/auth.py:require_platform_admin` `require_tenant_admin` `authenticate`
계약   테넌트 관리자는 플랫폼 작업을 할 수 없다 · 서비스 토큰은 관리자가 아니다
       정지된 테넌트의 토큰은 인증되지 않는다
고정   `test_auth.py::test_tenant_admin_cannot_do_platform_things`
       `test_auth.py::test_service_token_is_not_an_admin`
       `test_auth.py::test_token_of_suspended_tenant_is_rejected`
상태   구현됨

### AUTH-5  엔드유저 신원 해싱
정의   호출자가 넘긴 엔드유저 식별자를 그대로 저장하지 않고 테넌트별 솔트로 해싱한다.
표면   요청 본문의 `end_user` 필드 · `GET /v1/admin/usage` 의 엔드유저 축
구현   `app/identity.py:new_salt` `hash_end_user` `hash_prompt` `looks_like_pii`
계약   이메일이 DB 에 원문으로 남지 않는다 · 같은 사람이라도 테넌트가 다르면 다른 해시가 된다
       한 테넌트 안에서는 안정적이다(집계가 가능해야 하므로)
       PII 모양 식별자는 **표시만** 하고 막지는 않는다 — 막으면 호출자가 우회한다
고정   `test_auth.py::test_email_as_end_user_never_lands_in_the_database`
       `test_auth.py::test_same_end_user_hashes_differently_per_tenant`
       `test_auth.py::test_pii_shaped_identifiers_are_flagged_not_blocked`
상태   구현됨

### AUTH-6  역할 접근 제어
정의   토큰마다 쓸 수 있는 역할 목록을 두고, 그 밖의 역할 호출을 거부한다.
표면   `GET /v1/roles` · `POST /v1/admin/tokens` 의 `allow_roles`
구현   `app/auth.py:check_role_allowed` · `app/meta.py:visible_roles`
계약   목록에 없는 역할은 호출도 조회도 안 된다 · 밑줄로 시작하는 내부 역할은 와일드카드로도 안 보인다
고정   `test_auth.py::test_allow_roles_gate`
       `test_meta.py::test_internal_roles_are_invisible_even_with_wildcard`
       `test_pipeline.py::test_underscore_roles_are_internal`
상태   구현됨

### AUTH-7  역할 오버라이드
정의   테넌트가 역할의 일부 값(모델·타임아웃 등)을 자기 범위에서 덮어쓴다.
표면   `GET/POST/DELETE /v1/admin/overrides` · `config/roles.yaml` 이 기본값
구현   `app/roles.py:RoleResolver` `resolver_for`
계약   경계·레인 같은 **얼어붙은 필드**는 오버라이드 대상이 아니다 — 덮으려 하면 거부된다
       오버라이드는 테넌트 범위를 넘지 않는다
고정   `test_architecture.py::test_frozen_role_fields_are_not_overridable`
       `test_store.py::test_role_overrides_are_scoped`
상태   구현됨

---

## 2. 요청 파이프라인

주 모듈: `pipeline.py` `tokens.py` `completion.py`

### PIPE-1  제출 순서 계약
정의   ①인증 → ②가드 → ②-b라우팅 → ③저장 → ④배치 → ⑤실행 → ⑥출력 검사 → ⑦정산. 이 순서가 안전 보증이다.
표면   `POST /v1/generate` · `POST /v1/embed`
구현   `app/pipeline.py:Pipeline.submit` (`_authorize` `_inspect` `_route` `_seal` `_create_job`)
계약   가드는 **잡 행이 생기기 전에** 돈다 — ② 를 ③ 뒤로 옮기면 원문이 무방비로 DB 에 남는다
       잡을 만드는 곳은 `pipeline.py` 한 곳뿐이다(다른 모듈에서 잡 생성 금지)
       프롬프트 해시는 마스킹 **후에** 계산된다
고정   `test_pipeline.py::test_guard_runs_before_the_job_row_exists`
       `test_architecture.py::test_only_the_pipeline_creates_jobs`
       `test_pipeline.py::test_prompt_hash_is_computed_after_masking`
상태   구현됨

### PIPE-2  동기·비동기 흡수
정의   `wait` 값 하나로 같은 엔드포인트가 동기 응답과 작업 접수를 모두 처리한다.
표면   `POST /v1/generate` 의 `wait` 파라미터
구현   `app/pipeline.py:Pipeline.wait_for` · `app/completion.py:CompletionSignal`
계약   대기는 상태 컬럼만 읽는다(프롬프트 본문을 읽지 않는다) · 폴링 간격은 뒤로 물러난다
       스케줄러가 종결시키면 대기 중인 요청이 깨어난다 · 재시도는 대기자를 깨우지 않는다
고정   `test_pipeline.py::test_waiting_reads_only_the_status_column`
       `test_completion.py::test_the_scheduler_wakes_a_waiting_request`
       `test_completion.py::test_a_retry_does_not_wake_the_waiter`
상태   구현됨

### PIPE-3  멱등성 키
정의   같은 `Idempotency-Key` 로 다시 보내면 새 잡을 만들지 않고 원래 잡을 돌려준다.
표면   요청 헤더 `Idempotency-Key` · `GET /v1/meta` 가 헤더를 문서화한다
구현   `app/store.py` 유일 인덱스 + `app/pipeline.py:Pipeline.submit`
계약   키는 테넌트·서비스를 가로지르지 않는다 · 빈 키는 키가 아니다 · 길이 상한이 있다
       재시도가 두 번 과금되지 않는다 · 키가 만료돼도 잡 행은 남는다
       **페이로드를 비교하지 않는다** — 같은 키로 다른 프롬프트를 보내면 첫 잡이 온다
고정   `test_idempotency.py::test_the_same_key_returns_the_same_job`
       `test_idempotency.py::test_the_retry_does_not_charge_twice`
       `test_idempotency.py::test_keys_do_not_collide_across_tenants`
       `test_multiprocess.py::test_only_one_process_creates_the_job_for_a_key`
상태   부분 — 페이로드 비교 없음, `/v1/embed` 미적용 (D7)

### PIPE-4  작업 조회와 취소
정의   작업 상태를 조회하고, 대기 중인 작업을 취소한다. 실행 중인 작업은 취소할 수 없다.
표면   `GET /v1/jobs/{job_id}` · `DELETE /v1/jobs/{job_id}`
구현   `app/pipeline.py:Pipeline.cancel` `_retry_after` `_queue_position`
계약   대기 중이면 큐 위치에 맞춘 적응형 `retry_after` 가 함께 온다
       취소는 잡아 둔 비용 예약을 풀어 준다 · 큐 위치는 행을 읽지 않고 센다
고정   `test_pipeline.py::test_cancel_releases_the_cost_reservation`
       `test_capacity.py::test_the_queue_position_counts_instead_of_reading_rows`
       `test_capacity.py::test_the_poll_does_not_materialise_the_queue`
상태   구현됨

### PIPE-5  임베딩 경로
정의   임베딩은 동기로 처리하되 가드·배치·경계·비용은 생성과 같은 관문을 지난다.
표면   `POST /v1/embed`
구현   `app/pipeline.py:Pipeline.embed`
계약   입력마다 벡터 하나 · 입력은 각각 독립적으로 마스킹된다
       실패해도 슬롯과 예약이 반드시 풀린다
고정   `test_pipeline.py::test_embed_masks_each_input_independently`
       `test_pipeline.py::test_embed_releases_the_slot_even_on_failure`
       `test_pipeline.py::test_embed_frees_the_reservation_when_placement_fails`
상태   구현됨

### PIPE-6  입력 토큰 상한 추정
정의   과금 노드로 보내기 전에 입력 토큰 수를 위로 추정해 비용 예약에 반영한다.
표면   내부 동작 (소비자 표면 없음) · `config/pricing.yaml`
구현   `app/tokens.py:estimate_input_tokens` `estimate_outbound_tokens`
계약   한국어를 영어처럼 세지 않는다 · 추정은 **높게** 틀린다(낮게 틀리면 예산을 넘긴다)
       디스패치 경로가 대기 중인 잡의 입력 토큰까지 예약한다
고정   `test_cluster.py::test_korean_is_not_counted_as_if_it_were_english`
       `test_cluster.py::test_the_estimator_errs_high_not_low`
       `test_scheduler.py::test_the_dispatch_path_reserves_input_tokens_for_a_queued_job`
상태   구현됨

---

## 3. 가드 — 걸러내기

주 모듈: `guard.py` `evals.py` · 설정: `config/guard.yaml`

### GUARD-1  1단 결정론적 패턴
정의   주민번호·카드·전화·여권·SSN·마이넘버처럼 형식이 정해진 값을 정규식으로 찾는다. 마이크로초 단위, 노드 호출 없음.
표면   `config/guard.yaml` `locale_packs` · `GET /v1/platform/guard/baseline` · 관제 UI 가드 탭
구현   `app/guard.py:Guard._scan` `_match_spans` `normalize_for_match`
계약   유니코드 정규화로 전각·zero-width 우회를 막는다 · 겹치는 매치도 전부 마스킹된다
       연속 매치가 서로의 위치를 밀지 않는다 · 시스템 프롬프트도 같이 검사한다
       로케일 팩을 끄면 그 나라 PII 는 안 잡힌다(끈 것이 보이게)
고정   `test_guard.py::test_multiple_matches_are_all_masked`
       `test_guard.py::test_sequential_matches_do_not_corrupt_the_text`
       `test_guard.py::test_system_prompt_is_masked_too`
       `test_guard.py::test_locale_pack_off_means_that_countrys_pii_is_not_caught`
상태   구현됨

### GUARD-2  체크섬 검증
정의   숫자 형식만 맞는 값과 진짜 값을 가른다. Luhn(카드) · 주민번호 · 사업자번호 · 마이넘버 · IBAN.
표면   `config/guard.yaml` 규칙의 `checksum` 키
구현   `app/guard.py:luhn` `kr_rrn` `kr_biz` `jp_mynumber` `iban_mod97`
계약   설정에 적힌 모든 체크섬은 실제 구현이 있다 · 체크섬 실패 시 본문을 건드리지 않는다
       한국어 조사가 붙은 표기(`주민번호는123456-1234567입니다`)도 잡는다
고정   `test_guard.py::test_every_configured_checksum_has_an_implementation`
       `test_guard.py::test_checksum_suppresses_false_positives_in_real_config`
       `test_guard.py::test_checksum_failure_leaves_text_untouched`
상태   구현됨

### GUARD-3  2단 LLM 맥락 분류
정의   "환자 진료 기록" 처럼 형식이 없는 것을 관리자가 문장으로 정의하면 내부 노드 분류기가 판정한다.
표면   `config/guard.yaml` `context_rules` · `POST /v1/admin/guard/rules`
구현   `app/guard.py:Guard.set_classifier` `_apply_classifier_failure` · `app/pipeline.py:make_classifier` `_classify_on_cluster`
계약   분류기는 **internal 경계 노드에서만** 돈다 · 분류기는 마스킹본을 보지 원문을 안 본다
       인증(GUARD-13)을 통과한 모델만 쓴다 · 규칙이 없으면 분류기를 호출하지 않는다
       **실패는 판정이 아니다** — `on_classifier_error` 정책을 태운다(조용히 통과 아님)
고정   `test_pipeline.py::test_classifier_runs_only_on_internal_nodes`
       `test_guard.py::test_classifier_receives_masked_text_not_raw`
       `test_guard.py::test_classifier_failure_is_not_a_verdict`
       `test_pipeline.py::test_uncertified_classifier_model_is_refused`
상태   구현됨

### GUARD-4  등급 사다리
정의   `audit` → `partial` → `full` → `block` 네 단계. 기록만 → 일부 가리기 → 전부 가리기 → 차단.
표면   `config/guard.yaml` 규칙의 `grade` · `POST /v1/admin/guard/rules`
구현   `app/guard.py:Guard.rules_for` `soften` · `app/guard.py:GuardResult`
계약   `audit` 는 통과시키되 기록한다 · `partial` 은 꼬리만 남긴다(전체를 남길 수 없다)
       `block` 은 모든 경계에서 차단이면 요청 자체를 막는다
고정   `test_guard.py::test_audit_grade_passes_through_but_records`
       `test_guard.py::test_partial_masking_keeps_the_tail`
       `test_architecture.py::test_partial_masking_cannot_keep_the_whole_value`
       `test_guard.py::test_block_on_every_boundary_blocks_the_request`
상태   구현됨

### GUARD-5  경계별 마스킹본
정의   같은 프롬프트에서 internal 용과 external 용 두 벌을 만든다. 어디로 나가느냐에 따라 가리는 강도가 다르다.
표면   `config/guard.yaml` 규칙의 경계별 등급 · `config/roles.yaml` 의 `placement`
구현   `app/guard.py:GuardResult.prompt_for` · `app/pipeline.py:_candidate_boundaries`
계약   경계별 등급이 다르면 마스킹본이 두 벌 생긴다 · 규칙이 경계를 좁히면 잡이 그 경계를 넘지 않는다
       스케줄러는 나가는 방향에 맞는 변형을 보낸다
고정   `test_guard.py::test_boundary_specific_masking_produces_two_variants`
       `test_pipeline.py::test_narrowed_job_never_lands_on_an_external_node`
       `test_scheduler.py::test_external_variant_is_sent_when_leaving_the_boundary`
상태   구현됨

### GUARD-6  유예 모드
정의   도입 첫날은 `block` 을 마스킹으로 낮춰 서비스가 서지 않게 한다. 끄는 것이 정상 상태다.
표면   `POST /v1/platform/guard/grace-mode` · 관제 UI 상시 표시
구현   `app/guard.py:Guard.grace_mode` `set_grace_mode` `soften`
계약   유예 모드가 켜져 있다는 사실이 관제 화면에 계속 보인다
고정   `test_packaging.py::test_demo_pii_samples_are_synthetic_and_actually_trip_the_guard`
       `test_guard.py::test_tenant_can_tighten_a_baseline_rule`
상태   구현됨

### GUARD-7  테넌트 규칙 — 강화만
정의   테넌트가 자기 규칙을 추가하거나 베이스라인을 더 세게 만들 수 있다. 느슨하게는 못 한다.
표면   `GET/POST /v1/admin/guard/rules` · `DELETE /v1/admin/guard/rules/{rule_id}`
구현   `app/guard.py:Guard.rules_for` `validate_rule` · `app/store.py` `tenant_guard_rules`
계약   베이스라인 완화 시도는 거부된다 · 강화는 경계별로 따로 걸린다
       테넌트가 더한 규칙이 실제로 마스킹한다 · 규칙은 테넌트를 가로지르지 않는다
고정   `test_guard.py::test_tenant_cannot_loosen_a_baseline_rule`
       `test_guard.py::test_tiered_tightening_is_per_boundary`
       `test_guard.py::test_tenant_added_rule_actually_masks`
상태   구현됨

### GUARD-8  시크릿 탐지
정의   API 키·PEM 개인키·자사 서비스 토큰 같은 자격증명을 프롬프트에서 걸러낸다. 로케일과 무관하게 항상 켜져 있다.
표면   `config/guard.yaml` `secrets` 팩 (`always_on`)
구현   `app/guard.py:credential_shape` · `config/guard.yaml` 시크릿 규칙
계약   값이 실제로 제거된다(표시만 하지 않는다) · PEM 은 헤더뿐 아니라 본문까지 가린다
       하이픈 식별자·해시 길이 값은 시크릿이 아니다 · 오탐률이 승격 게이트를 통과한다
       **엔트로피 탐지는 기각됐다** — 실측에서 진짜 시크릿과 UUID·git SHA 를 못 갈랐다
고정   `test_secrets.py::test_every_secret_is_detected`
       `test_secrets.py::test_the_pem_body_is_masked_not_just_the_header`
       `test_secrets.py::test_the_false_positive_rate_clears_the_promotion_gate`
       `test_secrets.py::test_entropy_cannot_separate_secrets`
상태   부분 — 벤더 접두사도 대입 문맥도 없는 맨몸 자격증명은 못 잡음 (G2)

### GUARD-9  인젝션 내성
정의   분류기 프롬프트에서 지시와 자료를 울타리로 분리하고, 카나리아로 지시 이탈을 검출한다.
표면   `config/guard.yaml` `injection` 팩 (`always_on`) · `GET /v1/meta` 가 제어 토큰을 경고
구현   `app/pipeline.py:ClassifierFraming` `ClassifierEvaded` `vocabulary_in_last_line`
계약   울타리 토큰은 본문에서 추측할 수 없고 본문에 나타나지 않는다 · 지시는 자료 뒤에도 온다
       **카나리아 부재는 실패지 판정이 아니다** · 카나리아 줄을 답으로 읽지 않는다
고정   `test_injection.py::test_the_fence_cannot_be_guessed_from_the_text`
       `test_injection.py::test_a_missing_canary_is_a_failure_not_a_verdict`
       `test_injection.py::test_the_canary_line_is_not_read_as_an_answer`
       `test_injection.py::test_every_attack_is_detected`
상태   부분 — 카나리아는 형식 포기형만 잡음, 반향형은 울타리·탐지 팩이 맡음 (G4/D9)

### GUARD-10  출력 검사
정의   모델이 만든 응답도 저장·전달 전에 검사해 마스킹한다. 스케줄러의 응답 쓰기 지점이 유일한 관문이다.
표면   `POST /v1/generate` 응답 · `GET /v1/admin/jobs/{job_id}/raw`
구현   `app/guard.py:Guard` 출력 경로 · `app/scheduler.py:_succeed`
계약   응답은 저장 **전에** 마스킹된다 · 차단 규칙도 응답에서는 마스킹으로 동작한다(응답을 버리지 않는다)
       원문 응답은 버려지지 않고 봉인된다 · 응답 암호문을 프롬프트 자리에 옮겨 심을 수 없다
       응답 쓰기는 스케줄러 한 곳에서만 일어난다
고정   `test_output_guard.py::test_the_response_is_masked_before_it_is_stored`
       `test_output_guard.py::test_a_blocking_rule_masks_the_response_instead_of_dropping_it`
       `test_output_guard.py::test_the_response_ciphertext_cannot_be_transplanted_into_the_prompt`
       `test_architecture.py::test_only_the_scheduler_writes_the_response`
상태   부분 — 1단 패턴만, 2단 LLM 맥락 분류는 입력 전용 (G1/D1)

### GUARD-11  정답셋과 회귀 평가
정의   규칙마다 양·음성 표본을 두고, 규칙이 깨지면 평가가 잡아낸다.
표면   `GET/POST /v1/platform/evals` · 관제 UI 평가 탭
구현   `app/evals.py:Evaluator` `RuleEval` · `app/store.py` `eval_fixtures` `eval_runs`
계약   번들 표본은 실재하는 규칙을 가리킨다 · 출고 패턴 규칙에는 표본이 있다
       출고 규칙은 자기 표본을 통과한다 · 규칙이 깨지면 회귀가 잡힌다
       테넌트 표본은 그 테넌트에만 영향을 준다
고정   `test_evals.py::test_every_bundled_fixture_targets_a_real_rule`
       `test_evals.py::test_every_shipped_pattern_rule_has_fixtures`
       `test_evals.py::test_regression_is_caught_when_a_rule_breaks`
       `test_evals.py::test_tenant_fixtures_only_affect_that_tenant`
상태   구현됨

### GUARD-12  오탐 검토와 승격 게이트
정의   `audit` 등급의 탐지를 사람이 검토하고, 오탐률이 낮고 표본이 충분할 때만 `block` 승격을 허용한다.
표면   `GET /v1/admin/guard/events` · `POST /v1/admin/guard/events/{event_id}/review` · `GET /v1/admin/guard/rules/{rule_id}/promotion`
구현   `app/evals.py:ReviewRate` `PromotionVerdict` `Evaluator`
계약   검토 수가 모자라면 승격이 막힌다 · 오탐률이 높아도 막힌다
       검토는 판정만 저장하고 내용은 저장하지 않는다 · 등급을 낮추는 것은 승격이 아니다
       마스킹 등급은 게이트 대상이 아니다(차단만 게이트한다)
고정   `test_evals.py::test_promotion_blocked_without_enough_reviews`
       `test_evals.py::test_promotion_blocked_when_false_positive_rate_is_high`
       `test_evals.py::test_review_stores_verdict_not_content`
       `test_evals.py::test_masking_grades_are_not_gated`
상태   구현됨

### GUARD-13  분류기 인증
정의   판정용 모델이 구조화 출력 계약을 지키는지 미리 재고, 통과한 모델만 분류에 쓴다.
표면   내부 동작 · `GET /v1/platform/evals` 가 인증 결과를 보여준다
구현   `app/evals.py:Evaluator.certify` `ComplianceReport` · `app/pipeline.py:make_certifier`
계약   스키마를 깨는 모델은 거부된다 · 미인증 모델은 기본적으로 거부된다
       인증에는 최소 표본 수가 필요하다 · 노드가 없어 못 한 시도는 실패로 기록되지 않는다
고정   `test_evals.py::test_classifier_that_breaks_schema_is_rejected`
       `test_evals.py::test_uncertified_classifier_is_refused_by_default`
       `test_evals.py::test_certification_needs_a_minimum_sample_size`
       `test_routing.py::test_certification_is_not_polluted_by_missing_nodes`
상태   구현됨

---

## 4. 암호화 · 감사

주 모듈: `crypto.py` `keyrotation.py` `store.py`

### CRYPTO-1  봉투 암호화 (KEK / DEK)
정의   원문은 테넌트별 DEK 로 AES-GCM 봉인하고, DEK 는 마스터 KEK 로 래핑해 DB 에 둔다.
표면   `keys/master.key` (파일) · `GET /v1/admin/jobs/{job_id}/raw` 가 복호화 경로
구현   `app/crypto.py:KeyVault` `Sealed` `prompt_aad` `response_aad`
계약   마스터 키가 없으면 볼트가 꺼지고 **평문 폴백 경로가 존재하지 않는다**
       레코드마다 새 논스를 쓴다 · 한 테넌트의 DEK 는 다른 테넌트 암호문을 못 연다
       변조된 암호문은 거부된다 · 암호문을 다른 잡으로 옮겨 심을 수 없다(AAD 결합)
고정   `test_guard.py::test_no_plaintext_fallback_path_exists`
       `test_guard.py::test_each_record_gets_a_fresh_nonce`
       `test_guard.py::test_one_tenants_dek_cannot_open_anothers`
       `test_purge.py::test_ciphertext_cannot_be_transplanted_into_another_job`
상태   구현됨

### CRYPTO-2  마스터 KEK 회전
정의   유출 대응 절차. 암호문을 다시 암호화하지 않고 DEK 래핑만 교체한다.
표면   `python -m app rotate-kek` · `python -m app doctor` 가 상태를 진단
구현   `app/keyrotation.py:rotate_master_kek` `staged_key_path` `interrupted` `latest_retired`
계약   암호문은 한 줄도 다시 쓰이지 않는다 · 옛 키는 지우지 않고 보관한다
       어느 단계에서 죽어도 **두 키가 디스크에 남는다** — 중단된 회전은 다음 회전을 거부한다
       스테이징 키는 DB 커밋 **전에** fsync 된다 · `doctor` 는 유일하게 열리는 키를 지우라고 말하지 않는다
고정   `test_keyrotation.py::test_no_ciphertext_is_rewritten`
       `test_keyrotation.py::test_an_interrupted_rotation_leaves_both_keys_on_disk`
       `test_keyrotation.py::test_the_staged_key_is_fsynced_before_the_db_commit`
       `test_keyrotation.py::test_doctor_never_tells_you_to_delete_the_only_working_key`
상태   구현됨 — DEK 회전은 기각(D4/G5), 런북은 [runbook-key-compromise.md](runbook-key-compromise.md)

### CRYPTO-3  crypto-shredding
정의   테넌트를 파기할 때 DEK 를 폐기해 남은 암호문을 영구히 못 읽게 만든다.
표면   `DELETE /v1/platform/tenants/{tenant_id}` (확인 문구 + 사유 필수)
구현   `app/crypto.py:KeyDestroyed` · `app/store.py` 테넌트 파기 경로
계약   DEK 를 없애면 기존 암호문이 안 읽힌다 · 파기된 테넌트의 토큰은 인증되지 않는다
       파기는 사유와 함께 감사에 남고, 회전을 막지 않는다
고정   `test_purge.py::test_destroying_the_dek_makes_existing_ciphertext_unreadable`
       `test_purge.py::test_a_purged_tenants_tokens_stop_authenticating`
       `test_keyrotation.py::test_a_purged_tenant_does_not_block_the_rotation`
상태   구현됨

### CRYPTO-4  감사 해시 사슬
정의   관리 행위 기록마다 앞 행의 해시를 품어, 중간을 고치면 뒤가 전부 어긋나 조작이 드러난다.
표면   `GET /v1/admin/audit` · `python -m app doctor` 가 검증
구현   `app/store.py` `admin_audit` 의 `row_hash` / `prev_hash`
계약   첫 행도 null 링크가 아니다(앵커 키에서 시작) · 수정·삭제·상세 변경이 전부 검출된다
       판정은 **개수가 아니라 자리**를 지목한다 · 보존 정리는 조작처럼 보이지 않는다
       병합(coalesce)은 꼬리에만 일어나고 사슬을 끊지 않는다 · 낡은 꼬리를 읽어도 갈라지지 않는다
고정   `test_audit_chain.py::test_editing_a_row_is_detected`
       `test_audit_chain.py::test_the_verdict_names_the_spot_not_the_count`
       `test_audit_chain.py::test_retention_purge_does_not_look_like_tampering`
       `test_audit_chain.py::test_coalescing_a_stale_tip_does_not_break_the_chain`
       `test_multiprocess.py::test_the_audit_chain_does_not_fork_across_processes`
상태   구현됨

### CRYPTO-5  감사 외부 내보내기와 검증
정의   사슬을 외부로 증분 내보내고, 그 사본과 대조해 **전체 재계산** 조작까지 잡는다.
표면   `python -m app audit-export --out audit-export.jsonl`
구현   `app/cli.py:cmd_audit_export` · `app/store.py` 내보내기 표식
계약   내보내기는 증분이고 해시를 함께 싣는다 · 한 번도 안 내보냈으면 `unknown` 이지 `ok` 가 아니다
       내보낸 꼬리가 사슬만으로는 못 잡는 것을 잡는다 · 사슬 이전 DB 를 내보내도 죽지 않는다
고정   `test_audit_chain.py::test_the_export_is_incremental`
       `test_audit_chain.py::test_never_exported_is_unknown_not_ok`
       `test_audit_chain.py::test_the_exported_tip_catches_what_the_chain_cannot`
       `test_audit_chain.py::test_export_of_a_pre_chain_database_does_not_crash`
상태   구현됨 — 조작을 **막지는** 못함, 런북은 [runbook-audit-integrity.md](runbook-audit-integrity.md) (D10)

### CRYPTO-6  원문 열람과 그 감사
정의   테넌트 관리자가 프롬프트·응답 원문을 한 건씩 복호화해 본다. 열람 자체가 감사에 남는다.
표면   `GET /v1/admin/jobs/{job_id}/raw` · 관제 UI (한 번에 한 건)
구현   `app/main.py:tenant_job_raw`
계약   열람이 감사에 기록된다 · 마스킹본 조회는 특별 권한이 필요 없다
       보존 기간이 지나면 원문 열람이 **깨끗하게** 실패한다 · UI 는 한 번에 한 건만 연다
고정   `test_output_guard.py::test_reading_the_raw_response_is_audited`
       `test_purge.py::test_raw_read_fails_cleanly_after_retention`
       `test_ui.py::test_the_ui_only_opens_the_raw_prompt_one_job_at_a_time`
상태   구현됨

---

## 5. 클러스터 — 배치 · 비용 · 장애

주 모듈: `cluster.py` `cost.py` `models.py` · 설정: `config/nodes.yaml` `config/lanes.yaml` `config/pricing.yaml` `config/thresholds.yaml` `config/catalog.yaml`

### CLUSTER-1  노드 등록과 프로브
정의   GPU 노드·클라우드 API 를 등록하면 즉시 프로브해 재고와 헬스를 확인한다.
표면   `GET/POST /v1/platform/nodes` · `config/nodes.yaml` 이 시드
구현   `app/cluster.py:Cluster.probe` `NodeState`
계약   **DB 가 YAML 시드를 이긴다** — 등록한 노드는 재시작해도 살아남는다
       망가진 노드 행 하나가 기동을 막지 않는다 · 죽은 노드 프로브가 N배 시간을 먹지 않는다(병렬)
고정   `test_cluster.py::test_a_registered_node_survives_a_restart`
       `test_cluster.py::test_the_database_wins_over_the_yaml_seed`
       `test_cluster.py::test_probing_dead_nodes_does_not_take_n_times_the_timeout`
       `test_cluster.py::test_a_broken_node_row_does_not_stop_startup`
상태   구현됨

### CLUSTER-2  헬스 상태 전이
정의   연속 실패가 쌓여야 unhealthy 로, 연속 성공이 쌓여야 healthy 로 돌아온다. 한 번으로는 안 바뀐다.
표면   `GET /v1/status` · `GET /v1/platform/overview` · `config/thresholds.yaml`
구현   `app/cluster.py:Cluster` 헬스 판정 · `app/store.py` `node_health`
계약   실패 1회로 노드를 죽이지 않는다 · 성공 1회로 살리지도 않는다
       상태를 모르는 노드는 필터를 통과한다(모른다고 배제하지 않는다)
고정   `test_cluster.py::test_one_failure_does_not_kill_a_node`
       `test_cluster.py::test_three_consecutive_failures_mark_unhealthy`
       `test_cluster.py::test_unknown_health_passes_the_filter`
상태   구현됨

### CLUSTER-3  티어 배치
정의   역할이 선언한 티어 순서(`placement`)대로 노드를 고른다. 선언 순서가 곧 선호도다.
표면   `config/roles.yaml` 의 `placement` · `tier_models`
구현   `app/cluster.py:Cluster.place` `Placement` `PlacementResult`
계약   **티어 순서가 성능 휴리스틱보다 항상 우선한다** — 내부 티어가 살아 있으면 따뜻한 외부 모델보다 먼저다
       `external` 을 안 적은 역할은 절대 경계 밖으로 안 나간다(설정에 있어도 무시)
       외부 티어가 없으면 **기다린다** — 새어 나가지 않는다
       같은 티어 안에서만 따뜻한 모델·낮은 부하가 이긴다
고정   `test_cluster.py::test_internal_first_tier_wins_over_warm_external_model`
       `test_cluster.py::test_internal_only_role_ignores_external_tier_even_if_configured`
       `test_cluster.py::test_role_without_external_tier_waits_instead_of_leaking`
       `test_cluster.py::test_warm_model_wins_within_the_same_tier`
상태   구현됨

### CLUSTER-4  안정성 스냅샷 × 현재 설정 교집합
정의   잡에 박힌 경계 스냅샷과 현재 설정을 교집합해 배치한다. 좁히기만 하고 넓히지 않는다.
표면   내부 동작 (`config/roles.yaml` 변경 시 관측됨)
구현   `app/cluster.py:Cluster.place` 교집합 로직
계약   교집합은 **좁히기만** 한다 — 설정이 나중에 넓어져도 옛 잡은 안 넓어진다
       교집합이 비면 실패가 아니라 대기다
고정   `test_cluster.py::test_snapshot_intersects_with_current_config`
       `test_cluster.py::test_intersection_narrows_only_never_widens`
       `test_cluster.py::test_empty_intersection_waits_rather_than_fails`
상태   구현됨

### CLUSTER-5  슬롯·메모리 원자적 예약
정의   노드 동시 실행 수와 메모리를 프로세스 메모리가 아니라 DB 리스로 관리한다.
표면   `config/nodes.yaml` 의 `max_concurrent` · `GET /v1/platform/overview`
구현   `app/cluster.py:Occupancy` · `app/store.py` `node_leases`
계약   슬롯 1개 노드는 정확히 하나만 받는다 · 메모리는 확인이 아니라 **예약**된다
       워커를 몇 개 띄우든 초과 구독되지 않는다(장부가 프로세스를 가로지른다)
       경합에서 지면 실패가 아니라 대기다 · 만료된 리스는 슬롯을 잡고 있지 않는다
고정   `test_cluster.py::test_single_slot_node_accepts_exactly_one`
       `test_cluster.py::test_memory_budget_is_reserved_not_just_checked`
       `test_multiprocess.py::test_the_slot_ledger_crosses_processes`
       `test_multiprocess.py::test_an_expired_lease_stops_holding_the_slot`
       `test_cluster.py::test_losing_the_lease_race_waits_instead_of_failing`
상태   구현됨

### CLUSTER-6  비용 예약과 정산
정의   과금 노드로 보내기 전 상한만큼 예약하고, 끝나면 실사용으로 정산한다.
표면   `config/pricing.yaml` · `GET /v1/admin/usage` · `PUT /v1/admin/settings` 의 예산
구현   `app/cost.py:CostAccountant` `BudgetStatus`
계약   무료 로컬 경로는 아무것도 예약하지 않는다 · 과금 노드는 상한을 예약한다
       **예약이 예산에 계산된다** — 확인만 하면 동시 N건이 전부 통과한다
       경합의 승자만 과금된다 · 정산이 예약을 지운다 · 실패도 예약을 푼다
고정   `test_cluster.py::test_metered_node_reserves_upper_bound`
       `test_cluster.py::test_reservations_count_against_the_budget`
       `test_cluster.py::test_the_winner_of_the_race_is_the_only_one_charged`
       `test_scheduler.py::test_failed_job_releases_its_cost_reservation`
상태   구현됨

### CLUSTER-7  예산 소진 시 강등
정의   예산이 떨어지면 무료 경로로 자동 강등한다. 무료 티어가 없으면 막는다.
표면   `PUT /v1/admin/settings` · 알림 이벤트 · `GET /v1/platform/overview`
구현   `app/cost.py:CostAccountant` · `app/cluster.py:Cluster.place`
계약   무료 경로가 있으면 강등, 없으면 차단(조용히 계속 쓰지 않는다)
       예산 알림은 구간마다 한 번만 울린다
고정   `test_cluster.py::test_budget_exhaustion_demotes_to_the_free_path`
       `test_cluster.py::test_budget_exhaustion_blocks_when_no_free_tier`
       `test_ops.py::test_budget_exhaustion_notifies_once_per_band`
상태   구현됨

### CLUSTER-8  드레이닝
정의   노드를 점검할 때 신규 배치만 막고 실행 중인 잡은 끝까지 돌린다.
표면   `POST /v1/platform/nodes/{node}/drain`
구현   `app/cluster.py:Cluster` 드레이닝 플래그
계약   드레이닝은 즉시 차단이 아니라 신규 차단이다 · 강제 드레이닝은 실행 수를 잃지 않는다
       복귀는 초과 구독을 만들지 않는다
고정   `test_cluster.py::test_draining_blocks_new_but_keeps_running_jobs`
       `test_cluster.py::test_force_drain_does_not_lose_the_running_count`
       `test_cluster.py::test_undrain_does_not_oversubscribe`
상태   구현됨

### CLUSTER-9  에어갭 모드
정의   스위치 하나로 외부 노드를 배치 단계에서 즉시 차단한다. 등록 자체도 거부한다.
표면   `python -m app serve --airgap` · 관제 UI 노드 그리드가 상시 표시
구현   `app/cluster.py:Cluster` `PERMANENT_REJECTIONS`
계약   등록만 막는 게 아니라 **배치를 막는다**(이미 등록된 노드도) · 내부 노드는 그대로 돈다
       에어갭 거부는 **즉시 실패**다 — 900초 대기 후가 아니다
       UI 가 에어갭이 무엇을 껐는지 표시한다
고정   `test_packaging.py::test_airgap_blocks_external_placement_not_just_registration`
       `test_packaging.py::test_airgap_still_allows_internal_nodes`
       `test_cluster.py::test_airgap_rejection_fails_immediately_not_after_900_seconds`
       `test_packaging.py::test_the_node_grid_marks_what_airgap_disabled`
상태   구현됨

### CLUSTER-10  테넌트 친화
정의   특정 노드를 특정 테넌트 전용으로 묶는다.
표면   `config/nodes.yaml` 의 테넌트 지정 · `GET /v1/platform/overview`
구현   `app/cluster.py:Cluster.place` 친화 필터
계약   다른 테넌트는 그 노드를 못 쓴다 · 친화로 인한 부재는 **행정적 대기**지 영구 실패가 아니다
       영구 사유(에어갭)와 겹치면 영구 사유를 보고한다
고정   `test_cluster.py::test_tenant_affinity_node_rejects_other_tenants`
       `test_cluster.py::test_tenant_affinity_is_administrative_not_permanent`
       `test_cluster.py::test_a_permanent_reason_is_reported_even_when_the_node_is_also_down`
상태   구현됨

### CLUSTER-11  모델 설치 요청과 승인
정의   없는 모델을 감지해 설치 요청을 만들고, 플랫폼 관리자가 승인하면 노드에 내려받는다.
표면   `GET /v1/platform/models` · `POST /v1/platform/models/{request_id}/approve`
구현   `app/models.py:ModelRegistrar` `InstallRequest`
계약   **내려받기 전에** 디스크 용량으로 거른다 · 요청은 (노드, 모델) 쌍마다 하나다
       클라우드 노드에는 설치 생애주기가 없다 · 감지가 레인을 막지 않는다
       거부는 대기 중인 잡에 명확한 사유를 준다 · 승인·거부가 감사에 남는다
고정   `test_models.py::test_size_gate_rejects_before_downloading`
       `test_models.py::test_request_is_per_node_model_pair`
       `test_models.py::test_detect_missing_creates_requests_without_blocking_lanes`
       `test_models.py::test_rejection_gives_waiting_jobs_a_clear_reason`
상태   구현됨

### CLUSTER-12  모델 삭제 — 탈출구 없음
정의   쓰는 곳이 하나라도 있으면 삭제를 거부한다. `force` 플래그가 존재하지 않는다.
표면   `DELETE /v1/platform/nodes/{node}/models/{model}`
구현   `app/models.py:ModelRegistrar` 삭제 경로
계약   역할이 쓰는 중·큐에 잡이 있는 중·설치 진행 중이면 거부된다
       거부는 **차단 사유를 말한다** · `force` 우회로가 코드에 없다
고정   `test_models.py::test_role_in_use_blocks_deletion`
       `test_models.py::test_queued_jobs_block_deletion`
       `test_models.py::test_no_force_flag_exists`
       `test_architecture.py::test_model_deletion_has_no_force_escape_hatch`
상태   구현됨

### CLUSTER-13  번들 카탈로그
정의   설치 가능한 모델 목록을 번들에 담아 검색한다. 외부 레지스트리를 긁지 않는다.
표면   `GET /v1/platform/catalog` · `config/catalog.yaml`
구현   `app/models.py:ModelRegistrar` 카탈로그 조회
계약   검색은 로컬 전용이다(네트워크로 나가지 않는다) · 카탈로그에 없는 모델은 크기 제약이 없다
고정   `test_models.py::test_catalog_search_is_local_only`
       `test_models.py::test_unknown_model_has_no_size_constraint`
상태   구현됨

---

## 6. 스마트 라우팅

주 모듈: `pipeline.py` `evals.py` · 설정: `config/roles.yaml` 의 `routing` 절

### ROUTE-1  역할 단위 옵트인
정의   역할에 `routing` 절을 적은 경우에만 라우팅이 돈다. 안 적은 역할은 코드 경로 자체가 안 돈다.
표면   `config/roles.yaml` 의 `routing`
구현   `app/config.py` 라우팅 절 검증 · `app/pipeline.py:make_router`
계약   `routing` 없는 역할은 이전과 정확히 같게 동작한다
       기동 시 5가지를 거부한다: 경계 키를 든 라우트 · internal 전용이 아닌 분류기 · 없는 분류기 역할 ·
       빈 라우트 목록 · 설명 없는 라우트
고정   `test_routing.py::test_a_role_without_routing_behaves_exactly_as_before`
       `test_routing.py::test_a_route_carrying_a_boundary_key_is_refused_at_load`
       `test_routing.py::test_a_classifier_that_is_not_internal_only_is_refused`
       `test_routing.py::test_a_route_without_a_description_is_refused`
상태   구현됨

### ROUTE-2  제출 시 1회 판정과 스냅샷
정의   제출할 때 내부 분류기가 한 번 판정하고, 그 결과를 잡에 박아 둔다. 다시 계산하지 않는다.
표면   내부 동작 · `GET /v1/admin/jobs` 가 결정된 모델을 보여준다
구현   `app/pipeline.py:_route` `make_router`
계약   **모델 값만** 바꾼다 — 경계·레인·kind 는 못 건드린다
       라우터는 원문을 안 본다(internal 마스킹본을 본다) · 라우터는 잡을 만들지 않는다
       판정은 잡에 저장되고 재계산되지 않는다 · 라우트가 테넌트 오버라이드의 모델을 이긴다
       소비자 계약에는 라우팅이 나타나지 않는다
고정   `test_routing.py::test_the_route_substitutes_only_the_model`
       `test_routing.py::test_the_router_never_sees_the_raw_prompt`
       `test_routing.py::test_the_route_is_stored_on_the_job_not_recomputed`
       `test_routing.py::test_the_consumer_contract_does_not_mention_routing`
상태   구현됨

### ROUTE-3  실패는 기본 모델로
정의   판정이 실패하면(노드 없음·타임아웃·형식 이탈·미인증) 조용히 역할의 기본 모델로 간다.
표면   내부 동작 · `/metrics` 의 라우팅 실패 게이지
구현   `app/pipeline.py:_route` (`ROUTE_FAILED` / `ROUTE_NONE` 센티널)
계약   라우팅 실패는 **보안 사건이 아니라 최적화 기회의 상실**이라 정책 축이 없다
       마지막 줄만 읽는다 · 알려진 키 하나만 취한다 · 카나리아가 없으면 기본 모델
       `NONE` 판정은 실패가 아니라 결정이다 · 라우팅 이전 잡은 실패로 세지 않는다
고정   `test_routing.py::test_routing_failure_leaves_the_job_on_the_default_model`
       `test_routing.py::test_a_missing_canary_means_the_default_model`
       `test_routing.py::test_a_none_verdict_is_a_decision_not_a_failure`
       `test_routing.py::test_jobs_before_routing_was_enabled_do_not_count_as_failures`
상태   구현됨

### ROUTE-4  정확도 계측
정의   라우팅 정확도와 혼동 행렬을 내서 어느 설명을 고쳐야 하는지 지목한다. 통과·차단 게이트로는 쓰지 않는다.
표면   `GET/POST /v1/platform/evals` (라우터 종류)
구현   `app/evals.py:RouterReport` `Evaluator.measure_router`
계약   라우터 평가는 자기 종류로 따로 기록된다 · 실패는 버려지지 않고 "기본 모델" 로 집계된다
       없는 라우트를 가리키는 픽스처는 오류다
고정   `test_evals.py::test_the_router_report_counts_accuracy`
       `test_evals.py::test_the_report_names_which_description_to_fix`
       `test_evals.py::test_a_routing_failure_is_recorded_as_the_default_not_dropped`
       `test_evals.py::test_a_fixture_naming_an_unknown_route_is_an_error`
상태   부분 — 계측 도구는 있으나 **픽스처가 번들에 없다**. 무엇이 "단순" 인지는 설치처가 정한다 (L1)

---

## 7. 스케줄러 · 실행

주 모듈: `scheduler.py` `providers/` · 설정: `config/lanes.yaml` `config/thresholds.yaml`

### SCHED-1  디스패치 직렬화
정의   여러 워커가 같은 잡을 집어도 상태 전이 CAS 에서 하나만 이긴다.
표면   내부 동작
구현   `app/scheduler.py:_try_dispatch` · `app/store.py:update_job(expect_status=...)`
계약   상태 전이는 CAS 로만 한다 — 이긴 쪽만 디스패치한다 · 진 쪽은 행을 망가뜨리지 않는다
       실패한 트랜잭션은 아무것도 남기지 않는다 · 스케줄러는 프롬프트 본문 컬럼을 읽지 않는다
고정   `test_multiprocess.py::test_only_one_process_wins_the_state_transition`
       `test_multiprocess.py::test_the_losers_do_not_corrupt_the_row`
       `test_scheduler.py::test_the_scheduler_never_reads_a_blind_column`
상태   구현됨

### SCHED-2  레인 동시성
정의   `interactive` · `batch` · `guard` 세 레인마다 클러스터 전체 동시 실행 상한을 둔다.
표면   `config/lanes.yaml`
구현   `app/scheduler.py:LaneStats` `Scheduler`
계약   레인 상한이 지켜진다 · 가드 레인이 따로 있어 분류 잡이 대형 배치 뒤에 안 선다
       스냅샷이 모든 레인을 보고한다
고정   `test_scheduler.py::test_lane_concurrency_is_respected`
       `test_scheduler.py::test_snapshot_reports_every_lane`
상태   구현됨

### SCHED-3  공정성과 기아 방지
정의   테넌트를 라운드로빈으로 돌고, 오래 기다린 잡은 공정성·우선순위를 제친다.
표면   `config/lanes.yaml` 의 `starvation_seconds` · `config/thresholds.yaml`
구현   `app/scheduler.py:Scheduler` 스캔 로직
계약   한 테넌트가 다른 테넌트를 굶기지 못한다 · 기아가 공정성과 우선순위를 모두 이긴다
       스캔 창을 넘겨 잘린 경우 그 사실이 드러난다 · 스캔 창은 프롬프트 크기와 무관하다
고정   `test_scheduler.py::test_one_tenant_cannot_starve_another`
       `test_scheduler.py::test_starvation_beats_fairness_and_priority`
       `test_scheduler.py::test_scan_window_truncation_is_surfaced`
       `test_capacity.py::test_the_scan_window_does_not_grow_with_prompt_size`
상태   구현됨

### SCHED-4  재시도와 백오프
정의   실패하면 직전 실패 노드를 빼고 다른 노드로 재시도한다. 백오프는 잡마다 흔들어 준다.
표면   `config/thresholds.yaml` 의 `max_retries` `retry_backoff_seconds`
구현   `app/scheduler.py:Scheduler` 재시도 경로
계약   재시도가 방금 실패한 노드를 피한다 · 백오프가 레인을 막지 않는다
       재시도 불가 실패는 즉시 멈춘다 · 횟수 상한이 있다 · 지터는 한 잡 안에서 안정적이다
       단일 노드 설치라도 재시도할 수 있다
고정   `test_scheduler.py::test_retry_avoids_the_node_that_just_failed`
       `test_scheduler.py::test_backoff_does_not_block_the_lane`
       `test_scheduler.py::test_retry_backoff_is_jittered_per_job`
       `test_cluster.py::test_a_single_node_install_can_still_retry`
상태   구현됨

### SCHED-5  정산 원자성
정의   성공 종결 · 지출 반영 · 예약 해제를 한 트랜잭션으로 처리한다.
표면   내부 동작 · `GET /v1/admin/usage`
구현   `app/scheduler.py:_succeed` · `app/store.py:settle_job(expect_status=...)`
계약   성공은 정산 밖에서 커밋되지 않는다 — 사이에서 죽어도 반쪽 상태가 안 남는다
       종결 뒤의 실패 기록이 이미 끝난 잡을 덮어쓰지 않는다
       정산 도중 크래시가 멀쩡한 노드를 탓하지 않는다
고정   `test_scheduler.py::test_success_is_never_committed_outside_the_settlement`
       `test_scheduler.py::test_failure_recording_does_not_overwrite_a_finalized_job`
       `test_scheduler.py::test_a_finalize_crash_does_not_blame_the_healthy_node`
상태   구현됨

### SCHED-6  크래시 복구
정의   재시작하면 `running` 이던 잡을 되살린다. 단 과금 노드 잡은 자동 재큐하지 않고 사람에게 넘긴다.
표면   `POST /v1/admin/jobs/{job_id}/review` (사람이 종결)
구현   `app/scheduler.py:Scheduler.start` · `app/store.py` 복구 CAS
계약   실행 중이던 잡은 잃지 않는다 · **과금 잡은 재큐하지 않는다**(이중 청구 방지)
       대신 `needs_review` 로 드러내고 사람이 치울 경로를 준다
       크래시 복구가 단일 노드 설치를 고립시키지 않는다
고정   `test_scheduler.py::test_start_recovers_running_jobs`
       `test_blast_radius.py::test_metered_jobs_are_not_requeued_after_a_crash`
       `test_scheduler.py::test_start_flags_metered_jobs_for_review`
       `test_cluster.py::test_crash_recovery_does_not_strand_a_single_node_install`
상태   구현됨

### SCHED-7  완료 통지
정의   같은 프로세스 안에서 잡이 종결되면 대기 중인 요청을 깨운다.
표면   `POST /v1/generate` 의 `wait` 경로에서만 관측됨
구현   `app/completion.py:CompletionSignal`
계약   등록 안 된 잡을 기다리면 그냥 잔다(터지지 않는다) · 예외가 나도 등록이 풀린다
       레지스트리가 새지 않는다 · 취소도 대기자를 깨운다
고정   `test_completion.py::test_the_registry_does_not_leak`
       `test_completion.py::test_an_exception_still_unregisters`
       `test_completion.py::test_a_cancelled_job_wakes_its_waiter`
상태   구현됨 — 단일 프로세스 한정(설계상)

### SCHED-8  프로바이더
정의   Ollama · Anthropic · mock 세 백엔드. 능력(생성·임베딩)이 다른 것을 인터페이스가 흡수한다.
표면   `config/nodes.yaml` 의 노드 종류
구현   `app/providers/base.py:Provider` `build_provider` `known_providers` · `ollama.py` `anthropic_provider.py` `mock.py`
계약   데이터 경계는 **노드의 성질**이지 프로바이더의 성질이 아니다
       경계를 안 적으면 `external` 로 간주한다(안전한 쪽으로) · 지원 안 하는 연산은 명시적으로 거부한다
고정   `test_architecture.py::test_data_boundary_is_a_node_property_not_a_provider_property`
       `test_architecture.py::test_unspecified_data_boundary_defaults_to_external`
상태   구현됨

---

## 8. 데이터 수명주기

주 모듈: `store.py` `backup.py` `restore.py`

### DATA-1  테넌트 격리
정의   모든 테넌트 데이터 조회는 스코프를 먼저 요구한다. 스코프 없는 조회 경로가 존재하지 않는다.
표면   모든 `/v1/admin/*` 라우트
구현   `app/store.py:_scoped_where` · 스코프 필수 메서드들
계약   잡·사용량·감사·가드 규칙·서비스·토큰 중 **어느 경로로도** 남의 테넌트가 안 보인다
       잡을 테넌트 사이로 옮길 수 없다 · 빈 테넌트 ID 는 거부된다
       전역 조회는 명시적 플랫폼 스코프를 요구하고 감사에 남는다
       DB 커넥션은 `store.py` 만 만진다
고정   `test_store.py::test_no_unscoped_query_path_is_exposed`
       `test_store.py::test_cannot_move_a_job_between_tenants`
       `test_store.py::test_cross_tenant_query_is_audited`
       `test_architecture.py::test_only_the_store_touches_the_connection`
상태   구현됨

### DATA-2  원문 보존 기간
정의   테넌트가 정한 기간이 지나면 원문 암호문을 지우고 마스킹본만 남긴다.
표면   `PUT /v1/admin/settings` 의 보존 기간
구현   `app/store.py:purge_expired` · `app/scheduler.py` 보존 루프
계약   테넌트는 **짧게만** 바꿀 수 있다(플랫폼 상한을 못 넘긴다) · 0 이면 원문을 아예 안 남긴다
       음수는 거부된다 · 암호문을 먼저 지우고 잡을 지운다 · 끝나지 않은 잡은 안 지운다
       usage 보존은 예산 창(30일) 이상이어야 한다 — 구조로 강제된다
고정   `test_purge.py::test_tenant_cannot_lengthen_retention_past_the_platform_max`
       `test_purge.py::test_zero_retention_means_no_raw_storage_at_all`
       `test_store.py::test_retention_clears_cipher_before_deleting_jobs`
       `test_store.py::test_retention_keeps_unfinished_jobs`
상태   구현됨

### DATA-3  엔드유저 파기
정의   한 엔드유저의 데이터만 지운다. 집계 수치는 남긴다.
표면   `DELETE /v1/admin/end-users/{end_user_hash}` (확인 문구 필수)
구현   `app/store.py` 엔드유저 파기 경로
계약   그 사람만 지워진다 · 다른 테넌트를 안 건드린다 · 정확한 확인 문구를 요구한다
       감사는 **누가 몇 건**만 남기고 무엇을 지웠는지는 안 남긴다
       가드 이벤트는 연결만 끊고 카운트는 유지한다
고정   `test_purge.py::test_purging_one_end_user_leaves_the_others`
       `test_purge.py::test_purge_requires_an_exact_confirmation`
       `test_purge.py::test_purge_audit_records_who_and_how_many_not_what`
       `test_purge.py::test_purge_unlinks_the_guard_events_but_keeps_the_counts`
상태   구현됨

### DATA-4  테넌트 파기
정의   테넌트의 모든 스코프 테이블을 지우고 DEK 를 폐기한다. 되돌릴 수 없다.
표면   `DELETE /v1/platform/tenants/{tenant_id}` (확인 문구 + 사유)
구현   `app/store.py` 테넌트 파기 · `app/crypto.py:KeyDestroyed`
계약   확인 문구와 사유를 모두 요구한다 · 스코프 테이블을 빠짐없이 지운다
       사유와 함께 감사에 남는다
고정   `test_purge.py::test_tenant_purge_requires_confirmation_and_a_reason`
       `test_purge.py::test_tenant_purge_removes_every_scoped_table`
       `test_purge.py::test_tenant_purge_is_audited_with_the_reason`
상태   구현됨

### DATA-5  내보내기
정의   잡·사용량·감사·설정을 마스킹본 기준으로 내보낸다.
표면   `GET /v1/admin/export`
구현   `app/main.py:tenant_export` · `app/store.py` 내보내기 경로
계약   **암호문은 절대 안 실린다**(응답 암호문도) · 테넌트를 가로지르지 않는다
       재구축에 필요한 설정을 함께 싣는다 · 감사에 남는다
       크기 상한이 있고, 잘렸으면 잘렸다고 말한다
고정   `test_purge.py::test_export_carries_the_masked_copy_never_the_ciphertext`
       `test_output_guard.py::test_the_export_never_carries_response_ciphertext`
       `test_purge.py::test_export_includes_the_settings_needed_to_rebuild`
       `test_purge.py::test_a_truncated_export_says_so`
상태   구현됨

### DATA-6  백업 스냅샷
정의   `cp` 가 아니라 SQLite 백업 API 로 일관된 스냅샷을 뜬다. 암호문과 키는 담지 않는다.
표면   `backup.sh` · `app/backup.py`
구현   `app/backup.py:snapshot`
계약   **모든 암호문 컬럼이 벗겨진다** — 지운 데이터가 백업으로 되살아나지 않게
       KEK 는 백업에 없다(compose 에서 키 볼륨을 데이터와 분리)
고정   `test_output_guard.py::test_the_backup_strips_every_cipher_column`
       `test_packaging.py::test_compose_keeps_the_key_volume_separate_from_data`
상태   구현됨

### DATA-7  복원
정의   파일 하나 덮어쓰기가 아니라 스키마 호환성을 확인하고 설정까지 맞춘다.
표면   `restore.sh` · `app/restore.py`
구현   `app/restore.py:check_compatible` `read_schema_version` `install` `install_config`
계약   설치 경로가 스키마 버전을 실제로 검사한다 · 호환되지 않으면 거부한다
고정   `test_packaging.py::test_the_install_path_actually_gates_on_schema`
상태   구현됨

### DATA-8  스키마 마이그레이션
정의   스키마는 더하기만 한다. 컬럼을 지우거나 바꾸지 않는다.
표면   `python -m app doctor` 가 스키마 버전 보고 · `/metrics` 의 `schema_version`
구현   `app/store.py` 마이그레이션 목록
계약   마이그레이션은 add-only 다 · 새 잡 컬럼은 기본적으로 스케줄러에 도달한다
       업그레이드된 DB 는 살아 있는 잡의 추정치를 백필한다
고정   `test_architecture.py::test_schema_migrations_are_add_only`
       `test_scheduler.py::test_a_new_job_column_reaches_the_scheduler_by_default`
       `test_scheduler.py::test_an_upgraded_database_backfills_the_estimate_for_live_jobs`
상태   구현됨

---

## 9. 계약 자기 서빙

주 모듈: `meta.py`

### META-1  기계가 읽는 계약
정의   토큰마다 다른 계약 문서를 낸다. 그 토큰이 쓸 수 있는 역할·한도·오류 코드만 담는다.
표면   `GET /v1/meta` · `GET /v1/session`
구현   `app/meta.py:meta_document` `role_contract` `visible_roles`
계약   토큰마다 생성된다 · 관리 라우트는 소비자 계약에 안 들어간다
       가드 로케일 팩을 밝힌다 · 오류 코드는 문서화하되 enum 으로 고정하지 않는다
고정   `test_meta.py::test_meta_endpoint_is_generated_per_token`
       `test_meta.py::test_meta_endpoints_exclude_admin_routes`
       `test_meta.py::test_meta_names_the_guard_locale_pack`
       `test_meta.py::test_error_codes_are_documented_but_not_an_enum`
상태   구현됨

### META-2  OpenAPI
정의   OpenAPI 3.1 문서를 토큰별로 생성한다. 역할 enum 에 그 토큰이 쓸 수 있는 역할만 담긴다.
표면   `GET /v1/openapi.json` · `GET /v1/openapi.yaml`
구현   `app/meta.py:openapi_document` `inventory`
계약   재고는 **손으로 쓰지 않고 라우트 테이블에서 뽑는다**
       다른 테넌트의 역할이 새지 않는다 · **모델 이름을 노출하지 않는다**(역할이 계약이므로)
       `healthz` 는 인증 불필요로 표시된다
고정   `test_meta.py::test_inventory_is_derived_not_handwritten`
       `test_meta.py::test_openapi_role_enum_does_not_leak_other_tenants_roles`
       `test_meta.py::test_openapi_never_exposes_model_names`
       `test_meta.py::test_openapi_marks_healthz_as_unauthenticated`
상태   구현됨

### META-3  통합 가이드
정의   사람이 읽는 마크다운 통합 문서를 요청 로케일에 맞춰 낸다.
표면   `GET /v1/integration`
구현   `app/meta.py:integration_guide`
계약   허용된 역할을 나열한다 · **메시지 문자열로 분기하지 말라고 경고한다**(코드로 분기)
       제어 토큰과 자격증명 마스킹을 소비자에게 알린다
고정   `test_meta.py::test_integration_guide_is_markdown_and_lists_allowed_roles`
       `test_meta.py::test_integration_guide_warns_against_branching_on_message`
       `test_injection.py::test_the_contract_warns_about_control_tokens`
       `test_secrets.py::test_the_contract_tells_consumers_that_credentials_are_masked`
상태   구현됨

### META-4  번들 클라이언트
정의   단일 파일 클라이언트와 목 서버 원본을 그대로 내려준다.
표면   `GET /v1/client` · `GET /v1/client/{name}` · `clients/`
구현   `app/meta.py` · `app/main.py:client_index` `client_file`
계약   목록은 실제 번들 파일을 반영한다 · 경로 순회(`../`)를 거부한다
고정   `test_meta.py::test_client_index_lists_bundled_files`
       `test_meta.py::test_client_file_refuses_path_traversal`
상태   구현됨

### META-5  생존 확인
정의   컨테이너·로드밸런서용 무인증 엔드포인트.
표면   `GET /healthz`
구현   `app/main.py:healthz`
계약   인증이 필요 없다 · **DB 를 건드리지 않는다**(DB 가 죽어도 응답한다)
고정   `test_meta.py::test_healthz_needs_no_auth`
       `test_meta.py::test_healthz_does_not_touch_the_database`
상태   구현됨

---

## 10. 운영 · 관측

주 모듈: `observability.py` `notify.py` `cli.py` `loadtest.py` `i18n.py` · 자산: `static/` `locales/`

### OPS-1  메트릭
정의   Prometheus/OpenMetrics 형식으로 운영 신호를 노출한다.
표면   `GET /metrics` (플랫폼 관리자)
구현   `app/observability.py:collect` `Metric` `render_metrics`
계약   **테넌트 이름이 라벨에 없다** — 공용 대시보드도 정보 유출면이다
       프롬프트를 절대 싣지 않는다 · 상태를 모르는 노드는 0.5 로 보고한다
       라벨 값은 이스케이프된다 · 전달을 못 보는 지표는 전달했다고 주장하지 않는다
고정   `test_ops.py::test_metrics_never_label_by_tenant`
       `test_ops.py::test_metrics_never_carry_prompts`
       `test_ops.py::test_metrics_report_unknown_node_health_as_half`
       `test_ops.py::test_the_metric_does_not_claim_delivery_it_cannot_see`
상태   구현됨

### OPS-2  구조화 로그
정의   한 줄 JSON 로그. 한국어가 읽히게 나온다.
표면   `python -m app serve --log-level` · stdout
구현   `app/observability.py:JsonFormatter` `configure_logging` `log_event`
계약   프롬프트 본문과 비밀을 싣지 않는다 · ASCII 이스케이프로 직렬화하지 않는다
       앱이 실제로 이 경로를 쓴다(선언만 하고 안 쓰는 코드가 아니다)
고정   `test_ops.py::test_logs_are_one_json_line_with_readable_korean`
       `test_ops.py::test_logs_drop_prompt_bodies_and_secrets`
       `test_architecture.py::test_nothing_serializes_with_ascii_escapes`
       `test_ops.py::test_log_event_is_actually_called_by_the_app`
상태   구현됨

### OPS-3  진단 번들
정의   지원 요청 때 보낼 수 있는 진단 묶음. 비밀은 마스킹하고 프롬프트는 담지 않는다.
표면   `GET /v1/platform/diagnostics` · `python -m app doctor --bundle`
구현   `app/observability.py:diagnostic_bundle` `mask_secret` `strip_tenant_identity`
계약   비밀은 **길이만** 드러낸다 · 테넌트 이름이 안 들어간다(개수는 들어간다)
       최근 오류에 본문이 없다 · 노드 인증 방식은 보여주되 자격증명은 안 보여준다
       감사에 남고 플랫폼 전용이다 · `doctor` 가 문제를 찾아도 번들은 만들어진다
고정   `test_ops.py::test_diagnostic_bundle_masks_secrets`
       `test_ops.py::test_the_bundle_never_names_a_tenant`
       `test_ops.py::test_diagnostic_bundle_shows_node_auth_without_the_credential`
       `test_packaging.py::test_the_bundle_is_produced_even_when_doctor_finds_a_problem`
상태   구현됨

### OPS-4  알림
정의   상태가 **바뀔 때만** 알린다. 웹훅과 SMTP 를 지원한다.
표면   `GET/POST /v1/platform/notifications` · 환경변수로 채널 설정
구현   `app/notify.py:Notifier` `WebhookChannel` `SmtpChannel` `redact` `channels_from_env`
계약   전이에서만 발화한다 · 기동 시 복구를 알리지 않는다(이미 죽은 노드는 알린다)
       **죽은 채널이 호출자를 죽이지 않는다** · 느린 채널이 이벤트 루프를 막지 않는다
       프롬프트·응답·암호문을 절대 싣지 않는다 · 깜빡이는 주제는 최소 간격으로 막는다
       모든 알려진 이벤트가 모든 로케일에 메시지를 갖는다
고정   `test_ops.py::test_notifier_only_fires_on_a_transition`
       `test_ops.py::test_a_dead_channel_never_kills_the_caller`
       `test_ops.py::test_a_slow_channel_does_not_stall_the_event_loop`
       `test_output_guard.py::test_notifications_never_carry_response_text_or_ciphertext`
       `test_ops.py::test_every_known_event_has_a_message_in_every_locale`
상태   구현됨

### OPS-5  doctor 진단
정의   설정·키·사슬·회전 상태를 사람 말로 진단하고, 무엇을 해야 하는지까지 말한다.
표면   `python -m app doctor [--bundle --out ...]`
구현   `app/cli.py:cmd_doctor`
계약   유일하게 열리는 키를 지우라고 말하지 않는다 · 아무것도 안 열리면 삭제 조언을 거부한다
       회수된 키로 폴백해 판정한다 · **doctor 의 복구 지시가 실제로 복구시킨다**
       종료 코드가 번들 복사에 묻히지 않는다
고정   `test_keyrotation.py::test_doctor_refuses_to_advise_deletion_when_nothing_opens`
       `test_keyrotation.py::test_doctor_falls_back_to_the_retired_key`
       `test_keyrotation.py::test_doctor_recovery_instruction_actually_recovers`
       `test_packaging.py::test_the_doctor_exit_code_survives_the_bundle_copy`
상태   구현됨

### OPS-6  부하 측정
정의   제출 종단·배치 같은 단계별 원가를 실제로 재는 도구를 내장한다.
표면   `app/loadtest.py` (모듈 실행) · [capacity.md](capacity.md) 가 결과를 싣는다
구현   `app/loadtest.py:measure_stages`
계약   용량 문서의 숫자는 추정이 아니라 측정값이다
       정규화가 규칙마다 반복되지 않는다(스캔 밖에서 한 번)
고정   `test_capacity.py::test_the_capacity_doc_no_longer_calls_the_numbers_estimates`
       `test_capacity.py::test_the_scan_normalizes_outside_the_rule_loop`
       `test_capacity.py::test_normalization_does_not_run_once_per_rule`
상태   구현됨

### OPS-7  다국어
정의   오류 코드는 고정하고 메시지만 로케일에 맞춘다. 로케일이 가드 팩을 고른다.
표면   `Accept-Language` 헤더 · `PUT /v1/admin/settings` 의 기본 로케일 · `locales/`
구현   `app/i18n.py:Translator` `negotiate_locale` `guard_pack_for` `ApiError`
계약   **오류 코드는 로케일과 무관하게 같다** · 가드 규칙 ID 는 번역되지 않는다
       모든 로케일이 같은 키 집합을 갖는다 · 없는 키는 예외가 아니라 키로 폴백한다
       사용자 설정 > `Accept-Language` > 테넌트 기본 > 플랫폼 기본 순으로 정해진다
고정   `test_i18n.py::test_error_code_is_stable_across_locales`
       `test_i18n.py::test_guard_rule_ids_are_not_translated`
       `test_i18n.py::test_all_locales_have_the_same_keys`
       `test_i18n.py::test_user_setting_beats_accept_language`
상태   구현됨

### OPS-8  관제 UI
정의   빌드 단계 없는 정적 화면. 노드 그리드·큐·예산·감사·가드를 한 곳에서 본다.
표면   `GET /ui` · `static/`
구현   `app/main.py:ui_index` · `static/app.js` `static/index.html`
계약   빌드 없이 서빙된다 · 외부 자산과 프레임워크 번들을 안 쓴다
       서버 데이터를 HTML 로 쓰지 않는다(XSS) · 토큰은 세션 스토리지에만 둔다
       `index.html` 이 `app.js` 참조에 버전을 박는다 — 업그레이드 후 캐시된 옛 JS 가 새 API 를 안 때린다
       화면이 쓰는 모든 문자열이 모든 로케일에 있고, 안 쓰는 문자열은 없다
고정   `test_ui.py::test_the_ui_is_served_without_a_build_step`
       `test_ui.py::test_no_external_assets`
       `test_ui.py::test_server_data_is_never_written_as_html`
       `test_ui.py::test_the_token_is_kept_in_session_storage_only`
       `test_ui.py::test_every_ui_string_the_screen_uses_exists_in_every_locale`
       `test_ui.py::test_no_dead_ui_strings`
상태   구현됨

### OPS-9  클러스터 상태 조회
정의   소비자용 요약과 플랫폼용 전역 관제 두 층으로 상태를 보여준다.
표면   `GET /v1/status` (소비자) · `GET /v1/platform/overview` (플랫폼) · `GET /v1/admin/jobs`
구현   `app/cluster.py:Cluster.snapshot` · `app/main.py:status` `platform_overview`
계약   대기 사유가 UI 를 위해 기록된다 · 단일 호밍 역할이 경고로 드러난다
       스냅샷이 경계와 부하를 노출한다 · 잡 목록은 마스킹본만 보여준다
고정   `test_cluster.py::test_wait_reason_is_reported_for_the_ui`
       `test_cluster.py::test_single_homed_roles_are_surfaced`
       `test_cluster.py::test_snapshot_exposes_boundary_and_load`
       `test_scheduler.py::test_wait_reason_is_recorded_for_the_ui`
상태   구현됨

### OPS-10  사용량 집계
정의   서비스·엔드유저·역할·노드 축으로 사용량과 지출을 집계한다. 분당 토큰 처리율도 낸다.
표면   `GET /v1/admin/usage` · `GET /metrics`
구현   `app/store.py` 사용량 경로 · `app/cost.py`
계약   집계는 테넌트 범위를 넘지 않는다 · 처리율은 **보여줄 뿐 한도로 걸지 않는다**
       처리율 창은 낡은 사용량을 배제한다 · 처리율 지표에도 테넌트 라벨이 없다
고정   `test_store.py::test_usage_and_spend_are_scoped`
       `test_idempotency.py::test_the_rate_is_not_a_limit`
       `test_idempotency.py::test_the_rate_window_excludes_old_usage`
       `test_idempotency.py::test_the_metrics_carry_no_tenant_label`
상태   부분 — 토큰 축 **한도**는 없음, 요청 수 기준만 (D8)

---

## 11. 패키징 · 설치

주 모듈: `bootstrap.py` `cli_paths.py` · 자산: `Dockerfile` `compose.yml` `bundle.sh` `preflight.sh` `tls/nginx.conf`

### PKG-1  최초 기동
정의   키·관리자·첫 테넌트·도입 첫날 가드 등급을 한 번에 만든다.
표면   `python -m app bootstrap [--keys ... --data ...]`
구현   `app/bootstrap.py:bootstrap` `ensure_master_key` `generate_admin_password` `is_bootstrapped`
계약   **기본 자격증명이 존재하지 않는다** — 설치마다 다른 값이 무작위로 만들어진다
       키 디렉터리를 못 쓰면 명확히 거부한다 · 키 파일은 fsync 된다
       preflight 가 도커보다 먼저 키 디렉터리를 만든다
고정   `test_packaging.py::test_bootstrap_tokens_differ_between_installs`
       `test_packaging.py::test_preflight_creates_the_key_directory_before_docker_does`
상태   구현됨

### PKG-2  데모 프로파일
정의   GPU 없이 한 줄로 도는 시연 모드. 테넌트 둘을 심어 격리를 보여준다.
표면   `python -m app --demo` · `python -m app serve --demo`
구현   `app/bootstrap.py:demo_seed` · `app/providers/mock.py`
계약   테넌트가 둘이라 격리를 시연할 수 있다 · 로케일이 서로 다르다
       재시드는 새 토큰을 낸다 · **PII 표본은 합성이고 실제로 가드를 튕긴다**
       맨 `--demo` 플래그가 `serve --demo` 를 뜻한다
고정   `test_packaging.py::test_demo_seeds_two_tenants_so_isolation_is_demonstrable`
       `test_packaging.py::test_demo_pii_samples_are_synthetic_and_actually_trip_the_guard`
       `test_packaging.py::test_bare_demo_flag_means_serve_demo`
상태   구현됨

### PKG-3  설치 번들
정의   설치처가 받아 그대로 올릴 수 있는 묶음을 만든다.
표면   `bundle.sh` · `python -m app doctor --bundle`
구현   `bundle.sh` · `app/cli_paths.py`
계약   설치에 필요한 것이 다 들어 있다 · **프록시 설정(`tls/nginx.conf`)이 함께 실린다**
       번들 복사 실패는 보고된다
고정   `test_packaging.py::test_the_bundle_has_what_the_install_needs`
       `test_packaging.py::test_the_bundle_script_ships_the_proxy_config`
       `test_packaging.py::test_a_failed_bundle_copy_is_reported`
상태   구현됨

### PKG-4  에어갭 번들
정의   인터넷 없는 환경에 설치할 수 있게 의존물을 함께 담는다.
표면   `bundle.sh --airgap` · `python -m app serve --airgap`
구현   `bundle.sh` · `app/cluster.py` 에어갭 판정 (CLUSTER-9)
계약   에어갭 모드에서는 외부 노드 등록 자체가 거부된다
고정   `test_packaging.py::test_airgap_refuses_to_register_an_external_node`
상태   구현됨

### PKG-5  컨테이너와 프록시
정의   Docker/compose 로 올리고, nginx 가 TLS 를 끝내고 `/v1/platform/*` 를 밖에서 숨긴다.
표면   `Dockerfile` · `compose.yml` · `tls/nginx.conf` · [topology.md](topology.md)
구현   `Dockerfile` `compose.yml` `tls/nginx.conf` `preflight.sh`
계약   키 볼륨과 데이터 볼륨이 분리된다 · 데모 노트북이 재부팅해도 살아 돌아온다
       **`/v1/platform/*` 차단은 프록시가 한다** — 제품이 아니라 설치처 전제다(topology.md §2)
고정   `test_packaging.py::test_compose_keeps_the_key_volume_separate_from_data`
       `test_packaging.py::test_compose_restarts_so_the_demo_laptop_comes_back`
       `test_packaging.py::test_the_bundle_script_ships_the_proxy_config`
상태   구현됨

---

## 12. 플러그인

주 모듈: `plugins.py` · 배경: [plugin-exploration.md](plugin-exploration.md)

플러그인은 **앞문으로 지나는 소비자**다. LLM 을 쓸 때 `POST /v1/generate` 를 지나므로
가드·경계·레이트리밋·예산·사용량 집계·감사가 배선 없이 붙는다. 지금 지원하는 실행
형태는 `external` 하나 — **컨트롤 플레인이 프로세스를 띄우지 않는다.**

### PLUGIN-1  번들 포맷과 서명 검증
정의   `.lccp`(zip) + `plugin.toml` + `MANIFEST.sha256` + Ed25519 `SIGNATURE`. 서명 하나가 번들 전체를 고정한다.
표면   `POST /v1/platform/plugins` (raw body) · `keys/plugin-trust/*.pub`
구현   app/plugins.py:build_bundle · verify_bundle · load_trusted_keys · checksum_block
계약   서명은 `MANIFEST.sha256` 한 장에 걸리고 그 한 장이 나머지 파일 해시를 든다
       목록에 없는 파일이 끼어들어도 잡힌다 · 신뢰 목록 밖 키의 서명은 서명이 아니다
       **무서명과 변조는 다른 사건이다** — `unsigned` 와 `invalid` 를 섞지 않는다
       깨진 신뢰 키 하나가 나머지 키를 막지 않는다 · 새 의존성 0(cryptography 재사용)
고정   test_plugins.py::test_a_signed_bundle_verifies
       test_plugins.py::test_tampering_with_a_signed_bundle_is_detected
       test_plugins.py::test_an_extra_file_not_in_the_checksums_is_detected
       test_plugins.py::test_a_signature_from_an_untrusted_key_is_invalid
상태   구현됨

### PLUGIN-2  안전한 압축 해제
정의   경로 순회·심볼릭 링크·zip bomb 을 **풀기 전에** 막는다.
표면   설치 경로 내부 동작
구현   app/plugins.py:safe_names · install
계약   절대 경로·상위 디렉터리 참조·심볼릭 링크 항목은 거부한다
       링크 하나면 번들이 `/keys/master.key` 를 읽어 간다 · 해제 크기와 항목 수에 상한
       해제하고 나서 재는 것은 늦다 — 이미 디스크를 채운 뒤다
고정   test_plugins.py::test_a_path_traversal_entry_is_refused
       test_plugins.py::test_a_symlink_entry_is_refused
       test_plugins.py::test_a_zip_bomb_is_refused_before_extraction
       test_plugins.py::test_too_many_files_is_refused
상태   구현됨

### PLUGIN-3  설치 = 서비스 등록 + 토큰 발급
정의   매니페스트 `[service]` 절이 그대로 `create_service()` 인자가 되고, 서비스 토큰이 한 번 발급된다.
표면   `POST /v1/platform/plugins` · `plugin.toml` 의 `[plugin]` `[service]` `[run]`
구현   app/plugins.py:install · parse_manifest · host_satisfies · app/plugins.py:plugin_root
계약   **권한 모델을 새로 만들지 않는다** — 관리자가 읽는 문장과 DB 값과 강제되는 것이 같다
       설치는 켜는 것이 아니다(항상 inactive 로 착지) · 업그레이드도 inactive 로 착지한다
       재설치는 토큰을 재발급하지 않는다 — 갈아 치우면 도는 플러그인이 조용히 죽는다
       설치본은 데이터 디렉터리로 간다(`config/` 는 읽기 전용 마운트)
       내부(밑줄) 역할은 요청할 수 없다 · 호스트 버전 범위를 못 맞추면 거부한다
       거부 사유는 사람이 읽고 고칠 수 있는 문장이다
고정   test_plugins.py::test_installing_creates_a_service_and_a_token
       test_plugins.py::test_a_freshly_installed_plugin_is_not_active
       test_plugins.py::test_reinstalling_does_not_reissue_the_token
       test_plugins.py::test_the_payload_lands_under_the_data_dir
       test_plugins.py::test_an_internal_role_cannot_be_requested
상태   구현됨

### PLUGIN-4  활성 · 비활성
정의   플러그인을 켜고 끈다. **실체는 그 플러그인이 쓰는 `services.status` 다.**
표면   `POST /v1/platform/plugins/{plugin_id}/activate`
구현   app/plugins.py:set_active · app/store.py:set_service_status · app/pipeline.py 제출 경로
계약   **토글을 두 곳에 두지 않는다** — `plugins` 테이블에 `active` 컬럼이 없다
       강제는 `pipeline` 의 제출 경로 한 곳뿐이다 — 비활성이면 그 토큰으로 401
       선언하지 않은 역할은 활성 상태에서도 막힌다(AUTH-6)
고정   test_plugins.py::test_deactivating_a_plugin_stops_its_token_at_the_pipeline
       test_plugins.py::test_the_toggle_is_the_service_status
       test_plugins.py::test_the_plugins_table_has_no_active_column
       test_plugins.py::test_a_plugin_cannot_use_a_role_it_did_not_declare
상태   구현됨

### PLUGIN-5  제거
정의   플러그인 행과 설치본을 지운다. **서비스 행은 남긴다.**
표면   `DELETE /v1/platform/plugins/{plugin_id}`
구현   app/plugins.py:uninstall
계약   서비스를 지우면 그 서비스로 집계된 사용량·감사가 이름을 잃는다
       대신 `inactive` 로 내려 둔다 — 과거는 읽히고 미래는 막힌다
고정   test_plugins.py::test_uninstalling_keeps_the_service_row
       test_plugins.py::test_uninstalling_removes_the_payload
상태   구현됨

### PLUGIN-6  목록과 상태
정의   설치된 플러그인, 서명 상태, 활성 여부, 디스크에 파일이 있는지를 한 번에 본다.
표면   `GET /v1/platform/plugins` · 관제 UI 플러그인 탭 (설치·토글·제거)
구현   app/plugins.py:snapshot · app/store.py:list_plugins
계약   **활성 여부의 출처는 서비스 하나뿐이다**(파생값이지 저장값이 아니다)
       행은 있는데 파일이 없는 상태가 드러난다 — 백업은 DB 만 뜨므로 복원 뒤가 그렇다
       플랫폼 관리자 전용이다
고정   test_plugins.py::test_the_snapshot_shows_missing_files
       test_plugins.py::test_the_routes_need_platform_admin
       test_plugins.py::test_install_activate_and_deactivate_over_http
       test_plugins.py::test_the_lifecycle_is_audited
계약   화면이 자체 상태를 들고 있지 않다 — 활성 여부는 서버가 서비스에서 파생해 준다
       번들 업로드는 raw body 다(멀티파트는 6번째 의존성)
상태   부분 — `external` 외 실행 형태 없음. 이벤트·스케줄 트리거 없음

### PLUGIN-7  재귀 방지 — 플러그인이 만든 잡은 아무것도 깨우지 않는다
정의   잡마다 그것을 만든 플러그인을 적어 두고, 그 잡의 완료로는 어떤 플러그인도 깨우지 않는다.
       막는 고리는 "플러그인이 깨어난다 → `/v1/generate` → 완료 → 다시 깨어난다" 다 —
       한 바퀴마다 돈을 쓰면서 예산이 다 탈 때까지 안 멈춘다.
표면   `GET /v1/platform/plugins` 의 `jobs_created` · 관제 UI 플러그인 탭 「만든 잡」 칸
구현   app/pipeline.py:_create_job(표식) · app/store.py:plugin_id_for_service ·
       app/plugins.py:may_wake_plugins(판정) · jobs.origin_plugin(칸)
계약   표식은 **잡 행에 박힌다** — 플러그인을 지워도 남는다(되짚으면 지운 순간 출처가 사라진다)
       판정은 깨울 대상을 인자로 받지 않는다 — 자기 고리만 막으면 A→B→A 가 남는다
       `origin_plugin` 을 해석하는 곳은 `may_wake_plugins` 하나다(구조 검사가 지킨다)
       깊이 카운터를 쓰지 않는다 — 플러그인은 새 HTTP 요청으로 들어오므로 서버가 인과를 못 본다.
       상관 토큰을 되돌려 받으면 셀 수는 있으나 그러면 방지가 플러그인의 성의에 달린다
고정   test_plugins.py::test_a_job_a_plugin_created_does_not_wake_that_plugin
       test_plugins.py::test_a_job_a_person_created_does_wake_plugins
       test_plugins.py::test_a_plugins_job_wakes_no_plugin_at_all_not_just_its_own
       test_plugins.py::test_the_origin_survives_uninstalling_the_plugin
       test_architecture.py::test_the_job_creating_call_stamps_the_origin
       test_architecture.py::test_only_one_place_decides_what_the_origin_means
상태   부분 — 표식과 판정은 있고 **그 판정을 묻는 트리거가 아직 없다.**
       표식은 잡이 만들어지는 순간에만 붙일 수 있어 트리거보다 먼저 넣었다

---

## 13. 하기로 했지만 아직 없는 것

여기 있는 항목은 **구현이 없습니다.** 표면도 구현 위치도 없으므로 그 칸을 비웁니다.
ID 를 주는 이유는 고도화 논의에서 가리킬 이름이 있어야 하기 때문입니다.
"하지 않기로 한 것" 은 여기가 아니라 [README 부채 표](../README.md#열어두는-부채) 의 두 번째 절에 있습니다.

### AUTH-8  관리 신원 연동
정의   IdP(OIDC/SAML) 연동 · MFA · 관리 세션 만료.
없어서   관리자의 퇴사는 IdP 에서 일어나는데, 이 제품의 토큰은 거기 이어질 길이 없습니다. 사람이 손으로 폐기해야 합니다.
판정   D12
상태   미구현

### PIPE-7  선언적 체인 실행
정의   여러 단계를 묶어 한 요청으로 돌리는 map-reduce 형 실행.
없어서   호출자가 단계마다 따로 요청하고 중간 결과를 자기가 들고 있어야 합니다.
판정   L2
상태   미구현

### PIPE-8  스트리밍 응답
정의   토큰 단위로 흘려보내는 응답.
없어서   긴 생성에서 첫 글자까지의 체감 지연이 그대로 드러납니다. 출력 검사(GUARD-10)를 스트림 중간에 어떻게 걸 것인지가 함께 풀려야 합니다.
판정   G7
상태   미구현

### CLUSTER-14  테넌트별 클라우드 API 키
정의   클라우드 프로바이더 키를 테넌트마다 따로 두는 것.
없어서   키가 플랫폼 레벨(`api_key_env`)이라 테넌트별 분리·회수가 안 됩니다. 청구서도 한 장으로 옵니다.
판정   D5
상태   미구현

### CLUSTER-15  3단 이상 데이터 경계
정의   `internal`/`external` 사이에 `partner` 같은 중간 등급을 두는 것.
없어서   경계가 두 값 고정이라 "협력사까지는 되고 공개 클라우드는 안 됨" 을 표현할 수 없습니다. 구조는 순서 있는 신뢰 등급으로 일반화 가능합니다(architecture.md §13).
판정   D15
상태   미구현

### CRYPTO-7  테넌트 DEK 재래핑
정의   마스터 KEK 가 아니라 테넌트 DEK 자체를 교체하는 것.
없어서   DEK 가 샜다고 판단되면 그 테넌트는 파기(CRYPTO-3) 외에 선택지가 없습니다. 재래핑은 암호문 전체 재암호화를 뜻해 기각됐습니다.
판정   D4 / G5
상태   미구현

### META-6  OpenAI 호환 어댑터
정의   기존 OpenAI SDK 를 그대로 쓸 수 있는 호환 엔드포인트.
없어서   설치처가 SDK 를 버리고 이 제품의 계약(META-1)에 맞춰 코드를 고쳐야 합니다.
판정   G6
상태   미구현

---

## § 고도화 후보

`부분` 과 `미구현` 인 기능을 한자리에 모았습니다. **여기서 새 판정을 내리지 않습니다** —
판정과 해제 조건은 [design-decisions.md](design-decisions.md) 소관이고 이 절은 색인입니다.

| 기능 | 지금 어디까지 | 상태 | 판정 |
|---|---|---|---|
| GUARD-10 출력 검사 | 1단 패턴만. 응답도 마스킹·봉인·보존·열람 감사를 거치지만 2단 LLM 맥락 분류는 입력 전용 | 부분 | D1 / G1 |
| GUARD-8 시크릿 탐지 | 벤더 접두사와 대입 문맥 기반. 맨몸 랜덤 문자열은 못 잡음 | 부분 | G2 |
| GUARD-9 인젝션 내성 | 울타리는 표현과 무관하게 돌지만 카나리아는 형식 포기형만 잡음 | 부분 | D9 / G4 |
| PIPE-3 멱등성 키 | 키가 작업을 식별함. 페이로드를 비교하지 않고 `/v1/embed` 는 미적용 | 부분 | D7 / G3 |
| ROUTE-4 라우팅 정확도 | 계측 도구는 있으나 픽스처가 번들에 없음. 안 재면 맞는지 아무도 모름 | 부분 | L1 |
| OPS-10 토큰 처리율 | 보여주기만 하고 한도로 걸지 않음. 설치처 분포를 모르는 채 건 한도는 꺼짐 | 부분 | D8 |
| AUTH-8 관리 신원 연동 | 없음 | 미구현 | D12 |
| PIPE-7 선언적 체인 실행 | 없음 | 미구현 | L2 |
| PIPE-8 스트리밍 응답 | 없음 | 미구현 | G7 |
| CLUSTER-14 테넌트별 클라우드 키 | 없음 | 미구현 | D5 |
| CLUSTER-15 3단 이상 데이터 경계 | 없음 | 미구현 | D15 |
| CRYPTO-7 테넌트 DEK 재래핑 | 없음 | 미구현 | D4 / G5 |
| META-6 OpenAI 호환 어댑터 | 없음 | 미구현 | G6 |

### 기능 항목이 아닌 한계

기능을 더하는 것이 아니라 **이미 있는 기능의 한계**입니다. 별도 ID 를 주지 않습니다.

| 한계 | 어느 기능의 | 판정 |
|---|---|---|
| 감사 조작을 **막지는** 못한다. 사슬은 드러낼 뿐이고, DB 쓰기 권한자의 전체 재계산은 외부 사본 대조로만 걸린다 | CRYPTO-4 CRYPTO-5 | D10 |
| 라우팅과 가드 호출이 합쳐지지 않았다. 둘 다 켠 역할은 요청당 노드 호출이 최대 3회 | GUARD-3 ROUTE-2 | L1 |

---

## § 명세에 넣지 않은 것

**요청·응답 스키마를 손으로 옮겨 적지 않습니다.** `GET /v1/openapi.json` 이 토큰마다 생성하고
([META-2](#meta-2--openapi)), 그 재고는 라우트 테이블에서 뽑습니다. 여기 옮겨 적으면 두 벌이 되고
두 벌은 어긋납니다. 파라미터·응답 필드·오류 코드는 그쪽을 보십시오.

**"하지 않기로 한 것"** 은 [README 부채 표](../README.md#열어두는-부채) 의 두 번째 절에 있습니다.
위 § 고도화 후보는 **하기로 했지만 아직 안 된 것**만 모은 것입니다. 섞으면 미구현이 설계 결정으로 위장됩니다.

---

## 부록 A — 라우트 ↔ 기능 대응

라우트 이름은 `app/meta.py` 의 `ROUTE_SUMMARIES` 키입니다. 대상별로 나눴습니다.

### 소비자 (consumer)

| 라우트 이름 | 경로 | 기능 |
|---|---|---|
| `generate` | `POST /v1/generate` | PIPE-1 PIPE-2 |
| `embed` | `POST /v1/embed` | PIPE-5 |
| `job_get` | `GET /v1/jobs/{job_id}` | PIPE-4 |
| `job_cancel` | `DELETE /v1/jobs/{job_id}` | PIPE-4 |
| `roles` | `GET /v1/roles` | AUTH-6 |
| `status` | `GET /v1/status` | OPS-9 |
| `session` | `GET /v1/session` | META-1 OPS-8 |
| `meta` | `GET /v1/meta` | META-1 |
| `integration` | `GET /v1/integration` | META-3 |
| `openapi_json` | `GET /v1/openapi.json` | META-2 |
| `openapi_yaml` | `GET /v1/openapi.yaml` | META-2 |
| `client_index` | `GET /v1/client` | META-4 |
| `client_file` | `GET /v1/client/{name}` | META-4 |

### 테넌트 관리자 (tenant_admin)

| 라우트 이름 | 경로 | 기능 |
|---|---|---|
| `tenant_services` | `GET/POST /v1/admin/services` | AUTH-1 |
| `tenant_tokens` | `GET/POST /v1/admin/tokens` | AUTH-1 |
| `tenant_token_rotate` | `POST /v1/admin/tokens/{id}/rotate` | AUTH-2 |
| `tenant_token_revoke` | `DELETE /v1/admin/tokens/{id}` | AUTH-2 |
| `tenant_guard_rules` | `GET/POST /v1/admin/guard/rules` | GUARD-7 |
| `tenant_guard_rule_delete` | `DELETE /v1/admin/guard/rules/{id}` | GUARD-7 |
| `tenant_guard_events` | `GET /v1/admin/guard/events` | GUARD-12 |
| `tenant_guard_review` | `POST /v1/admin/guard/events/{id}/review` | GUARD-12 |
| `tenant_guard_promote` | `GET /v1/admin/guard/rules/{id}/promotion` | GUARD-12 |
| `tenant_settings` | `GET/PUT /v1/admin/settings` | AUTH-3 DATA-2 CLUSTER-7 |
| `tenant_overrides` | `GET/POST/DELETE /v1/admin/overrides` | AUTH-7 |
| `tenant_jobs` | `GET /v1/admin/jobs` | OPS-9 |
| `tenant_job_raw` | `GET /v1/admin/jobs/{id}/raw` | CRYPTO-6 |
| `tenant_job_review` | `POST /v1/admin/jobs/{id}/review` | SCHED-6 |
| `tenant_usage` | `GET /v1/admin/usage` | OPS-10 |
| `tenant_audit` | `GET /v1/admin/audit` | CRYPTO-4 |
| `tenant_export` | `GET /v1/admin/export` | DATA-5 |
| `tenant_purge_end_user` | `DELETE /v1/admin/end-users/{hash}` | DATA-3 |

### 플랫폼 관리자 (platform_admin)

| 라우트 이름 | 경로 | 기능 |
|---|---|---|
| `platform_tenants` | `GET/POST /v1/platform/tenants` | DATA-1 |
| `platform_tenant_purge` | `DELETE /v1/platform/tenants/{id}` | DATA-4 CRYPTO-3 |
| `platform_nodes` | `GET/POST /v1/platform/nodes` | CLUSTER-1 |
| `platform_node_drain` | `POST /v1/platform/nodes/{node}/drain` | CLUSTER-8 |
| `platform_models` | `GET /v1/platform/models` | CLUSTER-11 |
| `platform_model_approve` | `POST /v1/platform/models/{id}/approve` | CLUSTER-11 |
| `platform_model_delete` | `DELETE /v1/platform/nodes/{node}/models/{model}` | CLUSTER-12 |
| `platform_catalog` | `GET /v1/platform/catalog` | CLUSTER-13 |
| `platform_overview` | `GET /v1/platform/overview` | OPS-9 |
| `platform_guard_baseline` | `GET /v1/platform/guard/baseline` | GUARD-1 |
| `platform_grace_mode` | `POST /v1/platform/guard/grace-mode` | GUARD-6 |
| `platform_evals` | `GET/POST /v1/platform/evals` | GUARD-11 GUARD-13 ROUTE-4 |
| `platform_plugins` | `GET/POST /v1/platform/plugins` | PLUGIN-1 PLUGIN-3 PLUGIN-6 |
| `platform_plugin_activate` | `POST /v1/platform/plugins/{id}/activate` | PLUGIN-4 |
| `platform_plugin_delete` | `DELETE /v1/platform/plugins/{id}` | PLUGIN-5 |
| `platform_diagnostics` | `GET /v1/platform/diagnostics` | OPS-3 |
| `platform_notifications` | `GET/POST /v1/platform/notifications` | OPS-4 |
| `metrics` | `GET /metrics` | OPS-1 |

### 공개 (public)

| 라우트 이름 | 경로 | 기능 |
|---|---|---|
| `healthz` | `GET /healthz` | META-5 |
| `ui_index` | `GET /ui` | OPS-8 |
| `ui_index_slash` | `GET /ui/` | OPS-8 |
| `ui` | `/ui` 정적 자산 | OPS-8 |

---

## 부록 B — CLI ↔ 기능 대응

| 명령 | 기능 |
|---|---|
| `serve` | 전체 서비스 기동 (`--demo` PKG-2 · `--airgap` CLUSTER-9 PKG-4) |
| `bootstrap` | PKG-1 |
| `doctor` | OPS-5 CRYPTO-2 CRYPTO-4 |
| `rotate-kek` | CRYPTO-2 |
| `audit-export` | CRYPTO-5 |

---

## 부록 C — 설정 파일 ↔ 기능 대응

| 파일 | 무엇을 정하나 | 기능 |
|---|---|---|
| `config/roles.yaml` | 역할 = 모델·배치·레인 정책, 라우팅 옵트인 | AUTH-6 AUTH-7 CLUSTER-3 ROUTE-1 |
| `config/lanes.yaml` | 레인별 동시 실행 상한과 기아 임계 | SCHED-2 SCHED-3 |
| `config/nodes.yaml` | 노드 시드 (DB 가 이긴다) | CLUSTER-1 CLUSTER-5 CLUSTER-10 |
| `config/guard.yaml` | 로케일 팩·맥락 규칙·시크릿·인젝션 | GUARD-1 GUARD-2 GUARD-3 GUARD-8 GUARD-9 |
| `config/pricing.yaml` | 프로바이더별 단가 | CLUSTER-6 PIPE-6 |
| `config/thresholds.yaml` | 헬스·재시도·스캔 창·경고 임계 | CLUSTER-2 SCHED-3 SCHED-4 |
| `config/catalog.yaml` | 설치 가능한 모델 목록 | CLUSTER-13 |
