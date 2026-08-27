"""파이프라인 — 순서 강제 · 2단 분류기 배선 · 동기 임베딩의 비용 정산.

`test_api.py` 가 HTTP 계약을 본다면 여기는 **관문 자체**를 본다.
"""

from __future__ import annotations

import pytest

from app.auth import Principal
from app.config import EXTERNAL, INTERNAL, GuardRule
from app.evals import KIND_CLASSIFIER
from app.guard import Guard
from app.i18n import ApiError
from app.pipeline import (
    GUARD_ROLE,
    Pipeline,
    _classification_prompt,
    _parse_classification,
    is_public_role,
)
from app.store import TenantScope
from tests.conftest import auth, seed_tenant

VALID_RRN = "990101-1234563"
VALID_CARD = "4111 1111 1111 1111"


def principal_for(tokens, role="service", tenant="acme") -> Principal:
    return Principal(
        tenant_id=tenant, service_id=tokens["service_id"], token_id="t", role=role
    )


@pytest.fixture
def acme_principal(harness):
    tokens = seed_tenant(harness, "acme")
    return principal_for(tokens)


# ── 순서 ────────────────────────────────────────────────────────────────────


async def test_guard_runs_before_the_job_row_exists(harness, acme_principal):
    """②가 ③보다 먼저다. 차단이면 잡 행 자체가 안 생긴다."""
    with pytest.raises(ApiError) as exc:
        await harness.pipeline.submit(
            acme_principal, role="summarize", prompt=f"주민 {VALID_RRN}", wait=0
        )
    assert exc.value.code == "guard_blocked"
    assert harness.store._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


async def test_job_carries_the_narrowed_boundaries_into_placement(harness, acme_principal):
    """③이 ④에 넘기는 것이 축소된 허용 경계다 — 여기서 안전이 교집합이 된다."""
    harness.store.set_tenant_guard_rule(
        TenantScope("acme"),
        {"id": "card", "kind": "pattern", "action": {"internal": "audit", "external": "block"},
         "pattern": r"\b(?:\d[ -]?){13,19}\b", "checksum": "luhn", "keep_tail": 4},
    )
    result = await harness.pipeline.submit(
        acme_principal, role="summarize", prompt=f"카드 {VALID_CARD}", wait=0
    )
    job = harness.store.get_job(TenantScope("acme"), result.job_id)
    assert job.allowed_boundaries == ("internal",)


async def test_narrowed_job_never_lands_on_an_external_node(harness, acme_principal):
    """경계가 좁혀진 잡은 배치 필터에서 external 노드를 통과하지 못한다."""
    harness.store.set_tenant_guard_rule(
        TenantScope("acme"),
        {"id": "card", "kind": "pattern", "action": {"internal": "audit", "external": "block"},
         "pattern": r"\b(?:\d[ -]?){13,19}\b", "checksum": "luhn", "keep_tail": 4},
    )
    result = await harness.pipeline.submit(
        acme_principal, role="summarize", prompt=f"카드 {VALID_CARD}", wait=0
    )
    # 내부 노드를 전부 내려도 external 로 넘어가지 않고 대기한다.
    harness.cluster.drain("in-1")
    harness.cluster.drain("in-2")
    for _ in range(3):
        await harness.scheduler.tick("interactive")

    job = harness.store.get_job(TenantScope("acme"), result.job_id)
    assert job.node is None
    assert job.status == "queued"


async def test_internal_only_role_is_never_placed_externally(harness, acme_principal):
    role = harness.config.roles["_guard_classify"]
    result = harness.cluster.place(
        job_id="x", tenant_id="acme", service_id="acme-web", role=role,
        placement_snapshot=("internal", "external"),
        allowed_boundaries=(INTERNAL, EXTERNAL),
    )
    assert result.outcome == "placed"
    assert harness.cluster.state(result.placement.node).node.is_internal


# ── 저장 ────────────────────────────────────────────────────────────────────


async def test_prompt_hash_is_salted_per_tenant(harness):
    """같은 주민번호를 다른 테넌트가 넣어도 해시가 다르다."""
    a = principal_for(seed_tenant(harness, "acme"), tenant="acme")
    b = principal_for(seed_tenant(harness, "globex"), tenant="globex")

    ra = await harness.pipeline.submit(a, role="summarize", prompt="카드 정보", wait=0)
    rb = await harness.pipeline.submit(b, role="summarize", prompt="카드 정보", wait=0)

    ja = harness.store.get_job(TenantScope("acme"), ra.job_id)
    jb = harness.store.get_job(TenantScope("globex"), rb.job_id)
    assert ja.prompt_hash and jb.prompt_hash
    assert ja.prompt_hash != jb.prompt_hash


