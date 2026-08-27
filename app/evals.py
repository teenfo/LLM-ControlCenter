"""가드 품질 측정 — 정답셋 · 검토 큐 · 승격 게이트 · 분류기 인증.

**여기서는 LLM 이 보안 판정을 한다.** 그래서 "LLM 출력을 믿지 말고 측정한다" 는 원칙이
이 제품에서 가장 중요해진다. 체크섬조차 1자리라 무작위 값의 약 90%만 걸러낸다.

### 측정 원천이 두 개인 이유

| 원천 | 무엇을 재나 | 왜 따로인가 |
|---|---|---|
| **정답셋(fixture)** | 규칙·모델 변경 시 회귀 | 합성 샘플. 언제든 다시 돌릴 수 있다 |
| **검토 큐(review)** | 실제 트래픽의 오탐률 | 실제 분포를 반영한다. 승격 게이트의 근거 |

정답셋을 실제 트래픽에서 수확할 수 **없다.** `filter_events` 는 설계상 매칭된 값을 남기지
않으므로(감사가 유출 경로가 되면 안 되니까) 거기서 텍스트를 꺼낼 방법이 없다. 그래서
정답셋은 사람이 만든 합성 샘플이고, 실제 오탐률은 검토 판정으로 따로 잰다.

이 분리는 제약이 아니라 이득이다 — 합성 샘플로만 재면 실제 문서의 분포를 못 보고,
실제 트래픽으로만 재면 규칙을 바꿨을 때 회귀를 못 잡는다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .config import Config, GuardRule
from .guard import ACTION_STRENGTH, Guard
from .i18n import ApiError
from .store import SqliteStore, TenantScope

KIND_RULES = "rules"
KIND_CLASSIFIER = "classifier"

#: 승격 게이트가 요구하는 최소 검토 건수.
#:
#: 0건일 때 오탐률을 0% 로 보면 **아무것도 검토하지 않은 규칙이 전부 통과한다.**
#: 표본 없는 100% 정확도는 측정이 아니라 측정의 부재다.
MIN_REVIEWS_FOR_PROMOTION = 20

#: 분류기 인증에 필요한 최소 표본.
MIN_SAMPLES_FOR_CERTIFICATION = 20


# ── 결과 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuleEval:
    """정답셋 기준 규칙 하나의 성적."""

    rule_id: str
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0

    @property
    def sample_count(self) -> int:
        return (
            self.true_positives + self.false_positives
            + self.true_negatives + self.false_negatives
        )

    @property
    def false_positive_rate(self) -> float:
        """음성 샘플 중 잘못 잡은 비율. 규칙을 꺼버리게 만드는 것이 이 값이다."""
        negatives = self.false_positives + self.true_negatives
        return self.false_positives / negatives if negatives else 0.0

    @property
    def false_negative_rate(self) -> float:
        """양성 샘플 중 놓친 비율. 유출로 이어지는 것이 이 값이다."""
        positives = self.false_negatives + self.true_positives
        return self.false_negatives / positives if positives else 0.0

    @property
    def passed(self) -> int:
        return self.true_positives + self.true_negatives

    def as_metrics(self) -> dict[str, Any]:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "false_negatives": self.false_negatives,
            "false_positive_rate": round(self.false_positive_rate, 4),
            "false_negative_rate": round(self.false_negative_rate, 4),
        }


@dataclass(frozen=True)
class ReviewRate:
    """실제 트래픽 기준 오탐률. 승격 게이트가 보는 값."""

    rule_id: str
    reviewed: int = 0
    false_positives: int = 0

    @property
    def rate(self) -> float:
        return self.false_positives / self.reviewed if self.reviewed else 0.0

    @property
    def has_enough_samples(self) -> bool:
        return self.reviewed >= MIN_REVIEWS_FOR_PROMOTION


@dataclass(frozen=True)
class PromotionVerdict:
    allowed: bool
    reason: str
    rate: float = 0.0
    reviewed: int = 0
    limit: float = 0.0


@dataclass(frozen=True)
class ComplianceReport:
    """분류 모델의 구조화 출력 준수율."""

    model: str
    valid: int = 0
    total: int = 0
    failures: tuple[str, ...] = ()

    @property
    def rate(self) -> float:
        return self.valid / self.total if self.total else 0.0

    @property
    def has_enough_samples(self) -> bool:
        return self.total >= MIN_SAMPLES_FOR_CERTIFICATION


#: 분류기 한 번 호출. 유효한 판정이면 규칙 id 집합, 스키마를 어기면 예외를 던진다.
ClassifyOnce = Callable[[str], Awaitable[set[str]]]


# ── 번들 기본 정답셋 ─────────────────────────────────────────────────────────
#
# **전부 합성 값이다.** 실존 인물의 정보가 아니며, 체크섬이 필요한 항목은
# 유효한 검증부호를 갖도록 계산해 넣었다(그래야 체크섬 경로까지 시험된다).
#
# 설치처는 자기 도메인 샘플을 추가해야 한다 — 번들 세트만으로 잰 오탐률은
# 그 조직의 실제 문서 분포를 반영하지 못한다.

BUNDLED_FIXTURES: tuple[tuple[str, str, bool], ...] = (
    # (규칙 id, 텍스트, 잡혀야 하는가)
    ("kr_rrn", "주민등록번호는 900101-1234568 입니다", True),
    ("kr_rrn", "계약번호 900101-1234567 건", False),          # 체크섬 불일치
    ("kr_rrn", "주문번호 9001011234567 확인 바랍니다", False),  # 체크섬 불일치
    ("kr_rrn", "재고 수량은 1234 개입니다", False),

    ("kr_biz_no", "사업자등록번호 123-45-67891", True),
    ("kr_biz_no", "코드 123-45-67890 참조", False),

    ("kr_phone", "연락처는 010-1234-5678 입니다", True),
    ("kr_phone", "내선번호는 02-123-4567 입니다", False),

    ("credit_card", "카드 4111 1111 1111 1111 로 결제", True),
    ("credit_card", "주문 4111111111111112 건", False),        # Luhn 불일치
    ("credit_card", "송장번호 1234567890123456", False),

    ("email", "문의는 support@example.com 으로", True),
    ("email", "at 기호만 있는 문장 a @ b", False),

    ("iban", "계좌 GB82 WEST 1234 5698 7654 32", True),
    ("iban", "코드 GB82WEST12345698765433 무효", False),

    ("us_ssn", "SSN 123-45-6789 on file", True),
    ("us_ssn", "invoice 000-00-0000 void", False),

    ("jp_mynumber", "マイナンバーは 1234 5678 9018 です", True),
    ("jp_mynumber", "伝票 1234 5678 9012 番", False),

    ("ipv4", "서버 주소는 192.168.10.24 입니다", True),
    ("ipv4", "버전 1.2.3.4.5 릴리스", False),

    ("us_ein", "EIN 12-3456789 filed", True),
    ("us_ein", "quarter 1-2 review", False),

    ("us_phone", "call (415) 555-0132 for support", True),
    ("us_phone", "room 12-34 on floor 5", False),

    ("kr_passport", "여권번호 M12345678 확인", True),
    ("kr_passport", "모델명 M1234567 재고", False),

    ("jp_phone", "電話は 03-1234-5678 です", True),
    ("jp_phone", "西暦 2026 年", False),
)

#: 분류기 인증용 중립 프로브.
#:
#: **정답셋(PII 샘플)을 재사용하지 않는다.** 인증은 아직 믿지 못하는 모델을 시험하는
#: 절차인데, 그 모델에 개인정보 샘플을 먹이는 것은 앞뒤가 맞지 않는다.
#: 여기서 재는 것은 판정의 정확도가 아니라 **구조화 출력을 지키는가**뿐이므로
#: 내용은 평범할수록 좋다.
DEFAULT_CLASSIFIER_PROBES: tuple[str, ...] = (
    "오늘 회의는 3시에 시작합니다.",
    "분기 실적 자료를 정리해 주세요.",
    "이 문서를 세 문장으로 요약해 줘.",
    "다음 주 일정표를 공유합니다.",
    "배포 절차를 단계별로 설명해 주세요.",
    "회의록 초안을 검토 부탁드립니다.",
    "고객 문의에 대한 답변 초안입니다.",
    "테스트 환경 구성이 완료되었습니다.",
    "The quarterly report is attached for review.",
    "Please summarize the following article.",
    "Our deployment finished without incident.",
    "Schedule the sync for Tuesday morning.",
    "Translate this paragraph into Korean.",
    "The build pipeline completed successfully.",
    "Draft a short announcement for the team.",
    "Explain the difference between these two options.",
    "会議の議事録を確認してください。",
    "来週の予定を共有します。",
    "この文章を要約してください。",
    "プロジェクトの進捗は順調です。",
    "빈 문자열이 아닌 아주 짧은 문장.",
    "A sentence with numbers 42 and 7 in it.",
    "여러 줄로\n나뉜 입력도\n처리되어야 합니다.",
    "Mixed 한국어 and English in one sentence.",
)


# ── 평가기 ──────────────────────────────────────────────────────────────────


class Evaluator:
    def __init__(
        self,
        config: Config,
        store: SqliteStore,
        guard: Guard,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._store = store
        self._guard = guard
        self._now = now
        self._settings = config.guard_settings

    # -- 정답셋 ---------------------------------------------------------------

    def seed_bundled_fixtures(self) -> int:
        """번들 기본 세트를 적재한다. 부트스트랩에서 한 번 부른다."""
        known = {rule.id for rule in self._config.guard_rules}
        existing = {
            (row["rule_id"], row["text"])
            for row in self._store.list_fixtures()
        }

        added = 0
        for rule_id, text, expect in BUNDLED_FIXTURES:
            # 설정에 없는 규칙의 샘플은 넣지 않는다 — 평가할 대상이 없다.
            if rule_id not in known or (rule_id, text) in existing:
                continue
            self._store.add_fixture(rule_id, text, expect, source="bundled")
            added += 1
        return added

    def add_fixture(
        self,
        scope: TenantScope,
        rule_id: str,
        text: str,
        expect_match: bool,
        *,
        note: str | None = None,
    ) -> str:
        """설치처가 자기 도메인 샘플을 추가한다.

        **번들 세트만으로 잰 오탐률은 그 조직의 실제 문서 분포를 반영하지 못한다.**
        """
        return self._store.add_fixture(
            rule_id, text, expect_match, scope=scope, source="tenant", note=note
        )

    # -- 회귀 평가 -------------------------------------------------------------

    def evaluate_rules(
        self,
        *,
        scope: TenantScope | None = None,
        rule_ids: Sequence[str] | None = None,
        record: bool = True,
    ) -> list[RuleEval]:
        """정답셋으로 패턴 규칙들을 채점한다. 규칙을 바꾼 뒤 회귀를 잡는 용도."""
        by_id = {rule.id: rule for rule in self._config.guard_rules}
        results: list[RuleEval] = []

        wanted = set(rule_ids) if rule_ids else None
        grouped: dict[str, list[Any]] = {}
        for row in self._store.list_fixtures(scope=scope):
            if wanted and row["rule_id"] not in wanted:
                continue
            grouped.setdefault(row["rule_id"], []).append(row)

        for rule_id, rows in sorted(grouped.items()):
            rule = by_id.get(rule_id)
            if rule is None or rule.is_llm:
                # LLM 규칙은 분류기가 있어야 채점된다 — `evaluate_context_rule` 이 한다.
                continue

            tp = fp = tn = fn = 0
            for row in rows:
                matched = self._guard.probe_rule(rule, row["text"]) > 0
                expected = bool(row["expect_match"])
                if expected and matched:
                    tp += 1
                elif expected and not matched:
                    fn += 1
                elif not expected and matched:
                    fp += 1
                else:
                    tn += 1

            result = RuleEval(rule_id, tp, fp, tn, fn)
            results.append(result)

            if record:
                self._store.record_eval_run(
                    KIND_RULES, rule_id,
                    passed=result.passed, total=result.sample_count,
                    metrics=result.as_metrics(),
                    tenant_id=scope.tenant_id if scope else None,
                )
        return results

    async def evaluate_context_rule(
        self,
        rule: GuardRule,
        classify: ClassifyOnce,
        *,
        scope: TenantScope | None = None,
        system_hash: str | None = None,
        record: bool = True,
    ) -> RuleEval:
        """맥락(LLM) 규칙을 정답셋으로 채점한다.

        `system_hash` 를 함께 남기는 이유: **"어떤 프롬프트 버전에서 잰 오탐률인가" 를
        말할 수 없으면 그 숫자는 비교할 수 없다.** 프롬프트를 고치면 성적이 바뀌는데
        무엇이 바뀌었는지 모르면 개선인지 후퇴인지도 모른다.
        """
        rows = self._store.list_fixtures(scope=scope, rule_id=rule.id)
        tp = fp = tn = fn = 0

        for row in rows:
            try:
                hits = await classify(row["text"])
                matched = rule.id in hits
            except Exception:
                # 분류 실패는 "안 잡힘" 이 아니다. 놓친 것으로 계상해 낙관을 막는다.
                matched = not bool(row["expect_match"])

            expected = bool(row["expect_match"])
            if expected and matched:
                tp += 1
            elif expected and not matched:
                fn += 1
            elif not expected and matched:
                fp += 1
            else:
                tn += 1

        result = RuleEval(rule.id, tp, fp, tn, fn)
        if record:
            self._store.record_eval_run(
                KIND_RULES, rule.id,
                passed=result.passed, total=result.sample_count,
                metrics=result.as_metrics(),
                tenant_id=scope.tenant_id if scope else None,
                system_hash=system_hash,
            )
        return result

    # -- 검토 큐 --------------------------------------------------------------

    def review(self, scope: TenantScope, event_id: int, verdict: str) -> bool:
        """오탐 검토 큐 판정. 값이 아니라 판정만 남는다."""
        return self._store.review_filter_event(scope, event_id, verdict)

    def review_rate(self, scope: TenantScope, rule_id: str) -> ReviewRate:
        stats = self._store.review_stats(scope, rule_id).get(rule_id, {})
        false_positives = stats.get("false_positive", 0)
        return ReviewRate(
            rule_id=rule_id,
            reviewed=false_positives + stats.get("true_positive", 0),
            false_positives=false_positives,
        )

    def review_queue(self, scope: TenantScope, *, limit: int = 100) -> list[Any]:
        """아직 판정 안 된 `audit` 히트. 사람이 볼 목록."""
        return self._store.list_filter_events(
            scope, action="audit", unreviewed_only=True, limit=limit
        )

    # -- 승격 게이트 ----------------------------------------------------------

    def can_promote(
        self, scope: TenantScope, rule_id: str, target_action: str
    ) -> PromotionVerdict:
        """`audit` → 더 강한 등급으로 올릴 수 있는가.

        새 규칙을 바로 `block` 으로 켜면 오탐이 프로덕션을 세우고, 그러면 관리자가
        규칙을 통째로 꺼버린다. 며칠 재고 올리는 것이 의도한 운영 흐름이다.
        """
        limit = self._settings.promotion_max_false_positive_rate

        current = next(
            (r for r in self._config.guard_rules if r.id == rule_id), None
        )
        if current is None:
            raise ApiError("unknown_role", status=404, params={"role": rule_id})

        # 약하게 만드는 것은 게이트 대상이 아니라 아예 금지다(테넌트는 조일 수만 있다).
        if ACTION_STRENGTH.get(target_action, 0) <= ACTION_STRENGTH.get(
            current.action_for_boundary("internal"), 0
        ):
            return PromotionVerdict(True, "not_a_promotion", limit=limit)

        rate = self.review_rate(scope, rule_id)

        if not rate.has_enough_samples:
            # **0건을 0% 로 보면 아무것도 검토하지 않은 규칙이 전부 통과한다.**
            # 표본 없는 100% 정확도는 측정이 아니라 측정의 부재다.
            return PromotionVerdict(
                False, "insufficient_reviews",
                rate=rate.rate, reviewed=rate.reviewed, limit=limit,
            )

        if rate.rate > limit:
            return PromotionVerdict(
                False, "false_positive_rate_too_high",
                rate=rate.rate, reviewed=rate.reviewed, limit=limit,
            )

        return PromotionVerdict(
            True, "ok", rate=rate.rate, reviewed=rate.reviewed, limit=limit
        )

    # -- 분류기 인증 ----------------------------------------------------------

    async def certify_classifier(
        self,
        model: str,
        classify: ClassifyOnce,
        *,
        samples: Sequence[str] | None = None,
        record: bool = True,
    ) -> ComplianceReport:
        """분류 모델이 구조화 출력을 지키는지 확인한다.

        hybrid thinking 계열은 structured output 이 깨지는 미해결 이슈가 있고,
        2단 분류가 enum 판정을 요구하므로 **정확히 이 경로에 걸린다.** 준수율이
        임계 미만인 모델은 등록을 거부한다 — 스키마를 어기는 분류기는 판정을
        안 하는 것과 같은데, 안 한다는 사실조차 드러나지 않는다.

        기본 프로브는 중립 문장이다. **정답셋(PII 샘플)을 쓰지 않는 이유**는,
        인증이 아직 믿지 못하는 모델을 시험하는 절차인데 그 모델에 개인정보를
        먹이는 것이 앞뒤가 맞지 않기 때문이다. 여기서 재는 것은 판정의 정확도가
        아니라 구조화 출력을 지키는가뿐이다.
        """
        probes = list(samples or DEFAULT_CLASSIFIER_PROBES)

        valid = 0
        failures: list[str] = []

        for text in probes:
            try:
                hits = await classify(text)
            except Exception as exc:
                failures.append(type(exc).__name__)
                continue
            if isinstance(hits, (set, frozenset)):
                valid += 1
            else:
                failures.append(f"unexpected_type:{type(hits).__name__}")

        report = ComplianceReport(
            model=model, valid=valid, total=len(probes),
            failures=tuple(sorted(set(failures))),
        )
        if record:
            self._store.record_eval_run(
                KIND_CLASSIFIER, model,
                passed=report.valid, total=report.total,
                metrics={"rate": round(report.rate, 4), "failures": list(report.failures)},
            )
        return report

    def classifier_is_certified(self, model: str) -> bool:
        """이 모델을 2단 분류에 쓸 수 있는가. 인증 이력이 없으면 **거부**한다."""
        row = self._store.latest_eval_run(KIND_CLASSIFIER, model)
        if row is None or not row["total"]:
            return False
        if row["total"] < MIN_SAMPLES_FOR_CERTIFICATION:
            return False
        return row["passed"] / row["total"] >= self._settings.classifier_min_schema_compliance

    # -- 관제 -----------------------------------------------------------------

    def snapshot(self, scope: TenantScope) -> dict[str, Any]:
        """관제 UI 용 — 규칙별 정답셋 성적 · 실트래픽 오탐률 · 승격 가능 여부."""
        fixture_results = {r.rule_id: r for r in self.evaluate_rules(scope=scope, record=False)}
        review_stats = self._store.review_stats(scope)

        rows = []
        for rule in self._config.guard_rules:
            fixture = fixture_results.get(rule.id)
            rate = self.review_rate(scope, rule.id)
            rows.append(
                {
                    "rule_id": rule.id,
                    "kind": rule.kind,
                    "action": rule.action,
                    "fixture_samples": fixture.sample_count if fixture else 0,
                    "fixture_false_positive_rate": (
                        round(fixture.false_positive_rate, 4) if fixture else None
                    ),
                    "fixture_false_negative_rate": (
                        round(fixture.false_negative_rate, 4) if fixture else None
                    ),
                    "reviewed": rate.reviewed,
                    "review_false_positive_rate": round(rate.rate, 4),
                    "can_promote": self.can_promote(scope, rule.id, "block").allowed,
                }
            )

        return {
            "rules": rows,
            "unreviewed": len(self.review_queue(scope, limit=1000)),
            "promotion_limit": self._settings.promotion_max_false_positive_rate,
            "min_reviews": MIN_REVIEWS_FOR_PROMOTION,
            "raw_review_stats": review_stats,
        }
