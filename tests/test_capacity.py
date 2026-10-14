"""용량 회귀 — **자릿수가 무너지면 실패한다.**

`docs/capacity.md` §6 은 이제 추정이 아니라 실측이다. 그런데 실측값을 문서에 적어
두기만 하면 다음 커밋이 그것을 조용히 무너뜨린다. 실제로 그런 결함이 둘 있었고
둘 다 부하 측정으로만 보였다:

- 큐 위치 계산이 후보 행을 최대 500건 통째로 읽어서, 폴 원가가 큐 깊이에 **선형으로**
  붙었다(깊이 1,000 에서 39배). 적응형 `retry_after` 가 damp 하려던 피드백 루프를
  그 계산이 증폭하고 있었다.
- 텍스트 정규화가 규칙 루프 **안**에 있어서 같은 문자열을 규칙 수만큼 정규화했다.
  200KB 프롬프트 한 건에서 25ms 가 그렇게 샜다.

### 시간을 재지 않는다

CI 머신의 부하는 우리가 모른다. 절대 시간에 임계를 걸면 느린 러너에서 깜빡이고,
깜빡이는 장치는 결국 꺼진다 — 안 켜진 테스트는 없는 테스트다. 그래서 두 결함을
**시간이 아닌 성질**로 못박는다: 정규화 횟수는 규칙 수와 무관해야 하고, 큐 위치
계산은 행을 읽지 않아야 한다. 둘 다 위 결함이 있으면 반드시 실패하고, 머신이
느리다고 실패하지는 않는다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.config import load_config
from app.store import TenantScope
from tests.conftest import seed_tenant

ACME = TenantScope("acme")
APP = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme")


@pytest.fixture(scope="module")
def shipped():
    """**번들 설정으로 잰다.** 합성 설정은 규칙이 몇 개 없어서, 규칙 수에 비례하는
    결함이 바로 그 합성 설정에서는 안 보인다."""
    return load_config("config")


# ── 정규화는 텍스트당 한 번 ─────────────────────────────────────────────────


def test_normalization_does_not_run_once_per_rule(monkeypatch, shipped):
    """**규칙을 늘리면 정규화도 늘어나는가.** 늘어나면 안 된다.

    정규화는 텍스트의 성질이지 규칙의 성질이 아니다. 규칙 루프 안에 있으면 규칙
    하나를 추가할 때마다 큰 프롬프트 전체를 한 번 더 훑는 비용이 붙고, 그 대가는
    규칙을 늘릴수록 커진다 — 즉 **제품을 개선할수록 느려진다.**
    """
    import app.guard as guard_module
    from app.guard import Guard

    guard = Guard(shipped)
    rules = [r for r in guard.rules_for(("ko-KR",), ()) if r.kind == "pattern"]
    assert len(rules) >= 5, "규칙이 너무 적어 이 검사가 의미를 잃는다"

    calls = []
    original = guard_module.normalize_for_match
    monkeypatch.setattr(
        guard_module,
        "normalize_for_match",
        lambda text: (calls.append(len(text)), original(text))[1],
    )

    guard._scan("전화 010-1234-5678 로 연락 주세요", "시스템 지시", rules)

    assert len(calls) <= 2, (
        f"규칙 {len(rules)}개에 정규화가 {len(calls)}회 돌았다 — "
        "프롬프트와 system 각각 한 번이어야 한다"
    )


def test_the_scan_normalizes_outside_the_rule_loop():
    """위 테스트를 구조로도 못박는다 — 호출 횟수는 규칙이 0개면 못 잰다."""
    tree = ast.parse((APP / "guard.py").read_text(encoding="utf-8"))
    scan = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_scan"
    )
    inside_loop = [
        node.lineno
        for loop in ast.walk(scan) if isinstance(loop, ast.For)
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "normalize_for_match"
    ]
    assert not inside_loop, (
        f"_scan 의 규칙 루프 안에서 정규화한다 (줄 {inside_loop}) — "
        "같은 텍스트를 규칙 수만큼 다시 훑는다"
    )


# ── 큐 위치는 행을 읽지 않는다 ──────────────────────────────────────────────


def test_the_queue_position_counts_instead_of_reading_rows():
    """**폴 원가가 큐 깊이에 붙으면 폴링 파국 경로가 스스로 증폭된다.**

    이것이 `capacity.md` §6.2-a 가 막으려는 바로 그 루프다. 큐가 깊어질수록 폴이
    비싸지면, 큐를 깊게 만든 포화가 폴 부하를 키우고 그 부하가 다시 포화를 키운다.
    """
    source = (APP / "store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "queue_position"
    )
    sql = " ".join(
        node.value.lower()
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )

    assert "count(" in sql, "큐 위치가 집계 대신 행을 읽는다"
    assert "limit" not in sql, (
        "큐 위치에 LIMIT 이 있다 — 행을 세는 게 아니라 가져오고 있다"
    )


def test_the_poll_does_not_materialise_the_queue(harness, acme):
    """성질을 실제로도 확인한다 — 구조 검사만 두면 SQL 을 바꿔 놓고 통과할 수 있다.

    시간이 아니라 **파이썬으로 끌어올린 행 수**를 센다. `row_factory` 는 행 하나가
    객체가 될 때마다 정확히 한 번 불리므로, 그 횟수가 큐 깊이를 따라 늘면 이 함수는
    큐를 통째로 읽고 있는 것이다. 머신 속도와 무관하게 결정적이다.
    """
    conn = harness.store._conn

    def fill(depth: int) -> None:
        while harness.store.count_queued("interactive") < depth:
            harness.store.create_job(
                ACME, service_id="acme-web", role="summarize", lane="interactive",
                prompt_masked="채우기" * 40, created_at=harness.clock.now,
            )

    def rows_read() -> int:
        original = conn.row_factory
        count = 0

        def counting(cursor, row):
            nonlocal count
            count += 1
            return original(cursor, row)

        conn.row_factory = counting
        try:
            # **채운 잡보다 뒤에 선 잡으로 물어야 한다.** 같은 `created_at` 으로
            # 물으면 앞선 잡이 0건이라 어떤 구현이든 행을 안 읽고, 장치가 조용히
            # 아무것도 안 재게 된다.
            ahead = harness.store.queue_position(
                ACME, lane="interactive", priority=0,
                created_at=harness.clock.now + 1, job_id="없는-잡",
            )
        finally:
            conn.row_factory = original
        assert ahead > 0, "앞선 잡이 0건이면 이 측정은 아무것도 재지 않는다"
        return count

    fill(20)
    shallow = rows_read()
    fill(400)
    deep = rows_read()

    assert deep <= shallow + 1, (
        f"큐 깊이 20 에서 {shallow}행, 400 에서 {deep}행을 읽었다 — "
        "폴 원가가 큐 깊이에 붙어 있다"
    )
    assert harness.store.queue_position(
        ACME, lane="interactive", priority=0,
        created_at=harness.clock.now + 1, job_id="없는-잡",
    ) >= 400, "깊이가 늘었는데 큐 위치가 따라오지 않았다"


# ── 문서가 실측을 인용하고 있는가 ───────────────────────────────────────────


def test_the_capacity_doc_no_longer_calls_the_numbers_estimates():
    """**드리프트 장치.** 실측을 넣고도 "추정" 이라 적혀 있으면 아무도 안 믿는다."""
    doc = (APP.parent / "docs" / "capacity.md").read_text(encoding="utf-8")

    assert "부하 테스트로 검증했다" in doc, "capacity.md 가 아직 추정이라고 말한다"
    assert "app.loadtest" in doc, "실측을 재현할 도구를 문서가 안 가리킨다"


def test_the_debt_table_records_that_offload_does_not_defend():
    """오프로드가 머리 막힘을 막는다는 주장은 실측으로 기각됐다 — 표가 그걸 알아야 한다.

    안 적으면 다음 사람이 `stage1_threadpool_threshold_bytes` 를 조정하며 큰
    프롬프트의 정지를 고치려 든다. 그 손잡이는 그 일을 하지 않는다.
    """
    readme = (APP.parent / "README.md").read_text(encoding="utf-8")
    row = next(
        (line for line in readme.splitlines() if "이벤트 루프를 세운다" in line), ""
    )

    assert row, "README 부채 표에 큰 프롬프트 항목이 없다"
    assert "max_prompt_chars" in row, "실효 방어가 무엇인지 안 적혀 있다"
    assert "처리량 수치는 추정" not in readme, (
        "부하 테스트를 했는데 부채 표는 아직 추정이라고 말한다"
    )


# ── 스캔 창의 원가가 프롬프트 크기를 따라가면 안 된다 ────────────────────────


def test_the_scan_window_does_not_grow_with_prompt_size():
    """**세 번째 결함이 같은 모양이었다.**

    비용 상한을 재려면 텍스트가 필요했고, 그래서 스케줄러가 매 틱 스캔 창의
    프롬프트를 통째로 읽었다 — 200KB 프롬프트 51건에서 그것만 60ms 다.
    지금은 제출 시 잰 숫자 한 칸(`jobs.input_tokens_estimate`)을 읽는다.

    위 두 장치와 같은 이유로 **시간을 재지 않는다.** 스캔 창이 물어 오는
    바이트가 프롬프트 크기와 무관해야 한다는 성질로 못박는다 — 느린 러너에서
    깜빡이지 않고, 텍스트를 다시 읽기 시작하면 반드시 실패한다.
    """
    from app.store import SqliteStore, TenantScope

    def window_bytes(chars: int) -> int:
        store = SqliteStore(":memory:")
        store.create_tenant("acme", name="Acme", end_user_salt="s")
        store.create_service(TenantScope("acme"), service_id="web", name="web")
        for _ in range(20):
            store.create_job(
                TenantScope("acme"), service_id="web", role="summarize",
                lane="interactive", prompt_masked="가" * chars,
                prompt_external="가" * chars, system_masked="요약한다",
            )
        rows = store.claim_queued("interactive", limit=20)
        assert len(rows) == 20
        total = sum(
            len(value)
            for row in rows
            for value in vars(row).values()
            if isinstance(value, (str, bytes))
        )
        store.close()
        return total

    small = window_bytes(100)
    large = window_bytes(100_000)

    # 1,000배 긴 프롬프트인데 스캔 창이 물어 오는 양은 그대로여야 한다.
    assert large == small, (
        f"스캔 창이 프롬프트를 따라 커진다 ({small} → {large} 바이트) — "
        "claim_queued 가 본문을 다시 읽고 있다"
    )
