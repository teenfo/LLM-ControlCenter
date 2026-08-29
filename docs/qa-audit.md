# 구현 QA 감사 보고서 — 2026-08-29

`docs/audit.md`(구현 감사)·`docs/design-audit.md`(설계 감사)·`docs/orchestration-plan.md`
(오케스트레이션 계획)에서 제기된 항목들이 **실제로 구현됐고, 주장대로 동작하는가**를
매우 치밀한 감사자 입장에서 검증한 보고서다.

**검토 대상**: 구현 브랜치 `claude/mcp-enable-all-t0di60`, 커밋 범위 `f0ad820..d67f461`
(29개 커밋, 앱 코드 대폭 증가 + 신규 테스트 15파일). 전체 테스트 **1272 passed,
1 skipped**(skip 은 root 권한 비트 테스트로 정당).

**방법**: 4개 영역(가드 축 · 데이터/암호 · 실행/라우팅 · 운영/API/패키징)을 나눠
전 파일 정독 + 실제 실행 재현. 커밋 메시지·문서의 주장을 코드 및 실행과 대조.
표기 — **[재현]** 실행으로 확증 · **[확정]** 코드 경로 추적으로 확증.

---

## 총평

**수정의 질은 높다.** 두 감사 보고서의 HIGH 13 + MEDIUM 28 + LOW 27 항목 대부분이
실제로 구현됐고, 실행으로 검증했을 때 주장대로 동작한다. 문서(`design-decisions.md`)는
과장 없이 정직하며, 드리프트 방지 테스트(`test_decisions.py`·`test_blast_radius.py`·
`test_coverage_map.py`)가 문서-코드 정합을 강제한다. capacity 문서의 "실측" 수치는
이 하드웨어에서 재실행해 동일 자릿수로 재현됐다.

