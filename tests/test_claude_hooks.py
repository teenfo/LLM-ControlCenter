"""Claude Code 훅 — 큰 파일을 통째로 컨텍스트에 붓는 것을 막는다.

`.claude/settings.json` 이 `PreToolUse` 에 건 두 스크립트를 **설정에 적힌 명령 그대로**
실행해 판정을 본다. 스크립트를 직접 import 하지 않는 이유: 설정이 가리키는 경로가
틀리면(파일 이름을 바꿨다든가) 훅은 조용히 안 돈다 — Claude Code 는 깨진
settings.json 을 오류 없이 통째로 무시한다. 안 도는 훅은 없는 훅이다.

`test_architecture.py` 가 앱의 구조 불변식에 대해 하는 일을 여기서는 개발 도구
설정에 대해 한다. 앱 코드와 무관하다 — 앱을 설치한 곳에서는 이 파일이 대상 밖이다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / ".claude" / "settings.json"
HOOKS_DIR = ROOT / ".claude" / "hooks"
SCRIPTS = sorted(HOOKS_DIR.glob("*.py"))

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="훅은 셸로 실행된다")


# ── 실행 ────────────────────────────────────────────────────────────────────


def hook_command(matcher: str) -> str:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = [e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == matcher]
    assert len(entries) == 1, f"{matcher} 에 걸린 PreToolUse 훅이 {len(entries)}개다"
    [hook] = entries[0]["hooks"]
    assert hook["type"] == "command"
    return hook["command"]


def run_hook(matcher: str, payload, *, env: dict[str, str] | None = None) -> dict:
    """Claude Code 가 하듯 — 설정의 명령을 셸로, 페이로드를 stdin 으로.

    개발자 셸에 `LCC_READ_MAX_LINES` 가 켜져 있어도 기본값을 보도록 지운다.
    """
    environment = {k: v for k, v in os.environ.items() if k != "LCC_READ_MAX_LINES"}
    environment["CLAUDE_PROJECT_DIR"] = str(ROOT)
    environment.update(env or {})
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        ["bash", "-c", hook_command(matcher)],
        input=raw, capture_output=True, text=True, env=environment, timeout=30,
    )
    assert proc.returncode == 0, f"훅은 항상 exit 0 이어야 한다(오류로 막는 것은 막는 것이 아니다):\n{proc.stderr}"
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out


def read_decision(path, *, env=None, **extra) -> dict:
    return run_hook("Read", {"tool_name": "Read", "tool_input": {"file_path": str(path), **extra}}, env=env)


def bash_decision(command: str, *, cwd, env=None) -> dict:
    return run_hook("Bash", {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}, env=env)


def threshold() -> int:
    """두 스크립트가 적어 둔 기본 상한. 둘이 다르면 그 자체가 결함이다."""
    values: set[int] = set()
    for script in SCRIPTS:
        match = re.search(r"^DEFAULT_MAX_LINES = (\d+)$", script.read_text(encoding="utf-8"), re.M)
        assert match, f"{script.name} 에 DEFAULT_MAX_LINES 가 없다"
        values.add(int(match.group(1)))
    assert len(values) == 1, f"두 훅의 기본 상한이 다르다: {sorted(values)}"
    return values.pop()


def write_lines(path: Path, count: int) -> Path:
    path.write_text("".join(f"line {i}\n" for i in range(count)), encoding="utf-8")
    return path


@pytest.fixture
def files(tmp_path):
    """상한 기준의 파일들. 상한은 스크립트에서 읽는다 — 숫자를 여기 또 적으면 어긋난다."""
    limit = threshold()
    return SimpleNamespace(
        dir=tmp_path,
        limit=limit,
        big=write_lines(tmp_path / "big.txt", limit + 1),
        huge=write_lines(tmp_path / "huge.py", limit * 6),
        edge=write_lines(tmp_path / "edge.txt", limit),
        small=write_lines(tmp_path / "small.txt", 12),
        half=write_lines(tmp_path / "half.txt", limit // 2 + 1),   # 둘을 이으면 상한을 넘는다
    )


# ── 배선 ────────────────────────────────────────────────────────────────────


def test_the_settings_wire_every_hook_script_and_nothing_else():
    """설정이 없는 스크립트를 가리키거나, 스크립트가 설정에 안 걸려 있으면 훅은 조용히 없다."""
    assert SCRIPTS, "훅 스크립트가 없다"
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for hook in entry["hooks"]
    ]
    referenced = set()
    for command in commands:
        match = re.search(r'"\$CLAUDE_PROJECT_DIR/(\.claude/hooks/[\w.-]+\.py)"', command)
        assert match, f"훅 명령이 $CLAUDE_PROJECT_DIR 기준의 스크립트를 가리키지 않는다: {command}"
        script = ROOT / match.group(1)
        assert script.exists(), f"설정이 가리키는 스크립트가 없다: {script}"
        assert os.access(script, os.X_OK), f"실행 권한이 없다: {script}"
        referenced.add(script)
    assert referenced == set(SCRIPTS), (
        f"설정에 안 걸린 스크립트: {sorted(s.name for s in set(SCRIPTS) - referenced)}"
    )
    assert {e["matcher"] for e in settings["hooks"]["PreToolUse"]} == {"Read", "Bash"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_hooks_need_nothing_beyond_the_standard_library(script, files):
    """`python3 -I -S` — 가상환경도 site-packages 도 없이 돈다.

    훅은 `.venv` 가 아니라 시스템 `python3` 로 실행된다. 여기서 yaml 을 import 하는
    순간 훅은 매번 오류를 내고, 오류는 막는 것이 아니다.
    """
    payload = {"tool_name": "Read", "cwd": str(files.dir),
               "tool_input": {"file_path": str(files.big), "command": f"cat {files.big}"}}
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── Read ────────────────────────────────────────────────────────────────────


def test_a_whole_read_of_a_big_file_is_denied_and_the_reason_redirects(files):
    """막기만 하면 모델은 같은 시도를 반복한다. 이유가 **다음 행동**을 말해야 한다."""
    out = read_decision(files.big)
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "big.txt" in reason
    assert str(files.limit + 1) in reason, "몇 줄인지 말해야 한다"
    assert "offset/limit" in reason and "Grep" in reason, "대신 무엇을 할지 말해야 한다"
    assert "LCC_READ_MAX_LINES" in reason, "정말 필요할 때의 출구를 말해야 한다"


def test_a_file_at_the_threshold_is_still_allowed(files):
    """경계는 '이하 허용'. 상한이 500 인데 500줄 파일을 막으면 상한이 아니라 499 다."""
    assert read_decision(files.edge)["permissionDecision"] == "allow"
    assert read_decision(files.big)["permissionDecision"] == "deny"


@pytest.mark.parametrize("extra", [{"offset": 10}, {"limit": 40}, {"pages": "1-3"}],
                         ids=["offset", "limit", "pages"])
def test_ranged_reads_are_allowed(files, extra):
    """구간 읽기가 바로 권하는 방식이다. 그것까지 막으면 출구가 없다."""
    assert read_decision(files.huge, **extra)["permissionDecision"] == "allow"


def test_files_the_tool_handles_itself_are_left_alone(files):
    """없는 파일은 Read 가 오류를 내고, 이진·PDF 는 줄 수가 뜻이 없다."""
    assert read_decision(files.dir / "nope.txt")["permissionDecision"] == "allow"

    binary = files.dir / "blob.bin"
    binary.write_bytes(b"\x00\x01" + b"\n" * (files.limit * 10))
    assert read_decision(binary)["permissionDecision"] == "allow"

    pdf = files.dir / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x\n" * (files.limit * 10))
    assert read_decision(pdf)["permissionDecision"] == "allow"


def test_the_threshold_follows_the_environment(files):
    """`LCC_READ_MAX_LINES` — 올리면 통째 읽기가 열리고, 내리면 작은 파일도 막힌다. 쓰레기 값은 기본값."""
    assert read_decision(files.small, env={"LCC_READ_MAX_LINES": "10"})["permissionDecision"] == "deny"
    assert read_decision(files.huge, env={"LCC_READ_MAX_LINES": "100000"})["permissionDecision"] == "allow"
    for garbage in ("abc", "0", "-5", ""):
        assert read_decision(files.big, env={"LCC_READ_MAX_LINES": garbage})["permissionDecision"] == "deny"
        assert read_decision(files.edge, env={"LCC_READ_MAX_LINES": garbage})["permissionDecision"] == "allow"


# ── Bash ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("template", [
    "cat {big}",
    "less {big}",
    "more {big}",
    "tac {big}",
    "head -2000 {huge}",
    "head -n 2000 {huge}",
    "head --lines=2000 {huge}",
    "tail -n +1 {huge}",
    "head -n -3 {huge}",
    "git status && cat {big}",
    "echo hi; cat {big}",
    "cat {big} 2>/dev/null",
    "cat {big} 2>&1",
    "cat {half} {half}",
    "cat {dir}/*.txt",
    "cat > {dir}/x.sh <<'EOF'\ncat {small}\nEOF\ncat {big}",
])
def test_dumping_a_big_file_into_the_context_is_denied(files, template):
    command = template.format(**vars(files))
    assert bash_decision(command, cwd=files.dir)["permissionDecision"] == "deny", command


@pytest.mark.parametrize("template", [
    "cat {big} | grep line",
    "cat {big} > {dir}/out",
    "cat {big} >> {dir}/out",
    "head -50 {huge}",
    "tail -n 20 {huge}",
    "head -c 4000 {huge}",
    "sed -n '100,140p' {huge}",
    "grep -n 'line 7' {huge}",
    "wc -l {huge}",
    "cat {small}",
    "cat {edge}",
    "cat {small} {small}",
    "cat > {dir}/x.sh <<'EOF'\ncat {big}\nhead -2000 {huge}\nEOF",
    "python3 -c 'print(1)' && cat {small}",
    "cat {dir}/nonexistent.txt",
    'cat "unterminated {big}',
    "git log --oneline -3",
])
def test_filtered_ranged_and_redirected_reads_are_allowed(files, template):
    """과잉 차단은 사람이 훅을 꺼 버리게 만든다 — 허용 목록이 차단 목록만큼 중요하다."""
    command = template.format(**vars(files))
    assert bash_decision(command, cwd=files.dir)["permissionDecision"] == "allow", command


def test_relative_paths_resolve_against_the_tool_cwd(files):
    """Claude Code 는 cwd 를 페이로드로 준다. 훅 프로세스의 cwd 로 풀면 엉뚱한 파일을 센다."""
    assert bash_decision("cat big.txt", cwd=files.dir)["permissionDecision"] == "deny"
    assert bash_decision("cat big.txt", cwd=ROOT)["permissionDecision"] == "allow"


def test_the_bash_reason_teaches_the_range_idiom(files):
    reason = bash_decision("cat {big}".format(**vars(files)), cwd=files.dir)["permissionDecisionReason"]
    assert "big.txt" in reason
    assert "grep -n" in reason and "sed -n" in reason and "offset/limit" in reason
    assert str(files.limit + 1) in reason


# ── 둘의 일치 ───────────────────────────────────────────────────────────────


def test_both_hooks_share_the_threshold_and_its_override(files):
    """Read 로 막힌 것을 `cat` 으로 우회할 수 있으면 훅은 하나만 있는 것과 같다."""
    threshold()  # 적어 둔 숫자부터 같아야 한다
    lowered = {"LCC_READ_MAX_LINES": "10"}
    for path, expected in ((files.edge, "allow"), (files.big, "deny")):
        assert read_decision(path)["permissionDecision"] == expected
        assert bash_decision(f"cat {path}", cwd=files.dir)["permissionDecision"] == expected
    assert read_decision(files.small, env=lowered)["permissionDecision"] == "deny"
    assert bash_decision(f"cat {files.small}", cwd=files.dir, env=lowered)["permissionDecision"] == "deny"


@pytest.mark.parametrize("matcher", ["Read", "Bash"])
@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"tool_input": null}'])
def test_garbage_input_never_blocks(matcher, raw):
    """훅이 죽으면 Claude Code 는 오류를 보이고 도구를 그냥 실행한다. 그러니 죽지 말고 허용을 말한다."""
    assert run_hook(matcher, raw)["permissionDecision"] == "allow"
