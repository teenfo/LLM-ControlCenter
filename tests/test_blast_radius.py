"""장애 반경 — `docs/topology.md` §7 표의 각 행을 고정한다.

예전 §7 은 서술문이었다. "컨트롤 플레인이 죽어도 큐는 DB 에 보존, 재기동 시 복구" —
읽으면 맞는 말 같은데 **틀렸다.** 등록 노드가 영속화되지 않던 시절에는 재기동하면
노드가 사라져 복구된 잡이 갈 곳이 없었다. 표는 그 사실을 드러내지 못했다.

서술문은 배신당해도 조용하다. 그래서 표의 각 행을 검증 가능한 한 문장으로 바꾸고
여기서 고정한다. 표가 코드와 어긋나면 `test_the_blast_radius_table_names_real_tests`
가 실패한다 — `test_coverage_map.py` 가 요구사항 목록에 대해 하는 일과 같다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.cluster import HEALTHY, PLACED, UNHEALTHY, Cluster
from app.config import EXTERNAL, INTERNAL, GuardRule, GuardSettings
from app.evals import KIND_CLASSIFIER
from app.guard import Guard
from app.store import TenantScope
from tests.conftest import seed_tenant
from tests.test_coverage_map import collect_test_names

ACME = TenantScope("acme")

DOCS = Path(__file__).resolve().parent.parent / "docs"


def place(harness, role_name: str, **kwargs):
    role = harness.config.roles[role_name]
    defaults = dict(
        job_id="j1", tenant_id="acme", service_id="acme-web", role=role,
        placement_snapshot=role.placement,
    )
    defaults.update(kwargs)
    return harness.cluster.place(**defaults)


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme")


# ── 컨트롤 플레인 ───────────────────────────────────────────────────────────


def test_registered_nodes_survive_a_restart(harness, acme):
    """재기동 후에도 **등록 노드 선언이 남아 있다.**

    이것이 §7 첫 행의 전제다. 노드가 사라지면 "큐는 DB 에 보존" 이 아무 의미가 없다 —
    복구된 잡이 갈 곳이 없기 때문이다. 관리자 입장에서는 증설한 노드가 왜 없어졌는지도
    알 수 없다.
    """
    harness.store.save_node(
        {
            "name": "in-3", "provider": "mock", "base_url": "http://in-3:11434",
            "data_boundary": "internal", "mem_budget_gb": 24.0, "max_concurrent": 2,
            "tags": ["internal"], "models": ["m"], "tenant_affinity": [],
            "enabled": True,
        },
        actor="platform_admin",
    )
    assert "in-3" not in harness.config.nodes, "전제가 틀렸다 — YAML 에 이미 있다"

    # 재기동을 흉내 낸다: 같은 설정·같은 DB 로 클러스터를 새로 만든다.
    restarted = Cluster(harness.config, harness.store, now=harness.clock)

    assert "in-3" in restarted.nodes, "관제 UI 로 등록한 노드가 재기동에 사라졌다"
    assert restarted.nodes["in-3"].node.is_internal


def test_running_jobs_are_recovered_not_lost(harness, acme):
    """재기동 시 `running` 이던 잡은 **재큐되거나 `needs_review` 로 남는다.**

    조용히 `running` 인 채로 남는 것이 최악이다 — 아무도 그 잡을 돌리지 않는데
    소비자에게는 진행 중으로 보인다.
    """
    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role="inside", lane="interactive",
        kind="generate", status="running", priority=0, prompt_masked="x",
    )
    harness.store.update_job(ACME, job_id, node="in-1")

    counts = harness.store.recover_running_jobs(harness.cluster.metered_nodes())

    assert counts["requeued"] == 1
    assert harness.store.get_job(ACME, job_id).status == "queued"


def test_metered_jobs_are_not_requeued_after_a_crash(harness, acme):
    """metered 노드에 있던 잡은 **자동 재큐되지 않는다** — 이중 과금을 막는다.

    노드에도 클라우드에도 취소 의미론이 없다. 컨트롤 플레인이 재시작하는 동안
    프로바이더는 여전히 추론을 돌리고 있고, 재큐하면 같은 작업이 두 번 돌고
    두 번 청구된다. 막을 수는 없으니 **드러낸다.**
    """
    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        kind="generate", status="running", priority=0, prompt_masked="x",
    )
    harness.store.update_job(ACME, job_id, node="out")

    metered = harness.cluster.metered_nodes()
    assert "out" in metered, "전제가 틀렸다 — out 이 metered 로 안 잡힌다"

    counts = harness.store.recover_running_jobs(metered)

    assert counts["needs_review"] == 1
    assert counts["requeued"] == 0
    assert harness.store.get_job(ACME, job_id).status == "needs_review"


# ── 노드 1대 ────────────────────────────────────────────────────────────────


def test_one_node_down_only_stalls_what_it_alone_hosted(harness, acme):
    """단독 호밍한 모델의 잡만 멈추고, **나머지는 남은 노드가 흡수한다.**

    `guard-m` 은 in-1 에만 있고 `m` 은 in-1·in-2 둘 다에 있다. in-1 이 죽었을 때
    둘의 운명이 갈리는 것이 이 행의 요지다 — 갈리지 않으면 노드를 늘린 의미가 없다.
    """
    assert "guard-m" not in harness.config.nodes["in-2"].models, "전제가 틀렸다"

    harness.cluster.nodes["in-1"].status = UNHEALTHY

    absorbed = place(harness, "inside")
    assert absorbed.outcome == PLACED, f"남은 노드가 흡수하지 못했다: {absorbed.reason}"
    assert absorbed.placement.node == "in-2"

    stalled = place(harness, "_guard_classify", job_id="j2")
    assert stalled.outcome != PLACED, "단독 호밍 모델이 없는 노드에 배치됐다"


def test_losing_external_does_not_stall_internal_roles(harness, acme):
    """external / 클라우드가 통째로 죽어도 **internal 경로는 영향을 받지 않는다.**"""
    harness.cluster.nodes["out"].status = UNHEALTHY

    inside = place(harness, "inside")
    assert inside.outcome == PLACED
    assert inside.placement.node.startswith("in-")

    # 양쪽 티어를 다 쓰는 역할도 internal 이 살아 있으면 그대로 돈다.
    both = place(harness, "summarize", job_id="j2")
    assert both.outcome == PLACED
    assert both.placement.tier == "internal"


# ── 노드망 전체 ─────────────────────────────────────────────────────────────


def test_internal_only_jobs_wait_rather_than_cross_the_boundary(harness, acme):
    """internal 이 전멸하면 `placement: [internal]` 잡은 **대기한다.**

    폴백이 아니라 대기인 것이 요점이다. 경계를 넘어 계속 도는 것은 가용성이 아니라
    유출이다 — 내부가 자는 밤에 민감한 프롬프트가 나가는 것이 정확히 이 실패다.
    """
    for name in ("in-1", "in-2"):
        harness.cluster.nodes[name].status = UNHEALTHY
    assert harness.cluster.nodes["out"].status == HEALTHY, "전제가 틀렸다"

    result = place(harness, "inside")

    assert result.outcome != PLACED
    assert result.placement is None, "경계 밖 노드로 넘어갔다"


async def test_no_internal_node_falls_through_to_the_classifier_error_policy(
    harness, acme
):
    """internal 전멸 시 가드 2단은 **판정을 만들지 않는다.**

    다른 테스트가 가짜로 실패하는 분류기를 넣어 정책 분기를 보는 반면, 여기서는
    **실제 배선**을 쓴다 — 내부 노드가 없을 때 파이프라인의 분류기가 정말로
    실패로 끝나는지가 이 행의 주장이기 때문이다. 배선이 어딘가에서 조용히
    "민감하지 않음" 으로 떨어지면 그건 가드가 꺼진 것과 같다.
    """
    harness.store.record_eval_run(KIND_CLASSIFIER, "guard-m", passed=30, total=30)
    classify = harness.pipeline.make_classifier()

    for name in ("in-1", "in-2"):
        harness.cluster.nodes[name].status = UNHEALTHY

    rules = (
        GuardRule(id="deal", kind="llm", action="block", description="인수합병 논의"),
    )
    config = harness.config.__class__(
        **{
            **harness.config.__dict__,
            "guard_rules": rules,
            "guard_settings": GuardSettings(on_classifier_error="mask"),
        }
    )
    verdict = await Guard(config, classifier=classify).inspect("아무 문장")

    assert verdict.classifier_failed is True, "판정을 못 했는데 실패로 안 남았다"
    assert EXTERNAL not in verdict.allowed_boundaries, "판정 못 했는데 밖으로 내보냈다"
    assert INTERNAL in verdict.allowed_boundaries


# ── 표 자체를 검사한다 ──────────────────────────────────────────────────────


def blast_radius_rows() -> list[tuple[str, str, str]]:
    """`docs/topology.md` §7 표의 데이터 행. (죽는 것, 보증, 테스트 이름)."""
    doc = (DOCS / "topology.md").read_text(encoding="utf-8")
    section = re.split(r"^## ", doc, flags=re.M)
    body = next((s for s in section if s.startswith("7. 장애 반경")), "")
    rows: list[tuple[str, str, str]] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or set(cells[0]) <= {"-", ":"}:
            continue
        if cells[0] == "죽는 것":
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def test_the_blast_radius_table_is_not_empty():
    """표를 못 찾으면 아래 검사가 전부 공허하게 통과한다 — 먼저 그것을 막는다."""
    rows = blast_radius_rows()
    assert len(rows) >= 5, f"§7 표를 못 읽었거나 행이 너무 적다: {rows}"


@pytest.mark.parametrize("row", blast_radius_rows(), ids=lambda r: r[0])
def test_the_blast_radius_table_names_real_tests(row):
    """**표의 모든 행이 실재하는 테스트를 지목한다.**

    이것이 없으면 §7 은 다시 서술문이 된다. 테스트 이름을 바꾸거나 지우면
    여기서 실패한다 — 표가 조용히 거짓말이 되는 경로를 막는 장치다.
    """
    failure, guarantee, cited = row
    names = re.findall(r"`(test_[a-z0-9_]+)`", cited)
    assert names, f"{failure} — 고정하는 테스트가 표에 없다 ({guarantee})"

    known = collect_test_names()
    missing = sorted(name for name in names if name not in known)
    assert not missing, f"{failure} — 존재하지 않는 테스트: {missing}"