async def test_prompt_hash_is_computed_after_masking(harness, acme_principal):
    """원문 그대로 해싱하면 탐색 공간이 좁은 값이 전수조사로 복원된다."""
    from app.identity import hash_prompt

    prompt = "메일 hong@example.com"
    result = await harness.pipeline.submit(
        acme_principal, role="summarize", prompt=prompt, wait=0
    )
    scope = TenantScope("acme")
    job = harness.store.get_job(scope, result.job_id)
    salt = harness.store.get_tenant("acme")["end_user_salt"]

    assert job.prompt_hash == hash_prompt(job.prompt_masked, salt)
    assert job.prompt_hash != hash_prompt(prompt, salt)


async def test_system_hash_is_unsalted_so_it_compares_across_tenants(harness):
    """프롬프트 전략의 버전이다 — 테넌트를 가로질러 비교할 수 있어야 한다."""
    a = principal_for(seed_tenant(harness, "acme"), tenant="acme")
    b = principal_for(seed_tenant(harness, "globex"), tenant="globex")

    ra = await harness.pipeline.submit(a, role="summarize", prompt="안녕", wait=0)
    rb = await harness.pipeline.submit(b, role="summarize", prompt="안녕", wait=0)

    ja = harness.store.get_job(TenantScope("acme"), ra.job_id)
    jb = harness.store.get_job(TenantScope("globex"), rb.job_id)
    assert ja.system_hash == jb.system_hash


async def test_system_hash_changes_when_the_request_overrides_it(harness, acme_principal):
    """프롬프트는 호출자 소유다 — 요청의 system 이 역할 기본값을 대체한다."""
    default = await harness.pipeline.submit(
        acme_principal, role="summarize", prompt="안녕", wait=0
    )
    custom = await harness.pipeline.submit(
        acme_principal, role="summarize", prompt="안녕", system="다르게 요약한다", wait=0
    )
    scope = TenantScope("acme")
    assert (
        harness.store.get_job(scope, default.job_id).system_hash
        != harness.store.get_job(scope, custom.job_id).system_hash
    )


async def test_no_ciphertext_is_written_without_a_key(harness, config, store, clock):
    """**KEK 부재 시 원문이 어떤 컬럼에도 평문으로 안 남는다.** 평문 폴백은 없다."""
    from app.crypto import KeyVault

    keyless = Pipeline(
        config, store, harness.cluster, harness.guard, vault=KeyVault(None), now=clock
    )
    tokens = seed_tenant(harness, "acme")
    result = await keyless.submit(
        principal_for(tokens), role="summarize", prompt="메일 hong@example.com", wait=0
    )
    job = store.get_job(TenantScope("acme"), result.job_id)
    assert job.prompt_cipher is None and job.prompt_nonce is None
    assert "hong@example.com" not in (job.prompt_masked or "")


# ── 동기 임베딩 ──────────────────────────────────────────────────────────────


async def test_embed_returns_one_vector_per_input(harness, acme_principal):
    result = await harness.pipeline.embed(
        acme_principal, role="vec", inputs=["가", "나", "다"]
    )
    assert len(result["vectors"]) == 3


async def test_embed_masks_each_input_independently(harness, acme_principal):
    """이어 붙여 한 번에 검사하면 마스킹이 길이를 바꿔 되쪼갤 수 없다."""
    result = await harness.pipeline.embed(
        acme_principal, role="vec", inputs=["평범한 문장", "메일 hong@example.com"]
    )
    assert len(result["vectors"]) == 2

    job = harness.store.get_job(TenantScope("acme"), result["job_id"])
    assert "hong@example.com" not in (job.prompt_masked or "")


async def test_embed_settles_cost_and_records_usage(harness, acme_principal):
    """큐만 우회하고 비용은 우회하지 않는다."""
    result = await harness.pipeline.embed(acme_principal, role="vec", inputs=["가"])
    scope = TenantScope("acme")

    job = harness.store.get_job(scope, result["job_id"])
    assert job.status == "ok"
    assert job.cost_reserved_usd == 0.0   # 예약이 남으면 예산이 영원히 묶인다

    rows = harness.store.usage_summary(scope, since=0, group_by="role")
    assert [r["key"] for r in rows] == ["vec"]


