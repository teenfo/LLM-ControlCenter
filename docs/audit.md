# 코드 감사 보고서 — 2026-08-27

전 모듈(app 20개 · static 3개 · clients 2개 · 셸 스크립트 5개 · 설정 7개, 약 20,000줄)을
4개 영역(API/신원 · 데이터/암호 · 실행 경로 · 운영/UI/패키징)으로 나눠 정독하고,
의심 지점은 실제 실행 또는 호출 경로 추적으로 재현·확증했다. README 의
"열어두는 부채" 표에 이미 명시된 항목은 **결함으로 세지 않았다** — 승인된 부채다.

**총평**: 설계 문서와 코드의 정합성, 테넌트 스코프 초크포인트, 가드 순서 강제,
테스트 753개의 밀도는 이 규모의 프로토타입으로서 우수하다. 그러나 발견된 결함의
공통 패턴이 뚜렷하다 — **성공 경로는 테스트가 촘촘한데, 실패 경로·갱신 경로·
재기동 경로가 비어 있다.** HIGH 13건 중 12건이 753개 테스트가 전부 통과하는
상태에서 존재한다.

| 심각도 | 건수 | 성격 |
|---|---|---|
| HIGH | 13 | PII 유출 · 죽은 기능 · 데이터 소실 · 전면 장애 경로 |
| MEDIUM | 28 | 계약 위반 · 오분류 · 500 누수 · 자원 누수 |
| LOW / 최적화 | 27 | 방어턱 · 성능 · 일관성 |

표기: [재현] = 실행으로 재현함 · [확정] = 코드 경로 추적으로 확증함.

---

## HIGH

### H1. 겹치는 탐지 스팬에서 마스킹이 깨져 PII 일부가 유출된다 [재현]

`app/guard.py:487-503` — `_apply()` 는 치환을 시작 오프셋 역순으로만 적용한다.
서로 다른 두 규칙의 스팬이 겹치면 안쪽 치환 후 바깥 스팬의 오프셋이 밀린
텍스트를 가리킨다.

재현: 스팬 (5,24) + (10,19) 조합에서 출력이 `'AAAA [CARD]L]-3456 BBBB'` —
**카드번호 뒷자리 `-3456` 이 마스킹 없이 살아남는다.** 완전히 같은 스팬에
`full` 과 `partial` 규칙이 함께 걸리면 `full` 마스킹이 통째로 소실된다.
기존 테스트 `test_overlapping_offsets_do_not_corrupt_the_text` 는 이름과 달리
**겹치지 않는** 매치만 검증한다.

**권고**: 치환 전 스팬을 병합(coalesce)하고, 겹칠 때는 더 강한 등급/더 넓은
스팬을 채택한다. 실제 겹침 케이스를 테스트로 고정한다.

### H2. 정규식 컴파일 캐시가 갱신되지 않는다 — 규칙 수정 무시 + 테넌트 간 간섭 [재현]

`app/guard.py:259-261` — 캐시 키가 `rule.id` 뿐이고 무효화가 없다.

- 테넌트가 `PUT /v1/admin/guard/rules` 로 패턴을 수정해도 **재기동 전까지 옛
  패턴이 적용된다** (라벨·등급만 새 값). 관리자가 "고쳤다" 고 믿는 규칙이 옛
  규칙으로 돈다.
- 테넌트 A 와 B 가 같은 id 의 규칙을 다른 패턴으로 등록하면 **먼저 컴파일된
  쪽의 패턴이 둘 다에 적용된다.** 재현: B 의 `BBB\d+` 패턴이 전혀 매칭되지
  않고 A 의 `AAA\d+` 매치에 B 의 라벨이 붙었다.
- 부수: 캐시가 테넌트 규칙 수만큼 무한 성장한다.

**권고**: 캐시 키를 패턴 문자열(또는 `(id, pattern)`)로 바꾸거나 규칙 저장
시점에 무효화한다.

