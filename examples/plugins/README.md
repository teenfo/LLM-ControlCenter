# 예제 플러그인

플러그인 계약(`docs/plugin-authoring.md`)을 두 트리거로 보여 주는 교재다. 저장소 내용이지 배포물이 아니다 —
휠·이미지·에어갭 번들에 실리지 않는다.

| 디렉터리 | 트리거 | 보여 주는 것 |
|---|---|---|
| `finish-log/` | `event job.finished` | 종결을 JSONL 로 남긴다. **기본은 메타데이터만**(본문은 `--with-text`) · at-least-once 재전달을 `id` 로 거른다 · 예산 0 (LLM 을 부르지 않는다) |
| `daily-status/` | `schedule 0 8 * * *` (Asia/Seoul) | tick 을 가져온 쪽만 `GET /v1/status` → `summarize` 한 문단 · 너무 늦은 tick 은 건너뛴다 |
| `systemd/` | — | `llmcc-plugin@.service` 템플릿 — `EnvironmentFile` 600 · 임시 비루트 사용자 · `$STATE_DIRECTORY` |

```sh
# 규칙 검사 (호스트와 같은 코드)
python clients/lccp.py check examples/plugins/finish-log

# 목 서버로
python clients/mock_server.py --plugin-events --plugin-tick-every 30 &
LCC_SDK_DIR=clients LCC_URL=http://127.0.0.1:8610 LCC_TOKEN=x python3 examples/plugins/finish-log/main.py --once

# 데모 호스트로 — 디렉터리를 무서명으로 설치하고 켜고 기동마다 토큰을 찍는다
python -m app serve --demo --plugin-dev examples/plugins/finish-log --plugin-dev examples/plugins/daily-status --port 8683

# 운영으로 — 서명 번들 + 공개 키를 운영자에게
python clients/lccp.py keygen acme --out ~/keys
python clients/lccp.py build examples/plugins/finish-log --sign ~/keys/acme.key
```

두 예제의 `main.py` 는 표준 라이브러리와 SDK 두 파일(`client.py`·`plugin.py`)만 쓰고 Python 3.9 문법으로 써 있다 —
플러그인은 호스트와 다른 기계에서 돈다.
