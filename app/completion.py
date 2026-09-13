"""잡 완료 통지 — **같은 프로세스 안에서만.**

밖으로의 통지(완료 웹훅)를 기각한 근거는 문서화돼 있다: 아웃바운드는 인증과 SSRF
문제를 만들고, 그것을 감수할 만큼 폴링이 나쁘지 않다. 그런데 그 자리에 **안에서의
통지를 아무것도 두지 않아서**, `wait` 구현이 프로세스 내부 DB 폴링으로 귀결됐다.
같은 프로세스 안에서는 SSRF 도 인증도 문제가 아니다 — 기각한 이유가 하나도 적용되지
않는 자리에 기각의 결과만 남아 있었던 셈이다.

### 폴링을 없애지 않는다. 줄일 뿐이다.

다중 워커가 지원 구성이 되면서(architecture.md §11) 이 항목의 난이도가 올라갔다.
잡을 끝내는 프로세스는 **스케줄러**이고 `wait` 하는 프로세스는 **API 워커**다.
`asyncio.Event` 는 프로세스를 넘지 못하므로, 워커를 늘린 순간 이 신호는 도달하지
않는다.

그래서 신호를 **진실의 원천으로 쓰지 않는다.** 대기 루프는 그대로 DB 를 폴링하고,
이 신호는 `asyncio.sleep(interval)` 자리를 `wait(job_id, timeout=interval)` 로
바꿀 뿐이다:

- 단일 프로세스 — 신호가 즉시 오므로 다음 폴이 바로 돈다. 완료까지의 지연이
  최대 `MAX_POLL_INTERVAL` 에서 사실상 0 으로 줄어든다.
- 다중 워커 — 신호가 안 오므로 타임아웃되고, **오늘과 완전히 같게 동작한다.**

신호를 놓쳐도 폴이 잡으므로 잃어버린 깨움이 매달린 요청이 되지 않는다. 최적화는
틀려도 느려질 뿐이어야 한다.

### 종결 전이에서만 부른다

중간 전이(재시도로 `queued` 로 되돌아가는 것)까지 알리면 이벤트가 켜진 채 남아
다음 대기가 헛돈다. 종결에서만 부르면 **깨어난 시점에 잡은 이미 끝나 있으므로**
이벤트를 되돌릴 필요가 없고, 그래서 지우고-기다리는 사이의 경합도 없다.
"""

from __future__ import annotations

import asyncio


class CompletionSignal:
    """잡 id → 대기자들. 프로세스 하나 안에서만 유효하다."""

    def __init__(self) -> None:
        #: 잡 id → (이벤트, 대기자 수). **수를 세는 이유는 누수 때문이다** — 같은
        #: 잡을 두 요청이 기다릴 수 있고, 먼저 나가는 쪽이 지워 버리면 남은 쪽은
        #: 영원히 신호를 못 받는다.
        self._waiters: dict[str, tuple[asyncio.Event, int]] = {}

    def waiting_on(self, job_id: str) -> "_Waiting":
        """대기 등록과 해제를 **짝지어 강제한다.**

        해제를 손으로 부르게 두면 예외 경로에서 언젠가 빠뜨리고, 빠뜨린 항목은
        프로세스가 살아 있는 동안 계속 쌓인다 — 관제 서버는 오래 뜬 채로 도는
        프로세스라 그 누수가 그대로 메모리다.
        """
        return _Waiting(self, job_id)

    # -- 등록·해제 (컨텍스트 매니저가 부른다) ---------------------------------

    def _enter(self, job_id: str) -> None:
        event, count = self._waiters.get(job_id, (asyncio.Event(), 0))
        self._waiters[job_id] = (event, count + 1)

    def _leave(self, job_id: str) -> None:
        entry = self._waiters.get(job_id)
        if entry is None:
            return
        event, count = entry
        if count <= 1:
            self._waiters.pop(job_id, None)
        else:
            self._waiters[job_id] = (event, count - 1)

    def done(self, job_id: str) -> None:
        """이 잡이 **종결됐다**고 알린다. 대기자가 없으면 아무 일도 하지 않는다.

        스케줄러가 부른다. 대기자가 없는 것이 정상이다 — 소비자 대부분은 `wait` 없이
        제출하고 나중에 조회한다.
        """
        entry = self._waiters.get(job_id)
        if entry is not None:
            entry[0].set()

    async def wait(self, job_id: str, *, timeout: float) -> bool:
        """신호가 오거나 `timeout` 이 지날 때까지 기다린다. 신호를 받았으면 True.

        **등록 안 된 잡은 그냥 잔다.** 호출자가 `waiting_on` 밖에서 부르는 것은
        실수지만, 여기서 예외를 던지면 대기 경로가 깨진다 — 최적화가 요청을
        죽이면 안 된다.
        """
        entry = self._waiters.get(job_id)
        if entry is None:
            await asyncio.sleep(timeout)
            return False
        try:
            await asyncio.wait_for(entry[0].wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    @property
    def pending(self) -> int:
        """대기 중인 잡 수. 누수를 테스트가 볼 수 있게 하는 창이다."""
        return len(self._waiters)


class _Waiting:
    """`waiting_on` 이 돌려주는 컨텍스트 매니저.

    **제너레이터 기반(`@contextlib.contextmanager`)이면 안 된다.** 예외가 이 블록을
    지날 때 파이썬이 `gen.throw(exc)` 를 부르면서 예외 객체의 `__traceback__` 을
    건드리는데, 이 저장소의 `ApiError` 는 **동결 데이터클래스**라 그 대입이
    `FrozenInstanceError` 로 터진다 — 대기 루프 안에서 `job_not_found` 를 던지는
    정상 경로가 통째로 깨진다.

    실제로 그렇게 만들었다가 테넌트 격리 테스트가 잡았다. 클래스 기반 `__exit__`
    는 예외 객체를 건드리지 않으므로 그 문제가 없다.
    """

    __slots__ = ("_signal", "_job_id")

    def __init__(self, signal: CompletionSignal, job_id: str) -> None:
        self._signal = signal
        self._job_id = job_id

    def __enter__(self) -> "_Waiting":
        self._signal._enter(self._job_id)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._signal._leave(self._job_id)
