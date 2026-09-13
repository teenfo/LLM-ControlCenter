"""가드 품질 측정 — 정답셋 · 검토 큐 · 승격 게이트 · 분류기 인증."""

from __future__ import annotations

import pytest

from app.config import load_config
from app.evals import (
    BUNDLED_FIXTURES,
    KIND_CLASSIFIER,
    KIND_ROUTER,
    MIN_REVIEWS_FOR_PROMOTION,
    EvalError,
    Evaluator,
    RuleEval,
)
from app.guard import Guard
from app.store import SqliteStore, TenantScope

ACME = TenantScope("acme")
GLOBEX = TenantScope("globex")


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config():
    return load_config("config")


@pytest.fixture
def store(clock) -> SqliteStore:
    s = SqliteStore(":memory:", now=clock)
    for tenant in ("acme", "globex"):
        s.create_tenant(tenant, tenant.title(), end_user_salt=b"salt")
        s.create_service(TenantScope(tenant), f"{tenant}-web", "web")
    yield s
    s.close()


@pytest.fixture
def evaluator(config, store, clock) -> Evaluator:
    ev = Evaluator(config, store, Guard(config), now=clock)
    ev.seed_bundled_fixtures()
    return ev


def add_reviews(store, scope, rule_id, *, true_positive=0, false_positive=0):
    """검토 완료된 이벤트를 만든다."""
    for verdict, count in (("true_positive", true_positive), ("false_positive", false_positive)):
        for _ in range(count):
            store.record_filter_event(scope, rule_id=rule_id, stage="pattern", action="audit")
            event = store.list_filter_events(scope, unreviewed_only=True, limit=1)[0]
            store.review_filter_event(scope, event["id"], verdict)


# ── 번들 정답셋 ──────────────────────────────────────────────────────────────


def test_every_bundled_fixture_targets_a_real_rule(config):
    """설정에 없는 규칙 id 를 쓰면 그 샘플은 **조용히 건너뛰어진다.**

    실제로 kr_biz(올바른 값은 kr_biz_no) 오타가 있었고, 아무 오류 없이 로드만 안 됐다.
    """
    known = {rule.id for rule in config.guard_rules}
    referenced = {rule_id for rule_id, _, _ in BUNDLED_FIXTURES}

    assert referenced <= known, f"설정에 없는 규칙: {sorted(referenced - known)}"


def test_every_shipped_pattern_rule_has_fixtures(config):
    """정답셋이 없는 규칙은 **품질을 잴 방법이 없다.**

    회귀도 못 잡고 오탐률도 모르는 규칙이 베이스라인에 섞여 있으면,
    "측정한다" 는 이 절의 주장이 그 규칙에 대해서는 거짓이 된다.
    """
    covered = {rule_id for rule_id, _, _ in BUNDLED_FIXTURES}
    pattern_rules = {r.id for r in config.guard_rules if r.kind == "pattern"}

    assert pattern_rules <= covered, f"정답셋 없는 규칙: {sorted(pattern_rules - covered)}"


def test_seeding_is_idempotent(evaluator, store):
    before = len(store.list_fixtures())
    assert evaluator.seed_bundled_fixtures() == 0, "두 번째 적재에서 중복이 생겼다"
    assert len(store.list_fixtures()) == before


def test_bundled_fixtures_are_platform_wide(evaluator, store):
    """번들 세트는 tenant_id 가 NULL — 전 테넌트가 같은 기준선을 쓴다."""
    assert all(row["tenant_id"] is None for row in store.list_fixtures())
    assert len(store.list_fixtures(scope=ACME)) == len(store.list_fixtures(scope=GLOBEX))


def test_bundled_fixtures_cover_both_polarities(evaluator, store):
    """양성만 있으면 오탐률을, 음성만 있으면 미탐률을 못 잰다."""
    by_rule: dict[str, set[int]] = {}
    for row in store.list_fixtures():
        by_rule.setdefault(row["rule_id"], set()).add(row["expect_match"])

    for rule_id, polarities in by_rule.items():
        assert polarities == {0, 1}, f"{rule_id} 에 한쪽 극성만 있다"


# ── 회귀 평가 ────────────────────────────────────────────────────────────────


