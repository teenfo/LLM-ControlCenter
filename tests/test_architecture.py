"""구조 불변식 — 규율이 아니라 장치로 지킨다.

이 파일의 테스트는 전부 같은 형태다. 어떤 규칙을 주석에만 적어 두면 언젠가 누군가
어긴다. 어겼을 때 **테스트가 실패하면** 그때 알게 된다.

§13-8("손으로 관리하는 표는 반드시 어긋난다 — 실제로 어긋났다")과 같은 발상이며,
`test_meta.py::test_every_route_has_a_summary` 가 계약 문서에 대해 하는 일을
여기서는 아키텍처에 대해 한다.
"""

from __future__ import annotations

import ast
import re
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


# ── 재귀 방지 — 출처 표식이 붙는 자리와, 그것을 읽는 자리 ─────────────────────


def test_the_job_creating_call_stamps_the_origin():
    """**잡을 만드는 그 한 줄이 출처를 반드시 정한다.**

    `store.create_job` 의 `origin_plugin` 은 기본값이 `None` 이고, 그 기본값은
    편의가 아니라 **"사람이 만들었다" 는 주장**이다. 제품 경로가 그 주장을 우연히
    하게 두면, 새로 생긴 제출 경로가 만든 잡이 플러그인을 깨울 수 있게 된다 —
    막으려는 고리가 정확히 그것이다.

    런타임에 필수 인자로 막지 않는 이유: 잡을 만드는 곳은
    `test_only_the_pipeline_creates_jobs` 가 이미 하나로 묶어 두었고, 그 하나를
    여기서 보면 된다. 필수 인자로 바꾸면 잡을 직접 만드는 테스트 43곳이 전부
    이 칸을 적어야 하는데, 그 잡음은 장치를 지키는 게 아니라 흐리게 만든다.
    """
    calls = []
    for path in SOURCES:
        if path.name == "store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "create_job":
                    calls.append((path.name, node))

    assert len(calls) == 1, f"잡을 만드는 호출이 하나가 아니다: {[n for n, _ in calls]}"
    name, node = calls[0]
    stamped = {kw.arg for kw in node.keywords}
    assert "origin_plugin" in stamped, f"{name} 의 잡 생성이 출처를 안 남긴다"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_only_one_place_decides_what_the_origin_means(path):
    """**출처 칸을 읽고 판정하는 곳은 `plugins.may_wake_plugins` 하나다.**

    이 판정을 묻는 곳은 이벤트 트리거(`plugins.pull_events`)다. 다음 트리거를 짜는
    사람이 `job["origin_plugin"]` 을 직접 읽어 자기 규칙을 세우면 여기서 실패한다 —
    그때 규칙이 두 벌이 되고, 둘은 반드시 어긋난다.

    허용하는 세 곳은 역할이 각각 다르다:
      · `store.py`    — 스키마·마이그레이션·질의 (칸 자체)
      · `pipeline.py` — 잡을 만들 때 붙이는 표식 (쓰기)
      · `plugins.py`  — 그 표식이 무엇을 뜻하는지의 판정 (읽기)
    """
    if path.name in ("store.py", "pipeline.py", "plugins.py"):
        return
    source = path.read_text(encoding="utf-8")
    assert "origin_plugin" not in source, (
        f"{path.name} 이 출처를 직접 해석한다 — plugins.may_wake_plugins 를 쓸 것"
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_only_one_place_decides_whether_a_service_is_switched_on(path):
    """**꺼진 서비스를 거르는 판정은 `auth.active_service` 하나다.**

    플러그인 토글의 실체가 `services.status` 라서, 이 판정이 두 곳에 있으면 그
    둘은 반드시 어긋나고 그때 "껐는데 왜 도느냐" 가 된다. 실제로 그렇게 될 뻔했다 —
    스케줄 클레임(`/v1/plugin/tick`)은 제출 경로를 안 지나므로, 각자 상태를 읽었으면
    "제출은 막히는데 스케줄은 도는" 플러그인이 생겼다.

    `tenant["status"]` 는 대상이 아니다. 테넌트 판정은 `authenticate` 안에 있고
    토글과 무관하다 — 한 검사에 둘을 섞으면 어느 쪽이 깨졌는지 못 읽는다.
    """
    if path.name == "auth.py":
        return
    source = path.read_text(encoding="utf-8")
    for pattern in ('service["status"]', "service.get(\"status\")"):
        assert pattern not in source, (
            f"{path.name} 이 서비스 상태를 직접 판정한다 — auth.active_service 를 쓸 것"
        )


# ── 이벤트 아웃박스 — 쓰는 곳은 DB 트리거 하나, 내주기 전에 판정을 묻는다 ────────


def test_only_the_finish_trigger_writes_plugin_events():
    """**잡을 종결시키는 경로가 여덟 개 남짓이라, 파이썬에서 하나씩 삽입하면 반드시 하나가 빠진다.**

    정산·실패·취소·크래시 복구·동기 임베딩·종결 실패 — 저마다 다른 함수에서 `status`
    를 종결로 바꾼다. 그래서 아웃박스는 `jobs.status` 의 전이를 보는 SQLite 트리거가
    쓴다: 새 종결 경로가 생겨도 빠지지 않고, 종결과 같은 트랜잭션이다. 누군가 편의로
    파이썬에서 직접 넣기 시작하면 쓰는 경로가 둘이 되고, 그때부터 "종결은 한 번인데
    이벤트가 둘" 이 가능해진다.
    """
    needle = re.compile(r"INSERT\s+INTO\s+plugin_events", re.I)
    writers = [
        path.name for path in SOURCES if needle.search(path.read_text(encoding="utf-8"))
    ]
    assert writers == ["store.py"], f"트리거 정의 말고도 이벤트를 쓰는 파일이 있다: {writers}"

    source = (APP / "store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    [trigger_fn] = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_finish_trigger_sql"
    ]
    assert len(needle.findall(ast.get_source_segment(source, trigger_fn))) == 1
    assert len(needle.findall(source)) == 1, "store.py 안에서도 트리거 정의 밖에서 이벤트를 쓴다"


def test_the_event_trigger_asks_the_recursion_judgment():
    """`pull_events` 는 내주기 전에 `may_wake_plugins` 를 묻는다.

    안 물으면 플러그인이 만든 잡의 종결이 플러그인을 다시 깨우고, 그 고리는 예산이
    다 탈 때까지 안 멈춘다. 위의 검사는 판정을 **다른 곳에서** 하지 못하게 막고,
    이 검사는 판정을 **아예 안 하는** 트리거를 막는다 — 둘은 다른 실패다.
    """
    source = (APP / "plugins.py").read_text(encoding="utf-8")
    [pull] = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "pull_events"
    ]
    asked = {
        node.func.id for node in ast.walk(pull)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "may_wake_plugins" in asked, "이벤트 트리거가 재귀 방지 판정을 안 묻는다"


def test_only_hash_password_writes_password_hashes():
    """`accounts.password_hash` 에 들어가는 값은 전부 `hash_password(...)` 의 결과다.

    평문이나 다른 해시가 들어가는 호출이 하나라도 생기면 로그인이 조용히 영영 실패하거나
    — 더 나쁘게 — 평문이 표에 남는다. 스토어 메서드 둘의 인자를 AST 로 본다.
    """
    checked = 0
    offenders = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("create_account", "set_account_password"):
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                continue  # 스토어 자신의 내부 호출은 해시를 만들지 않는다
            value = next((kw.value for kw in node.keywords if kw.arg == "password_hash"), None)
            if value is None and node.func.attr == "set_account_password" and len(node.args) >= 2:
                value = node.args[1]
            checked += 1
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "hash_password"
            ):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert checked >= 3, "계정 해시를 쓰는 호출을 하나도 못 읽었다 — 형식이 바뀌었다"
    assert not offenders, f"hash_password 를 거치지 않고 해시 칸을 쓰는 호출: {offenders}"


def test_both_submit_paths_force_the_account_end_user():
    """계정 세션의 end_user 는 `_authorize` **앞**에서 정해진다 — 뒤면 한도·해시가 남의 이름으로 걸린다.

    generate·embed·chat 세 경로가 따로 있으므로 셋 다 본다. 하나만 고치면 그 경로가 사칭 경로가 된다.
    """
    tree = ast.parse((APP / "pipeline.py").read_text(encoding="utf-8"))
    checked = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in ("submit", "embed", "chat"):
            continue
        calls = [
            (sub.lineno, sub.func.attr)
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in ("_effective_end_user", "_authorize")
        ]
        forced = [line for line, name in calls if name == "_effective_end_user"]
        authorized = [line for line, name in calls if name == "_authorize"]
        assert forced and authorized, f"{node.name} 에 두 호출이 다 있어야 한다"
        assert min(forced) < min(authorized), f"{node.name}: end_user 강제가 _authorize 뒤에 있다"
        checked.add(node.name)
    assert checked == {"submit", "embed", "chat"}
