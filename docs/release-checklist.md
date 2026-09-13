# 1차 배포 체크리스트

이 문서는 **순서표**다. 설명은 [deployment.md](deployment.md) · [README](../README.md) ·
런북에 있고, 여기서는 그것을 언제 어떤 순서로 하는지만 적는다. 같은 내용을 두 곳에 적으면
둘은 어긋나므로, 문장이 길어질 것 같으면 여기가 아니라 저쪽에 쓰고 여기서는 가리킨다.

판은 `pyproject.toml` 의 `version` 이다. 다섯 곳(pyproject · `app.__version__` ·
`main.VERSION` · `compose.yml` 기본 태그 · `bundle.sh` 기본값)이 같은 값인지는
`test_every_version_string_agrees` 가 본다 — 한 곳만 올리면 이미지 태그와 `/healthz` 가
서로 다른 판을 말한다.

---

## 0. 이번 판의 범위 — 먼저 읽을 것

| 무엇 | 어디 |
|---|---|
| 되는 것 (기능마다 계약·고정 테스트·상태) | [feature-spec.md](feature-spec.md) |
| 하기로 했지만 아직 없는 것 | feature-spec §13 · [README 부채 표](../README.md#열어두는-부채) 첫 절 |
| 하지 않기로 한 것과 그 근거 | [README 부채 표](../README.md#열어두는-부채) 둘째 절 · [design-decisions.md](design-decisions.md) |
| 배포 프로파일 (Starter · Standard) | [deployment.md §2](deployment.md) — Scale(Postgres·Helm)은 이 판에 없다 |
| 서버 대수 · 사양 | [README](../README.md#서버는-몇-대인가) · [capacity.md](capacity.md) |

`--demo` 는 배포가 아니다 — 시드 테넌트 둘과 목 노드로 뜨는 시연 프로파일이다. 실사용
설치는 `bootstrap` 이 만드는 첫 테넌트에서 시작한다.

---

## 1. 출하 전 — 만드는 쪽에서

- [ ] `.venv/bin/pytest -q` 전부 통과 · `.venv/bin/python -m pyflakes app/ tests/` 클린
- [x] **도커 데몬이 있는 곳에서** `docker build` 한 번. 0.1.0 은 2026-09-10 hosub 에서 빌드해
      (이미지 228MB) 컨테이너가 uid 10001 로 뜨고 `/healthz` 가 응답하는 것까지 봤다. 이 판의
      첫 리허설은 데몬이 없는 환경이라 의존성 레이어가 깨진 것을 시뮬레이션으로 먼저 잡았다(§5)
- [ ] **버전을 올렸다.** 같은 버전으로 두 번 배포하지 않는다 — 이미지 태그·번들 이름·`/healthz` 가 판을
  가르는 유일한 값이다. 화면 자산(`app.js`·`style.css`·`client/*`)의 캐시 키는 버전에 **내용 해시**를 붙여
  만들므로 같은 버전을 다시 올려도 브라우저는 새 자산을 받지만, 그것은 사고 대비이지 절차가 아니다.
  설치처의 `.env` 가 `LCC_VERSION` 을 박아 두었으면 그 값도 함께 올린다(compose 의 이미지 태그가 그것을 따른다).
  0.2.0 은 2026-09-10 에 그렇게 올렸다 — 0.1.0 을 하루에 세 번 재배포하면서 브라우저 캐시를 의심하게 된 뒤다
  0.3.0 은 같은 날 관제 UI 를 디자인 핸드오프 구조로 갈아 끼운 판이다 — 자산 세 파일이 통째로 바뀌었으니
  마이너를 올렸고, 캐시 키(`버전-내용해시`)도 전부 바뀐다
  0.4.0 은 클라이언트 페이지(`/client/`) · `GET /v1/jobs` · 계정 역할 `user` 를 더한 판이다 — 새 표면·새 API·새 계정
  역할이라 마이너다. 캐시 키 하나를 관제 UI 와 클라이언트 페이지가 나눠 쓰므로 어느 쪽 자산이 바뀌어도 함께 바뀐다
  0.5.0 은 대화 API(`POST /v1/chat` · 역할 `kind: chat`)와 서비스 정책 수정(`PUT /v1/admin/services/{id}`)을 더한 판이다 —
  새 소비자 라우트·새 역할 종류·새 관리 라우트라 마이너다. **사이트 `roles.yaml` 의 `chat` 이 `kind: chat` 으로 바뀌어야
  대화 탭이 살고, 0.4.0 으로 되돌릴 때는 그 파일도 함께 되돌린다**(0.4.0 은 `kind: chat` 을 모른다)
  0.6.0 은 플러그인 개발 키트를 더한 판이다 — 플랫폼 라우트 둘(`POST /v1/platform/plugins/inspect` · `…/{id}/rotate-token`) ·
  `POST /v1/plugin/events` 의 `limit: 0`(ack 만) · 번들 클라이언트 두 파일(`plugin.py` 런타임 · `lccp.py` 패키징) · 데모
  `--plugin-dev` 라 마이너다. **스키마 변경이 없다** — `.env` 한 줄로 0.5.0 되돌리기가 그대로 성립한다. 신뢰 키(`keys/plugin-trust/*.pub`)는
  키 볼륨에 두므로 되돌려도 남는다
  **마이너를 올리면 플러그인의 `requires_host` 범위를 본다** — `>=0.1,<0.2` 로 선언한 플러그인은 0.2.0 호스트에
  설치되지 않는다(그것이 그 필드의 뜻이다). 0.2.0 을 올릴 때 테스트 번들이 정확히 그렇게 거절됐다
- [ ] `./bundle.sh` — 같은 이유로 도커 데몬이 있는 곳에서. 없으면 `image.tar` 없이
      소스만 담기고, 설치처는 `docker compose up -d --build` 로 직접 빌드해야 한다
- [ ] 번들을 빈 디렉터리에 풀어 `./preflight.sh` 가 도는지 (스크립트 실행 권한이 tar 를 지났는지)
- [ ] 태그 — `v<version>`. 태그는 되돌리기 어려우니 사람이 붙인다

---

## 2. 설치 당일 — 설치처에서

순서가 계약이다. 특히 **5번의 값은 그 자리에서 딱 한 번 보인다.**

1. **호스트 준비** — [deployment §2.2 체크리스트](deployment.md): 절전·뚜껑 억제, 부팅 시
   자동 기동, LUKS. 데모 노트북이라도 5번(디스크 암호화)은 넘어가지 않는다
2. **번들 풀기 + 점검**
   ```sh
   tar xzf llm-controlcenter-airgap-<ver>-<날짜>.tgz && cd llm-controlcenter
   ./preflight.sh        # 고장 0건이어야 한다. 경고는 읽고 넘어간다
   ```
   `./keys` 소유권 경고가 나오면 preflight 가 알려 주는 `chown` 을 **먼저** 한다 — 안 하면
   컨테이너가 마스터 KEK 를 못 써서 `restart: unless-stopped` 아래에서 조용히 크래시 루프가 된다
3. **에어갭이면** `docker load -i image.tar` (+ `image-nginx.tar`) 뒤 `LCC_AIRGAP=1`
4. **기동** — 신뢰 네트워크 안이 아니면 반드시 tls 프로파일이다. 없으면 플랫폼 관리 면이 그대로 열린다
   ```sh
   docker compose --profile tls up -d      # 인증서는 ./tls/ 에 미리
   ```
5. **최초 기동 값 회수** — `docker compose logs controlcenter`
   - 마스터 KEK → **백업과 다른 곳**에. 잃으면 기존 암호문은 영구히 못 연다
   - 관제 UI 계정 `admin` 의 비밀번호 — 관제 UI 로그인은 이것으로 한다. 첫 로그인 뒤 **내 계정** 탭에서 바꾼다
   - 플랫폼 관리자 토큰 · 첫 테넌트의 관리자 토큰 · 서비스 토큰 — API·자동화용. 화면 로그인에는 쓰지 않는다
   - 유예 모드 안내가 찍힌다 — 차단 규칙이 마스킹으로 낮춰져 도는 상태다(§3)
6. **노드 등록** — 관제 UI 에서 URL · `data_boundary` · 용량 ([deployment §3.2](deployment.md)).
   등록 즉시 프로브가 돌고, 실패하면 그 화면에서 안다. `internal` 노드는 사설망 전제,
   `external` 은 TLS·인증 필수 ([deployment §7](deployment.md))
7. **첫 소비자 연결** — `GET /v1/integration` 이 그 토큰 기준의 통합 가이드를 준다.
   노드도 토큰도 없이 먼저 붙여 보려면 `/v1/client/` 의 단일 파일 클라이언트와 목 서버
8. **진단** — `./doctor.sh --probe`. 갓 설치한 시스템에서 "확인이 필요한 항목" 둘은 정상이다:
   유예 모드가 켜져 있다는 것과 감사를 아직 내보낸 적이 없다는 것

---

## 3. 첫 주

- [ ] **유예 모드 해제** — 관제 UI 가드 탭에서 며칠 오탐률을 본 뒤. 유예 중에는 화면·API·`doctor`
      가 계속 알린다 ([README](../README.md#도입-첫날에-막히지-않습니다))
- [ ] **백업 주기** — `./backup.sh /mnt/<nas>/llmcc` 를 cron 에. 백업에는 원문 암호문도 KEK 도
      없다 ([deployment §8](deployment.md)). **복원 리허설을 한 번 한다** — 절차가 없으면 백업은 없는 것과 같다
      ```sh
      ./restore.sh /mnt/<nas>/llmcc/llmcc-backup-<stamp>.tgz    # 스키마 게이트 → "restore" 입력
      ```
- [x] **감사 내보내기** — `python -m app audit-export --out <다른 저장소>/audit.jsonl` 을 정기 실행.
      체인은 조작을 드러낼 뿐 막지 못하고, 재계산은 밖의 사본과 대조할 때만 걸린다
      ([runbook-audit-integrity.md](runbook-audit-integrity.md)). `.62` 는 2026-09-11 부터 hosub 의 systemd 타이머
      (04:40 KST · Persistent)가 컨테이너 안 증분 내보내기를 `/backup/llmcc-audit/`(날짜별 사본 + `latest` · 접두 검사 ·
      180일 보관)로 당겨 간다 — 첫 실행 440행, `doctor` 경고 0
- [ ] **알림 채널** — `LCC_NOTIFY_WEBHOOK` 또는 `LCC_SMTP_*` 를 넣고 관제 UI 에서 테스트 발송.
      없으면 "사람이 모르면 조용히 멈추는 지점" 이 전부 조용하다
- [ ] **메트릭** — `GET /metrics` 를 기존 Prometheus 에 물린다. 테넌트 이름은 라벨에 없다

---

## 4. 되돌리기

| 상황 | 방법 | 근거 |
|---|---|---|
| 새 판이 문제 | 이미지 태그를 이전 판으로. 스키마가 ADD COLUMN 전용이라 구버전이 신버전 DB 를 읽는다 | [deployment §5](deployment.md) |
| 데이터가 문제 | `./restore.sh <백업>` — 역할 오버라이드가 백업 시점으로 되돌아간다는 경고를 읽는다 | [deployment §8](deployment.md) |
| KEK 유출 | `python -m app rotate-kek` — 암호문 재암호화 없음 | [runbook-key-compromise.md](runbook-key-compromise.md) |
| 감사 체인 어긋남 | `doctor` 가 자리를 지목한다 | [runbook-audit-integrity.md](runbook-audit-integrity.md) |

---

## 5. 이 판(0.1.0)에서 실제로 돌려 본 것 — 2026-09-09 리허설

측정 > 추정. 아래는 테스트가 아니라 **번들을 풀고 스크립트를 순서대로 돌린** 기록이다.
도커 데몬이 없는 환경이라 컨테이너 경로는 못 돌렸고, 그 자리는 시뮬레이션으로 대신했다.

| 항목 | 결과 |
|---|---|
| `./bundle.sh` → 빈 디렉터리에 풀기 → `python -m app bootstrap` | 통과. `data/`·`keys/` 가 번들 루트에 생기고 `master.key` 는 600 |
| `./backup.sh` → DB 삭제 → `./restore.sh` → `doctor` | 통과. 백업에 암호문 0건·KEK 없음. 복원이 설정 7개를 되돌리고 `.before-restore` 사본을 남김 |
| 살아 있는 서버(`serve --demo`)에 앞문으로: `/healthz` · UI · `/v1/meta` · `/v1/generate`(목 노드) · 플러그인 설치→켜기→이벤트 풀→끄기 | 통과. 끈 플러그인은 401, 켠 직후 밀림 0, 종결 1건이 도착 |
| `doctor --bundle` | 토큰 유출 없음 |
| 스케줄 트리거 판에서 만든 DB 를 현재 코드로 열기 | 마이그레이션 자동. 이벤트 표·트리거가 생기고 옛 플러그인 행이 그대로 읽힘 |
| **Dockerfile 의존성 레이어** (pyproject 만 있는 상태에서 `pip install .`) | **깨져 있었다** — `package directory 'app/providers' does not exist`. 휠 검증은 전체 트리에서 빌드해 이 실패를 못 봤다. pyproject 에서 목록만 읽어 설치하도록 고쳤고 `test_the_dockerfile_dependency_layer_does_not_build_the_package` 가 지킨다 |
| 유예 모드 배너 | **거짓말을 하고 있었다** — "audit 로 낮춰집니다" 라고 찍혔는데 코드는 마스킹이다. 고쳤고 배너 검사에 못박았다 |
| 번들 이름 | README·deployment 가 `llm-controlcenter-<ver>.tgz` 라고 적었지만 `bundle.sh` 는 `llm-controlcenter-airgap-<ver>-<날짜>.tgz` 를 만든다. 문서를 고쳤다 |
| 빌드 컨텍스트 | `.dockerignore` 가 없어 `.venv`·`keys`·`data` 가 데몬에 올라가는 구조였다. 추가했다 |
| `./preflight.sh` | 이 환경에서는 "도커 데몬에 연결할 수 없습니다" 로 실패하는 것이 **맞다** |
| **hosub 에서 실제 빌드 → `.62` 에 설치** (2026-09-10) | 번들(73MB, 이미지 포함) 전송 → `docker load` → `preflight` 통과 → `compose up` → 4초 만에 `/healthz` → `doctor` 통과. 유예 모드 배너가 고친 문구로 찍혔다 |
| **계정 로그인 판으로 `.62` 업그레이드** (2026-09-10, 같은 날 두 번째 판) | 새 번들 sha 검증 → `backup.sh` → 옛 이미지를 `0.1.0-pre-accounts` 로 태그(되돌리기용) → `docker load` → 파일 교체(`.env`·`keys/`·데이터 볼륨은 그대로) → `compose up --force-recreate` → 4초 만에 `/healthz` → `doctor` 통과. **이미 부트스트랩된 설치라 admin 계정이 안 생긴다** — `account create admin --role platform_admin --generate` 로 만들었고 API 로그인·로그아웃이 200. 첫 설치의 관리자 토큰 둘은 회전으로 폐기해(옛 값 401) 회전 경로도 실물에서 확인했다 |
| **공개 진입점** (2026-09-10) | 번들 nginx 대신 같은 망의 hosub 에 이미 있던 Caddy 에 사이트 하나를 얹었다 — DuckDNS 가 `*.hosub.duckdns.org` 를 같은 IP 로 돌려줘 새 도메인·토큰·포트포워딩이 없었고, 인증서는 Caddy 가 HTTP-01 로 30초 만에 받았다. `/v1/platform/*` 404 와 본문 4MB 규칙을 [deployment §3](deployment.md) 의 Caddy 블록대로 옮겼고 `/healthz` 200 · 플랫폼 면 404 · 관리 면 401 을 확인했다 |
| **공개 첫 접속이 빈 화면** (2026-09-10) | 사용자가 `/ui` 를 쳤고 아무것도 안 나왔다. index.html 의 자산 참조가 상대 경로라 슬래시 없이 서빙되면 `/style.css` 가 404 다 — 스크립트가 안 돌아 로그인 폼이 숨겨진 채로 남는다. 프록시에 `redir /ui /ui/ 308` 을 먼저 넣어 그 자리에서 풀고, 앱에도 같은 리다이렉트를 넣어(`6b0843f`, `test_the_index_without_a_trailing_slash_redirects_to_one`) 재배포했다. 리허설은 늘 `/ui/` 로만 쳤기 때문에 못 봤다 |
| **공개 주소에서 관제가 비어 보임 · 비밀번호 폼이 지워짐** (2026-09-10) | 둘 다 첫 실사용이 드러냈다. ① 공개 진입점의 `/v1/platform/*` 404 를 화면이 "표시할 항목이 없음" 으로 그렸다 — 세션을 열 때 HEAD 로 한 번 묻고 막혔으면 배너와 탭 본문이 이유를 말하게 했다(`test_the_screen_explains_a_blocked_platform_surface`). 이 설치는 **프로토타입 단계라 관제 전체를 밖에서 써야 한다는 운영자 결정으로 차단을 풀었다** — 남는 통제는 계정 잠금 · TLS · 앱 역할 검사. ② 15초 자동 갱신이 입력 중인 폼을 다시 그려 치던 글자를 지웠다 — 폼을 만지는 중이면 조용한 갱신은 기다린다(`test_auto_refresh_does_not_paint_over_a_form_being_edited`) |
| **0.2.0 으로 올림** (2026-09-10) | 0.1.0 을 하루에 세 번 재배포하고서야 버전 규율이 없다는 것을 알았다. 버전을 올리고 자산 캐시 키를 `버전-내용해시` 로 바꿨다(`test_asset_cache_keys_follow_the_content` · `test_versioned_assets_are_immutable_and_the_index_is_not`). `.62` 는 `.env` 의 `LCC_VERSION` 을 함께 올려 `docker compose up --force-recreate` 로 새 태그의 이미지를 받았고, `/healthz` 0.2.0 · `app.js?v=0.2.0-2b5fc2a9` · `/ui/` no-cache · 키 있는 자산 immutable 을 공개 주소에서 확인했다. 0.1.0 이미지는 태그 그대로 남아 되돌리기는 `.env` 한 줄이다 |
| **0.3.0 — 관제 UI 디자인 개편** (2026-09-10, 같은 날 네 번째 판) | 디자인 핸드오프의 구조(아이콘 레일 · 헤더 상태 필 · 카드 · 드로어 · 토스트 · 라이트/다크)를 바닐라 JS/CSS 로 옮기고 마이너를 올렸다. 로컬 데모에 대한 헤드리스 브라우저 스모크(34장면: 로그인 실패·성공 · 레일 아홉 페이지 · 테마 전환 · 드로어 열고 닫기 · 작업 상세와 원문 열람 · 입력 중 자동 갱신 가드 · 폰 너비 가로 스크롤 없음 · 테넌트 관리자·서비스 토큰 시야)가 콘솔 오류 0 · 4xx/5xx 0 으로 통과한 뒤 배포했다. hosub 빌드 → `.62` 에 `upgrade-62-0.3.0.sh`(sha 검증 → `backup.sh` → `docker load` → 파일 교체 → `.env` `LCC_VERSION` 0.3.0 → `compose up --force-recreate`) — healthz 4초 · doctor 통과. 공개 주소에서 `/healthz` 0.3.0 · `/ui` 308 · `/ui/` no-cache · `app.js?v=0.3.0-1ffc10f5` immutable · 키 없는 자산 no-cache · `/v1/platform/overview` 401 을 확인했다. 스모크가 잡은 것 둘: 새 로케일 키가 원문 그대로 보였다(서버가 기동 시점 카탈로그를 들고 있어서 — 재기동으로 해소, 배포에서는 이미지가 새로 뜨므로 해당 없음) · 헤더 부제가 한 줄에서 잘렸다(두 줄 클램프로). 0.2.0 이미지는 태그 그대로 남아 되돌리기는 `.env` 한 줄이다 |
| **0.3.1 — 실제 노드 전환 · 노드 삭제** (2026-09-10, 같은 날 다섯 번째 판) | `.62` 를 데모 시드에서 실제 기계로 옮겼다. ① 유예 모드 해제(API, `grace_mode: false`) ② macstudio(Ollama · LAN 192.168.0.31 · internal · 40GB · 동시 2) 를 API 로 등록 — 등록 즉시 프로브가 모델 5개를 읽었다 ③ 사이트 설정(`config/roles.yaml` 을 qwen2.5 7b/14b/32b · bge-m3 로, `catalog.yaml` 을 실제 모델로, `nodes.yaml` 시드는 비움)을 앱의 `load_config` 로 로컬 검증한 뒤 올리고 재기동 ④ 노드를 지울 API 가 없어서 만들었다 — `DELETE /v1/platform/nodes/{node}` 와 묘비(`node_tombstones`), 시드 노드는 기동 때마다 들어오므로 DB 행 삭제만으로는 안 지워진다는 것을 코드에서 확인하고 설계했다 ⑤ 업그레이드 스크립트가 이 판부터 `config/` 를 보존한다(배포본 기본값은 `config.dist-0.3.1/` 에 옆에 풀고 `diff -rq`) — 그 전 세 판은 `config/` 를 통째로 갈아 끼웠다. 확인: 노드 1대 healthy · 승인 대기 0(옛 역할이 만든 demo-* 자동 설치 요청 2건은 Ollama 레지스트리에 없어 실패했고 guard-classifier 1건은 대기 — 셋 다 사유를 적어 거부) · 실제 잡 `summarize` 가 macstudio/qwen2.5:7b 에서 `ok`(입력 111 · 출력 43 토큰) · 공개 주소 `/healthz` 0.3.1 · 새 라우트 미인증 401 · doctor 남은 항목은 감사 내보내기 하나. 단일 호밍 경고 5건은 노드가 하나뿐이라 당연하다 — 두 번째 노드가 생기면 사라진다. Tailscale 을 `.62` 에 설치했고 로그인 URL 승인은 사람 몫이다 |
| **0.4.0 — 클라이언트 페이지 · 사용자 계정 · 내 작업 목록** (2026-09-11) | 사람이 브라우저에서 LLM 을 쓰는 화면이 생겼다. `/client/` 를 같은 앱이 서빙하고(관제 UI 와 같은 규칙 · 자산 캐시 키 공유), 계정 역할 `user`(테넌트·서비스 귀속, 세션은 `service` 역할)와 `GET /v1/jobs`(범위는 테넌트·서비스·엔드유저), 테넌트 관리자의 `/v1/admin/accounts*` 가 함께 들어갔다. 검증: pytest 1746 통과 · 브라우저 스모크 콘솔 34장면 + 사용자 계정 탭 2장면 + 클라이언트 11장면(콘솔 오류 0 · 4xx/5xx 0) · 휠이 `static/client/` 를 담는지 테스트로 못박음(글롭을 빼면 실패). 배포: hosub 빌드 → `.62` 에 `upgrade-62-0.4.0.sh`(`config/` 보존 · 사이트 `roles.yaml` 에 `chat`(qwen2.5:14b · 300초)을 없을 때만 덧붙이고 **재기동 전에 새 이미지로 `load_config` 검증**) — healthz 4초 · doctor 통과. 스크립트가 잡은 것: 이미지 ENTRYPOINT 가 앱 CLI 라 `docker run … python` 이 `invalid choice: python` 으로 죽었다 → `--entrypoint python`; 검증 실패 분기가 설계대로 `roles.yaml` 을 되돌리고 멈췄고 컨테이너는 0.3.1 그대로였다(두 번째 실행에서 통과). 실노드 E2E(클라이언트 페이지가 부르는 API 순서 그대로): 테넌트 관리자가 사용자 `hosub` 생성(비밀번호는 `.62` 의 `client-user-credentials.txt` 600 에만) → 로그인(`service` 세션 · `account_role: user`) → `summarize` qwen2.5:7b `ok` 2.7초 · 가드 `email: audit`(이 사이트는 베이스라인이 audit 등급이라 기록만 하고 가리지 않는다) → `chat` 2턴 qwen2.5:14b `ok` 1.9초 · 6.0초, 두 번째 턴의 저장 프롬프트에 첫 턴과 표식이 그대로 → `GET /v1/jobs` scope=account · 관리자 필드 없음 · `GET /v1/jobs/{id}` 가 저장된 가드 판정을 돌려줌 → 토큰 모드 이름 없이 400 `end_user_required` · user 세션의 관리 면 403. 공개 주소: `/healthz` 0.4.0 · `/client` 308 · `/client/` no-cache · `client.js?v=0.4.0-899d2542` immutable · `/v1/jobs` 미인증 401. 실측이 바꾼 것 하나: 첫 대화 턴에서 qwen2.5:14b 가 한국어 질문에 **중국어로** 답했다 → `chat` 지시문에 "항상 한국어로만 답한다" 를 더했다(저장소 기본값·사이트 둘 다 · 재시도에서 한국어). 0.3.1 이미지는 그대로 남아 되돌리기는 `.env` 한 줄이고 `chat` 역할은 0.3.1 도 읽는다 |
| **0.5.0 — 대화 API · 서비스 정책 수정 · 운영 3건** (2026-09-11) | `POST /v1/chat`(역할 `kind: chat` · 턴별 독립 마스킹 · 2단 분류는 제출당 1회 · 저장은 마스킹 JSON 트랜스크립트)과 `PUT /v1/admin/services/{id}`(콘솔 편집 드로어 · 역할 카탈로그)가 들어간 판. 검증: pytest 1811 통과 · 브라우저 스모크 — 클라이언트 11장면(`/v1/chat` 2턴 → 저장 트랜스크립트가 `kind=chat` 의 JSON 3턴 · 기록 상세가 턴 말풍선) · 콘솔 34+2 장면 회귀 · 서비스 정책 4장면(편집 드로어에서 classify 제외 → 저장 → 그 역할 403 `forbidden_role` · summarize 통과 → `*` 복원 → 서비스 추가) · 콘솔 대화 잡 표·드로어 2장면 — 콘솔 오류 0 · 4xx/5xx 0. 배포: hosub 빌드(73MB) → `.62` 에 `upgrade-62-0.5.0.sh` — `config/` 보존, 사이트 `roles.yaml` 의 `chat:` 블록을 `kind: chat` 블록으로 갈아 끼우고(`roles.yaml.bak-0.4.0`) **재기동 전에 새 이미지로 `load_config` 검증**(kind 단언) → healthz 4초 · doctor 통과. 실노드 E2E: `hosub` 로그인 → summarize qwen2.5:7b `ok` 149.9초(재기동 직후 노드가 헬스 2회 성공을 얻을 때까지 잡이 대기했다 — 맥스튜디오가 잠들어 있어 첫 성공이 +200초였고, 실패한 프로브는 로그에 남지 않는다) → `/v1/chat` 2턴 qwen2.5:14b `ok` 9.5초 · 22.6초, 둘 다 한국어 → `/v1/generate` 에 chat 역할은 400 `wrong_kind` · `messages` 안의 system 턴은 400 → `GET /v1/jobs` 행이 `kind=chat` · 마스킹 JSON 3턴 → `PUT` 으로 classify 제외 → 같은 세션의 classify 가 403 `forbidden_role` → `*` 복원 → 한도 0 은 400 · user 세션은 403 → 감사 `update_service` 2건. 공개 주소: `/healthz` 0.5.0 · `client.js?v=0.5.0-67872459` · `POST /v1/chat` 미인증 401. 운영 3건: ① 감사 사본 — hosub 의 `llmcc-audit-export.timer`(04:40 KST · Persistent · 접두 검사 · 180일)가 `/backup/llmcc-audit/` 로 당긴다, 첫 실행 440행 · `doctor` 경고 0(잡은 것: systemd 유닛의 줄 끝 주석이 `Persistent=true` 를 조용히 무시하게 했다 → 주석을 윗줄로 · 같은 결함이 hosub-backup.timer 에도 있다) ② hosub Caddy 배포 사본 둘(`/opt/hosub-trading`·`/opt/hosub-mcp` 의 `deploy/Caddyfile`)에 `import /etc/caddy/conf.d/*.caddy` 두 줄 — `caddy validate` 통과, 커밋은 그 저장소 몫(jw-mcp 블록은 여전히 라이브에만 있다) ③ `.62` 테일넷 호스트명 `llmcc`(`llmcc.tail95a635.ts.net` · 100.76.50.93). 되돌리기는 `.env` 한 줄 + `roles.yaml.bak-0.4.0` — 0.4.0 이미지는 `kind: chat` 을 모른다 |
| **0.6.0 — 플러그인 개발 키트 · 토큰 회전 · 사전 검사** (2026-09-12) | 만드는 쪽의 도구가 생긴 판이다 — 런타임 SDK `clients/plugin.py`(처리 뒤 ack · `limit: 0` · 간격 · 401 대기) · 패키징 CLI `clients/lccp.py`(호스트 규칙을 AST 동일성으로 묶음 · 재현 가능 서명 번들) · 목 서버 트리거 플래그 · `serve --demo --plugin-dev` · 예제 둘 + systemd 템플릿, 호스트에는 `POST /v1/platform/plugins/inspect` · `…/{id}/rotate-token` · 이벤트 풀의 `limit: 0`. 검증: pytest 1926 통과 · 데모 스모크(개발 모드로 설치된 예제 둘 · 토큰 회전 드로어, 콘솔 오류 0). 배포: hosub 빌드(74MB, 스키마 변경 없음) → `.62` `upgrade-62-0.6.0.sh` — `keys/plugin-trust/` 를 만들고 hosub 의 `.pub` 을 넣음 → healthz 4초 · doctor 통과 · `trusted_keys 1` · 네 파일이 `/v1/client/` 에서 나감(인증 필요). **예제 플러그인 실가동**(hosub 에서 공개 주소로): `lccp keygen`(hosub, 비밀 키는 hosub 에만) → `build --sign` 둘(finish-log · daily-status `*/2` 변형) → `.62` 에서 inspect 둘 다 `signed` → 설치 → 켜기 → SDK 두 파일을 공개 주소에서 플러그인 자기 토큰으로 내려받아 저장소 원본과 바이트 일치 → `llmcc-plugin@finish-log`·`@daily-status`(DynamicUser · `EnvironmentFile` 600). ① 스케줄: 2분마다 tick 클레임(`late_by` 1~8초 · `last_run_at`·`jobs_created` 증가) ② 이벤트: 사용자 chat 1턴의 종결이 수 초 안에 finish-log 저널·JSONL 로(본문 키 없음 · `end_user` 해시) → `events_pending 0` ③ 재귀 방지: 플러그인이 만든 잡의 종결 8건(취소 7 · `ok` 1)이 커서만 밀고 finish-log 에 한 건도 안 옴 ④ 회전 훈련: `rotate-token` 유예 60초 → 만료 시각에 플러그인이 401 을 만나고 죽지 않고 대기(NRestarts 0) · 옛 토큰 401 / 새 토큰 200 → env 교체·재시작 → 다음 종결(event 20)이 새 토큰으로 옴. **정직하게 적을 것:** 실노드(`macstudio`, Ollama 192.168.0.31)가 창 대부분 `unhealthy`(ping 은 되고 11434 는 안 열림)라 사용자 chat 잡은 508초 뒤에도 `pending` 이었고, 이벤트 경로는 그 잡을 **취소**해(취소도 종결이다 — `status=cancelled`) 확인했다 · 노드가 잠깐 깨어 플러그인 잡 하나를 `ok` 로 끝냈고 그것은 재귀 방지대로 안 왔다 · `ok` 상태의 종결이 finish-log 에 오는 것은 이번 창에서 못 봤다(데모 호스트와 테스트에서는 봤다). 실가동이 드러낸 결함 셋을 고쳤다: 런타임이 클라이언트 타임아웃을 30초로 줄여 `generate(wait=30)` 이 서버보다 먼저 끊김(`TimeoutError`) → 클라이언트가 소켓 타임아웃을 `wait` 만큼 늘리고 런타임은 기본값(320초)을 따름 · tick 핸들러 실패를 "배치 미확정" 으로 말하던 로그 → "예정이 지나갔다" · daily-status 가 노드가 죽어 있으면 고아 잡을 남김 → 600초 뒤 취소하고 포기. 이 설치처의 Caddy 는 `/v1/platform/*` 를 **운영자 결정(2026-09-10, 파일에 기록)으로 열어 두었다** — topology §2 의 기본과 다르며 손대지 않았다. 끝에 둘 다 끄고(`activate false`) hosub 유닛을 정지했다 — 설치본·`/etc/llmcc-plugins/*.env`(600)·기록은 남겼다. 되돌리기는 `.env` 한 줄(`LCC_VERSION=0.5.0`) + `force-recreate`, 신뢰 키는 남겨도 무해 |

---

## 6. 알고 넘어가는 것

첫 배포에서 특히 설명이 필요한 것만 — 전체는 [README 부채 표](../README.md#열어두는-부채).

- **관리 신원은 로컬 계정뿐이다.** 관제 UI 는 `admin` 계정으로 들어가지만 IdP·MFA 가 없다. 관리자 퇴사는 계정 정지(`account disable`)와 토큰 폐기로 사람이 처리한다
- **컨트롤 플레인은 1대다**(SPOF). 의도한 선택이고 Scale 프로파일에서 푼다
- **토큰 처리율은 보여 주기만 하고 한도로 걸지 않는다.** 설치처 분포를 본 뒤 건다
- **플러그인은 플랫폼 테넌트 전용이다.** 토큰 회전·재발급은 0.6.0 부터 `rotate-token` 으로 한다 — 콘솔 「토큰 회전」은 유예 60분
  ([plugin-authoring §5](plugin-authoring.md), [plugin-exploration §11](plugin-exploration.md))
- **가드 2단·라우팅 분류기는 인증(certify)이 끝나야 판정한다.** 기동 직후 `doctor` 가
  `model_not_certified` 를 경고하는 것은 정상이고, 스케줄러가 자동 인증한다
- **첫 설치처(2026-09-10)는 2코어·4GB 노트북이고 Wi-Fi 로 붙어 있다.** 컨트롤 플레인은
  31MB 로 돌지만, 유선이 가능해지면 옮기는 편이 낫다 — 게이트웨이의 가용성이 무선 품질에 매인다