def test_shipped_rules_pass_their_own_fixtures(evaluator):
    """번들 규칙이 번들 정답셋을 통과하지 못하면 출하할 수 없다."""
    results = {r.rule_id: r for r in evaluator.evaluate_rules()}

    assert results, "평가된 규칙이 없다"
    for rule_id, result in results.items():
        assert result.false_positive_rate == 0.0, (
            f"{rule_id} 오탐 {result.false_positives}건: {result.as_metrics()}"
        )
        assert result.false_negative_rate == 0.0, (
            f"{rule_id} 미탐 {result.false_negatives}건: {result.as_metrics()}"
        )


def test_checksum_rules_reject_invalid_samples(evaluator):
    """체크섬이 없으면 이 음성 샘플들이 전부 오탐이 된다."""
    result = next(r for r in evaluator.evaluate_rules() if r.rule_id == "kr_rrn")

    assert result.true_negatives >= 2
    assert result.false_positives == 0


def test_tenant_fixtures_only_affect_that_tenant(evaluator, store):
    """설치처가 자기 도메인 샘플을 넣어도 옆 테넌트의 성적이 바뀌면 안 된다."""
    evaluator.add_fixture(ACME, "email", "사내코드 AC-1234 는 이메일이 아니다", False)

    acme = next(r for r in evaluator.evaluate_rules(scope=ACME) if r.rule_id == "email")
    globex = next(r for r in evaluator.evaluate_rules(scope=GLOBEX) if r.rule_id == "email")

    assert acme.sample_count == globex.sample_count + 1


def test_regression_is_caught_when_a_rule_breaks(config, store, clock):
    """규칙을 고쳐 놓고 정답셋을 안 돌리면 회귀를 못 잡는다."""
    import dataclasses

    broken = dataclasses.replace(
        next(r for r in config.guard_rules if r.id == "kr_rrn"),
        checksum=None,   # 체크섬 검증을 없애면 오탐이 살아난다
    )
    broken_config = dataclasses.replace(
        config,
        guard_rules=tuple(broken if r.id == "kr_rrn" else r for r in config.guard_rules),
    )

    evaluator = Evaluator(broken_config, store, Guard(broken_config), now=clock)
    evaluator.seed_bundled_fixtures()

    result = next(r for r in evaluator.evaluate_rules() if r.rule_id == "kr_rrn")
    assert result.false_positives > 0, "체크섬을 없앴는데 오탐이 안 늘었다"


def test_eval_runs_are_recorded(evaluator, store):
    evaluator.evaluate_rules()
    runs = store.list_eval_runs(kind="rules")

    assert runs
    assert all(row["total"] > 0 for row in runs)


# ── 지표 계산 ────────────────────────────────────────────────────────────────


def test_rate_math():
    result = RuleEval("r", true_positives=8, false_negatives=2, true_negatives=9, false_positives=1)

    assert result.false_positive_rate == pytest.approx(0.1)   # 1 / (1+9)
    assert result.false_negative_rate == pytest.approx(0.2)   # 2 / (2+8)
    assert result.sample_count == 20
    assert result.passed == 17


def test_empty_evaluation_does_not_divide_by_zero():
    empty = RuleEval("r")
    assert empty.false_positive_rate == 0.0
    assert empty.false_negative_rate == 0.0


# ── 검토 큐 ──────────────────────────────────────────────────────────────────


def test_review_queue_lists_only_unreviewed_audit_hits(evaluator, store):
    store.record_filter_event(ACME, rule_id="email", stage="pattern", action="audit")
    store.record_filter_event(ACME, rule_id="kr_rrn", stage="pattern", action="full")

    queue = evaluator.review_queue(ACME)
    assert [row["rule_id"] for row in queue] == ["email"], "audit 이 아닌 것이 큐에 들어왔다"


def test_reviewing_removes_it_from_the_queue(evaluator, store):
    store.record_filter_event(ACME, rule_id="email", stage="pattern", action="audit")
    event = evaluator.review_queue(ACME)[0]

    assert evaluator.review(ACME, event["id"], "false_positive") is True
    assert evaluator.review_queue(ACME) == []


def test_review_stores_verdict_not_content(evaluator, store):
    store.record_filter_event(ACME, rule_id="email", stage="pattern", action="audit")
    event = evaluator.review_queue(ACME)[0]
    evaluator.review(ACME, event["id"], "false_positive")

    row = store.list_filter_events(ACME)[0]
    assert row["verdict"] == "false_positive"
    assert "text" not in row.keys(), "검토 테이블에 본문 컬럼이 생겼다"


