# finish-log — event 트리거 예제

모델 경계를 지난 잡의 종결(`job.finished`)을 받아 JSONL 로 남긴다. **기본은 메타데이터만**(역할·모델·노드·경계·지연·
사용량·오류 코드)이고, 프롬프트·응답 본문은 `--with-text` 를 줄 때만 쓴다 — 마스킹본이라도 테넌트의 글이 남의
저널에 남는 것은 기본값이 아니어야 한다.

이 플러그인은 LLM 을 부르지 않는다. 매니페스트의 예산이 0 이라 이 토큰으로는 유료 호출이 안 된다.

```sh
# 1. SDK 두 파일을 옆에 둔다 (호스트에서 내려받는다 — 어떤 토큰이든 인증은 필요하다)
H='Authorization: Bearer <토큰>'
curl -fsSL -H "$H" https://llmcc.example.com/v1/client/client.py -o client.py
curl -fsSL -H "$H" https://llmcc.example.com/v1/client/plugin.py -o plugin.py

# 2. 목 서버로 먼저 — 끝난 잡마다 이벤트가 쌓인다
python ../../../clients/mock_server.py --plugin-events &
LCC_URL=http://127.0.0.1:8610 LCC_TOKEN=x python3 main.py --once

# 3. 데모 호스트로 — 무서명 설치 + 켜기 + 토큰이 배너에 찍힌다
python -m app serve --demo --plugin-dev examples/plugins/finish-log --port 8683
LCC_URL=http://localhost:8683 LCC_TOKEN=<배너의 토큰> python3 main.py

# 4. 운영 — 서명해서 넘긴다
python ../../../clients/lccp.py build . --sign acme.key      # example.finish-log-0.1.0.lccp
```

운영 배포는 `../systemd/llmcc-plugin@.service` 를 본다. 기록 파일은 `$STATE_DIRECTORY`(systemd 가 준다) 나
`LCC_PLUGIN_STATE_DIR`, 없으면 이 디렉터리의 `finish-log.jsonl` 이다.

**재전달.** 이벤트는 at-least-once 다 — SDK 는 배치의 모든 `on_event` 가 예외 없이 끝난 뒤에만 ack 하므로, 쓰다가
죽으면 같은 배치가 다시 온다. 이 예제는 기록 파일의 마지막 `id` 를 기억해 거른다.
