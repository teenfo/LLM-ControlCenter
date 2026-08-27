"""구조 불변식 — 규율이 아니라 장치로 지킨다.

이 파일의 테스트는 전부 같은 형태다. 어떤 규칙을 주석에만 적어 두면 언젠가 누군가
어긴다. 어겼을 때 **테스트가 실패하면** 그때 알게 된다.

§13-8("손으로 관리하는 표는 반드시 어긋난다 — 실제로 어긋났다")과 같은 발상이며,
`test_meta.py::test_every_route_has_a_summary` 가 계약 문서에 대해 하는 일을
여기서는 아키텍처에 대해 한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

#: 앱 소스 전체. 테스트가 파일을 새로 추가해도 자동으로 대상이 된다 —
#: 목록을 손으로 적으면 새 파일이 검사에서 빠진다.
SOURCES = sorted(APP.rglob("*.py"))


def calls_in(path: Path) -> set[str]:
    """이 파일이 부르는 속성 호출 이름들. `a.b.c()` 는 `c` 로 센다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            found.add(node.func.attr)
    return found


def _call_window(source: str, start: int, span: int = 8) -> str:
    """호출이 여러 줄에 걸쳐 있을 수 있으므로 뒤 몇 줄을 함께 본다."""
    lines = source.splitlines()
    return "\n".join(lines[start : start + span])


def test_sources_were_actually_found():
    """검사 대상이 비면 아래 테스트가 전부 무의미하게 통과한다."""
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_only_the_store_touches_the_connection(path):
    """**테넌트 스코프 초크포인트를 우회하는 경로를 만들지 않는다.**

    `store._conn` 을 밖에서 쓰면 `_scoped_where()` 를 지나지 않는 쿼리가 생기고,
    그 순간 한 번의 스코프 누락이 다른 조직의 프롬프트를 노출시킨다.
    """
    if path.name == "store.py":
        return
    source = path.read_text(encoding="utf-8")
    assert "._conn" not in source, f"{path.name} 이 스토어 커넥션을 직접 만진다"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_only_the_pipeline_creates_jobs(path):
    """**잡을 만드는 경로는 파이프라인 하나다.**

    라우터가 `store.create_job()` 을 직접 부를 수 있으면 언젠가 누군가 가드를
    건너뛴 경로를 만든다. 순서(인증 → 가드 → 저장 → 배치)는 규율이 아니라 구조여야 한다.
    """
    if path.name in ("store.py", "pipeline.py"):
        return
    assert "create_job" not in calls_in(path), f"{path.name} 이 잡을 직접 만든다"