def test_unknown_verdict_is_rejected(evaluator, store):
    from app.store import StoreError

    store.record_filter_event(ACME, rule_id="email", stage="pattern", action="audit")
    event = evaluator.review_queue(ACME)[0]

    with pytest.raises(StoreError):
        evaluator.review(ACME, event["id"], "maybe")


def test_review_rate_is_per_tenant(evaluator, store):
    add_reviews(store, ACME, "email", true_positive=5, false_positive=5)
    add_reviews(store, GLOBEX, "email", true_positive=10)

    assert evaluator.review_rate(ACME, "email").rate == pytest.approx(0.5)
    assert evaluator.review_rate(GLOBEX, "email").rate == 0.0


def test_reviews_do_not_cross_tenants(evaluator, store):
    add_reviews(store, ACME, "email", false_positive=3)
    assert evaluator.review_rate(GLOBEX, "email").reviewed == 0


# ── 승격 게이트 ──────────────────────────────────────────────────────────────


def test_promotion_blocked_without_enough_reviews(evaluator):
    """0건을 0% 로 보면 아무것도 검토하지 않은 규칙이 전부 통과한다.

    표본 없는 100% 정확도는 측정이 아니라 측정의 부재다.
    """
    verdict = evaluator.can_promote(ACME, "email", "block")

    assert verdict.allowed is False
    assert verdict.reason == "insufficient_reviews"
    assert verdict.reviewed == 0


def test_promotion_blocked_when_false_positive_rate_is_high(evaluator, store):
    add_reviews(store, ACME, "email", true_positive=10, false_positive=15)

    verdict = evaluator.can_promote(ACME, "email", "block")

    assert verdict.allowed is False
    assert verdict.reason == "false_positive_rate_too_high"
    assert verdict.rate > verdict.limit


def test_promotion_allowed_when_clean_and_well_sampled(evaluator, store):
    add_reviews(store, ACME, "email", true_positive=MIN_REVIEWS_FOR_PROMOTION)

    verdict = evaluator.can_promote(ACME, "email", "block")

    assert verdict.allowed is True
    assert verdict.reason == "ok"
    assert verdict.rate == 0.0


def test_exactly_at_the_limit_is_allowed(evaluator, store):
    """임계 '초과' 만 막는다 — 경계값에서 애매하면 관리자가 못 믿는다."""
    add_reviews(store, ACME, "email", true_positive=98, false_positive=2)   # 정확히 2%

    verdict = evaluator.can_promote(ACME, "email", "block")
    assert verdict.rate == pytest.approx(0.02)
    assert verdict.allowed is True


def test_lowering_the_grade_is_not_a_promotion(evaluator):
    """약화는 게이트 대상이 아니다 — 아예 다른 경로(테넌트는 조일 수만 있다)에서 막힌다."""
    verdict = evaluator.can_promote(ACME, "kr_rrn", "audit")   # full -> audit
    assert verdict.allowed is True
    assert verdict.reason == "not_a_promotion"


def test_an_unknown_rule_is_judged_as_a_brand_new_one(evaluator):
    """**베이스라인에 없는 규칙도 판정 대상이다.**

    404 로 끝내던 탓에 테넌트가 새로 만드는 규칙은 게이트를 아예 지나지 않았고,
    측정 없이 바로 `block` 으로 켤 수 있었다 — 게이트가 막으려던 그 경우다.
    """
    verdict = evaluator.can_promote(ACME, "does-not-exist", "block")

    assert verdict.allowed is False
    assert verdict.reason == "insufficient_reviews"


def test_masking_grades_are_not_gated(evaluator):
    """게이트의 이유는 "차단이 프로덕션을 세운다" 이다.

    `partial`·`full` 은 텍스트를 바꿀 뿐 요청을 멈추지 않는다. 마스킹까지 막으면
    관리자가 규칙을 켤 방법 자체가 없어져서 결국 게이트를 통째로 우회하게 된다.
    """
    assert evaluator.can_promote(ACME, "새-규칙", "full").allowed is True
    assert evaluator.can_promote(ACME, "새-규칙", "partial").allowed is True


def test_promotion_gate_is_per_tenant(evaluator, store):
    add_reviews(store, ACME, "email", true_positive=MIN_REVIEWS_FOR_PROMOTION)

    assert evaluator.can_promote(ACME, "email", "block").allowed is True
    assert evaluator.can_promote(GLOBEX, "email", "block").allowed is False


