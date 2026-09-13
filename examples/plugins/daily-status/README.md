# daily-status — schedule 트리거 예제

매일 08:00(Asia/Seoul)에 `GET /v1/status` 를 읽어 `summarize` 역할로 한 문단짜리 한국어 상태문을 만든다.
호스트가 예정을 갖고 있고 플러그인은 "지금 내 차례인가" 를 묻는다(`POST /v1/plugin/tick`) — 여러 곳에서 띄워도
한 곳만 `due` 를 받고, 사흘 꺼져 있다 켜져도 사흘치를 몰아 돌지 않는다. 6시간 넘게 늦은 tick 은 만들지 않는다.

```sh
H='Authorization: Bearer <토큰>'          # 어떤 토큰이든 인증은 필요하다 — 플러그인 자기 토큰이면 된다
curl -fsSL -H "$H" https://llmcc.example.com/v1/client/client.py -o client.py
curl -fsSL -H "$H" https://llmcc.example.com/v1/client/plugin.py -o plugin.py

# 목 서버 — 30초마다 한 번 tick 이 due 가 된다
python ../../../clients/mock_server.py --plugin-tick-every 30 &
LCC_URL=http://127.0.0.1:8610 LCC_TOKEN=x python3 main.py --once

# 데모 호스트 — 08:00 을 기다리지 않으려면 --now 로 지금 한 번 만든다 (tick 을 묻지 않는다)
python -m app serve --demo --plugin-dev examples/plugins/daily-status --port 8683
LCC_URL=http://localhost:8683 LCC_TOKEN=<배너의 토큰> python3 main.py --now
```

- 스케줄을 자주 확인하고 싶으면 `plugin.toml` 의 `schedule` 을 `*/2 * * * *` 로 바꿔 데모에 다시 설치한다.
- LLM 호출은 tick 당 한 번이고 매니페스트의 예산(월 1 USD)·레이트리밋(분당 2)이 상한이다.
- 운영 배포는 `../systemd/llmcc-plugin@.service` — 기록은 `$STATE_DIRECTORY/daily-status.log`.