### H3. `GET /v1/platform/tenants` 가 항상 500 — 플랫폼 콘솔의 테넌트 목록이 죽어 있다 [확정]

`app/main.py:1060-1061` 이 `row["rate_limit_per_min"]` · `row["dek_wrapped"]` 를
읽지만 `app/store.py:1441` 의 `list_tenants` SELECT 에 그 두 컬럼이 없다.
`sqlite3.Row` 키 오류 → 500. 기존 테스트는 이 경로의 성공 케이스를 호출하지
않는다(403 과 POST 만 검증).

**권고**: SELECT 에 두 컬럼 추가 + 성공 경로 테스트.

### H4. 역할 오버라이드가 실제 요청에 전혀 적용되지 않는다 — 문서화된 기능이 죽은 코드 [확정]

`PUT /v1/admin/overrides` 는 저장·감사·조회·export 까지 하지만, 요청 처리는
`self._config.roles`(설정 원본)만 쓴다. `config.py:560-601` 의
`apply_override`/`merge_overrides` 는 **정의부와 단위 테스트 외 호출이 0건**이다.
실측으로 `max_prompt_chars=5` 오버라이드 후 100자 프롬프트가 200 통과,
`timeout=7` 이 저장 잡에서 `timeout_s=120`. 아키텍처 §5(스냅샷 ∩ 현재 설정)의
"현재 설정" 절반이 실체가 없다.

**권고**: 역할 해석 지점(`pipeline`·`scheduler`)이 `merge_overrides` 결과를
타도록 배선하고, 오버라이드→실동작 테스트를 추가한다.

### H5. 관제 UI 로 등록한 노드가 재기동 시 소멸한다 [확정]

`app/cluster.py:483-484` — `register_node` 는 메모리 dict 와 감사 로그에만
남긴다. `Cluster.__init__`(147-150행)은 YAML `config.nodes` 에서만 노드를
만들고, **노드 선언을 담는 테이블이 스키마에 없다**(`node_health` 는 상태
전용). `config/nodes.yaml` 의 "부트스트랩 이후로는 DB 가 권위다" 주석은
구현되지 않은 약속이다. 프로덕션에서 UI 로 증설한 노드가 컨테이너 재시작
한 번에 사라지고, 그 노드에서 돌던 잡은 복구 후 배치 불가가 된다.

**권고**: 노드 선언 테이블 신설 + 기동 시 DB 우선 적재(YAML 은 시드).

### H6. `last_failed_node` 영구 배제 — 단일 노드 구성에서 재시도가 불가능하다 [확정]

`app/cluster.py:345-347` 은 직전 실패 노드를 조건 없이 탈락시킨다. 노드가
1대뿐인 구성(README 사양표의 **Starter 가 정확히 이 구성**)에서는 일시 오류
(타임아웃·429) 1회 후 잡이 그 노드로 다시는 못 가고, 900초 대기 후 엉뚱한
`administrative_wait_timeout` 으로 죽는다. `recover_running_jobs` 도
`last_failed_node` 를 심으므로 **재기동 후 복구된 잡 전원이 같은 함정에
빠진다.** 테스트는 항상 내부 노드 2대라 이 경로를 못 잡는다.

**권고**: 다른 후보가 없으면 배제를 완화하거나, 백오프 경과 후 배제를 푼다.

### H7. KEK 를 나중에 설정하면 기존 테넌트의 모든 생성 요청이 500 — DEK 백필 경로 부재 [확정]