# ── 분류기 인증 ──────────────────────────────────────────────────────────────


async def test_compliant_classifier_is_certified(evaluator):
    async def good(text: str) -> set[str]:
        return set()

    report = await evaluator.certify_classifier("good-model", good)

    assert report.rate == 1.0
    assert evaluator.classifier_is_certified("good-model") is True


async def test_classifier_that_breaks_schema_is_rejected(evaluator):
    """hybrid thinking 계열이 structured output 을 깨뜨리는 이슈가 정확히 여기 걸린다.

    스키마를 어기는 분류기는 판정을 안 하는 것과 같은데, 안 한다는 사실조차 드러나지 않는다.
    """
    calls = {"n": 0}

    async def flaky(text: str) -> set[str]:
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise ValueError("모델이 JSON 대신 산문을 뱉었다")
        return set()

    report = await evaluator.certify_classifier("thinking-model", flaky)

    assert report.rate < 0.98
    assert report.failures
    assert evaluator.classifier_is_certified("thinking-model") is False


async def test_classifier_returning_wrong_type_fails(evaluator):
    async def wrong(text: str):
        return "medical_context"   # 집합이 아니라 문자열

    report = await evaluator.certify_classifier("wrong-model", wrong)

    assert report.rate == 0.0
    assert any(f.startswith("unexpected_type") for f in report.failures)


def test_uncertified_classifier_is_refused_by_default(evaluator):
    """인증 이력이 없으면 거부한다 — 모르는 모델을 낙관적으로 통과시키지 않는다."""
    assert evaluator.classifier_is_certified("never-tested") is False


async def test_certification_needs_a_minimum_sample_size(evaluator):
    async def good(text: str) -> set[str]:
        return set()

    tiny = await evaluator.certify_classifier(
        "tiny-sample", good, samples=["하나", "둘"]
    )

    assert tiny.rate == 1.0
    assert tiny.has_enough_samples is False
    assert evaluator.classifier_is_certified("tiny-sample") is False, (
        "표본 2개로 100% 를 받아 인증됐다"
    )


async def test_certification_is_recorded(evaluator, store):
    async def good(text: str) -> set[str]:
        return set()

    await evaluator.certify_classifier("m", good)
    runs = store.list_eval_runs(kind=KIND_CLASSIFIER)

    assert runs and runs[0]["subject"] == "m"


# ── 맥락 규칙 평가 ───────────────────────────────────────────────────────────


async def test_context_rule_evaluation_records_the_prompt_version(evaluator, store, config):
    """어떤 프롬프트 버전에서 잰 오탐률인가를 말할 수 없으면 그 숫자는 비교할 수 없다."""
    rule = next(r for r in config.guard_rules if r.id == "medical_context")
    evaluator.add_fixture(ACME, rule.id, "환자 진료 기록입니다", True)
    evaluator.add_fixture(ACME, rule.id, "점심 메뉴 추천해줘", False)

    async def classify(text: str) -> set[str]:
        return {rule.id} if "진료" in text else set()

    result = await evaluator.evaluate_context_rule(
        rule, classify, scope=ACME, system_hash="abc123"
    )

    assert result.true_positives == 1 and result.true_negatives == 1
    run = store.list_eval_runs(kind="rules")[0]
    assert run["system_hash"] == "abc123"


async def test_classifier_failure_counts_as_a_miss(evaluator, config):
    """분류 실패를 "안 잡힘" 으로 처리하면 성적이 낙관적으로 부풀려진다."""
    rule = next(r for r in config.guard_rules if r.id == "medical_context")
    evaluator.add_fixture(ACME, rule.id, "환자 진료 기록", True)

    async def failing(text: str) -> set[str]:
        raise RuntimeError("노드 다운")

    result = await evaluator.evaluate_context_rule(rule, failing, scope=ACME, record=False)

    assert result.false_negatives == 1, "실패가 통과로 계상됐다"
    assert result.true_positives == 0


# ── 관제 ────────────────────────────────────────────────────────────────────


def test_snapshot_exposes_both_measurement_sources(evaluator, store):
    add_reviews(store, ACME, "email", true_positive=5, false_positive=1)

    snapshot = evaluator.snapshot(ACME)
    email = next(r for r in snapshot["rules"] if r["rule_id"] == "email")

    assert email["fixture_samples"] > 0
    assert email["reviewed"] == 6
    assert email["review_false_positive_rate"] == pytest.approx(1 / 6, abs=1e-4)
    assert email["can_promote"] is False   # 표본 부족
    assert snapshot["min_reviews"] == MIN_REVIEWS_FOR_PROMOTION


