#!/usr/bin/env python3
"""큰 파일의 통째 읽기를 막는다 — Claude Code `PreToolUse` 훅, `Read` 도구용.

배경: 스포티파이의 Shunt 플러그인에서 **훅 부분만** 가져왔다. 저쪽은 막은 뒤
저가 모델에 위임하지만, 여기는 위임할 곳이 없으니 **Grep 으로 자리를 찾고
offset/limit 으로 그 구간만 읽으라**고 돌려보낸다. 그것만으로도 이 저장소에서
`store.py`(3300줄 남짓) 한 번 통째 읽기 ≈ 30K 토큰이 안 들어온다.

임계값은 이 저장소의 분포로 정했다(추적 파일 108개 중 501줄 이상이 33개). 350이면
43% 가 막혀서 과하다. `LCC_READ_MAX_LINES` 로 바꾼다.

허용: offset·limit·pages 가 있는 읽기(이미 구간 읽기다) · 임계값 이하 · 없는 파일
(Read 가 스스로 오류를 낸다) · 이진 파일과 PDF·이미지(줄 수가 뜻이 없다).

**항상 exit 0.** 판정은 JSON 의 permissionDecision 으로만 전한다 — 훅이 죽으면
Claude Code 는 그것을 오류로 보고하고 도구를 그냥 실행한다. 오류로 막는 것은
막는 것이 아니다.

`tests/test_claude_hooks.py` 가 이 파일을 설정에 적힌 명령 그대로 실행해 판정을 본다.
"""
from __future__ import annotations

import json
import os
import sys

DEFAULT_MAX_LINES = 500

#: Read 가 줄이 아니라 페이지·픽셀로 다루는 파일들. 줄 수를 세어 봐야 뜻이 없다.
NON_TEXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}


def max_lines() -> int:
    raw = os.environ.get("LCC_READ_MAX_LINES", "")
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_MAX_LINES
    except ValueError:
        return DEFAULT_MAX_LINES


def count_lines(path: str) -> int | None:
    """줄 수. 이진이거나 못 읽으면 None — 그 경우는 막지 않는다."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
            if b"\x00" in head:
                return None
            n = head.count(b"\n")
            while chunk := fh.read(1 << 20):
                n += chunk.count(b"\n")
            return n
    except OSError:
        return None


def allow() -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        allow()
    if not isinstance(payload, dict):
        allow()
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if not path or any(tool_input.get(key) is not None for key in ("offset", "limit", "pages")):
        allow()
    if os.path.splitext(path)[1].lower() in NON_TEXT:
        allow()
    limit = max_lines()
    lines = count_lines(path)
    if lines is None or lines <= limit:
        allow()

    reason = (
        f"{os.path.basename(path)} 은(는) {lines}줄입니다 (통째 읽기 상한 {limit}줄). "
        "전체를 컨텍스트에 넣지 마세요. 먼저 Grep 으로 필요한 함수·문자열의 줄 번호를 찾고, "
        "Read 에 offset/limit 을 주어 그 구간만 읽으세요. 파일 구조가 필요하면 "
        "Grep 으로 `^def |^class |^## ` 같은 선언 줄만 뽑으세요. "
        "정말 전체가 필요하면 LCC_READ_MAX_LINES 를 올려 재시도할 수 있습니다."
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
