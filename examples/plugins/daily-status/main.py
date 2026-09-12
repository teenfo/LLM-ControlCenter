#!/usr/bin/env python3
"""daily-status — 매일 아침 클러스터 상태를 한 문단 한국어로 만든다 (schedule 트리거 예제).

예정 시각이 지나면 호스트가 tick 을 **한 번만** 내준다(이 플러그인을 여러 곳에서 띄워도 한 곳만 `due`). 그때
`GET /v1/status`(레인·노드 수)를 읽어 `summarize` 역할로 한 문단을 만들고, 저널(stdout)과 상태 디렉터리의
`daily-status.log` 에 남긴다. LLM 호출은 tick 당 한 번이고, 이 플러그인 서비스의 예산·레이트리밋이 상한이다.

    LCC_URL=https://llmcc.example.com LCC_TOKEN=lcc_... python3 main.py
    python3 main.py --once              # 예정이 지났으면 한 번 돌고 끝난다 (cron 에서 띄울 때)
    python3 main.py --now               # 예정과 무관하게 지금 한 번 만든다 (개발 확인용 — tick 을 묻지 않는다)

`client.py`·`plugin.py` 를 이 디렉터리에 둔다(호스트의 GET /v1/client/) — 또는 `LCC_SDK_DIR` 로 위치를 준다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("LCC_SDK_DIR") or str(HERE))
try:
    from plugin import Plugin
except ImportError as exc:  # pragma: no cover — 설치 실수를 문장으로 끝낸다
    sys.exit(f"SDK 를 찾지 못했다: {exc}\n  client.py 와 plugin.py 를 {HERE} 에 두거나 LCC_SDK_DIR 로 위치를 준다")

log = logging.getLogger("daily-status")

PROMPT = """다음은 LLM 클러스터의 지금 상태를 담은 JSON 이다. 운영자가 아침에 읽을 한 문단(3~4문장)의 한국어
상태문으로 요약하라. 숫자는 그대로 옮기고, JSON 에 없는 사실을 만들지 마라.

{facts}"""

#: 예정보다 이만큼 넘게 늦은 tick 은 "아침 상태문" 이 아니다 — 만들지 않고 기록만 남긴다.
TOO_LATE_SECONDS = 6 * 3600
#: 결과를 이만큼 기다리고도 안 끝나면 **잡을 취소하고** 포기한다 — 노드가 죽어 있을 때 큐에 고아 잡을 남기지 않는다.
DEADLINE_SECONDS = float(os.environ.get("LCC_PLUGIN_DEADLINE") or 600)


def state_dir() -> Path:
    return Path(os.environ.get("STATE_DIRECTORY") or os.environ.get("LCC_PLUGIN_STATE_DIR") or HERE)


class Reporter:
    def __init__(self, plugin: Plugin, path: Path) -> None:
        self.plugin = plugin
        self.path = path

    def compose(self) -> bool:
        """상태를 읽고 한 문단을 만든다. 성공 여부를 돌려준다."""
        status = self.plugin.llm.status()
        facts = json.dumps(status, ensure_ascii=False, sort_keys=True)
        # run() 은 서버가 기다려 준 뒤에도 안 끝났으면 retry_after 를 지켜 폴링한다 — deadline 까지만.
        result = self.plugin.llm.run("summarize", PROMPT.format(facts=facts), wait=30, deadline=DEADLINE_SECONDS)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
        if not result.done:
            self.plugin.llm.cancel(result.job_id)
            line = f"[{stamp}] 포기: {DEADLINE_SECONDS:.0f}초 안에 답이 없어 잡 {result.job_id} 을(를) 취소했다 (노드가 죽어 있나?)"
        elif result.ok:
            line = f"[{stamp}] {result.text.strip()}"
        else:
            line = f"[{stamp}] 실패: status={result.status} error_code={result.error_code} error={result.error}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return result.ok

    def on_tick(self, tick) -> None:
        late = tick.late_by or 0.0
        if late > TOO_LATE_SECONDS:
            log.warning("예정(%s)보다 %.0f초 늦은 tick — 아침 상태문이 아니라서 건너뛴다", tick.scheduled_for, late)
            return
        log.info("tick: scheduled_for=%s late_by=%.0fs", tick.scheduled_for, late)
        self.compose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="매일 아침 클러스터 상태를 한 문단 한국어로")
    parser.add_argument("--log", help="기록 경로 (기본: 상태 디렉터리의 daily-status.log)")
    parser.add_argument("--once", action="store_true", help="한 바퀴만 — 예정이 지났으면 한 번 만들고 끝낸다")
    parser.add_argument("--now", action="store_true", help="tick 을 묻지 않고 지금 한 번 만든다 (개발 확인용)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    path = Path(args.log) if args.log else state_dir() / "daily-status.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    reporter = Reporter(Plugin.from_env(name="daily-status"), path)
    if args.now:
        return 0 if reporter.compose() else 1

    report = reporter.plugin.run(on_tick=reporter.on_tick, once=args.once)
    log.info("끝: ticks=%s errors=%s stopped_by=%s", report.ticks, report.errors, report.stopped_by)
    return 1 if report.stopped_by == "not_a_plugin_token" else 0


if __name__ == "__main__":
    sys.exit(main())
