"""완료 신호 — **폴링을 없애지 않고 줄인다.**

이 파일이 지키는 불변식 하나: **신호가 틀려도 요청은 끝난다.** 다중 워커에서는 신호가
프로세스를 넘지 못하므로 늘 안 오는데, 그때의 동작이 신호를 넣기 전과 같아야 한다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.completion import CompletionSignal
from app.store import TenantScope
from tests.conftest import seed_tenant

ACME = TenantScope("acme")


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme")


async def test_a_waiter_is_woken_by_the_signal():
    signal = CompletionSignal()
    with signal.waiting_on("j1"):
        signal.done("j1")
        assert await signal.wait("j1", timeout=5.0) is True


async def test_an_unsignalled_wait_times_out():
    """**다중 워커의 정상 경로다.** 신호가 안 와도 예전만큼 자고 넘어가야 한다."""
    signal = CompletionSignal()
    with signal.waiting_on("j1"):
        assert await signal.wait("j1", timeout=0.01) is False


async def test_waiting_on_an_unregistered_job_just_sleeps():
    """등록 밖 호출은 실수지만, 최적화가 요청을 죽이면 안 된다."""
    signal = CompletionSignal()
    assert await signal.wait("없는잡", timeout=0.01) is False


async def test_the_registry_does_not_leak():
    """오래 뜬 채로 도는 프로세스라 누수가 그대로 메모리다."""
    signal = CompletionSignal()
    with signal.waiting_on("j1"):
        assert signal.pending == 1
    assert signal.pending == 0


async def test_an_exception_still_unregisters():
    """예외 경로에서 해제를 빠뜨리면 그 항목이 영원히 쌓인다."""
    signal = CompletionSignal()
    with pytest.raises(RuntimeError):
        with signal.waiting_on("j1"):
            raise RuntimeError("무언가 터졌다")
    assert signal.pending == 0


async def test_a_frozen_dataclass_exception_passes_through():
    """**`@contextlib.contextmanager` 로 만들면 여기서 깨진다.**

    예외가 블록을 지날 때 파이썬이 `gen.throw(exc)` 로 `__traceback__` 을 건드리는데,
    이 저장소의 `ApiError` 는 동결 데이터클래스라 그 대입이 터진다. 실제로 그렇게
    만들었다가 테넌트 격리 테스트가 잡았다.
    """
    from app.i18n import ApiError

    signal = CompletionSignal()
    with pytest.raises(ApiError):
        with signal.waiting_on("j1"):
            raise ApiError("job_not_found", status=404)
    assert signal.pending == 0


async def test_two_waiters_on_the_same_job_are_both_woken():
    """먼저 나가는 쪽이 지워 버리면 남은 쪽은 영원히 신호를 못 받는다."""
    signal = CompletionSignal()
    with signal.waiting_on("j1"):
        with signal.waiting_on("j1"):
            assert signal.pending == 1
            signal.done("j1")
            assert await signal.wait("j1", timeout=5.0) is True
        # 안쪽이 나가도 바깥쪽 등록은 살아 있어야 한다.
        assert signal.pending == 1
        assert await signal.wait("j1", timeout=5.0) is True
    assert signal.pending == 0


async def test_the_scheduler_actually_signals_on_completion(harness, acme):
    """**배선이 끊겨도 폴링이 덮으므로 증상이 안 난다.**

    파이프라인과 스케줄러가 신호 객체를 따로 만들면 신호가 아무 데도 안 닿는데,
    대기는 여전히 폴링으로 끝난다 — 유일한 증상이 "대기가 늘 최대치" 뿐이라
    아무도 눈치채지 못한다. 그래서 결과가 아니라 **호출 자체**를 본다.
    """
    signalled: list[str] = []
    original = harness.completion.done
    harness.completion.done = lambda job_id: (signalled.append(job_id), original(job_id))[1]

    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="요약",
        placement=["internal", "external"],
    )
    try:
        for _ in range(6):
            await harness.scheduler.tick("interactive")
            await asyncio.sleep(0)
    finally:
        harness.completion.done = original

    assert job_id in signalled, "스케줄러가 완료를 알리지 않았다 — 배선이 끊겼다"


async def test_the_scheduler_wakes_a_waiting_request(harness, acme):
    """종단 — 스케줄러가 잡을 끝내면 대기 중인 요청이 **바로** 돌아온다."""
    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="요약",
        placement=["internal", "external"],
    )

    async def drain():
        for _ in range(6):
            await harness.scheduler.tick("interactive")
            await asyncio.sleep(0)

    waiter = asyncio.create_task(
        harness.pipeline.wait_for(ACME, job_id, seconds=5.0)
    )
    await asyncio.sleep(0)
    await drain()
    result = await asyncio.wait_for(waiter, timeout=5.0)

    assert result.status == "ok", "대기가 완료를 못 받았다"
    assert harness.completion.pending == 0, "대기 등록이 남았다"


async def test_a_cancelled_job_wakes_its_waiter(harness, acme):
    """취소도 종결이다 — 안 깨우면 끝난 잡을 최대 대기 시간까지 붙잡는다."""
    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="요약",
    )
    with harness.completion.waiting_on(job_id):
        harness.pipeline.cancel(ACME, job_id, actor="tenant_admin")
        assert await harness.completion.wait(job_id, timeout=5.0) is True


async def test_a_retry_does_not_wake_the_waiter(harness, acme):
    """중간 전이까지 알리면 이벤트가 켜진 채 남아 다음 대기가 헛돈다."""
    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="요약",
    )
    with harness.completion.waiting_on(job_id):
        harness.store.update_job(ACME, job_id, status="queued", attempts=1)
        assert await harness.completion.wait(job_id, timeout=0.01) is False