async def test_embed_releases_the_slot_even_on_failure(harness, acme_principal):
    provider = harness.cluster.provider_for("in-1")
    other = harness.cluster.provider_for("in-2")
    provider.kill()
    other.kill()

    with pytest.raises(Exception):
        await harness.pipeline.embed(acme_principal, role="vec", inputs=["가"])

    assert harness.cluster.state("in-1").running == 0
    assert harness.cluster.state("in-2").running == 0


async def test_embed_frees_the_reservation_when_placement_fails(harness, acme_principal):
    harness.cluster.drain("in-1")
    harness.cluster.drain("in-2")
    with pytest.raises(ApiError) as exc:
        await harness.pipeline.embed(acme_principal, role="vec", inputs=["가"])
    assert exc.value.retryable is True

    scope = TenantScope("acme")
    assert harness.store.reserved_cost(scope) == 0.0


# ── 취소 ────────────────────────────────────────────────────────────────────


async def test_cancel_releases_the_cost_reservation(harness, acme_principal):
    result = await harness.pipeline.submit(
        acme_principal, role="summarize", prompt="안녕", wait=0
    )
    scope = TenantScope("acme")
    harness.store.update_job(scope, result.job_id, cost_reserved_usd=1.5)

    harness.pipeline.cancel(scope, result.job_id, actor="tester")
    assert harness.store.reserved_cost(scope) == 0.0


# ── 2단 분류기 ───────────────────────────────────────────────────────────────


def certify(harness, model="guard-m", *, rate=1.0) -> None:
    total = 30
    harness.store.record_eval_run(
        KIND_CLASSIFIER, model, passed=int(total * rate), total=total,
        metrics={"rate": rate},
    )


async def test_classifier_runs_only_on_internal_nodes(harness, acme_principal):
    """**분류기가 원문을 밖으로 보내는 경로가 구조적으로 없다.**"""
    certify(harness)
    classify = harness.pipeline.make_classifier()
    rules = (GuardRule(id="deal", kind="llm", action="block", description="인수합병 논의"),)

    await classify("우리 회사가 인수된다", rules)

    calls = [
        call
        for name in ("in-1", "in-2", "out")
        for call in harness.cluster.provider_for(name).call_log
        if call.get("op") == "generate"
    ]
    assert calls
    assert not harness.cluster.provider_for("out").call_log


async def test_uncertified_classifier_model_is_refused(harness):
    """구조화 출력 준수율을 통과하지 못한 모델로 보안 판정을 하지 않는다."""
    classify = harness.pipeline.make_classifier()
    rules = (GuardRule(id="deal", kind="llm", action="block", description="인수합병"),)
    with pytest.raises(RuntimeError, match="인증"):
        await classify("아무 말", rules)


async def test_noncompliant_classifier_model_is_refused(harness):
    certify(harness, rate=0.5)
    classify = harness.pipeline.make_classifier()
    rules = (GuardRule(id="deal", kind="llm", action="block", description="인수합병"),)
    with pytest.raises(RuntimeError, match="인증"):
        await classify("아무 말", rules)


async def test_classifier_failure_is_not_a_verdict(harness, config, store, clock):
    """**분류 실패는 "민감하지 않음" 이 아니다** — `on_classifier_error` 를 탄다."""

    async def broken(text, rules):
        raise RuntimeError("모델이 죽었다")

    rules = (
        GuardRule(id="deal", kind="llm", action="block", description="인수합병 논의"),
    )
    guard = Guard(
        config.__class__(**{**config.__dict__, "guard_rules": rules}), classifier=broken
    )
    verdict = await guard.inspect("아무 말", candidate_boundaries=(INTERNAL, EXTERNAL))
    assert verdict.classifier_failed is True
    # 기본 정책은 mask 다 — 통과시키되 그 사실을 남긴다.
    assert verdict.allowed_boundaries


async def test_classifier_releases_its_slot(harness):
    certify(harness)
    classify = harness.pipeline.make_classifier()
    rules = (GuardRule(id="deal", kind="llm", action="block", description="인수합병"),)
    await classify("문장", rules)
    assert all(s.running == 0 for s in harness.cluster.nodes.values())


def test_build_app_wires_the_classifier(config, store, clock):
    """**안 꽂으면 맥락 규칙이 조용히 아무것도 안 한다.**

    배선이 원형이라 생성자에서 묶이지 않으므로, 조립부가 빠뜨리기 가장 쉬운 지점이다.
    """
    from app.main import build_app

    guard = Guard(config)
    assert not guard.has_classifier
    build_app(config=config, store=store, guard=guard, now=clock)
    assert guard.has_classifier


