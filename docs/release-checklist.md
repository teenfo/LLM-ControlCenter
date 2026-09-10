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
  가르는 유일한 값이다. 화면 자산(`app.js`·`style.css`)의 캐시 키는 버전에 **내용 해시**를 붙여
  만들므로 같은 버전을 다시 올려도 브라우저는 새 자산을 받지만, 그것은 사고 대비이지 절차가 아니다.
  설치처의 `.env` 가 `LCC_VERSION` 을 박아 두었으면 그 값도 함께 올린다(compose 의 이미지 태그가 그것을 따른다).
  0.2.0 은 2026-09-10 에 그렇게 올렸다 — 0.1.0 을 하루에 세 번 재배포하면서 브라우저 캐시를 의심하게 된 뒤다
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
- [ ] **감사 내보내기** — `python -m app audit-export --out <다른 저장소>/audit.jsonl` 을 정기 실행.
      체인은 조작을 드러낼 뿐 막지 못하고, 재계산은 밖의 사본과 대조할 때만 걸린다
      ([runbook-audit-integrity.md](runbook-audit-integrity.md))
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

---

## 6. 알고 넘어가는 것

첫 배포에서 특히 설명이 필요한 것만 — 전체는 [README 부채 표](../README.md#열어두는-부채).

- **관리 신원은 로컬 계정뿐이다.** 관제 UI 는 `admin` 계정으로 들어가지만 IdP·MFA 가 없다. 관리자 퇴사는 계정 정지(`account disable`)와 토큰 폐기로 사람이 처리한다
- **컨트롤 플레인은 1대다**(SPOF). 의도한 선택이고 Scale 프로파일에서 푼다
- **토큰 처리율은 보여 주기만 하고 한도로 걸지 않는다.** 설치처 분포를 본 뒤 건다
- **플러그인은 플랫폼 테넌트 전용이고 토큰 회전 경로가 없다** ([plugin-exploration §11](plugin-exploration.md))
- **가드 2단·라우팅 분류기는 인증(certify)이 끝나야 판정한다.** 기동 직후 `doctor` 가
  `model_not_certified` 를 경고하는 것은 정상이고, 스케줄러가 자동 인증한다
- **첫 설치처(2026-09-10)는 2코어·4GB 노트북이고 Wi-Fi 로 붙어 있다.** 컨트롤 플레인은
  31MB 로 돌지만, 유선이 가능해지면 옮기는 편이 낫다 — 게이트웨이의 가용성이 무선 품질에 매인다
