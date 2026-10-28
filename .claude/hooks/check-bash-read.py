#!/usr/bin/env python3
"""`cat`·`less`·`more` 로 큰 파일을 통째로 컨텍스트에 붓는 것을 막는다 — `Bash` 도구용.

Read 훅과 같은 규칙, 같은 임계값. 막는 것은 **출력이 그대로 컨텍스트로 들어오는
경우뿐**이다:

    cat app/store.py               ← 막는다 (3300줄이 그대로 들어온다)
    cat app/store.py 2>/dev/null   ← 막는다 (stderr 만 밖으로 갔다)
    cat app/store.py | grep def    ← 허용 (필터를 거친다)
    cat app/store.py > /tmp/x      ← 허용 (컨텍스트로 안 들어온다)
    head -50 app/store.py          ← 허용 (50줄이다)
    head -2000 app/store.py        ← 막는다 (상한을 넘는 구간이다)
    tail -n +1 app/store.py        ← 막는다 (cat 과 같다)
    sed -n '100,140p' app/store.py ← 허용 (구간 읽기 — 권장하는 방식이다)

세미콜론·&&·|| 로 이어진 명령은 조각마다 본다. 히어독 본문은 명령이 아니므로
건너뛴다 — 이 훅을 처음 넣던 세션에서, 바로 이 독스트링을 히어독으로 쓰다가
윗줄의 `cat app/store.py` 에 막혔다. 판단이 안 서는 문법(명령 치환·바이트 단위
`head -c`·깨진 따옴표)은 허용한다 — 과잉 차단은 사람이 훅을 꺼 버리게 만들고,
꺼진 훅은 없는 훅이다.

**항상 exit 0.** 판정은 JSON 의 permissionDecision 으로만 전한다.

`tests/test_claude_hooks.py` 가 이 파일을 설정에 적힌 명령 그대로 실행해 판정을 본다.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sys

DEFAULT_MAX_LINES = 500

#: 파일을 그대로 내놓는 명령들. 비대화식 셸에서 less/more 는 cat 과 같다.
DUMPERS = {"cat", "tac", "nl", "bat", "less", "more"}
#: 일부만 내놓는 명령들 — 몇 줄인지 보고 판단한다.
PARTIAL = {"head", "tail"}

_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def max_lines() -> int:
    try:
        value = int(os.environ.get("LCC_READ_MAX_LINES", ""))
        return value if value > 0 else DEFAULT_MAX_LINES
    except ValueError:
        return DEFAULT_MAX_LINES


def count_lines(path: str) -> int | None:
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


def segments(command: str) -> list[str]:
    """`;` `&&` `||` 줄바꿈으로 자른 조각들.

    히어독 본문은 뺀다 — 거기 적힌 `cat` 은 실행되는 명령이 아니라 파일에 쓰이는
    글자다. 파이프는 자르지 않는다: 파이프가 있는 조각은 통째로 허용이다.
    """
    out: list[str] = []
    terminator: str | None = None
    for line in command.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = _HEREDOC.search(line)
        if match:
            terminator = match.group(1)
        out.extend(s for s in re.split(r";|&&|\|\|", line) if s.strip())
    return out


def stdout_leaves_context(segment: str) -> bool:
    """파이프를 거치거나 파일로 간다.

    `2>/dev/null` 과 `>&2` 는 아니다 — stderr 도 도구 결과로 컨텍스트에 들어온다.
    """
    cleaned = re.sub(r"\d?>&\d", "", segment)     # 2>&1, >&2, 1>&2 — 어느 쪽도 밖이 아니다
    cleaned = re.sub(r"2>>?", "", cleaned)        # 2>/dev/null — stderr 만 밖으로 간다
    return "|" in cleaned or ">" in cleaned


def partial_plan(argv: list[str]) -> tuple[list[str], str, int] | None:
    """head/tail 의 (파일들, 방식, N).

    "count" 는 N줄(`head -n 200`·`head -200`·`--lines=200`), "rest" 는 N줄을 뺀
    나머지(`tail -n +N`·`head -n -N` — 파일이 크면 이것도 통째 읽기다).
    바이트 단위(`-c`)면 None — 판단하지 않는다.
    """
    files: list[str] = []
    mode, count = "count", 10
    i = 1
    while i < len(argv):
        arg = argv[i]
        value = None
        if arg in ("-c", "--bytes") or arg.startswith("--bytes=") or re.fullmatch(r"-c\d+", arg):
            return None
        if arg in ("-n", "--lines"):
            if i + 1 >= len(argv):
                return None
            i += 1
            value = argv[i]
        elif arg.startswith("--lines="):
            value = arg[len("--lines="):]
        elif re.fullmatch(r"-n[+-]?\d+", arg):
            value = arg[2:]
        elif re.fullmatch(r"-\d+", arg):
            value = arg[1:]
        elif arg.startswith("-"):
            pass                                   # -q, -v, -f 같은 다른 옵션
        else:
            files.append(arg)
        if value is not None:
            match = re.fullmatch(r"([+-]?)(\d+)", value)
            if not match:
                return None
            mode = "rest" if match.group(1) else "count"
            count = int(match.group(2))
        i += 1
    return files, mode, count


def expand(files: list[str], cwd: str) -> list[str]:
    """`~` 와 글롭을 셸이 하듯 편다. `cat app/*.py` 도 통째 읽기다."""
    out: list[str] = []
    for name in files:
        path = os.path.expanduser(name)
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        out.extend(sorted(glob.glob(path)) or [path])
    return out


def offending(segment: str, limit: int, cwd: str) -> tuple[str, int] | None:
    """이 조각이 상한을 넘는 줄을 컨텍스트에 붓는가. (가장 큰 파일, 내놓을 총 줄 수)."""
    if stdout_leaves_context(segment):
        return None
    try:
        argv = shlex.split(segment)
    except ValueError:
        return None
    if not argv:
        return None
    command = os.path.basename(argv[0])
    if command in DUMPERS:
        files, mode, count = [a for a in argv[1:] if not a.startswith("-")], "all", 0
    elif command in PARTIAL:
        plan = partial_plan(argv)
        if plan is None:
            return None
        files, mode, count = plan
    else:
        return None

    emitted = 0
    largest: tuple[str, int] | None = None
    for path in expand(files, cwd):
        total = count_lines(path)
        if total is None:
            continue
        if mode == "count":
            part = min(count, total)
        elif mode == "rest":
            part = max(0, total - count)
        else:
            part = total
        emitted += part
        if largest is None or total > largest[1]:
            shown = os.path.relpath(path, cwd) if path.startswith(cwd + os.sep) else path
            largest = (shown, total)
    if largest is not None and emitted > limit:
        return largest[0], emitted
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
    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd") or os.getcwd()
    limit = max_lines()
    for segment in segments(command):
        hit = offending(segment, limit, cwd)
        if hit:
            name, emitted = hit
            reason = (
                f"`{segment.strip()}` 은(는) {emitted}줄을 통째로 컨텍스트에 넣습니다 (상한 {limit}줄). "
                f"대신 `grep -n <패턴> {name}` 으로 줄 번호를 찾고 `sed -n '<시작>,<끝>p' {name}` 으로 "
                "그 구간만 보거나, Read 도구에 offset/limit 을 주세요. 파이프로 필터를 거치는 것"
                "(`| grep`, `| head -50`)과 파일로 보내는 것(`> /tmp/x`)은 허용됩니다."
            )
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}, ensure_ascii=False))
            sys.exit(0)
    allow()


if __name__ == "__main__":
    main()
