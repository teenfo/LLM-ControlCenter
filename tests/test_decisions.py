"""결정 기록의 드리프트 장치 — **판정 자체가 낡는다.**

`design-decisions.md` 는 "판정을 안 남기면 다음 사람이 같은 항목을 처음부터 다시
저울질한다" 를 막으려고 존재한다. 그런데 그 문서의 **판정 칸이 조용히 낡았다**:
여섯 항목이 `이번 라운드` · `다음 라운드 이후` 로 적혀 있었고, 그 라운드는 여러 커밋
전에 끝났다. 오늘 읽는 사람은 그 항목이 됐는지 안 됐는지 알 수 없다.

이 저장소는 같은 실패를 여러 번 겪었고 그때마다 장치를 붙였다(architecture §13-8
"손으로 관리하는 표는 반드시 어긋난다" — 모듈 목록 · 라우트 요약 · 장애 반경 표 ·
용량 수치). **결정 기록만 장치가 없었다.**

여기서 거는 규칙은 둘 다 객관적이다. 판정의 내용이 옳은지는 못 보지만, 판정이
**말이 되는 형식인지**는 볼 수 있다.
"""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "design-decisions.md"

#: `### D1 / G1. 제목 — **판정**` 을 읽는다. 복합 헤더(`D1 / G1`)가 실재하므로
#: 하나만 잡으면 G1 참조가 영영 안 풀린 것처럼 보인다.
_HEADING = re.compile(
    r"^### ((?:[DGLP]\d+(?:-[a-z])?)(?:\s*/\s*(?:[DGLP]\d+))*)\.\s*(.+?)\s*—\s*\*\*(.+?)\*\*",
    re.M,
)

#: 판정에 쓰면 안 되는 **상대 시점** 표현. 쓰인 날에만 참이다.
_RELATIVE = ("이번 라운드", "다음 라운드", "곧", "조만간", "나중에")


def items() -> dict[str, str]:
    """항목 id → 판정. 복합 헤더는 양쪽 다 같은 판정으로 편다."""
    found: dict[str, str] = {}
    for ids, _title, verdict in _HEADING.findall(DOC.read_text(encoding="utf-8")):
        for one in re.findall(r"[DGLP]\d+(?:-[a-z])?", ids):
            found[one] = verdict
    return found


def bodies() -> dict[str, str]:
    text = DOC.read_text(encoding="utf-8")
    marks = list(_HEADING.finditer(text))
    out: dict[str, str] = {}
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[match.end():end]
        for one in re.findall(r"[DGLP]\d+(?:-[a-z])?", match.group(1)):
            out[one] = body
    return out


def test_the_document_is_parseable_at_all():
    """장치가 아무것도 못 읽으면서 통과하면 **없는 것보다 나쁘다.**"""
    found = items()

    assert len(found) >= 25, f"항목을 {len(found)}개밖에 못 읽었다 — 형식이 바뀌었다"
    for expected in ("D1", "G1", "D10", "L2", "P1"):
        assert expected in found, f"{expected} 를 못 읽었다"


def test_no_verdict_uses_a_relative_point_in_time():
    """**"이번 라운드" 는 쓰인 날에만 참이다.**

    실제로 여섯 항목이 그렇게 남아 있었다. 그 라운드는 여러 커밋 전에 끝났고, 문서는
    그것을 아직 진행 중인 것처럼 말하고 있었다 — 결정을 기록해 재론을 막겠다는 문서가
    **자기 상태를 잃은 것**이다.

    판정은 절대적이어야 한다: 완료 · 보류 · 기각 · 조건부.
    """
    guilty = {
        item: verdict
        for item, verdict in items().items()
        if any(phrase in verdict for phrase in _RELATIVE)
    }

    assert not guilty, (
        "판정에 상대 시점이 쓰였다 — 언제 읽어도 참인 말로 바꾸세요: "
        + ", ".join(f"{k}({v})" for k, v in sorted(guilty.items()))
    )


def test_every_deferral_carries_a_release_condition():
    """**조건 없는 보류는 망각이다** — 이 문서 자신이 서두에 적어 둔 규칙이다.

    L1 이 실제로 그랬다. 판정이 "다음 라운드 이후" 라 보류로 안 보였고, 그래서 해제
    조건을 안 적고도 아무도 눈치채지 못했다. **상대 시점 표현이 규칙을 비껴간 것**이라
    위 테스트와 이 테스트는 같은 결함의 두 얼굴이다.
    """
    missing = [
        item
        for item, verdict in items().items()
        if "보류" in verdict and "해제 조건" not in bodies()[item]
    ]

    assert not missing, f"보류인데 해제 조건이 없다: {sorted(missing)}"


def test_prerequisites_point_at_items_that_exist():
    """선행 조건이 없는 항목을 가리키면 다음 사람이 찾다가 포기한다."""
    known = set(items())
    dangling: list[str] = []
    for item, body in bodies().items():
        block = re.search(r"\*\*선행 조건\*\*:(.+?)(?:\*\*해제|\n\n)", body, re.S)
        if not block:
            continue
        for ref in re.findall(r"\b([DGL]\d+)\b", block.group(1)):
            if ref not in known:
                dangling.append(f"{item} → {ref}")

    assert not dangling, f"없는 항목을 선행 조건으로 가리킨다: {dangling}"


def test_a_satisfied_prerequisite_does_not_still_read_as_a_blocker():
    """**선행 조건이 다 풀렸는데 문서가 모르면, 다음 사람이 그것을 다시 확인한다.**

    L2 가 그랬다. D13·D6·G1 을 선행 조건으로 걸어 뒀는데 셋 다 완료됐고, 문서는 아직
    막혀 있는 것처럼 읽혔다. 해소됐다는 사실을 적는 것과 "그러니 하자" 는 다르다 —
    L2 의 해제 조건은 여전히 수요다.

    기계로 볼 수 있는 것은 **해소 사실을 적었는가**까지다. 적었는지만 본다.
    """
    stale: list[str] = []
    verdicts = bodies()
    for item, body in verdicts.items():
        block = re.search(r"\*\*선행 조건\*\*:(.+?)(?:\*\*해제|\n\n\*\*|\Z)", body, re.S)
        if not block:
            continue
        refs = {r for r in re.findall(r"\b([DGL]\d+)\b", block.group(1))}
        if not refs:
            continue
        if all("완료" in items().get(ref, "") for ref in refs):
            # 전부 완료됐다면 본문이 그 사실을 인정해야 한다.
            if "해소" not in block.group(1):
                stale.append(f"{item} (선행 {sorted(refs)})")

    assert not stale, (
        "선행 조건이 전부 완료됐는데 본문이 그것을 안 적었다 — "
        f"'해소됐다' 를 적으세요: {stale}"
    )


def test_the_roadmap_and_the_verdicts_do_not_contradict():
    """§4 로드맵 표와 각 항목의 판정이 갈리면 둘 중 하나는 거짓말이다."""
    text = DOC.read_text(encoding="utf-8")
    roadmap = text.split("## 4. 다음 라운드 순서")[1]
    verdicts = items()

    contradictions = []
    for line in roadmap.splitlines():
        if not line.startswith("|") or "✅" not in line:
            continue
        for ref in re.findall(r"\b([DGL]\d+(?:-[a-z])?)\b", line):
            verdict = verdicts.get(ref)
            if verdict and not ("완료" in verdict or "기각" in verdict):
                contradictions.append(f"{ref}: 로드맵은 ✅, 판정은 '{verdict}'")

    assert not contradictions, contradictions
