#!/usr/bin/env python3
"""finish-log — 모델 경계를 지난 잡의 종결을 JSONL 로 남긴다 (event 트리거 예제).

기본은 **메타데이터만** 쓴다 — 역할·모델·노드·경계·지연·사용량·오류 코드. 프롬프트·응답 본문은 `--with-text` 를
줄 때만 쓴다. 마스킹본이라도 테넌트의 글이 남의 저널에 남는 것은 기본값이 아니어야 한다.

    LCC_URL=https://llmcc.example.com LCC_TOKEN=lcc_... python3 main.py
    python3 main.py --once              # 한 바퀴만 — 밀린 이벤트를 다 받고 끝낸다 (cron 에서)
    python3 main.py --with-text         # 본문까지

동작 원리는 SDK(`plugin.py`)가 지킨다: 배치의 모든 이벤트를 이 파일의 `on_event` 가 예외 없이 끝낸 뒤에만 ack 하므로,
쓰다가 죽으면 같은 배치가 다시 온다(at-least-once). 그래서 `id` 로 중복을 거른다.

`client.py`·`plugin.py` 를 이 디렉터리에 둔다(호스트의 GET /v1/client/client.py · /v1/client/plugin.py) — 또는
`LCC_SDK_DIR` 로 그 위치를 준다. 기록은 `$STATE_DIRECTORY`(systemd) 나 `LCC_PLUGIN_STATE_DIR`, 없으면 이 디렉터리다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("LCC_SDK_DIR") or str(HERE))
try:
    from plugin import Plugin
except ImportError as exc:  # pragma: no cover — 설치 실수를 문장으로 끝낸다
    sys.exit(f"SDK 를 찾지 못했다: {exc}\n  client.py 와 plugin.py 를 {HERE} 에 두거나 LCC_SDK_DIR 로 위치를 준다")

log = logging.getLogger("finish-log")

#: 기본으로 남기는 것 — 본문이 아니라 **경계에서 일어난 일**이다.
METADATA_FIELDS = (
    "id", "ts", "job_id", "tenant", "service", "end_user", "job_kind", "role", "route",
    "status", "error_code", "model", "node", "boundary", "created_at", "started_at", "finished_at",
)
#: `--with-text` 일 때만 더하는 것.
TEXT_FIELDS = ("prompt", "system", "output", "error")


def state_dir() -> Path:
    return Path(os.environ.get("STATE_DIRECTORY") or os.environ.get("LCC_PLUGIN_STATE_DIR") or HERE)


def row_for(event, with_text: bool = False) -> dict:
    """이벤트 한 건을 JSONL 한 줄로. 기본은 메타데이터만이다."""
    row = {name: getattr(event, name) for name in METADATA_FIELDS}
    row["latency"] = event.latency
    row["usage"] = dict(event.usage)
    if with_text:
        row.update({name: getattr(event, name) for name in TEXT_FIELDS})
    return row


class Journal:
    """JSONL 한 파일. 마지막으로 쓴 `id` 를 기억해 재전달을 거른다."""

    def __init__(self, path: Path, with_text: bool = False) -> None:
        self.path = path
        self.with_text = with_text
        self.last_id = self._last_id()

    def _last_id(self) -> int:
        if not self.path.is_file():
            return 0
        last = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    last = max(last, int(json.loads(line).get("id", 0)))
                except ValueError:
                    continue
        return last

    def write(self, event) -> None:
        if event.id <= self.last_id:
            log.info("이미 쓴 이벤트 %s — 재전달을 거른다", event.id)
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row_for(event, self.with_text), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())     # 이 함수가 돌아가면 SDK 가 ack 한다 — 그 전에 디스크에 있어야 한다
        self.last_id = event.id
        log.info("event %s role=%s status=%s model=%s boundary=%s latency=%s",
                 event.id, event.role, event.status, event.model, event.boundary, event.latency)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="job.finished 를 JSONL 로 남긴다 — 기본은 메타데이터만")
    parser.add_argument("--log", help="JSONL 경로 (기본: 상태 디렉터리의 finish-log.jsonl)")
    parser.add_argument("--with-text", action="store_true", help="프롬프트·시스템·응답 본문도 남긴다")
    parser.add_argument("--once", action="store_true", help="밀린 이벤트를 다 받고 끝낸다")
    parser.add_argument("--exit-when-off", action="store_true",
                        help="플러그인이 꺼지면(401) 기다리지 않고 끝낸다 (기본은 상한 간격으로 계속 묻는다)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    path = Path(args.log) if args.log else state_dir() / "finish-log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(path, with_text=args.with_text)
    log.info("기록: %s (본문 %s, 마지막 id %s)", path, "포함" if args.with_text else "제외", journal.last_id)

    plugin = Plugin.from_env(name="finish-log")
    report = plugin.run(
        on_event=journal.write, once=args.once,
        on_unauthorized="exit" if args.exit_when_off else "wait",
    )
    log.info("끝: events=%s acked=%s errors=%s stopped_by=%s",
             report.events, report.acked, report.errors, report.stopped_by)
    # 토큰이 플러그인 것이 아니면 설정 실수다 — systemd 가 failed 로 보이게 한다.
    return 1 if report.stopped_by == "not_a_plugin_token" else 0


if __name__ == "__main__":
    sys.exit(main())