`app/crypto.py:107-112` 의 `_unwrap` 은 `wrapped` 가 비면 무조건
`KeyDestroyed` 를 던진다 — "DEK 를 한 번도 안 만든 테넌트" 와 "파기된
테넌트" 를 구분하지 않는다. `pipeline._seal`(253-260행)은 `vault.enabled` 만
확인하므로, KEK 없이 생성된 테넌트(dek_wrapped=NULL)는 이후
`LCC_PROMPT_KEY` 를 설정하는 순간 `/v1/generate`·`/v1/embed` 전부가 미처리
예외로 500 이 된다. 부트스트랩 배너가 정확히 이 순서("나중에 키를 설정하면
켜집니다")를 안내한다.

**권고**: KEK 활성 시 DEK 없는 활성 테넌트에 DEK 백필(또는 seal 시점 생성).

### H8. 비용 예약이 입력 토큰을 0 으로 계상한다 [확정]

`app/scheduler.py:201` 이 항상 `prompt_chars=0` 으로 배치를 호출해
`cost.estimate_upper_bound` 가 출력분만 예약한다. cost.py 서두의 "상한
예약" 원칙(동시 디스패치 예산 초과 방지)이 생성 경로에서 무력화된다 —
대형 프롬프트에서는 입력비가 지배적이다. 동기 임베딩 경로만 실제 길이를
넘긴다. 부수: `CHARS_PER_TOKEN=3.0` 은 한국어/CJK(실측 1~2자/토큰)에서
상한이 아니라 과소 추정이다.

**권고**: 잡 행의 `length(prompt_masked)` 를 넘긴다(본문 재조회 불필요).
CJK 비율을 고려해 상수를 보수화한다.

### H9. `restore.sh` 3중 결함 — WAL 재생 · root 소유 · 검사 없는 "검사" [확정]

`restore.sh:26-68`:
1. DB 만 덮어쓰고 이전 `-wal`/`-shm` 을 지우지 않는다. 앱이 비정상 종료로
   남긴 핫 WAL 이 있으면 재기동 시 **이전 DB 의 WAL 프레임이 복원된 DB 위에
   체크포인트**되어 손상되거나, 보존 기간으로 지워졌어야 할 데이터가
   되살아난다.
2. 도커 경로의 `docker compose cp` 는 파일을 root 소유로 넣는데 컨테이너는
   `USER llmcc`(uid 10001)라 복원 직후 SQLite 쓰기(WAL 생성 포함)가 실패한다.
   네이티브 경로에는 있는 복원 전 안전 사본도 도커 경로에는 없다.
3. "스키마 호환 검사" 는 두 버전을 **출력만 하고 비교·차단하지 않는다.**
   현재 버전 조회 실패 시 `?` 를 찍고 계속 진행한다. 또한 backup.sh 가 담는
   `config/` 를 restore.sh 는 복원하지 않는다(조용한 비대칭).

**권고**: 복원 시 `-wal`/`-shm` 삭제, 복사 후 `chown`(또는 tar 경유), 버전
불일치 시 명시적 거부.

### H10. 알림 발송이 이벤트 루프를 블로킹한다 — 웹훅이 죽으면 관제 전체가 멈칫거린다 [확정]

`app/notify.py:115-150` — `WebhookChannel.send` 는 동기 `httpx.post`(5초),
`SmtpChannel` 은 동기 `smtplib`(10초)이고, 호출자는 전부 asyncio 안이다
(헬스 프로브의 `_announce`, 워치 루프, 알림 테스트 핸들러). 웹훅
엔드포인트가 다운이면 노드 전이·워치 주기마다 루프가 최대 5~15초 정지하고
그동안 `/v1/generate` 를 포함한 **모든 요청 처리가 멈춘다.** README 의
"실패가 파이프라인을 죽이지 않습니다" 는 예외만 삼킬 뿐 블로킹은 못 막는다.

**권고**: `asyncio.to_thread`(또는 전용 스레드 큐)로 오프로드.

### H11. `doctor.sh --bundle` — 진단이 실패하는 바로 그 순간 번들이 안 나온다 [확정]

`doctor.sh:10-30` — `set -eu` 아래에서 컨테이너 내 doctor 가 고장을 발견해
exit 1 을 돌려주면 스크립트가 그 자리에서 종료된다. 뒤따르는
`status=$?`(죽은 코드)와 `docker compose cp`(번들 복사)가 실행되지 않아,
**지원 번들이 가장 필요한 상황에서 정확히 번들이 로컬에 남지 않는다.**

**권고**: `exec ... || status=$?` 패턴으로 실패를 붙잡고 복사를 항상 수행.

### H12. `docker compose up -d` 최초 기동이 `./keys` 권한으로 크래시 루프할 가능성이 높다 [확정]

`compose.yml:28` 의 `./keys:/keys` 바인드는 호스트에 디렉터리가 없으면 root
소유로 생성된다(있어도 대개 uid 10001 이 아니다). `bootstrap.ensure_master_key`
(bootstrap.py:120-124)가 `/keys/master.key` 를 쓰다 `PermissionError` →
`cmd_serve` 에 예외 처리가 없어 트레이스백으로 죽고 `restart: unless-stopped`
로 크래시 루프한다. Dockerfile 의 `chown llmcc /keys` 는 바인드 마운트에
덮여 무효다. README 의 표준 설치 절차가 기본값 그대로면 이 경로를 밟는다.

**권고**: 기동 시 권한 검사 + 사람 말로 된 실패 메시지(preflight.sh 에 검사
추가), 문서에 `mkdir -p keys && chown` 안내.

### H13. 진단 번들에 테넌트 ID 가 실린다 — 번들 자신의 마스킹 약속 위반 [확정]

번들의 config 절은 의도적으로 `tenant_affinity_count`(개수만)를 쓰는데
(observability.py:318), 같은 번들에 실리는 `cluster.snapshot()` 은
`app/cluster.py:594` 에서 `tenant_affinity` 에 **테넌트 ID 목록을 그대로**
담는다. 예산 알림 이력의 `detail.tenant` 도 같은 경로로 새어 나간다.
기존 테스트는 픽스처에 affinity 노드·예산 알림이 없어서만 통과한다.

**권고**: snapshot 의 번들 경로에서 개수로 대체(또는 번들 조립 시 redact),
affinity·예산 알림이 있는 픽스처로 테스트 보강.

---

## MEDIUM

### 실행 경로

- **M1. 역할 기본 `system` 프롬프트가 실제 추론에 전달되지 않는다** [확정] —
  `scheduler.py:266-267` 은 잡의 마스킹본만 보고 `role.system` 폴백이 없다.
  반면 `pipeline.py:307` 의 `system_hash` 는 기본값을 **보낸 것처럼** 해싱해
  프롬프트 드리프트 추적도 왜곡된다. roles.yaml 의 "없을 때만 기본값" 계약
  위반.
- **M2. 주민등록번호 규칙의 실질 미탐** — 패턴(`guard.yaml:73`)이 7번째 자리
  1-4 만 허용해 외국인등록번호(5-8)를 배제하고, **2020-10 이후 부여/재발급
  번호는 뒷자리가 임의번호라 `kr_rrn` 체크섬이 성립하지 않아** 약 90% 가
  마스킹 없이 통과한다. 체크섬 실패 매치를 audit 등급으로라도 남기는 완화
  검토 필요.
- **M3. 헬스 프로브 순차 실행 + Anthropic `health()` 의 timeout 미전달** —
  `cluster.py:445` 가 순차 await 라 죽은 노드 N대면 한 바퀴 N×10초.
  `anthropic_provider.py:178-189` 는 받은 `timeout` 을 SDK 호출에 넘기지
  않는다. `asyncio.gather` 병렬화 + timeout 전달.
- **M4. 노드의 200+비정형 응답이 프로브 사이클을 중단시킨다** —
  `ollama.py:141-158` 의 `health()` 는 `httpx.HTTPError` 만 잡아
  `JSONDecodeError`/`KeyError` 가 탈출 → `probe_all` 컴프리헨션 중단 →
  `_health_loop` 의 `suppress` 가 통째로 삼킨다. 사전순 뒤 노드들이 매 주기
  프로브를 못 받고, 문제 노드는 unhealthy 판정도 못 받아 계속 배치된다.
- **M5. `asyncio.create_task` 참조 미보관 + `stop()` 미정리** —
  `scheduler.py:227` 태스크가 GC 로 증발하면 잡이 running 인 채 남고
  `finally` 의 예약 해제가 안 돈다. `stop()` 은 루프만 취소해 in-flight
  `_execute` 가 미정리 파괴 — 정상 종료가 크래시 복구 경로를 탄다.
- **M6. 영구적 배치 불가가 "행정적 부재" 로 오분류** — `cluster.py:359-394`
  는 가드가 좁힌 경계·`tenant_affinity`·에어갭처럼 잡 생애 동안 절대 변하지
  않는 조건도 WAIT 로 분류해 900초 후 `administrative_wait_timeout` 이라는
  오해를 부르는 코드로 죽인다.
- **M7. 인벤토리 미확보 배치 + `ModelNotFound` 즉사의 조합** — 기동 직후
  `state.models` 가 비면 의도적으로 통과시키는데(cluster.py:331-335), 그렇게
  잘못 간 노드의 `ModelNotFound` 는 `retryable=False`(base.py:74-84)라 다른
  노드가 멀쩡해도 재배치 없이 잡을 잃는다. `model_not_installed` 만 재배치
  예외로.
- **M8. `wait_for` 가 대기 요청당 50ms 간격 동기 전행 SELECT** —
  `pipeline.py:36,417-426`. 동시 대기 수십 건이면 초당 수백 건의 동기 DB
  호출이 루프를 점유한다. 완료 이벤트(asyncio.Event) 또는 간격 상향 +
  경량 컬럼 조회.

### 데이터 레이어

- **M9. 스토어에 rollback 이 0건** — 다중 문장 메서드(`purge_end_user`,
  `purge_expired`, `recover_running_jobs`)가 중간에 실패하면 부분 쓰기가 열린
  트랜잭션으로 남았다가 **다음 무관한 commit 에 섞여 무감사 영속화**된다.
- **M10. `purge_end_user` 의 IN 절이 SQLite 파라미터 한도를 넘을 수 있다** —
  `store.py:1697-1702`. 잡 3.3만 건 이상 엔드유저 파기가 `too many SQL
  variables` 로 실패하고 M9 와 결합하면 부분 파기가 남는다.
  `filter_events.job_id` 인덱스도 없어 이 UPDATE 는 풀스캔이다.
- **M11. `update_job` 에 상태 선행조건(CAS)이 없다** — store.py:717-733.
  문서가 지원한다는 다중 프로세스 구성에서 API 워커의 취소와 스케줄러의
  디스패치가 경합하면 "취소됨" 응답을 받은 잡이 실행·과금까지 간다.
- **M12. `settle` 이 2개의 독립 커밋** — cost.py:167-184. `update_job` 과
  `record_usage` 사이 크래시 시 예약은 풀리고 지출 기록은 사라져 예산이
  영구 과소 계상된다.
- **M13. `needs_review` 잡의 무한 축적 + 해소 경로 부재** — 보존 정리의 상태
  목록(store.py:1657, `TERMINAL_STATUSES` 를 안 쓰고 하드코딩)에 없어 영원히
  안 지워지고, 종결로 바꾸는 API/UI 도 없다.
- **M14. `backup.py` 가 자기 계약("행 수가 말이 되는지")을 안 지킨다** —
  테이블 존재만 검사하고 빈 스냅샷도 성공으로 내보낸다(backup.py:46-53).
  부수: 검증 실패 시 `prompt_cipher` 가 든 `/tmp/backup.db` 가 컨테이너에
  남는다(암호문 제거가 검증 뒤라서).
- **M15. `admin_audit`·`eval_runs` 는 어떤 보존 정리에도 없다** — 그런데
  플랫폼 개요 화면이 호출마다 감사 2행을 쓴다(store.py:1421-1438). 대시보드
  폴링 = 감사 테이블 무한 증식.

### API 계층

- **M16. 숫자 입력 미검증 → 400 대신 500 이 광범위하다** — `priority`,
  `wait`, `since`, `limit`, `grace_seconds`, `keep_tail`,
  `raw_prompt_retention_days`, `int(event_id)` 등 10여 곳이 `int()`/`float()`
  ValueError 를 그대로 500 으로 흘린다. `embed` 의 비리스트 `input` 도 동일
  (dict 를 주면 키 리스트로 **조용히 잘못 처리**된다).
- **M17. 승격 게이트가 규칙 저장(PUT)에서 강제되지 않는다** — promote
  핸들러 독스트링은 "실제 적용은 PUT 이고 그쪽이 게이트를 다시 검사한다"
  라고 주장하지만 PUT 은 `can_promote` 를 호출하지 않는다. 신규 규칙을
  측정 없이 바로 `block` 으로 저장 가능.
- **M18. 중복 생성 `IntegrityError` → 500** — create_tenant/create_service 의
  PK 충돌이 409 아닌 500 으로 나간다.
- **M19. 예약 테넌트 `_platform` 을 파기할 수 있다** — 확인값만 맞으면
  플랫폼 콘솔 토큰·플랫폼 설정(`guard_grace_mode` 포함)이 삭제되어 전체 관리
  접근을 되돌릴 수 없이 상실한다(자기 잠금).
- **M20. `/v1/status` 가 내부 역할명·노드 토폴로지를 소비자 토큰에 노출** —
  `single_homed_roles()` 가 `_guard_classify` 포함 역할→노드 매핑을 그대로
  돌려준다. meta/openapi 가 공들여 숨긴 것을 이 엔드포인트가 흘린다.
- **M21. 요청 본문 크기 상한 없음** — `_body` 가 전량을 메모리에 읽은 뒤에야
  `max_prompt_chars` 를 프롬프트 필드에만 적용한다. 거대 본문/`metadata` 로
  메모리 소진 가능.

### 운영/UI

- **M22. 기동·회복 시 가짜 "급증" 알림** [에이전트 재현] — 정상 상태도
  `observe()` 로 넣는데 `guard_blocks_spike`·`classifier_error_rate` 가
  `RECOVERY_EVENTS` 에 없어 재시작 후 첫 워치 주기마다 "0건 급증" 알림이
  나가고, 회복 전이도 "급증" 제목으로 발송된다. "상태 전이에서만" 원칙 위반.
- **M23. 로그인 화면이 항상 원시 i18n 키를 표시한다** — `boot()` 가 빈
  카탈로그 상태에서 `applyStaticStrings()` 를 호출해 index.html 의 폴백
  텍스트를 `"ui.sign_in"` 류 리터럴로 덮어쓴다. 모든 설치의 첫 화면이 깨져
  보인다.
- **M24. `clients/client.py` 의 한도 초과 처리 코드가 자기 자신이 죽는다**
  [에이전트 재현] — `limited.retry_after` 속성이 없어 AttributeError.
  오류 계약 시범이 목적인 파일에서 정확히 그 시범 경로가 깨진다.
- **M25. 테스트 알림 버튼이 거짓 성공을 보고한다** — `notifier.send` 의
  반환값을 버리고 무조건 `{"sent": true}`. 5분 중복 억제에 걸린 두 번째
  테스트는 아무 데도 안 나가는데 UI 는 성공 표시.
- **M26. 구조화 로그가 사실상 비어 있다** — README 가 약속한 "구조화 JSON
  stdout" 의 `log_event()` 는 **앱 코드 어디서도 호출되지 않고**, 배경 루프
  4곳은 `suppress(Exception)` 으로 예외를 무음 소멸시킨다. "조용한 실패를
  시끄럽게" 원칙의 정반대.
- **M27. 셸 스크립트가 `python`(비 `python3`)을 부른다** — README 권장 데모
  OS(Xubuntu 24.04)에 `python` 바이너리가 없다. restore.sh 는 도커 모드에서도
  호스트 python 으로 스키마 검사를 하므로 복원 자체가 중단된다.
- **M28. 모델 화면이 서버의 `missing` 목록을 렌더링하지 않는다** —
  `/v1/platform/models` 가 주는 "역할이 요구하는데 어느 노드에도 없는 모델"
  이 화면 어디에도 안 뜬다.

---

## LOW / 최적화

### 보안 방어턱

- `crypto.seal` 이 AAD 없이 AES-GCM 사용 — `tenant_id`/`job_id` 를 AAD 로
  바인딩하면 DB 쓰기 가능 공격자의 암호문 이식을 탐지할 수 있다.
- 가드에 유니코드 정규화(NFKC) 부재 — 전각 하이픈·NBSP·zero-width 삽입으로
  패턴 회피 가능. 검사 전 정규화는 저비용 방어다.
- `keep_tail` 상한 검증 없음 — `keep_tail: 100` 이면 값 전체가 남는 "마스킹".
- `rotate_token` 에 발급 경로에는 있는 `platform_admin` 차단이 없다(비대칭).
- `/metrics` 가 최고 권한 토큰을 요구 — Prometheus 설정에 플랫폼 관리자
  토큰을 평문 보관하게 된다. 읽기 전용 메트릭 자격증명 검토.
- `authenticate` 의 "상수 시간 비교" 는 실효 없음(인덱스 조회가 이미 타이밍
  경로) — 실질 위험은 없으나 독스트링이 과장이다.
- 관리·계약 엔드포인트(`session`·`meta`·`openapi` 등 비캐시 생성 경로)에
  레이트리밋 없음.

### 정합성·강건성

- RateLimiter 의 검사-증가 비원자(동시 요청이 한도를 약간 초과 가능).
- `touch_token` 만 commit 없는 쓰기 — 다중 프로세스 구성에서 WAL 쓰기 락을
  쥔 채 늘어질 수 있다.
- `recover_running_jobs` 재큐가 `max_retries` 상한을 우회(무한 재큐 가능).
- 사용량 보존 30일과 예산 롤링 창 30일의 암묵 결합 — 한쪽 상수만 바뀌면
  `spend_since` 가 조용히 과소 계상. reviewed filter_events 도 같은 30일
  정리로 지워져 승격 게이트 표본이 퇴행할 수 있다.
- 임베딩 병합 `GuardResult` 가 `classifier_attempted` 누락 — 분류 실패율
  분모 과소 집계.
- Ollama 오류 분류가 본문 부분문자열 `"not found"` 로 `ModelNotFound` 판정 —
  무관한 5xx 도 비재시도로 오분류.
- Anthropic 프로바이더가 `temperature`/`top_p` 를 침묵 폐기 — 같은 역할이
  티어에 따라 다른 샘플링으로 돈다(테스트가 의도로 고정 중 — 정책 재검토).
- `place()` 가 락 안에서 metered 후보마다 SQLite 2회 조회, 예약 기록은 락
  밖 — 락의 존재 이유인 다중 스레드에서 확인-예약 비원자.
- `drain(force=True)`→`undrain` 시 실행 중 잡 카운터 소실로 과배치.
- 재시도 백오프·프로브 주기에 지터 없음.
- evals 의 규칙 미존재 오류가 `unknown_role` 코드로 나감(오용).
- `_ok` 의 `Content-Language` 는 설정하는 곳이 없어 죽은 경로.
- `/v1/session` 이 `_classifier_ready` 를 2회 계산(DB 조회 2회).
- 중복 생성·검증 실패 시 Slack `attachments.fields` 스키마 불일치로 웹훅이
  400 거절될 수 있음 — 예외는 삼켜져 알림 통째 유실. attachments 제거가 안전.

### 자원·성능

- `/metrics` 스크레이프마다 filter_events 풀스캔 2회(전역 집계에 쓸 인덱스
  없음 — `idx_filter_review` 는 tenant_id 선두).
- `Notifier.history` 무한 성장(절단 없음). `llmcc_notifications_sent` 는 발송
  실패·채널 0개여도 증가(의미 왜곡).
- `MockProvider.call_log` 무한 성장 — Demo 프로파일은 상시 구동 대상이다.
- `export_tenant` 가 잡·사용량·이벤트 전량을 무제한 메모리 적재.
- UI: 캐시버스팅 없는 `app.js` 참조(업그레이드 후 스테일 JS), 자동 갱신
  전무(노드가 죽어도 새로고침 전까지 과거 화면), `refresh()` 탭 경합.
- 노드 등록 폼에 인증 필드가 없어 external 노드 등록이 항상 실패(서버는
  TLS+인증 강제, 폼은 입력 수단 없음).
- 비-editable 설치(`pip install .`) 깨짐 — `pyproject.toml` 의
  `include=["app*"]` 라 config/locales/static/clients 가 배포물에 없고
  `cli.py` 는 파일 위치 기준 경로를 쓴다. `-e` 설치와 도커 WORKDIR 에서만
  동작하는 취약한 패키징.

---

## 잘 되어 있는 것 (검증 완료)

- 테넌트 스코프: `_scoped_where` 초크포인트 우회 없음(전수 확인). 전 테넌트
  조회는 `PlatformScope` 강제 + 감사 기록.
- SQL 주입: 동적 SQL 전부 화이트리스트 뒤. 파라미터 바인딩 일관.
- 가드 순서(인증→가드→저장→배치)와 잡 생성 단일 경로 — 구조로 강제되고
  아키텍처 테스트가 지킨다.
- 백업의 `prompt_cipher` 제외 — NULL + VACUUM 으로 바이트 수준까지 제거.
  KEK 미포함. crypto-shredding 동작.
- 원문 미노출: UI·목록·export 는 마스킹본만, 원문은 단건 API + 감사.
- 논스: 레코드마다 신선. 체크섬 알고리즘(luhn·kr_biz·jp_mynumber·iban)
  표준과 일치.
- XSS: `el()` textContent 일관, innerHTML 0건. 토큰은 sessionStorage.
- Docker: 비루트, 단일 포트, 기본 127.0.0.1 바인드, 레이어에 비밀 없음.
- 메트릭 라벨에 테넌트 없음(H13 의 번들 경로 제외).
- 로케일: en-US/ko-KR 157키 완전 일치, 죽은 키 0건.
- 에어갭: 배치 차단 + 등록 거부 + UI 표시 모두 구현.
- 재시도 시 비용 이중 정산 없음.

---

## 패턴 분석 — 왜 753개 테스트가 이것들을 놓쳤나

1. **성공 환경 전제**: 웹훅 다운, 진단 실패, 권한 불일치, 재기동 같은 실패
   경로가 테스트 환경에 존재하지 않는다 (H9~H12, M22).
2. **저장만 검증, 적용 미검증**: 오버라이드(H4)·승격 게이트(M17)·가드 규칙
   수정(H2)은 "저장됐는가" 만 테스트하고 "다음 요청에 반영되는가" 를 안 본다.
3. **픽스처가 항상 넉넉하다**: 노드 2대(H6), affinity 없음(H13), 겹치지 않는
   매치(H1) — 경계 조건이 픽스처에 없다.
4. **핸들러 성공 경로 미호출**: `GET /v1/platform/tenants`(H3)는 403 테스트만
   있다.

**권고 우선순위**: ① H1·H2(가드 = 제품의 핵심 보증) → ② H3~H5(죽은
기능·데이터 소실) → ③ H7·H9·H12(설치·복원 첫날 장애) → ④ H6·H8·H10 →
⑤ MEDIUM 의 실행 경로(M1~M8) → 나머지. 각 수정은 위 패턴 1~4 에 해당하는
테스트를 함께 추가할 것.