**그러나 새 코드 약 15,000줄이 새 결함을 들여왔다.** 그리고 그 결함들의 성격이
일관된다 — **원래 감사가 지적한 바로 그 실패 유형("초록 테스트 뒤에 살아 있는
결함", "성공 경로만 검증", "표본 편향")이 새 코드에도 재현됐다.** 1272개 테스트가
전부 통과하는 상태에서 아래 CRITICAL·HIGH가 존재한다.

| 심각도 | 신규 발견 | 성격 |
|---|---|---|
| CRITICAL | 1 | 비-editable 설치가 출력 가드 없는 낡은 앱을 설치 |
| HIGH | 4 | KEK 회전 크래시 시 데이터 손실 오진 · 라우팅 전건 무력 · 한국어 조사 PII 유출 · 복구-정산 경합 이중 실행 |
| MEDIUM | 8 | 시크릿 FP/FN · 성공 정산 비원자 · 감사 체인 경합 · 미인덱스 핫쿼리 등 |
| LOW/INFO | 12 | 경계 처리 · 문서 부정확 · 잔여 한계 |

**출하 차단**: P-1(패키징). **차기 릴리스 전 필수**: V1·R-HIGH·G-HIGH·V2.

---

## CRITICAL

### P-1. 커밋된 `build/lib/**` 가 비-editable 설치를 낡은 앱으로 오염시킨다 [재현]

`build/lib/` 44개 파일이 커밋돼 있고(`.gitignore` 에 `/build/` 없음 — `/dist/`·
`/bundle/` 만), 이 스냅샷은 **어느 커밋과도 일치하지 않는 낡은 작업본**이다.
setuptools `build_py` 가 mtime 으로만 갱신하는데 git 체크아웃 시 `build/`(알파벳상
`app/` 뒤)가 더 늦게 쓰여 소스가 "더 오래된" 것으로 판정된다.

**재현**(저장소 밖 깨끗한 venv 에서 `pip install .`):
```
설치본 위치: .../site-packages/app
설치본 guard.py 의 inspect_output(출력 가드): 부재
설치본 cli.py 의 rotate-kek·audit-export: 부재
설치본 guard.py vs 소스: DIFFERS (낡음)
```
**결과**: `pip install .` 한 설치처는 **출력 가드(G1)·인젝션 방어·감사 체인·리스
슬롯·KEK 회전 CLI 가 빠진 앱**을 받으면서 문서·README 는 전부 구현됐다고 말한다.
`config/`·`static/`·`locales/`(알파벳상 `build/` 뒤)는 갱신돼 **신구가 섞인 키메라
설치**가 된다. `python -m app --help` 는 정상 동작해 겉보기엔 멀쩡하다.

부작용으로, 저장소에서 `pip install`·빌드 명령을 돌릴 때마다 `build/lib/` 아래
파일들이 수정·생성되어 **작업 트리가 오염된다**(이 감사 중 실제로 발생·정리함).

`tests/test_packaging.py` 는 `pyproject.toml` 의 TOML 문자열만 검사하므로 이
오염을 잡지 못한다. **조치**: `git rm -r build/` + `.gitignore` 에 `/build/` 추가.
실제 휠을 빌드해 소스와 대조하는 테스트 추가.

---

## HIGH

### V1. KEK 회전 크래시 시 `doctor` 가 유일한 작동 키를 지우라고 안내 — 전면 데이터 손실 [재현]

`app/cli.py:238` `wrapped = store.wrapped_deks() if vault.enabled else {}`. 회전이
`master.key`→`master.key.rotated-*` 이동(keyrotation.py:183)과
`master.key.new`→`master.key` 이동(:185) **사이**에서 죽으면:
- `master.key` 부재 → `vault.enabled=False` → `wrapped={}`
- DB 는 **새 키**(`master.key.new`)로만 열림

이 상태에서 `staged_opens = bool(wrapped) and ...`(cli.py:249)가 무조건 `False` 가
되어 doctor 는 **정반대** 가지를 출력한다. 재현:
```
! 중단된 KEK 회전의 잔여 파일이 있습니다: .../master.key.new
    DB 는 아직 **현재 키**로 감싸여 있습니다 — 회전은 반영되지 않았습니다.
      rm .../master.key.new    # 그 뒤 다시 `rotate-kek`
```
지시대로 `rm master.key.new` 를 실행하면 **DB 를 열 수 있는 유일한 키가 파기**되어
전 테넌트 원문이 영구 손실된다. `keyrotation.py` 독스트링·런북의 "어디서 죽어도
doctor 가 사람 말로 알려 준다" 보증이 이 창에서 정확히 반대로 작동한다.
**조치**: `master.key` 부재 시 `staged`/`retired` 두 파일로 각각 열어 보고 판정.

### R-HIGH. 라우팅이 신규 설치·데모에서 전건 무력 — 인증을 수행할 제품 경로가 없다 [재현]

라우터(`pipeline.py:883`)는 `evaluator.classifier_is_certified(model)` 을 요구하고,
`evals.py:671` 은 인증 이력이 없으면 무조건 거부한다. 그런데 `certify_classifier`
를 호출하는 곳은 **테스트뿐** — API·CLI·모델 등록·부트스트랩 어디에도 없다(전수
grep). 재현: 데모 프로파일 신규 설치에서 `analyze` 제출 → `route=None`(certified?
False), 항상 기본 모델.

fail-to-default 라 정확성·보안 피해는 없으나, `README.md:29`("잡 목록에
`← simple`/`← complex` 판정이 붙습니다")·`design-decisions.md`("Demo 에서 그대로
시연된다")·`architecture.md:290`("certify_classifier 가 등록 시점에 걸린다")가
**현 상태에서 거짓**이다 — 시연자는 그 자리에서 막힌다. 계획서 8종 테스트가 전부
인증을 명시 시드하거나 `_router` 를 주입해 이 빈칸이 테스트에 안 걸린다.
**조치**: 모델 등록/기동 시 `certify_classifier` 배선 또는 관리자 API/CLI 한 개 +
라우팅용 `classifier_ready` 가시화.

### G-HIGH. 한국어 조사가 붙은 PII 가 마스킹을 우회 — 이 제품의 차별화 자산이 실사용 표기에서 실패 [재현]

`config/guard.yaml` 의 RRN·전화·카드·사업자번호 규칙이 `\b`(단어 경계)를 쓰는데,
유니코드 모드에서 한글 음절도 단어 문자라 **숫자 뒤에 조사가 바로 붙는 가장
자연스러운 한국어 표기**에서 경계가 성립하지 않는다. 재현:
```
LEAK: '제 주민번호는 900101-1234568입니다.'   → 마스킹 없음
MASK: '제 주민번호는 900101-1234568 입니다.'  → [주민등록번호] (공백 있을 때만)
LEAK: '전화번호 010-1234-5678로 연락주세요'    → 마스킹 없음
LEAK: '카드번호 4111-1111-1111-1111입니다'     → 마스킹 없음
```
정답셋(`evals.py:191~`)이 전부 조사 앞에 부자연스러운 공백을 넣은 표본이라 이
미탐이 커버리지 장치에 안 걸린다 — **코퍼스 편향이 결함을 가린 사례.** 한국어 PII
차단은 설계 감사가 "글로벌 제품 공백지대의 차별화 자산" 으로 꼽은 바로 그것인데,
실사용 표기 다수에서 작동하지 않는다. **조치**: `\b` 대신 `(?<![\d-])…(?![\d-])`
룩어라운드, 정답셋에 조사 밀착 표본 추가.

### V2. 크래시 복구가 CAS 없이 동시 정산을 덮어써 이중 실행/이중 청구 [재현]

`app/store.py:1404·1411·1419` — `recover_running_jobs` 의 상태 쓰기 3종이 전부
`expect_status='running'` 없이 `WHERE id=?` 로만 갱신하고, 대상 행을 트랜잭션 밖
(:1392)에서 SELECT 한다. 다중 워커 구성(설계가 지원한다고 선언)에서 스케줄러가
복구를 도는 사이 API 워커가 잡을 `running→ok` 로 정산하면 그 잡이 `queued` 로
되돌아간다. 재현: 이미 `ok` 로 정산(usage 기록·과금 완료)된 잡이 `requeued:1` →
`status: queued` 로 되살아나 재실행. audit.md M11 의 CAS 가 막으려던 정확히 그
이중 실행이다(`_try_dispatch`·`cancel`·`review` 는 CAS 를 쓰는데 복구만 빠짐).
**조치**: 세 UPDATE 에 `AND status='running'`.

---

## MEDIUM

### 데이터/암호

- **V3. 성공 정산이 2개의 독립 커밋 — M12 가 성공 경로에서 미해결** [재현] —
  `scheduler.py:540` 이 `status='ok'` 를 커밋한 뒤 `:549` 에서 별도로 settle 을
  커밋한다. 그 사이 크래시 시 예약(`status IN queued,running` 조건)은 더는 안 세고
  usage 는 아직 안 쓰여 **예산 영구 과소 계상**. 재현: commit#1 직후
  `reserved_cost=0, spend_since=0`. **조치**: `status='ok'` 를 settle 트랜잭션 안으로.
- **V4. 감사 합치기(coalesce) UPDATE 가 다중 워커에서 해시 체인을 끊는다** [재현] —
  `store.py:2264` 이 팁/후보를 트랜잭션 밖에서 읽고 합치기 UPDATE 로 팁 해시를
  다시 계산한다. 유일 인덱스는 INSERT 포크만 막고 이 UPDATE 는 못 막는다. 재현:
  두 커넥션 경합 후 `verify → ok:False, 앞 고리가 끊겼습니다`. `architecture.md:575`
  의 "다중 프로세스 경합 검증됨" 이 이 경로는 안 덮는다. **조치**: 합치기 읽기를
  UPDATE 와 같은 트랜잭션에.
- **V5. 정상 운영(대시보드 폴링 + audit-export)이 doctor 의 체인 재계산 거짓
  경보를 낸다** [재현] — 합치기가 내보낸 팁 행 해시를 300초 창 안에 바꿔
  `audit_export_still_agrees()=False`. "정상을 사고로 신고하는 검증은 곧 꺼진다"
  는 런북 자신의 원칙에 걸린다. **조치**: 내보낼 팁으로 비-합치기 행 선택 또는
  런북 정상 목록에 추가.
- **V6. `route_counts` 가 /metrics 스크레이프마다 jobs 전면 스캔 + 임시 B-tree**
  [재현] — `store.py:1192` `GROUP BY role, route` 에 인덱스 없음(`EXPLAIN`:
  `SCAN jobs | USE TEMP B-TREE`). 라우팅 커밋이 들여온 새 미인덱스 핫쿼리. **조치**:
  `jobs(role, route)` 부분 인덱스 또는 별도 카운터.
- **V7. 회전 중 워커가 살아 있으면 양쪽 키로도 못 여는 혼합 상태** [재현] —
  `keyrotation.py:136` 이 DEK 집합을 스냅샷한 뒤 교체하는데, 그 사이 워커가
  테넌트/DEK 를 만들면 옛 키로 감싸인다. 재현: `t1 opens new, t2 opens old`,
  롤백도 거부. **조치**: 정지(quiesce) 상태에서만 회전 허용(런북에 워커 정지 단계).

### 가드

- **G-MED1. `sk-` 시크릿 규칙에 좌측 경계가 없어 평범한 하이픈 단어를 full 마스킹**
  [재현] — `config/guard.yaml:159`. 재현: `task-management-system-v2-backup` →
  `ta[시크릿] ...`, `kiosk-...`·`risk-...` 동일. `full` 등급이라 정상 프롬프트가
  훼손된 채 추론으로 간다. 커밋의 "34건 오탐 0" 은 코퍼스에 이 부류가 없어서 참으로
  보인 것. **조치**: `\bsk-`, `\b(?:AKIA|ASIA|…)`.
- **G-MED2. PEM 개인키가 머리말 한 줄만 마스킹되고 본문(실제 비밀)은 생존** [재현] —
  `guard.yaml:167`. 재현: `-----BEGIN RSA PRIVATE KEY-----` 만 `[개인키]` 로 가려지고
  base64 본문·END 줄 전량 생존("secretmaterial" 통과). 탐지됐다는 착시만 남는다.
  **조치**: BEGIN~END 블록 전체 패턴.
- **G-MED3. 동강도(partial↔partial) 겹침에서 시작이 빠른 규칙의 keep_tail 이
  병합 스팬 전체에 적용** [재현] — `guard.py:845`. `_coalesce` 가 강할 때만 교체하므로
  동강도는 시작 오프셋이 이긴다. 재현: keep_tail=2 규칙 값의 뒷자리가 keep_tail=8 로
  노출(상한 MAX_KEEP_TAIL=8). dd6b622 의 "더 강한 등급이 이긴다" 보증이 동강도엔
  성립 안 함. **조치**: 병합 시 `min(keep_tail)`.

### API

- **M18 잔여 — 다중 워커 동시 중복 생성은 여전히 500** [확정] — 409 는
  `get_service`/`get_tenant` 사전 조회로만 나오고 `create_tenant/service` 는
  `IntegrityError` 를 안 잡는다(`main.py:842`, `store.py:879`). 멱등성 경로가 DB
  유일성으로 처리하는 것과 비대칭.

---

## LOW / INFO

- **G-LOW1. 폭 0 정규식 매치 + NFKC 인덱스에서 `to_source` 랩어라운드로 프롬프트
  전체가 라벨 하나로 치환** [재현] — `guard.py:757`. `\d*` 류 흔한 실수 시 과잉
  마스킹(유출 아님). **조치**: 폭 0 매치 스킵 또는 `validate_rule` 에서 `match("")` 검사.
- **G-LOW2. GitHub fine-grained PAT(`github_pat_…`) 미커버** [재현] — `guard.yaml:159`
  classic 토큰만.
- **G-LOW3. 카나리아는 형식 포기형 인젝션만 잡고 echo 형(카나리아를 싣고 NONE
  답)은 통과** [재현] — 카나리아 값이 지시부에 노출되는 설계상 완전 방어 아님.
  울타리+정책이 완화하나 문서가 한계를 명시 안 함.
- **R-LOW1. 비ASCII 라우트 키는 영원히 선택 불가** [재현] — `pipeline.py:1128`
  `[A-Za-z0-9_]+` 만 매칭. 설정 검증은 한글 키를 통과시켜 비대칭. `NONE` 키는
  센티널과 충돌. **조치**: 키 문자셋 기동 검증.
- **R-LOW2. 라우트 스펙의 모르는 키(timeout·options·오타)가 조용히 무시** [재현] —
  5개 금지 키만 거부, 역할 유효 키는 라우트에서 묵살. 노드 등록 API 가 모르는
  키를 거부하는 것과 비대칭.
- **R-LOW3. `_handle_failure` 가 CAS 없이 실패 기록 — 성공 후반부 예외 시 ok→failed
  역전 + 건강한 노드 오귀책** [확정] — `scheduler.py:591`. 출력 가드/settle 예외 같은
  컨트롤 플레인 오류가 노드 헬스로 전가. 취소 경합에 CAS 를 넣은 철학과 어긋남.
- **R-LOW4. 에어갭 배치 불가가 WAIT 로 분류돼 900초 후 오해 코드** [확정] —
  `cluster.py:457`. `_airgap` 은 프로세스 수명 중 불변인데 M6 이 없애려던 그 코드
  잔존.
- **V8. 보존 정리가 감사 체인을 통째로 비우면 앵커/genesis 어긋나 검증 영구 실패**
  [재현] — `store.py:2810`. 트리거 좁음(365일 감사 0).
- **V9. `audit-export` 가 전부 체인 이전(NULL 해시) 행뿐인 DB 에서 TypeError** [재현] —
  `cli.py:470` `None[:16]`. 업그레이드 직후 첫 내보내기.
- **V10. 멱등성이 가드 차단 요청을 안 덮어 재시도가 2단 분류를 다시 돈다** [확정] —
  결과는 같으나 "재분류 회피" 의도가 차단 경로엔 미적용.
- **M21 잔여 — 청크 전송은 전량 읽은 뒤 검사** [확정] — `main.py:358`.
  `Content-Length` 없으면 `await request.body()` 로 전량 적재 후 검사. tls 프로파일
  nginx `client_max_body_size 4m` 있을 때만 완결.
- **알림 채널 도달 순서 비보장** [확정] — `notify.py:401` 4워커 풀. 상태 판정·중복
  억제 자체는 순서 보존.
- **route_failures 합산 왜곡** [확정] — `observability.py:270` 라우팅 켜기 전 과거
  잡 + 정당한 NONE 판정이 실패로 합산돼 켠 직후 실패율 부풀림.

---

## 검증 완료 — 수정이 주장대로 동작하는 항목

원 감사 항목 중 실행/추적으로 정상 확인한 주요 항목:

**구현 감사(audit.md) HIGH**: H1(겹침 마스킹 병합 — `[CARD]…-3456` 생존 완전
해소), H2(정규식 캐시 패턴 키 — 테넌트 간섭 없음·수정 즉시 반영·상한 유지),
H3(플랫폼 테넌트 목록 200), H4(오버라이드 `RoleResolver` 배선 — 실동작), H5(nodes
테이블), H6(단일 노드 재시도 — 백오프로 타이트 루프 없음), H7(KEK 후행 설정 시
DEK 백필 — `/v1/generate` 200 [재현]), H8(예약에 입력 토큰 반영), H9(restore WAL/SHM
제거·신버전 거부·소유권), H10(알림 to-thread-pool 오프로드), H11(doctor --bundle
실패 포획 — 가짜 docker 로 실행 테스트), H12(키 디렉터리 권한 사전 검사),
H13(번들 테넌트 ID redaction — affinity 픽스처 3축 테스트).

**MEDIUM**: M16(숫자 입력 400 — 전 엔드포인트 [재현]), M17(승격 게이트 PUT 강제
409), M19(`_platform` 파기 차단 400), M20(status 내부 역할 미노출 — 집계 수치만
[재현]), M22(기동 가짜 급증 알림 제거), M23(로그인 i18n 폴백), M24(client
RateLimited), M25(테스트 알림 정직한 결과), M26(log_event 실사용 + 배경 루프 예외
로깅), M27(python3), M28(models 화면 missing 렌더), M21(본문 파싱 전 413 — Content-
Length 있을 때).

**설계 감사(design-audit.md) 채택 항목**: 출력 축(마스킹·봉인·보존·차단 등급
강등 — 저장·반환·export·backup·purge·알림 전 경로 [재현]), 유니코드 정규화 + 스팬
매핑(전각/ZWSP/NBSP 원문 오프셋 정확), 시크릿 팩 정상건(sk-ant·AKIA·ghp·PEM 헤더·
자기 토큰 탐지, UUID·git SHA 미탐), 멱등성 키(스코프·TTL·경합 DB 판정), KEK 회전
본질(암호문 불변·래핑만 교체·fsync 파일+디렉터리), 감사 체인 단일 프로세스 변조
탐지(수정/삭제/삽입/절단), 완료 신호(등록-읽기 순서로 깨움 유실 없음·누수 없음),
CAS 정상 경로(디스패치·취소·review). 체크섬 알고리즘(luhn·kr_rrn·kr_biz·jp_mynumber·
iban) 독립 구현 대조 일치.

**용량 실측**: `app/loadtest.py` 재실행 결과가 `capacity.md` 수치와 동일 자릿수로
재현 — 복사가 아닌 진짜 측정. 오프로드 무력(GIL) 판정을 자기 실측으로 기각한 점이
정직.

**문서 정직성**: `design-decisions.md` 10개 항목 코드 대조 — 과장 없음(오히려
한계를 판정에 명시). `test_decisions.py`·`test_blast_radius.py`·`test_coverage_map.py`
는 실질 드리프트 장치. **단 이 정직성은 P-1 때문에 editable 설치에만 유효** —
`pip install .` 설치본은 문서가 서술한 코드를 실제로 담지 않는다.

---

## 조치 우선순위

| 순서 | 항목 | 근거 |
|---|---|---|
| 1 | **P-1** `git rm -r build/` + `.gitignore` + 휠 대조 테스트 | 출하 차단 — 설치본이 출력 가드 없는 앱 |
| 2 | **V1** doctor 회전 오진 (master.key 부재 시 두 파일로 판정) | 데이터 손실 유발 안내 |
| 3 | **V2** recover_running_jobs 3종 CAS + **V3** 성공 정산 원자화 | 이중 실행·예산 손상 |
| 4 | **G-HIGH** 한국어 조사 PII 룩어라운드 + 정답셋 표본 | 차별화 자산의 실사용 실패 |
| 5 | **R-HIGH** 라우팅 인증 배선 또는 문서 정정 | 데모/신규 설치 전건 무력 |
| 6 | **G-MED1·2** 시크릿 좌측 경계·PEM 블록 + **V4** 감사 합치기 트랜잭션 | 훼손·미탐·무결성 |
| 7 | MEDIUM·LOW 잔여 | 정합성·성능·문서 |

**메타 관찰**: 이번 구현은 원 감사의 지적을 성실히 반영했고 테스트·문서 규율도
높다. 그럼에도 남은 결함이 **전부 "테스트는 초록인데 현실 표본/실패 경로/동시성
창에서 살아 있는" 유형**이라는 점이 반복된다 — 이는 원 감사 §"왜 753개 테스트가
놓쳤나" 의 네 패턴(성공 환경 전제 · 저장만 검증 · 픽스처가 넉넉 · 성공 경로만 호출)
이 1272개 테스트에서도 동일하게 재현됨을 뜻한다. 커버리지 숫자가 아니라 **표본의
현실성과 실패·동시성 경로의 명시적 재현**이 다음 테스트 투자처다.