def test_only_the_scheduler_writes_the_response():
    """**응답을 쓰는 경로는 스케줄러 하나다** — 출력 축의 초크포인트다.

    입력에서 `pipeline.py` 가 잡 생성의 유일한 경로인 것과 같은 구조다. 다른
    모듈이 `response=` 를 직접 쓸 수 있으면 그 경로는 출력 가드를 지나지 않고,
    응답 필터는 "대부분의 경우에는 도는" 것이 된다 — 그건 필터가 아니다.

    `_succeed` 안에서 검사 → 봉인 → 저장이 한 덩어리라, 이 불변식이 지켜지는 한
    마스킹되지 않은 응답이 DB 에 들어가는 경로는 존재하지 않는다.
    """
    # **저장 호출만 본다.** `Submission(response=job.response)` 처럼 이미 마스킹된
    # 값을 읽어 응답 객체를 만드는 것은 대상이 아니다 — 문자열이나 인자 이름만으로
    # 훑으면 그것까지 걸려서, 장치가 시끄러워지고 시끄러운 장치는 결국 꺼진다.
    mutations = {"create_job", "update_job", "settle_job"}
    fields = {"response", "response_cipher", "response_nonce"}

    writers = []
    for path in SOURCES:
        if path.name in ("store.py", "scheduler.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in mutations:
                continue
            for keyword in node.keywords:
                if keyword.arg in fields:
                    writers.append(
                        f"{path.name}:{node.lineno} {node.func.attr}({keyword.arg}=)"
                    )

    assert not writers, f"스케줄러 밖에서 응답을 쓴다: {writers}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_nothing_serializes_with_ascii_escapes(path):
    """`json.dumps` 를 직접 부르지 않는다 — 스토어의 `_json()` 을 쓴다.

    기본값 `ensure_ascii=True` 는 한글을 `\\uXXXX` 로 바꾼다. 감사 로그가 사람이
    못 읽는 형태가 되고 저장 크기가 두 배가 된다.
    """
    if path.name == "store.py":
        return
    source = path.read_text(encoding="utf-8")
    offenders = [
        n + 1
        for n, line in enumerate(source.splitlines())
        if "json.dumps(" in line and "ensure_ascii" not in _call_window(source, n)
    ]
    assert not offenders, f"{path.name}:{offenders} 의 json.dumps 에 ensure_ascii=False 가 없다"


def test_data_boundary_is_a_node_property_not_a_provider_property():
    """`provider: ollama` 라고 로컬인 것이 아니다.

    임대 GPU 에 Ollama 를 올리면 소프트웨어는 같지만 프롬프트는 남의 기계로 나간다.
    경계를 프로바이더 능력에 걸면 "분류기는 내부 노드 전용" 보장이 그 순간 무너진다.
    """
    from app.providers.base import Capabilities

    fields = set(Capabilities.__dataclass_fields__)
    assert not {"data_boundary", "internal", "local"} & fields

    from app.config import Node

    assert "data_boundary" in Node.__dataclass_fields__


def test_unspecified_data_boundary_defaults_to_external():
    """미기재를 내부로 간주하면 실수가 **새는 쪽으로** 향한다."""
    from app.config import EXTERNAL, Node

    assert Node("x", "mock").data_boundary == EXTERNAL
    assert Node("x", "mock").is_internal is False


def test_model_deletion_has_no_force_escape_hatch():
    """차단 사유를 우회하는 손잡이가 있으면 차단이 아니다."""
    import inspect

    from app.models import ModelRegistrar

    assert "force" not in inspect.signature(ModelRegistrar.delete).parameters


def test_filter_events_cannot_record_the_matched_value():
    """**받을 수 있게 두면 언젠가 누군가 넣는다.**

    감사가 새 유출 경로가 되면 가드의 나머지 노력이 전부 무의미해진다.
    """
    import inspect

    from app.store import SqliteStore

    params = set(inspect.signature(SqliteStore.record_filter_event).parameters)
    assert not {"value", "match", "text", "matched", "sample"} & params


def test_frozen_role_fields_are_not_overridable():
    from app.config import FROZEN_ROLE_FIELDS, OVERRIDABLE_ROLE_FIELDS

    assert not FROZEN_ROLE_FIELDS & OVERRIDABLE_ROLE_FIELDS
    assert {"kind", "system", "internal_only"} <= FROZEN_ROLE_FIELDS


def test_schema_migrations_are_add_only():
    """재작성·삭제 마이그레이션은 구버전이 신버전 DB 를 못 읽게 만든다(전진 호환)."""
    from app.store import _MIGRATIONS

    for table, column, ddl in _MIGRATIONS:
        upper = ddl.upper()
        assert "NOT NULL" not in upper or "DEFAULT" in upper, (
            f"{table}.{column}: 기본값 없는 NOT NULL 은 기존 행을 깨뜨린다"
        )


# ── 문서 드리프트 ────────────────────────────────────────────────────────────


def test_the_architecture_doc_names_every_module():
    """**문서의 모듈 표도 손으로 관리하는 표다** — 반드시 어긋난다.

    실제로 어긋났다: 10~15단계에서 만든 모듈 7개가 1단계에 쓴 문서에 없었다.
    설계 문서가 코드의 절반만 설명하면 그 문서를 읽고 붙은 사람이 나머지 절반을
    모른 채 고치게 된다.
    """
    doc = (APP.parent / "docs" / "architecture.md").read_text(encoding="utf-8")
    modules = {
        path.stem for path in APP.glob("*.py")
        if path.stem not in ("__init__", "__main__")
    }
    missing = sorted(name for name in modules if f"{name}.py" not in doc)
    assert not missing, f"docs/architecture.md 에 없는 모듈: {missing}"


def test_the_doc_does_not_name_modules_that_no_longer_exist():
    """반대 방향도 본다 — 지운 모듈이 문서에 남으면 없는 코드를 찾게 된다."""
    import re

    doc = (APP.parent / "docs" / "architecture.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\b([a-z_]+)\.py\b", doc))
    # providers/ 하위 모듈과 테스트 파일은 이 검사 대상이 아니다.
    named -= {p.stem for p in (APP / "providers").glob("*.py")}
    named = {n for n in named if not n.startswith("test_")}

    ghosts = sorted(n for n in named if not (APP / f"{n}.py").exists())
    assert not ghosts, f"문서에만 있고 코드에 없는 모듈: {ghosts}"


