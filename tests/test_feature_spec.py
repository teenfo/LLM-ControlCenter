"""기능 명세의 드리프트 장치 — **명세는 반드시 코드보다 늦는다.**

`docs/feature-spec.md` 는 "이 제품이 지금 무엇을 하는가" 를 손으로 적은 표다. 이
저장소는 손으로 관리하는 표가 어긋나는 것을 이미 여러 번 겪었고 그때마다 장치를
붙였다(architecture §13-8 — 모듈 목록 · 라우트 요약 · 장애 반경 표 · 용량 수치 ·
결정 판정). 기능 명세는 그중 가장 넓은 표라 가장 빨리 낡는다.

여기서 거는 규칙은 전부 **객관적**이다. 명세의 설명이 옳은지는 못 본다. 다만
명세가 **없는 것을 가리키거나, 있는 것을 빠뜨렸는지**는 볼 수 있다.

이 파일은 새 동작을 검증하지 않는다 — 있는 표면과 있는 테스트를 대조할 뿐이다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "feature-spec.md"
DECISIONS = ROOT / "docs" / "design-decisions.md"
APP = ROOT / "app"
TESTS = ROOT / "tests"

#: `### AUTH-1  서비스 토큰 발급` 을 읽는다. 들여쓴 예시 블록은 걸리지 않는다.
_FEATURE = re.compile(r"^### ([A-Z]+-\d+)\s+(.+)$", re.M)

#: 쓰면 안 되는 **상대 시점** 표현. `test_decisions.py` 의 목록에서 "곧" 을 뺐다 —
#: 이 문서의 산문은 "선언 순서가 곧 선호도다" 처럼 "바로 그것" 의 뜻으로 쓴다.
#:
#: 검사 범위도 `test_decisions.py` 와 같게 **판정 성격의 칸에만** 건다. 문서 전체에
#: 걸면 "설정이 나중에 넓어져도" 같은 조건절까지 잡혀 장치가 오탐으로 꺼진다.
_RELATIVE = ("이번 라운드", "다음 라운드", "조만간", "나중에")

_STATUSES = ("구현됨", "부분", "미구현")


def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def features() -> dict[str, str]:
    """기능 ID → 그 항목의 본문."""
    text = spec_text()
    marks = list(_FEATURE.finditer(text))
    out: dict[str, str] = {}
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out[match.group(1)] = text[match.end():end]
    return out


def _defined_tests() -> dict[str, set[str]]:
    """파일 이름 → 그 파일에 정의된 테스트 함수 이름들."""
    found: dict[str, set[str]] = {}
    for path in TESTS.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found[path.name] = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
    return found


# ── 0. 장치가 아무것도 못 읽으면서 통과하는 것을 막는다 ──────────────────────


def test_the_spec_is_parseable_at_all():
    """읽어 낸 항목이 0개인데 초록이면 **없는 것보다 나쁘다.**"""
    found = features()

    assert len(found) >= 60, f"기능 항목을 {len(found)}개밖에 못 읽었다 — 형식이 바뀌었다"
    for expected in ("AUTH-1", "GUARD-1", "CLUSTER-3", "ROUTE-1", "DATA-1"):
        assert expected in found, f"{expected} 를 못 읽었다"


def test_feature_ids_are_unique():
    """같은 ID 가 둘이면 고도화 논의에서 서로 다른 것을 가리키게 된다."""
    ids = [match.group(1) for match in _FEATURE.finditer(spec_text())]

    duplicates = sorted({one for one in ids if ids.count(one) > 1})
    assert not duplicates, f"중복된 기능 ID: {duplicates}"


# ── 1·2. 라우트 ─────────────────────────────────────────────────────────────


def _appendix(letter: str) -> str:
    """부록 한 절만 잘라 낸다 — 부록 B(CLI)·C(설정) 표가 섞여 들어오면 안 된다."""
    text = spec_text()
    start = text.index(f"## 부록 {letter} ")
    tail = text[start + 1:]
    end = tail.find("\n## ")
    return tail if end < 0 else tail[:end]


def _spec_route_names() -> set[str]:
    """부록 A 표의 첫 칸(백틱 친 라우트 이름)만 읽는다."""
    return set(re.findall(r"^\| `([a-z_]+)` \|", _appendix("A"), re.M))


def test_every_route_is_in_the_spec():
    """새 라우트를 추가하고 명세를 안 고치면 여기서 걸린다.

    `meta.ROUTE_SUMMARIES` 가 이미 요약을 강제하지만(`test_meta.py`), 그 요약은
    한 줄이라 "그 기능이 무엇인지" 를 말하지 못한다. 명세는 그 한 줄이 어느
    기능에 속하는지까지 적는다.
    """
    from app.meta import ROUTE_SUMMARIES

    missing = sorted(set(ROUTE_SUMMARIES) - _spec_route_names())
    assert not missing, f"docs/feature-spec.md 부록 A 에 없는 라우트: {missing}"


def test_the_spec_names_no_route_that_does_not_exist():
    """반대 방향 — 지운 라우트가 명세에 남으면 없는 API 를 찾게 된다."""
    from app.meta import ROUTE_SUMMARIES

    ghosts = sorted(_spec_route_names() - set(ROUTE_SUMMARIES))
    assert not ghosts, f"명세에만 있고 코드에 없는 라우트: {ghosts}"


# ── 3·4. CLI 와 설정 파일 ───────────────────────────────────────────────────


def test_every_cli_command_is_in_the_spec():
    """CLI 는 설치처가 유출 대응 중에 치는 것이라 빠지면 그때 아쉽다."""
    source = (APP / "cli.py").read_text(encoding="utf-8")
    commands = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', source))
    assert commands, "cli.py 에서 서브커맨드를 하나도 못 읽었다 — 형식이 바뀌었다"

    text = spec_text()
    missing = sorted(one for one in commands if f"`{one}`" not in text)
    assert not missing, f"명세에 없는 CLI 명령: {missing}"


def test_every_config_file_is_in_the_spec():
    """설정 파일이 늘면 설치처가 만질 손잡이가 는다 — 안 적으면 안 만진다."""
    files = {path.name for path in (ROOT / "config").glob("*.yaml")}
    assert files, "config/ 에서 yaml 을 하나도 못 찾았다"

    text = spec_text()
    missing = sorted(one for one in files if f"config/{one}" not in text)
    assert not missing, f"명세에 없는 설정 파일: {missing}"


# ── 5·6. 명세가 가리키는 것이 실재하는가 ────────────────────────────────────


def test_every_cited_test_exists_where_the_spec_says():
    """`고정` 칸이 없는 테스트를 가리키면 그 계약은 아무도 안 지키고 있다.

    이름만 보는 것이 아니라 **그 파일에 있는지**까지 본다 — 테스트를 다른 파일로
    옮기면 명세의 경로가 조용히 거짓이 된다.
    """
    defined = _defined_tests()
    cited = re.findall(r"(test_\w+\.py)::(test_\w+)", spec_text())
    assert cited, "명세에서 테스트 인용을 하나도 못 읽었다 — 형식이 바뀌었다"

    broken = sorted(
        f"{file}::{name}"
        for file, name in cited
        if name not in defined.get(file, set())
    )
    assert not broken, f"명세가 가리키는데 그 파일에 없는 테스트: {broken}"


def test_every_cited_module_exists():
    """지운 모듈이 명세에 남으면 읽는 사람이 없는 파일을 찾는다."""
    text = spec_text()
    named = set(re.findall(r"app/(\w+)\.py", text))
    for line in re.findall(r"^주 모듈: (.+)$", text, re.M):
        named |= set(re.findall(r"`(\w+)\.py`", line))

    assert named, "명세에서 모듈 참조를 하나도 못 읽었다"
    ghosts = sorted(name for name in named if not (APP / f"{name}.py").exists())
    assert not ghosts, f"명세에만 있고 app/ 에 없는 모듈: {ghosts}"


# ── 7. 상태 어휘 ────────────────────────────────────────────────────────────


def test_every_feature_declares_one_of_three_statuses():
    """상태가 자유 서술이면 "부분" 이 무슨 뜻인지 사람마다 달라진다."""
    bad: list[str] = []
    for name, body in features().items():
        found = re.search(r"^상태\s+(\S+)", body, re.M)
        if found is None or found.group(1) not in _STATUSES:
            bad.append(f"{name}({found.group(1) if found else '없음'})")

    assert not bad, f"상태 칸이 {_STATUSES} 중 하나가 아닌 항목: {bad}"


def test_the_enhancement_section_carries_no_relative_time_words():
    """"다음 라운드" 는 적은 날에만 참이다 — 그 라운드는 이미 지났다.

    고도화 후보 절은 이 문서에서 **언제 할 것인가**를 말하는 유일한 곳이다. 답을
    적지 않는 것이 규칙이다 — 해제 조건은 `design-decisions.md` 가 갖는다.
    """
    section = spec_text().split("## § 고도화 후보", 1)[1].split("## § 명세에 넣지")[0]

    offenders = [word for word in _RELATIVE if word in section]
    assert not offenders, f"§ 고도화 후보에 상대 시점 표현이 있다: {offenders}"


# ── 8. 고도화 후보가 가리키는 판정 ──────────────────────────────────────────


def test_every_cited_decision_exists():
    """`부분`·`미구현` 이 가리키는 판정이 없으면 해제 조건을 못 찾는다."""
    section = spec_text().split("## § 고도화 후보", 1)
    assert len(section) == 2, "§ 고도화 후보 절을 못 찾았다"

    cited = set(re.findall(r"\b([DGLP]\d+)\b", section[1].split("## § 명세에 넣지")[0]))
    assert cited, "고도화 후보에서 판정 ID 를 하나도 못 읽었다"

    known = set(re.findall(r"^### ([^\n.]+)\.", DECISIONS.read_text(encoding="utf-8"), re.M))
    flat = {one for header in known for one in re.findall(r"[DGLP]\d+", header)}

    ghosts = sorted(cited - flat)
    assert not ghosts, f"design-decisions.md 에 없는 판정 ID: {ghosts}"


@pytest.mark.parametrize("status", _STATUSES)
def test_each_status_is_actually_used(status):
    """세 값을 정의해 놓고 하나도 안 쓰면 어휘가 아니라 장식이다.

    특히 `부분` 이 0개면 명세가 **되는 것만 적고 있다**는 뜻이다 — 그 명세로는
    고도화 대상을 고를 수 없다.
    """
    used = [name for name, body in features().items() if re.search(rf"^상태\s+{status}", body, re.M)]
    assert used, f"상태 '{status}' 인 항목이 하나도 없다"
