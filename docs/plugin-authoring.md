# 플러그인 작성 가이드

플러그인을 **만드는 사람**을 위한 문서다. 계약이 왜 이렇게 생겼는지는 [plugin-exploration.md](plugin-exploration.md),
기능마다의 표면·구현·고정 테스트는 [feature-spec.md §12](feature-spec.md#12-플러그인) 에 있다. 여기는 "무엇을 어떻게 쓰면
되는가" 만 적는다.

## 0. 세 줄 모델

1. **플러그인은 앞문으로 지나는 소비자다.** LLM 을 쓸 때 `POST /v1/generate`·`/v1/chat` 을 자기 서비스 토큰으로 부른다.
   가드·경계·레이트리밋·예산·사용량·감사가 배선 없이 붙는다.
2. **컨트롤 플레인은 프로세스를 띄우지 않는다**(`kind = "external"`). 운영자가 systemd·컨테이너로 띄우고, 컨트롤 플레인은
   신원(서비스 + 토큰)·카탈로그·스위치·트리거만 갖는다. 언어는 자유다 — 이 문서의 SDK 는 Python 이지만 계약은 HTTP 다.
3. **깨어나는 길은 둘이고 둘 다 풀이다.** 시각이 원인이면 `schedule`(`POST /v1/plugin/tick` — "지금 내 차례인가"),
   잡 종결이 원인이면 `event`(`POST /v1/plugin/events` — "못 본 종결이 있나"). 컨트롤 플레인이 플러그인을 부르러 나가지 않는다.

## 1. 수명주기

```
lccp init → 코드 → 목 서버 / 데모 호스트에서 돌려 본다 → lccp build --sign → 운영자에게 .lccp + .pub
운영자:  사전 검사(inspect) → 설치(= 서비스 + 토큰 1회) → 켜기 → [tick / events] → 끄면 401 → 토큰 회전 → 제거
```

- **설치는 켜지 않는다.** 설치 응답에 토큰이 **한 번** 실린다. 켜기 전에는 그 토큰으로 무엇을 해도 401 이다.
- **켜고 끄는 것은 그 서비스의 `status` 다.** 별도 플래그가 없다. 끄면 `/v1/generate` 도 `/v1/plugin/*` 도 같은 지점에서 401 이다.
- **재설치·업그레이드는 토큰을 다시 주지 않는다** — 판올림마다 외부 프로그램의 설정을 갈지 않게 하려는 의도다. 토큰을 잃었거나
  바꿔야 하면 **회전**(§5)이 유일한 길이다.
- **제거는 서비스 행을 남긴다** — 사용량·감사가 이름을 잃지 않게.

## 2. 매니페스트 `plugin.toml`

```toml
[plugin]
id = "acme.daily-digest"         # 역DNS. 점이 하나는 있어야 한다 — 일반 서비스 id 와 섞이지 않게
name = "일일 요약"
version = "1.0.0"                # 숫자.숫자[.숫자[.숫자]][-태그]
requires_host = ">=0.6,<0.7"     # 호스트 판 범위. 비우면 통과. >= > <= < == 만, 쉼표로 여럿
description = "…"

[service]                        # create_service() 의 인자 그대로 — 관리자가 읽는 문장 = DB 값 = 강제되는 것
allow_roles = ["summarize"]      # 비어 있으면 안 된다. 밑줄 역할(_guard_classify…)은 내부 전용이라 못 쓴다
rate_limit_per_min = 10          # 양의 정수. 없으면 서비스 한도 없음(테넌트 한도는 그대로)
budget_usd_per_month = 5.0       # 0 이상. 0 이면 유료 호출이 전부 거절된다 — LLM 을 안 부르는 플러그인의 최소 권한

[run]
kind = "external"                # 지금 되는 것은 이것뿐
endpoint = "http://…"            # 선택. 정보용이다 — 컨트롤 플레인은 여기로 부르러 가지 않는다
# requires_runtime 은 "none" 이어야 한다 — 컨트롤 플레인이 띄우지 않으므로 런타임을 줄 수 없다

[trigger]                        # 절이 없으면 스스로 깨어나지 않는 플러그인이다 (토큰으로 API 를 부르는 프로그램)
kind = "schedule"                # 또는 "event"
schedule = "0 8 * * *"           # 분 시 일 월 요일. 숫자만 — 이름(MON·JAN)·@daily·L·W·# 는 없다
timezone = "Asia/Seoul"          # IANA. 모르는 이름은 거부(조용히 UTC 로 떨어뜨리지 않는다)
# kind = "event" 이면:
# event = "job.finished"         # 지금은 이것 하나
# roles = ["summarize"]          # 비우면 모든 역할
```

**거부는 설치 시점이고 문장으로 온다.** `0 0 30 2 *`(2월 30일)처럼 문법은 맞는데 영원히 안 도는 스케줄, 모르는 이벤트 이름,
설정에 없는 역할(`allow_roles` · `[trigger].roles`)은 설치가 거부한다 — 받아 두면 "켜져 있는데 아무 일도 안 하는" 플러그인이
되고 그 원인을 화면에서 못 읽는다. `lccp check` 가 역할 실재만 빼고 같은 판정을 오프라인에서 한다(§7).

## 3. 두 트리거의 성질

| | `schedule` | `event` |
|---|---|---|
| 묻는 곳 | `POST /v1/plugin/tick` `{}` | `POST /v1/plugin/events` `{"ack": n, "limit": k}` |
| 답 | `{due, scheduled_for, next_run_at}` | `{events: [...], cursor, pending}` |
| 원인 | 예정 시각이 지났다 | 잡이 종결됐다(`ok`·`failed`·`cancelled`…) |
| 한 번만 | **CAS 클레임** — 복제본이 여럿이어도 한 예정에 한 곳만 `due: true` | **at-least-once** — ack 전엔 같은 배치가 다시 온다 |
| 밀린 것 | 몰아서 돌지 않는다. 사흘 꺼져 있었으면 한 번 | 커서가 **켤 때** 잡힌다. 꺼져 있던 동안의 종결은 안 준다 |
| 안 오는 것 | — | **자기(어떤 플러그인이든)가 만든 잡의 종결** — 재귀 방지. 플러그인이 만든 잡은 아무것도 깨우지 않는다 |
| 범위 | — | 플랫폼 테넌트의 플러그인은 전 테넌트, 보통 테넌트의 플러그인은 자기 테넌트 |
| 끄면 | 401 | 401 |

**이벤트는 관찰이지 개입이 아니다.** 종결 뒤에 본다. 나가기 전에 고치거나 막는 자리가 아니다.

이벤트 한 건에 실리는 것 — **모델이 본 것**이지 소비자가 보낸 원문이 아니다:

| 필드 | 뜻 |
|---|---|
| `id` `ts` `kind` | 커서용 일련번호 · 종결 시각 · `job.finished` |
| `job_id` `tenant` `service` `end_user` | 잡과 그 주인. `end_user` 는 테넌트 솔트 해시다 |
| `job_kind` `role` `route` | `generate`/`chat`/`embed` · 역할 · 라우팅 판정(있으면) |
| `status` `error` `error_code` | 종결 상태와 사유(`guard_blocked` 등) |
| `model` `node` `boundary` | 어디서 돌았나. `boundary` 는 `internal`/`external` — 노드를 모르면(지워진 노드) 밖으로 친다 |
| `prompt` `system` | **가드가 가린 뒤** 노드로 간 것. 밖 노드였으면 더 가린 변형. 노드에 안 간 잡(큐 취소·배치 실패)은 `null` |
| `output` | 출력 가드를 지나 소비자에게 나간 응답. `ok` 가 아니면 `null` |
| `usage` | `input_tokens` `output_tokens` `cost_usd` |
| `created_at` `started_at` `finished_at` | 지연은 `finished_at - started_at` |

## 4. 어떤 언어로든 — HTTP 계약

- 모든 호출은 `Authorization: Bearer <플러그인 토큰>` · JSON 본문 · 오류는 `{error: {code, message, ...}}` 이고 `code` 는 로케일과
  무관하게 고정이다(`GET /v1/meta` 에 목록).
- `tick` 은 자주 물어도 된다 — 컨트롤 플레인이 `next_run_at` 을 준다. 하한 5초쯤(호출마다 토큰의 `last_used` 를 쓴다), 상한은
  `next_run_at` 이 정한다. 하루에 한 번만 물으면 예정을 최대 하루 늦게 안다.
- `events` 는 **처리를 끝낸 배치의 `cursor` 를 다음 요청의 `ack` 에** 넣는다. ack 하기 전에 죽으면 같은 배치가 다시 온다 —
  `id` 로 중복을 거르는 것은 플러그인 몫이다. `pending > 0` 이면 바로 다시 받는다. 마지막 확정은 `{"ack": cursor, "limit": 0}` —
  ack 만 하고 아무것도 받지 않는다.
- **401 은 "꺼졌다"** 는 뜻이다(관리자가 껐거나 토큰이 회전·폐기됐다). 다시 켜면 그대로 살아나므로 죽지 말고 느리게 계속 묻는 것이
  기본이다. **404 `not_found`** 는 이 토큰이 플러그인 것이 아니라는 뜻이라 즉시 멈춘다. **409 `plugin_no_event_trigger`** 는
  이벤트를 선언하지 않았다는 뜻 — tick 은 계속. **429** 는 `retry_after` 를 지킨다.
- 공개 주소에 열어 두는 것은 `/v1/plugin/*` 와 소비자 라우트다. `/v1/platform/*`(설치·검사·켜기·회전)은 프록시에서 감추는 것이
  기본이다(번들의 nginx 가 그렇게 하고, 이유는 [topology.md §2](topology.md#2-신뢰-경계를-넘는-것--넘지-않는-것)) — 프록시는 운영자 몫이다.

## 5. 토큰

- **설치 응답에 한 번**, 그다음은 **회전에 한 번**. 어디에도 다시 나오지 않는다. 받자마자 배포 대상의 환경 파일(600)로 옮긴다.
- 회전: `POST /v1/platform/plugins/{id}/rotate-token {"grace_seconds": 3600}` (콘솔 플러그인 탭 「토큰 회전」은 유예 60분).
  응답의 새 토큰으로 환경 파일을 갈고 재시작한다. 옛 토큰은 유예 뒤에 죽는다 — 유예 0 이면 즉시. 살아 있는 토큰이 하나도
  없으면(폐기했거나 잃었거나) 회전이 **발급**이 된다(`reissued: true`). 재설치는 토큰을 주지 않으므로 이것이 유일한 재발급 경로다.
- 토큰이 새는 순간 그 플러그인이 할 수 있는 모든 일(허용 역할·예산 안에서의 LLM 호출, 이벤트 열람)이 샌다. 매니페스트의
  `allow_roles`·`budget_usd_per_month` 를 필요한 만큼만 — LLM 을 안 부르는 플러그인은 예산 0.

## 6. Python SDK — 두 파일

호스트에서 내려받는다(어떤 토큰이든 인증은 필요하다 — 플러그인 자기 토큰이면 된다). 둘 다 **표준 라이브러리만** 쓰고 Python 3.9
이상에서 돈다 — 플러그인이 도는 기계에 pip 승인 절차가 있어도 파일 두 개면 끝난다.

```sh
H='Authorization: Bearer <토큰>'
curl -fsSL -H "$H" https://<호스트>/v1/client/client.py -o client.py     # HTTP · 오류 계약 (소비자 SDK 와 같은 파일)
curl -fsSL -H "$H" https://<호스트>/v1/client/plugin.py -o plugin.py     # 런타임 — 옆의 client.py 를 쓴다
```

```python
from plugin import Plugin

plugin = Plugin.from_env()                       # LCC_URL · LCC_TOKEN

def on_event(event):                             # 배치의 모든 on_event 가 예외 없이 끝나야 ack 된다
    print(event.id, event.role, event.status, event.model, event.latency)

def on_tick(tick):                               # due 일 때만 — 이 프로세스가 그 실행을 가져왔다
    result = plugin.llm.generate("summarize", "…", wait=30)
    print(result.status, result.text[:80])

plugin.run(on_event=on_event)                    # 또는 on_tick=on_tick, 둘 다도 된다. once=True 면 한 바퀴
```

`run()` 이 대신 지키는 것:

| 성질 | 동작 |
|---|---|
| 처리 뒤 ack | 배치의 핸들러가 **전부** 성공한 뒤 `limit: 0` 으로 ack. 하나라도 예외면 ack 하지 않고 백오프 뒤 같은 배치가 다시 온다 |
| 밀린 것 | `pending > 0` 이면 즉시 재풀(최대 1000배치) |
| 선언하지 않은 것 | `on_tick` 이 없으면 tick 을 안 묻고 `on_event` 가 없으면 이벤트를 안 당긴다. 409 면 이벤트 풀만 멈춘다 |
| 간격 | tick 은 `next_run_at` 까지, `[min_interval=5, max_interval=300]` 으로 자르고 지터 10%. 이벤트는 하한 간격 |
| 401 | `on_unauthorized="wait"`(기본): 상한 간격으로 계속 묻는다 — 다시 켜면 살아난다. `"exit"`: `stopped_by="unauthorized"` 로 끝 |
| 404 `not_found` | `stopped_by="not_a_plugin_token"` 으로 즉시 끝 — 토큰이 플러그인 것이 아니다 |
| 429 · 네트워크 | `retry_after` · 지수 백오프(하한→상한) |
| 신호 | 메인 스레드면 SIGTERM/SIGINT 에 현재 배치를 끝내고 정지. `stop=threading.Event()` 를 주면 그것으로 |
| 진단 | `python plugin.py --tick` · `--events`(ack 안 함) · `--once` |

`plugin.llm` 이 소비자 SDK(`ControlCenter`)다 — `generate`·`chat`·`status`·`meta`. 오류 클래스(`ControlCenterError`·`Blocked`·
`RateLimited`)도 거기서 재수출된다.

## 7. 개발 루프

```sh
python lccp.py init my-plugin --id acme.my-plugin --trigger event      # plugin.toml · main.py · README.md
python lccp.py check my-plugin                                          # 호스트와 같은 규칙 — 오프라인

# ① 목 서버 — 트리거를 흉내 낸다. 플래그가 없으면 진짜처럼 due:false / 409 다
python mock_server.py --plugin-events --plugin-tick-every 30 &
LCC_URL=http://127.0.0.1:8610 LCC_TOKEN=any python my-plugin/main.py

# ② 데모 호스트 — 진짜 호스트가 무서명으로 설치하고 켜고, 기동마다 쓸 수 있는 토큰을 배너에 찍는다
python -m app serve --demo --plugin-dev my-plugin --port 8683
LCC_URL=http://localhost:8683 LCC_TOKEN=<배너의 토큰> python my-plugin/main.py
#   데모의 acme 서비스 토큰으로 /v1/generate 를 부르면 그 종결이 이벤트로 온다. 콘솔(/ui/) 플러그인 탭에서 켜고 끄고 회전해 본다

# ③ 운영자 사전 검사 — 설치와 같은 함수로 검증만 한다(DB 도 디스크도 안 건드린다). 거부 사유는 설치와 같은 문장
curl -sS -X POST https://<LAN 주소>/v1/platform/plugins/inspect -H "Authorization: Bearer <플랫폼 관리자>" \
     --data-binary @acme.my-plugin-0.1.0.lccp
```

`lccp check` 가 못 보는 것은 하나 — **역할이 그 호스트에 실재하는가**. 그것은 ③ 이 본다.

## 8. 패키징 · 서명 — `lccp`

`GET /v1/client/lccp.py` 로 내려받는다(토큰 필요). Python 3.11 이상(`tomllib`) · `init`·`check`·`build`(무서명)·`inspect` 는 표준
라이브러리만 · 서명·검증·`keygen` 에만 `cryptography`. 낮은 파이썬뿐이면 호스트 이미지로 돌린다:
`docker run --rm -v "$PWD:/w" -w /w --entrypoint python llm-controlcenter:<판> /app/clients/lccp.py check .`

```sh
python lccp.py keygen acme --out ~/keys        # acme.key (비밀 · 0600 · 서명하는 기계 밖으로 내보내지 않는다) · acme.pub (hex 텍스트)
python lccp.py build my-plugin --sign ~/keys/acme.key      # acme.my-plugin-0.1.0.lccp — 같은 입력이면 같은 바이트
python lccp.py verify acme.my-plugin-0.1.0.lccp --pub ~/keys/acme.pub   # signed 0 · unsigned/invalid 1 · 판정 불가 2
python lccp.py inspect acme.my-plugin-0.1.0.lccp --json
```

- 번들 = zip. `plugin.toml` + 파일들 + `MANIFEST.sha256`(정렬된 `해시  이름` 줄) + `SIGNATURE`(그 한 장의 Ed25519 서명, hex).
  서명 하나가 번들 전체를 고정한다 — 어느 파일이 바뀌어도, 목록에 없는 파일이 끼어들어도 `invalid` 다.
- 호스트는 `keys/plugin-trust/*.pub` 의 키로만 검증한다. **번들 안의 키로 번들을 검증하지 않는다.** 운영자에게 넘길 것은 `.lccp` 와
  `.pub` 둘이고, `.pub` 은 `.lccp` 와 **다른 경로**(사람 대 사람)로 건넨다.
- `build` 는 `.git`·`__pycache__`·`*.pyc`·`.venv`·`dist`·`*.lccp`·**`*.key`** 를 빼고, 심볼릭 링크와 호스트 상한(항목 512 ·
  해제 32MiB · 번들 8MiB)을 **만들 때** 거른다 — 올려서 거절당하는 것보다 싸다.
- **재현 가능하다.** 고정 시각·정렬·같은 압축 — 운영자가 sha256 을 비교할 수 있다.

## 9. 배포

```sh
# 플러그인이 도는 기계 (호스트와 다른 기계여도 된다 — 공개 주소의 /v1/plugin/* 만 있으면 된다)
install -d /opt/llmcc-plugins/acme.my-plugin /etc/llmcc-plugins
cp main.py client.py plugin.py /opt/llmcc-plugins/acme.my-plugin/
printf 'LCC_URL=https://<호스트>\nLCC_TOKEN=<설치 응답의 토큰>\n' > /etc/llmcc-plugins/acme.my-plugin.env
chmod 600 /etc/llmcc-plugins/acme.my-plugin.env
install -m 644 llmcc-plugin@.service /etc/systemd/system/ && systemctl daemon-reload     # examples/plugins/systemd/
systemctl enable --now llmcc-plugin@acme.my-plugin
```

- 템플릿 유닛은 임시 비루트 사용자(`DynamicUser`)로 돌리고, 쓸 수 있는 곳은 `$STATE_DIRECTORY`(`/var/lib/llmcc-plugins/<이름>`)뿐이다.
  예제들은 기록을 거기에 쓴다. **값 뒤에 주석을 달지 않는다** — systemd 는 그 주석을 값으로 읽는다.
- 토큰 회전 절차: 콘솔에서 회전(유예 60분) → 새 토큰을 `.env` 에 → `systemctl restart` → 유예 안에 끝내면 끊김이 없다. 플러그인은
  회전 뒤 옛 토큰으로 401 을 만나도 죽지 않고 기다린다(§6).
- 예제 둘이 [examples/plugins/](../examples/plugins/) 에 있다 — `finish-log`(event · JSONL · 기본은 메타데이터만) ·
  `daily-status`(schedule · 아침 상태문 한 문단). 둘 다 이 호스트 판(`>=0.6,<0.7`)에 그대로 설치된다.

## 10. 하지 말 것

- **받자마자 ack 하지 않는다.** 처리가 끝난 뒤 ack — 그것이 at-least-once 를 살리는 유일한 규칙이다. SDK 를 쓰면 저절로 지켜진다.
- **이벤트 본문을 기본으로 남기지 않는다.** 마스킹본이라도 테넌트의 글이다. 남길 이유가 있으면 플래그 뒤에 두고 보존 기간을 정한다.
- **토큰을 코드·이미지·저장소에 넣지 않는다.** 환경 파일 600 하나에만. 잃으면 회전한다.
- **`requires_host` 를 비우지 않는다.** 마이너가 오르면 계약이 바뀔 수 있다 — 범위를 박아 두면 설치가 거절해 준다(그것이 이 필드의 뜻이다).
- **플러그인이 만든 잡의 종결을 기다리지 않는다.** 그것은 어떤 플러그인에게도 오지 않는다(재귀 방지). 자기 잡의 결과는 `wait`·`job()` 으로 본다.
- **고정 간격으로 tick 을 때리지 않는다.** `next_run_at` 이 답이다.

## 참조

- 계약 고정: [feature-spec.md](feature-spec.md) PLUGIN-1~11 (개발 키트는 PLUGIN-10, 회전·사전 검사는 PLUGIN-11)
- 배경·판단: [plugin-exploration.md](plugin-exploration.md) §11-a(스케줄) · §11-b(이벤트) · 지금 알고 있는 흠
- 운영: [deployment.md §9](deployment.md#9-운영-인터페이스) · [topology.md §2](topology.md#2-신뢰-경계를-넘는-것--넘지-않는-것)
