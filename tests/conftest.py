"""API 레이어 공용 픽스처.

**목 프로바이더로 전부 돈다** — 실제 노드도 클라우드 키도 없이 제품의 대부분을
검증할 수 있어야 하고, 그게 그대로 Demo 프로파일이 된다.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.auth import ROLE_PLATFORM_ADMIN, ROLE_SERVICE, ROLE_TENANT_ADMIN, issue_token
from app.cluster import HEALTHY, Cluster
from app.completion import CompletionSignal
from app.config import (
    CatalogEntry,
    Config,
    GuardRule,
    GuardSettings,
    Lane,
    Node,
    Pricing,
    Role,
    Thresholds,
)
from app.cost import CostAccountant
from app.crypto import KeyVault, generate_master_key
from app.evals import Evaluator
from app.guard import Guard
from app.identity import new_salt
from app.main import build_app
from app.models import ModelRegistrar
from app.notify import Notifier, RecordingChannel
from app.pipeline import Pipeline
from app.scheduler import Scheduler
from app.store import SqliteStore, TenantScope

ACME = TenantScope("acme")
GLOBEX = TenantScope("globex")


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**overrides) -> Config:
    return Config(
        nodes={
            "in-1": Node(
                name="in-1", provider="mock", data_boundary="internal",
                mem_budget_gb=40, max_concurrent=4, tags=("internal",),
                models=("m", "guard-m", "embed-m"),
            ),
            "in-2": Node(
                name="in-2", provider="mock", data_boundary="internal",
                mem_budget_gb=40, max_concurrent=4, tags=("internal",),
                models=("m", "embed-m"),
            ),
            "out": Node(
                name="out", provider="mock", data_boundary="external",
                max_concurrent=4, tags=("external",), models=("cm",),
                metered_override=True,
            ),
        },
        roles={
            "summarize": Role(
                name="summarize", model="m", lane="interactive",
                placement=("internal", "external"), tier_models={"external": "cm"},
                system="요약한다", max_prompt_chars=overrides.pop("max_chars", 200_000),
            ),
            "inside": Role(
                name="inside", model="m", lane="interactive", placement=("internal",),
            ),
            "vec": Role(
                name="vec", model="embed-m", kind="embed", lane="batch",
                placement=("internal",),
            ),
            "_guard_classify": Role(
                name="_guard_classify", model="guard-m", lane="guard",
                placement=("internal",), internal_only=True,
                system="민감 맥락을 판정한다",
            ),
        },
        lanes={
            "interactive": Lane("interactive", 2),
            "batch": Lane("batch", 1),
            "guard": Lane("guard", 2),
        },
        guard_rules=overrides.pop("guard_rules", (
            GuardRule(
                id="card", kind="pattern", action={"internal": "audit", "external": "full"},
                label="카드번호", pattern=r"\b(?:\d[ -]?){13,19}\b", checksum="luhn",
                keep_tail=4, locale_pack="common",
            ),
            GuardRule(
                id="email", kind="pattern", action="partial", label="이메일",
                pattern=r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", keep_tail=0, locale_pack="common",
            ),
            GuardRule(
                id="kr_rrn", kind="pattern", action="block", label="주민등록번호",
                pattern=r"\b\d{6}[-\s]?\d{7}\b", checksum="kr_rrn", locale_pack="ko_KR",
            ),
        )),
        guard_settings=GuardSettings(),
        pricing=Pricing(
            table={
                "mock": {
                    "cm": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
                    "*": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
                }
            },
        ),
        thresholds=Thresholds(max_retries=2, retry_backoff_seconds=(2, 4, 8)),
        catalog=(
            CatalogEntry(name="m", provider="mock", est_size_gb=5.0),
            CatalogEntry(name="guard-m", provider="mock", est_size_gb=1.2),
            CatalogEntry(name="embed-m", provider="mock", est_size_gb=0.5),
            CatalogEntry(name="cm", provider="mock", est_size_gb=0.0),
        ),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def vault() -> KeyVault:
    import base64

    return KeyVault(base64.b64decode(generate_master_key()))


@pytest.fixture
def store(clock):
    s = SqliteStore(":memory:", now=clock)
    yield s
    s.close()


@pytest.fixture
def harness(config, store, clock, vault, tmp_path):
    """앱 + 부품 묶음. 테스트가 내부 상태를 직접 만질 수 있게 함께 돌려준다."""
    accountant = CostAccountant(config.pricing, store, now=clock)
    channel = RecordingChannel()
    notifier = Notifier([channel], now=clock, min_interval_seconds=0.0)
    cluster = Cluster(
        config, store, accountant=accountant, now=clock, notifier=notifier
    )
    for name, state in cluster.nodes.items():
        state.models = frozenset(config.nodes[name].models)
        state.status = HEALTHY

    guard = Guard(config)
    evaluator = Evaluator(config, store, guard, now=clock)
    registrar = ModelRegistrar(
        config, cluster, store, now=clock, notify=notifier.as_callable()
    )
    completion = CompletionSignal()
    pipeline = Pipeline(
        config, store, cluster, guard,
        vault=vault, accountant=accountant, evaluator=evaluator, now=clock,
        completion=completion,
    )
    scheduler = Scheduler(
        config, store, cluster, accountant=accountant, registrar=registrar,
        now=clock, notifier=notifier, guard=guard, vault=vault,
        completion=completion,
        # 프로덕션 조립(cli.Assembly)과 같은 배선 — 테스트만 다른 모양으로 조립하면
        # 배선 누락이 테스트에 안 걸린다. QA R-HIGH 가 정확히 그 빈칸이었다.
        evaluator=evaluator, certifier_factory=pipeline.make_certifier,
    )

    # 플러그인 설치본과 신뢰 키는 **테스트마다 격리된 임시 경로**에 둔다.
    # 기본값은 저장소 상대 경로라 그대로 두면 테스트가 저장소에 파일을 쓴다.
    data_dir = tmp_path / "data"
    trust_dir = tmp_path / "plugin-trust"

    app = build_app(
        config=config, store=store, cluster=cluster, guard=guard, scheduler=scheduler,
        pipeline=pipeline, vault=vault, evaluator=evaluator, registrar=registrar,
        accountant=accountant, notifier=notifier, now=clock,
        data_dir=data_dir, plugin_trust_dir=trust_dir,
    )

    class Harness:
        pass

    h = Harness()
    h.app = app
    h.config = config
    h.store = store
    h.cluster = cluster
    h.guard = guard
    h.pipeline = pipeline
    h.scheduler = scheduler
    h.evaluator = evaluator
    h.registrar = registrar
    h.vault = vault
    h.clock = clock
    h.accountant = accountant
    h.notifier = notifier
    h.channel = channel
    h.completion = completion
    h.data_dir = data_dir
    h.trust_dir = trust_dir
    return h


def seed_tenant(
    harness, tenant_id: str, *, locale: str = "ko-KR", allow_roles=("*",),
    require_end_user: bool = False, rate_limit: int | None = None,
    budget: float | None = None,
) -> dict[str, str]:
    """테넌트 + 서비스 + 3종 토큰. 반환은 {역할: 원 토큰}."""
    store = harness.store
    store.create_tenant(
        tenant_id, tenant_id.title(), locale=locale, end_user_salt=new_salt(),
        dek_wrapped=harness.vault.create_dek(), rate_limit_per_min=rate_limit,
        budget_usd_per_month=budget,
    )
    scope = TenantScope(tenant_id)
    service_id = f"{tenant_id}-web"
    store.create_service(
        scope, service_id, service_id, allow_roles=list(allow_roles),
        require_end_user=require_end_user,
    )
    tokens = {}
    for role in (ROLE_SERVICE, ROLE_TENANT_ADMIN, ROLE_PLATFORM_ADMIN):
        _, raw = issue_token(store, scope, service_id, role=role)
        tokens[role] = raw
    tokens["service_id"] = service_id
    return tokens


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(harness):
    with TestClient(harness.app) as c:
        yield c


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme")


@pytest.fixture
def globex(harness):
    return seed_tenant(harness, "globex", locale="en-US")