def test_build_app_keeps_an_injected_classifier(config, store, clock):
    from app.main import build_app

    async def mine(text, rules):
        return set()

    guard = Guard(config, classifier=mine)
    build_app(config=config, store=store, guard=guard, now=clock)
    assert guard._classifier is mine


async def test_stage_two_hit_masks_through_the_whole_pipeline(harness, config, store, clock):
    """맥락 규칙이 1단 패턴과 같은 경로로 마스킹까지 이어지는지."""

    async def classify(text, rules):
        return {"deal"} if "인수" in text else set()

    rules = (*config.guard_rules, GuardRule(
        id="deal", kind="llm", action="block", label="인수합병", description="인수합병 논의",
    ))
    tightened = type(config)(**{**config.__dict__, "guard_rules": rules})
    guard = Guard(tightened, classifier=classify)
    pipeline = Pipeline(
        tightened, store, harness.cluster, guard, vault=harness.vault, now=clock
    )
    principal = principal_for(seed_tenant(harness, "acme"))

    with pytest.raises(ApiError) as exc:
        await pipeline.submit(principal, role="summarize", prompt="우리 회사 인수 건", wait=0)
    assert exc.value.code == "guard_blocked"
    assert "deal" in exc.value.params["rules"]

    ok = await pipeline.submit(principal, role="summarize", prompt="평범한 문장", wait=0)
    assert ok.status == "pending"


async def test_classifier_with_no_rules_asks_nothing(harness):
    """물어볼 맥락이 없으면 모델을 부르지 않는다 — 공짜 호출이 아니다."""
    certify(harness)
    classify = harness.pipeline.make_classifier()
    assert await classify("문장", ()) == set()
    assert not harness.cluster.provider_for("in-1").call_log


# ── 모델 출력 흡수 ───────────────────────────────────────────────────────────


RULES = (
    GuardRule(id="deal", kind="llm", action="block", description="인수합병"),
    GuardRule(id="hr", kind="llm", action="audit", description="인사"),
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("deal", {"deal"}),
        ("deal, hr", {"deal", "hr"}),
        ("deal,hr", {"deal", "hr"}),
        ("답: deal 입니다.", {"deal"}),          # 산문에 섞여도 읽는다
        ("- deal, - hr", {"deal", "hr"}),        # 불릿
        ('["deal"]', {"deal"}),                  # JSON 흉내
        ("NONE", set()),
        ("아무말 대잔치", set()),
        ("", set()),
        ("   \n  \n", set()),
    ],
)
def test_classification_parsing_absorbs_formatting(raw, expected):
    """형식이 흔들려도 판정이 흔들리면 안 된다(§15-8)."""
    assert _parse_classification(raw, RULES) == expected


def test_classification_reads_only_the_last_line():
    """**맥락 카탈로그를 되읊는 출력에서 모든 규칙이 걸리면 안 된다.**

    오탐이 쏟아지면 관리자가 규칙을 꺼버리고, 안 켜진 필터는 없는 필터다.
    """
    echoed = "[맥락]\n- deal: 인수합병\n- hr: 인사\n[출력]\nhr"
    assert _parse_classification(echoed, RULES) == {"hr"}

    reasoning = "deal 인지 고민했다.\nhr 도 봤다.\nNONE"
    assert _parse_classification(reasoning, RULES) == set()


def test_classification_never_invents_a_rule():
    """모델이 없는 규칙 이름을 지어내도 결과에 들어오지 않는다(교집합)."""
    assert _parse_classification("deal, nuclear_secrets", RULES) == {"deal"}


def test_classification_prompt_carries_the_descriptions():
    rules = (GuardRule(id="deal", kind="llm", action="block", description="인수합병 논의"),)
    prompt = _classification_prompt("본문", rules)
    assert "deal: 인수합병 논의" in prompt
    assert "본문" in prompt


# ── 내부 역할 ────────────────────────────────────────────────────────────────


def test_underscore_roles_are_internal():
    assert is_public_role("summarize")
    assert not is_public_role(GUARD_ROLE)
    assert not is_public_role("_anything")


# ── 감사 M1 — 역할 기본 system 이 추론에 전달되지 않는다 ─────────────────────
#
# roles.yaml 의 계약은 "요청의 system 이 우선하고 **없을 때만** 역할 기본값을 쓴다"
# 인데, 없을 때 아무것도 안 썼다. 그런데 `system_hash` 는 기본값을 보낸 것처럼
# 해싱해서 프롬프트 드리프트 추적까지 "쓰고 있다" 고 거짓 보고했다.


