"""멱등성 키 — **재시도가 두 번째 청구서를 만들지 않는다.**

`wait` + pending 모델은 타임아웃-재시도를 오히려 장려한다. 소비자가 30초 기다리다
`pending` 을 받고 네트워크가 끊기면, 그가 할 수 있는 합리적인 행동은 재시도다 —
그리고 그 재시도가 두 번째 잡을 만들면 metered 경로에서 두 번 과금된다.

### 유일성은 DB 가 지킨다

"먼저 조회하고 없으면 삽입" 은 다중 워커에서 반드시 진다. 두 워커가 조회를 나란히
통과하는 창이 실재하고, 그 창을 애플리케이션 락으로 막으려 해도 락이 프로세스를
넘지 못한다(architecture.md §11). 그래서 유일 인덱스를 걸고 **삽입 실패를 정상
경로로** 다룬다. `test_multiprocess.py` 가 그것을 진짜 프로세스로 잰다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.store import IDEMPOTENCY_TTL_HOURS, TenantScope
from tests.conftest import auth, seed_tenant

ACME = TenantScope("acme")


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme")


def submit(client, tokens, **body):
    payload = {"role": "summarize", "prompt": "요약해줘", "wait": 0, **body}
    headers = dict(auth(tokens["service"]))
    key = payload.pop("_key", None)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/v1/generate", headers=headers, json=payload)


# ── 기본 계약 ───────────────────────────────────────────────────────────────


def test_the_same_key_returns_the_same_job(client, acme):
    """**이 파일에서 가장 중요한 단언이다.** 재시도가 잡을 하나 더 만들지 않는다."""
    first = submit(client, acme, _key="retry-1")
    second = submit(client, acme, _key="retry-1")

    assert first.status_code in (200, 202), first.text
    assert second.status_code in (200, 202)
    assert first.json()["job_id"] == second.json()["job_id"]


def test_without_a_key_every_request_is_a_new_job(client, acme):
    """헤더를 안 보내면 예전과 같다 — 멱등성은 **옵트인**이다."""
    first = submit(client, acme)
    second = submit(client, acme)

    assert first.json()["job_id"] != second.json()["job_id"]


def test_an_empty_key_is_not_a_key(client, acme):
    """빈 문자열을 키로 받으면 그것을 보낸 소비자 전원이 잡 하나를 공유한다."""
    first = submit(client, acme, _key="   ")
    second = submit(client, acme, _key="")

    assert first.json()["job_id"] != second.json()["job_id"]


def test_a_different_key_makes_a_different_job(client, acme):
    assert (
        submit(client, acme, _key="a").json()["job_id"]
        != submit(client, acme, _key="b").json()["job_id"]
    )


def test_an_overlong_key_is_refused(client, acme):
    """키는 유일 인덱스에 들어가고 잡 행에 붙어 산다 — 프롬프트를 키로 넣으면 안 된다."""
    response = submit(client, acme, _key="x" * 5000)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_field"


# ── 격리 ────────────────────────────────────────────────────────────────────


def test_keys_do_not_collide_across_tenants(harness, client, acme):
    """`retry-1` 은 흔한 값이다. 테넌트를 가로질러 부딪히면 남의 응답을 받는다."""
    globex = seed_tenant(harness, "globex")

    mine = submit(client, acme, _key="retry-1")
    theirs = submit(client, globex, _key="retry-1")

    assert mine.status_code in (200, 202)
    assert theirs.status_code in (200, 202)
    assert mine.json()["job_id"] != theirs.json()["job_id"]


def test_keys_do_not_collide_across_services(harness, client, acme):
    """한 테넌트의 두 서비스도 갈려야 한다 — A 가 B 의 응답을 받으면 안 된다."""
    store = harness.store
    store.create_service(ACME, "acme-batch", "batch", allow_roles=["*"])
    from app.auth import ROLE_SERVICE, issue_token

    _, batch_token = issue_token(store, ACME, "acme-batch", role=ROLE_SERVICE)

    web = submit(client, acme, _key="retry-1")
    batch = client.post(
        "/v1/generate",
        headers={**auth(batch_token), "Idempotency-Key": "retry-1"},
        json={"role": "summarize", "prompt": "요약해줘", "wait": 0},
    )

    assert batch.status_code in (200, 202), batch.text
    assert web.json()["job_id"] != batch.json()["job_id"]


# ── 완료된 잡의 재시도 ──────────────────────────────────────────────────────


async def test_retrying_a_finished_job_returns_its_result(harness, client, acme):
    """끝난 뒤 재시도하면 **그 결과**가 온다 — 다시 돌리지 않는다."""
    for state in harness.cluster.nodes.values():
        state.provider.reply = "요약 결과입니다"

    first = submit(client, acme, _key="retry-1")
    job_id = first.json()["job_id"]

    for _ in range(6):
        await harness.scheduler.tick("interactive")
        await asyncio.sleep(0)

    again = submit(client, acme, _key="retry-1")
    body = again.json()

    assert body["job_id"] == job_id
    assert body["status"] == "ok"
    assert body["response"] == "요약 결과입니다"
    assert harness.store._conn.execute(
        "SELECT COUNT(*) AS n FROM jobs"
    ).fetchone()["n"] == 1, "재시도가 잡을 하나 더 만들었다"


async def test_the_retry_does_not_charge_twice(harness, client, acme):
    """**metered 경로에서 이 공백이 곧 이중 과금이다.**"""
    first = submit(client, acme, _key="retry-1")
    for _ in range(6):
        await harness.scheduler.tick("interactive")
        await asyncio.sleep(0)

    before = harness.store.spend_since(ACME, 0.0)
    submit(client, acme, _key="retry-1")
    for _ in range(6):
        await harness.scheduler.tick("interactive")
        await asyncio.sleep(0)

    assert harness.store.spend_since(ACME, 0.0) == before
    rows = harness.store._conn.execute("SELECT COUNT(*) AS n FROM usage").fetchone()
    assert rows["n"] <= 1, "재시도가 사용량을 두 번 기록했다"
    assert first.json()["job_id"]


# ── TTL ─────────────────────────────────────────────────────────────────────


def test_the_key_is_released_after_the_window(harness, client, acme):
    """**잡 보존(30일)과 같이 두면 안 된다.**

    한 달 뒤 같은 키를 다시 쓴 소비자는 새 작업을 원하는 것이지 옛 응답을 원하는
    것이 아니다. 그 사이에 프롬프트도 모델도 바뀌었을 수 있다.
    """
    first = submit(client, acme, _key="retry-1")

    harness.clock.advance(IDEMPOTENCY_TTL_HOURS * 3600 + 60)
    harness.store.purge_expired(job_retention_days=30, raw_prompt_retention_days=7)

    second = submit(client, acme, _key="retry-1")
    assert second.json()["job_id"] != first.json()["job_id"], "창을 넘긴 키가 안 풀렸다"


def test_the_job_row_outlives_the_key(harness, client, acme):
    """키만 놓아주고 잡은 남긴다 — 보존 기간은 잡의 몫이다."""
    job_id = submit(client, acme, _key="retry-1").json()["job_id"]

    harness.clock.advance(IDEMPOTENCY_TTL_HOURS * 3600 + 60)
    harness.store.purge_expired(job_retention_days=30, raw_prompt_retention_days=7)

    job = harness.store.get_job(ACME, job_id)
    assert job is not None, "키를 놓아주면서 잡까지 지웠다"
    assert job.idempotency_key is None


# ── 계약 ────────────────────────────────────────────────────────────────────


def test_the_contract_documents_the_header(client, acme):
    """소비자가 모르면 없는 기능이다."""
    guide = client.get("/v1/integration", headers=auth(acme["service"])).text

    assert "Idempotency-Key" in guide
    assert "재시도" in guide


# ── D8 — 토큰 처리율은 계기지 한도가 아니다 ─────────────────────────────────


def record(store, *, tokens_in: int, tokens_out: int = 0, tenant: str = "acme") -> None:
    store.record_usage(
        TenantScope(tenant), service_id=f"{tenant}-web", job_id=None, role="summarize",
        model="m", node="in-1", provider="mock", status="ok",
        input_tokens=tokens_in, output_tokens=tokens_out,
    )


def test_the_token_rate_is_per_minute(harness, acme):
    """창 안의 합계를 분으로 나눈 값이다."""
    record(harness.store, tokens_in=600, tokens_out=300)

    rate = harness.store.token_rate(ACME, window_seconds=60.0)

    assert rate["input_tokens_per_minute"] == 600.0
    assert rate["output_tokens_per_minute"] == 300.0
    assert rate["tokens_per_minute"] == 900.0


def test_the_rate_sees_what_the_request_count_cannot(harness, acme):
    """**이것이 D8 의 요지다.**

    큰 프롬프트 1건과 작은 프롬프트 1건은 레이트리밋에서 같은 1건이고, 무료 경로면
    예산에서도 같은 0 달러다. 토큰 축이 없으면 그 차이가 어디에도 안 보인다.
    """
    big = seed_tenant(harness, "big")
    record(harness.store, tokens_in=50_000, tenant="big")
    record(harness.store, tokens_in=100, tenant="acme")

    heavy = harness.store.token_rate(TenantScope("big"), window_seconds=60.0)
    light = harness.store.token_rate(ACME, window_seconds=60.0)

    assert heavy["calls_per_minute"] == light["calls_per_minute"], "건수로는 같다"
    assert heavy["tokens_per_minute"] > light["tokens_per_minute"] * 100
    assert big


def test_the_rate_is_not_a_limit(harness, client, acme):
    """**상한을 걸지 않았다.** 설치처의 분포를 모르는 채 건 한도는 꺼진다."""
    record(harness.store, tokens_in=10_000_000)

    response = client.post(
        "/v1/generate", headers=auth(acme["service"]),
        json={"role": "summarize", "prompt": "요약", "wait": 0},
    )
    assert response.status_code in (200, 202), "처리율이 요청을 막았다 — 계기여야 한다"


def test_the_console_shows_the_tenant_rate(harness, client, acme):
    record(harness.store, tokens_in=600)

    body = client.get("/v1/admin/usage", headers=auth(acme["tenant_admin"])).json()

    assert "token_rate" in body, "관제 API 에 토큰 처리율이 없다"
    assert body["token_rate"]["tokens_per_minute"] > 0


def test_the_metrics_carry_no_tenant_label(harness, client, acme):
    """전체가 보는 대시보드에 테넌트별 소비량이 뜨면 그것도 정보 유출이다."""
    record(harness.store, tokens_in=600)

    text = client.get("/metrics", headers=auth(acme["platform_admin"])).text
    lines = [ln for ln in text.splitlines() if "tokens_per_minute" in ln]

    assert lines, "토큰 처리율이 메트릭에 없다"
    assert not any("acme" in ln for ln in lines), f"테넌트 이름이 라벨에 실렸다: {lines}"
    assert any('direction="input"' in ln for ln in lines)


def test_the_rate_window_excludes_old_usage(harness, acme):
    """순간값은 튀고 전체 평균은 어제 일을 오늘로 끌고 온다."""
    record(harness.store, tokens_in=6000)
    harness.clock.advance(3600)

    assert harness.store.token_rate(ACME, window_seconds=300.0)["tokens_per_minute"] == 0.0