def test_the_plan_is_marked_as_history_not_as_the_current_spec():
    """계획서는 **착수 시점의 기록**이지 현재 동작의 설명서가 아니다.

    계획서를 조용히 고쳐 놓으면 왜 달라졌는지가 사라진다 — 그래서 본문은 그대로
    두고 머리말에 차이를 모은다. 그 머리말이 사라지면 이 문서는 코드와 어긋나는
    또 하나의 손으로 관리하는 표가 되므로, 있는지 확인한다.
    """
    plan = (APP.parent / "docs" / "plan.md").read_text(encoding="utf-8")
    assert "착수 시점의 계획서다" in plan
    assert "구현하면서 계획과 달라진 것" in plan
    assert "계획했으나 하지 않은 것" in plan
    # 현재 동작의 권위가 어디인지 가리킨다.
    assert "architecture.md" in plan


def test_the_plan_body_is_the_original():
    """차이는 머리말에만 적고 본문은 손대지 않는다 — 본문이 곧 기록이다."""
    plan = (APP.parent / "docs" / "plan.md").read_text(encoding="utf-8")
    header, _, body = plan.partition("\n\n# LLM-ControlCenter")

    # 머리말은 전부 인용 블록이다. 인용을 벗어난 줄이 있으면 본문을 고친 것이다.
    stray = [
        line for line in header.splitlines()
        if line.strip() and not line.startswith(">")
    ]
    assert not stray, f"머리말이 인용 블록을 벗어났다: {stray[:3]}"
    assert body, "계획서 본문이 없다"


# ── 요청 입력 강제 ───────────────────────────────────────────────────────────


def test_handlers_do_not_coerce_request_numbers_directly():
    """**`int()` 의 `ValueError` 가 그대로 올라가면 400 이어야 할 것이 500 이 된다.**

    소비자는 "서버가 고장났다" 로 읽고 재시도하며, 실제로는 자기 요청이 틀린 것이다.
    오류 계약(§5.4)이 `retryable` 로 분기하라고 못박아 둔 만큼 이 구분이 중요하다.

    `_int()` · `_float()` 을 쓰면 같은 값이 `invalid_field` 400 으로 나간다.
    이것을 규율로 두면 다음 핸들러에서 다시 깨진다.
    """
    source = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()

    #: 요청에서 온 값을 나타내는 이름들. 상수·리터럴 변환은 대상이 아니다.
    REQUEST_NAMES = ("body", "params", "request", "raw")

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in ("int", "float") or not node.args:
            continue
        argument = ast.dump(node.args[0])
        if any(name in argument for name in REQUEST_NAMES):
            offenders.append(f"{node.lineno}: {lines[node.lineno - 1].strip()}")
    assert not offenders, "요청 값을 직접 형변환한다:\n" + "\n".join(offenders)


def test_partial_masking_cannot_keep_the_whole_value():
    """상한이 없으면 `keep_tail: 100` 이 **값 전체를 남기는 "마스킹"** 이 된다.

    관제 화면에는 마스킹 규칙으로 표시되면서. 안 켜진 필터보다 나쁜 것이
    켜져 있다고 표시되는 안 듣는 필터다.
    """
    from app.config import MAX_KEEP_TAIL

    assert 0 < MAX_KEEP_TAIL <= 8
    assert "MAX_KEEP_TAIL" in (APP / "main.py").read_text(encoding="utf-8")