async def test_the_role_default_system_actually_reaches_the_job(harness, client, acme):
    """저장된 잡이 역할 기본 system 을 들고 있어야 스케줄러가 그것을 보낸다."""
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    job = harness.store.get_job(TenantScope("acme"), job_id)
    assert job.system_masked == harness.config.roles["summarize"].system


async def test_the_request_system_still_wins(harness, client, acme):
    """**프롬프트는 호출자 소유다** — 기본값이 요청을 덮으면 계약 위반이다."""
    job_id = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "안녕", "system": "내가 정한다", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    job = harness.store.get_job(TenantScope("acme"), job_id)
    assert job.system_masked == "내가 정한다"


async def test_the_role_default_system_goes_through_the_guard(harness, config, store, clock):
    """관리자가 역할 기본 system 에 실수로 넣은 PII 가 필터를 우회하면 안 된다."""
    import dataclasses

    from app.cluster import Cluster
    from app.guard import Guard
    from app.pipeline import Pipeline

    role = config.roles["summarize"]
    leaky = dataclasses.replace(role, system="담당자 lee@example.com 에게 문의")
    patched = dataclasses.replace(config, roles={**config.roles, "summarize": leaky})

    store.create_tenant("acme", "Acme", end_user_salt=b"salt")
    store.create_service(TenantScope("acme"), "acme-web", "web")
    cluster = Cluster(patched, store, now=clock)
    pipeline = Pipeline(patched, store, cluster, Guard(patched), now=clock)

    verdict = await pipeline._inspect(leaky, store.get_tenant("acme"), "본문", leaky.system)
    assert "lee@example.com" not in (verdict.system_for("internal") or "")


async def test_the_system_hash_reflects_what_was_actually_sent(harness, client, acme):
    """해시가 "보냈다" 고 말하는데 안 보냈으면 드리프트 추적이 거짓말을 한다."""
    from app.identity import hash_system

    default_job = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]
    custom_job = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "안녕", "system": "다른 전략", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]

    scope = TenantScope("acme")
    default_row = harness.store.get_job(scope, default_job)
    custom_row = harness.store.get_job(scope, custom_job)

    assert default_row.system_hash == hash_system(harness.config.roles["summarize"].system)
    assert custom_row.system_hash == hash_system("다른 전략")
    assert default_row.system_hash != custom_row.system_hash


# ── 감사 M8 — 대기 폴링이 잡 전체를 초당 20회 읽는다 ─────────────────────────


async def test_waiting_reads_only_the_status_column(harness, client, acme, monkeypatch):
    """`get_job` 은 `SELECT *` 다 — 폴링이 보는 것은 상태 한 칸뿐인데."""
    scope = TenantScope("acme")
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]
    harness.store.update_job(scope, job_id, status="queued")

    full_reads = 0
    original = harness.store.get_job

    def counted(*args, **kwargs):
        nonlocal full_reads
        full_reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.store, "get_job", counted)
    await harness.pipeline.wait_for(scope, job_id, seconds=0.4)

    # 마지막에 한 번만 전체를 읽는다. 폴마다 읽으면 이 수가 폴 횟수만큼 는다.
    assert full_reads == 1, f"대기 중 잡 전체를 {full_reads}회 읽었다"


async def test_the_poll_interval_backs_off(harness, client, acme):
    """짧은 잡은 첫 폴에서 잡히고, 긴 잡을 20회/초로 확인할 이유는 없다."""
    import asyncio

    from app.pipeline import MAX_POLL_INTERVAL, POLL_BACKOFF, POLL_INTERVAL

    assert POLL_BACKOFF > 1.0
    assert POLL_INTERVAL < MAX_POLL_INTERVAL

    scope = TenantScope("acme")
    job_id = client.post(
        "/v1/generate", json={"role": "summarize", "prompt": "안녕", "wait": 0},
        headers=auth(acme["service"]),
    ).json()["job_id"]
    harness.store.update_job(scope, job_id, status="queued")

    intervals: list[float] = []
    real_sleep = asyncio.sleep

    async def recording(seconds, *args, **kwargs):
        intervals.append(seconds)
        return await real_sleep(0)

    import app.pipeline as pipeline_module

    original = pipeline_module.asyncio.sleep
    pipeline_module.asyncio.sleep = recording
    try:
        await harness.pipeline.wait_for(scope, job_id, seconds=2.0)
    finally:
        pipeline_module.asyncio.sleep = original

    assert len(intervals) > 3
    assert intervals[-1] > intervals[0], "간격이 안 늘어난다"
    assert max(intervals) <= MAX_POLL_INTERVAL