def test_snapshot_counts_the_unreviewed_backlog(evaluator, store):
    for _ in range(3):
        store.record_filter_event(ACME, rule_id="email", stage="pattern", action="audit")

    assert evaluator.snapshot(ACME)["unreviewed"] == 3


# ── 라우터 품질 (P1-7) ───────────────────────────────────────────────────────
#
# 가드 정답셋 체계를 재사용하되 **쓰임이 반대다.** 규칙 성적은 승격 게이트의
# 입력이고(틀리면 못 켠다), 라우터 성적은 계기판이다(틀려도 켜져 있다).
# 오분류의 대가가 보안이 아니라 비용이기 때문이다.


def routed_role():
    from app.config import Role, RoleRouting, RouteSpec

    return Role(
        name="summarize", model="기본",
        routing=RoleRouting(
            classifier="_guard_classify",
            routes={
                "simple": RouteSpec(model="작은모델", description="한두 문장 요약"),
                "complex": RouteSpec(model="큰모델", description="장문 분석"),
            },
        ),
    )


async def test_the_router_report_counts_accuracy(evaluator):
    role = routed_role()
    answers = {"짧은 글": "simple", "긴 보고서": "complex", "표 분석": "complex"}

    async def route_once(_role, text):
        return answers[text]

    report = await evaluator.measure_router(
        role, route_once,
        [("짧은 글", "simple"), ("긴 보고서", "complex"), ("표 분석", "simple")],
    )

    assert report.total == 3
    assert report.correct == 2
    assert report.rate == pytest.approx(2 / 3)


async def test_the_report_names_which_description_to_fix(evaluator):
    """**정확도 하나만 주면 관리자는 고칠 데를 모른다.**

    운영 루프는 "82% 입니다" 가 아니라 "simple 이 complex 로 샌다" 에서 돈다 —
    고칠 것은 `routes["simple"].description` 이다.
    """
    role = routed_role()

    async def always_complex(_role, _text):
        return "complex"

    report = await evaluator.measure_router(
        role, always_complex,
        [("짧은 글", "simple"), ("또 짧은 글", "simple"), ("긴 보고서", "complex")],
    )

    assert report.worst_leak == ("simple", "complex", 2)


async def test_a_routing_failure_is_recorded_as_the_default_not_dropped(evaluator):
    """판정 실패는 **버리지 않고 기본 모델로 샌 것**으로 센다.

    건너뛰면 라우터가 절반을 포기해도 나머지 절반의 정확도만 보이고, 그 수치는
    높다. 라우팅을 켜 둔 채 아무 이득이 없는 상태가 계기판에서 좋아 보인다.
    """
    role = routed_role()

    async def always_fails(_role, _text):
        raise RuntimeError("분류 백엔드가 죽었다")

    report = await evaluator.measure_router(
        role, always_fails, [("짧은 글", "simple"), ("긴 보고서", "complex")],
    )

    assert report.total == 2
    assert report.correct == 0
    assert report.confusion == {("simple", None): 1, ("complex", None): 1}


async def test_a_fixture_naming_an_unknown_route_is_an_error(evaluator):
    """**픽스처의 오타가 정확도 100% 로 보이면 안 된다.**"""
    role = routed_role()

    async def never_called(_role, _text):     # pragma: no cover - 도달하면 실패다
        raise AssertionError("픽스처를 검증하기 전에 라우터를 불렀다")

    with pytest.raises(EvalError, match="어휘에 없다"):
        await evaluator.measure_router(role, never_called, [("글", "simpel")])


async def test_the_router_run_is_recorded_under_its_own_kind(evaluator, store):
    """분류기 인증(`KIND_CLASSIFIER`)과 **섞이면 안 된다.**

    같은 칸에 쓰면 라우터 성적이 낮을 때 가드 2단 분류기가 인증을 잃는다 —
    라우팅을 켰다가 가드가 꺼지는 경로다.
    """
    role = routed_role()

    async def always_simple(_role, _text):
        return "simple"

    await evaluator.measure_router(role, always_simple, [("글", "simple")])

    assert store.latest_eval_run(KIND_ROUTER, "summarize")["passed"] == 1
    assert store.latest_eval_run(KIND_CLASSIFIER, "summarize") is None
