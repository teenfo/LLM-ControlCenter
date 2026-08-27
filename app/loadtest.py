"""부하 측정 — **추정치를 사실로 승격시키는 절차.**

    python -m app.loadtest

`docs/capacity.md` §6 은 요청당 원가를 단계별로 쪼개 코어당 처리량을 냈고, 그 표
아래에 이렇게 적혀 있다: *"이 수치는 엔지니어링 추정이다. 부하 테스트로 검증하기
전까지 사실로 취급하지 않는다."* 이 모듈이 그 검증이다.

### 왜 제품에 넣는가

**내 하드웨어의 숫자보다 설치처 하드웨어의 숫자가 훨씬 쓸모 있다.** 용량 문서도 같은
말을 한다 — "설치처의 워크로드가 다르면 공식에 자기 값을 넣는다". 그러려면 재는
도구가 손에 있어야 하고, 그래서 번들에 들어간다.

실제 노드도 클라우드 키도 필요 없다. 목 프로바이더로 도는 것이 데모와 같은 이유다.

### 무엇을 재는가

1. **단계별 원가** — capacity §6.1 표와 같은 칸. 가드 1단이 지배적이라는 주장을
   확인한다.
2. **종단 제출 처리량** — 실제 ASGI 앱을 지나는 값. 라우팅·인증·가드·저장이 전부 든다.
3. **큰 프롬프트가 이벤트 루프를 막는가** — 200KB 제출이 도는 동안 작은 요청의 p99.
   스레드 풀 오프로드(capacity §6.2-b)가 실제로 값을 하는지가 여기서 갈린다.
4. **폴링 원가** — 상태 조회는 제출과 다른 크기여야 한다.

### 재지 않는 것

추론이다. 목 프로바이더는 즉시 답하므로 여기 나오는 수치는 **컨트롤 플레인의 한계**
이지 클러스터의 처리량이 아니다. 클러스터 상한은 `노드 × 슬롯 ÷ 평균 지연`이고,
그 둘의 비율이 capacity §6 의 요지다 — 컨트롤 플레인은 병목이 아니다.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import platform
import statistics
import time
from typing import Any, Callable

#: 잴 프롬프트 크기. capacity §6.2-b 표와 같은 칸이다.
SIZES = (4_096, 40_960, 204_800)

#: 각 측정의 기본 반복 수. 큰 프롬프트는 한 번이 비싸므로 적게 돈다.
DEFAULT_ROUNDS = {4_096: 200, 40_960: 60, 204_800: 20}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def _stats(samples: list[float]) -> dict[str, float]:
    """초 단위 표본 → 밀리초 통계 + 초당 처리량.

    **평균만 내지 않는다.** 이벤트 루프를 막는 문제는 평균이 아니라 꼬리에서
    드러나고, 평균만 보면 "괜찮다" 로 읽힌다.

    **최댓값까지 낸다.** 루프가 한 번 크게 서는 사건은 p99 로도 안 잡힌다 — 표본이
    3000개면 149ms 짜리 정지 한 번은 p99 인덱스 바깥에 있다. 머리 막힘은 정의상
    **가장 오래 기다린 요청**의 이야기다.
    """
    if not samples:
        return {"n": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p99_ms": 0.0,
                "max_ms": 0.0, "per_second": 0.0}
    return {
        "n": len(samples),
        "mean_ms": round(statistics.mean(samples) * 1000, 4),
        "p50_ms": round(_percentile(samples, 0.50) * 1000, 4),
        "p99_ms": round(_percentile(samples, 0.99) * 1000, 4),
        "max_ms": round(max(samples) * 1000, 4),
        "per_second": round(1.0 / statistics.mean(samples), 1),
    }


def _measure(fn: Callable[[], Any], rounds: int, warmup: int = 5) -> dict[str, float]:
    """`fn` 을 `rounds` 회 돌리고 각 회의 시간을 모은다.

    워밍업을 버리는 이유: 첫 호출은 정규식 컴파일·임포트·페이지 폴트를 포함해서
    정상 상태보다 훨씬 비싸다. 그것을 평균에 섞으면 **적게 돌수록 느려 보인다.**
    """
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return _stats(samples)


# ── 조립 ────────────────────────────────────────────────────────────────────


def _build(config_dir: str, *, stage1_threshold: int | None = None):
    """측정용 부품 한 벌. 디스크 DB 를 쓴다 — `:memory:` 는 WAL 을 안 태운다.

    `stage1_threshold` 를 주면 가드 1단의 스레드 풀 임계만 바꾼 **독립된 한 벌**이
    나온다. 대조군을 만들 때 앱만 다시 지으면 안 되기 때문이다 — `Pipeline` 이
    생성 시점의 `guard` 를 들고 살아서, 부품을 물려주면 설정이 안 바뀐 가드가
    그대로 따라온다. 그렇게 재면 **같은 구성을 두 번 재고 "차이 없음" 이라고
    적게 된다.**
    """
    import tempfile
    from dataclasses import replace
    from pathlib import Path

    from .auth import ROLE_SERVICE, issue_token
    from .cluster import HEALTHY, Cluster
    from .completion import CompletionSignal
    from .config import load_config
    from .cost import CostAccountant
    from .crypto import KeyVault, generate_master_key
    from .evals import Evaluator
    from .guard import Guard
    from .identity import new_salt
    from .main import build_app
    from .pipeline import Pipeline
    from .store import SqliteStore, TenantScope
    import base64

    config = load_config(config_dir)
    if stage1_threshold is not None:
        config = replace(
            config,
            guard_settings=replace(
                config.guard_settings,
                stage1_threadpool_threshold_bytes=stage1_threshold,
            ),
        )
    workdir = Path(tempfile.mkdtemp(prefix="lcc-loadtest-"))
    store = SqliteStore(workdir / "loadtest.db")
    vault = KeyVault(base64.b64decode(generate_master_key()))

    store.create_tenant(
        "load", "Load", locale="ko-KR", end_user_salt=new_salt(),
        dek_wrapped=vault.create_dek(),
    )
    scope = TenantScope("load")
    store.create_service(scope, "load-web", "web", allow_roles=["*"])
    _, token = issue_token(store, scope, "load-web", role=ROLE_SERVICE)

    accountant = CostAccountant(config.pricing, store)
    cluster = Cluster(config, store, accountant=accountant)
    for name, state in cluster.nodes.items():
        state.models = frozenset(config.nodes[name].models)
        state.status = HEALTHY

    guard = Guard(config)
    completion = CompletionSignal()
    pipeline = Pipeline(
        config, store, cluster, guard, vault=vault, accountant=accountant,
        evaluator=Evaluator(config, store, guard), completion=completion,
    )
    app = build_app(
        config=config, store=store, cluster=cluster, guard=guard, scheduler=None,
        pipeline=pipeline, vault=vault, version="loadtest", start_scheduler=False,
    )

    class Parts:
        pass

    parts = Parts()
    parts.config, parts.store, parts.vault = config, store, vault
    parts.cluster, parts.guard, parts.pipeline = cluster, guard, pipeline
    parts.app, parts.token, parts.scope = app, token, scope
    parts.workdir = workdir
    return parts


def _prompt(size: int) -> str:
    """지정 크기의 한국어 프롬프트.

    한글을 쓰는 이유: UTF-8 에서 한 글자가 3바이트라 **문자 수와 바이트 수가
    다르다.** 영문으로만 재면 정규식이 훑는 실제 길이를 과소평가한다.
    """
    unit = "분기 실적을 요약해 주세요. "
    return (unit * (size // len(unit.encode("utf-8")) + 1))[: size // 3]


# ── 측정 ────────────────────────────────────────────────────────────────────


def measure_stages(parts, rounds: dict[int, int]) -> dict[str, Any]:
    """capacity §6.1 의 단계별 표를 실제로 잰다."""
    from .crypto import prompt_aad
    from .identity import hash_end_user
    from .store import TenantScope

    tenant = parts.store.get_tenant("load")
    salt = tenant["end_user_salt"]
    wrapped = tenant["dek_wrapped"]
    scope = TenantScope("load")
    out: dict[str, Any] = {}

    out["identity_hash"] = _measure(lambda: hash_end_user("u_8f3a91", salt), 2000)

    for size in SIZES:
        text = _prompt(size)
        label = f"{size // 1024}KB"
        # **1단 정규식만.** `inspect()` 는 임계 초과 시 스레드 풀로 넘기므로
        # 원가가 아니라 오프로드 비용까지 섞인다 — 표가 말하는 값은 원가다.
        pattern_rules = [r for r in parts.guard.rules_for(["ko_KR"]) if r.kind == "pattern"]
        out[f"guard_stage1_{label}"] = _measure(
            lambda t=text, r=pattern_rules: parts.guard._scan(t, None, r),
            rounds[size],
        )
        out[f"seal_{label}"] = _measure(
            lambda t=text: parts.vault.seal(wrapped, t, aad=prompt_aad("load", "j")),
            rounds[size],
        )

    # **잡 삽입을 여기서 재지 않는다.** 잡을 만드는 경로는 `pipeline` 하나이고
    # (`tests/test_architecture.py::test_only_the_pipeline_creates_jobs`), 측정
    # 도구라고 그 불변식에 구멍을 내면 그 구멍이 다음 사람의 선례가 된다.
    # 삽입 원가는 아래 종단 제출 수치 안에 들어 있다.
    out["db_poll"] = _measure(lambda: parts.store.job_status(scope, "none"), 2000)
    out["place_release"] = _measure(_placement_probe(parts), 500)
    return out


def _placement_probe(parts) -> Callable[[], Any]:
    """배치 한 번(획득 + 해제)의 원가.

    **재는 이유**: 점유 장부가 DB 로 가면서 배치마다 쓰기 트랜잭션이 하나 늘었고,
    SQLite 는 라이터를 직렬화한다. 정합성을 얻는 대가가 얼마인지 모른 채 그 거래를
    하면 안 된다 — 자릿수가 무너지면 설계를 되돌려야 한다.

    비교 대상은 클러스터 상한이다. 노드 3대 × 슬롯 3개면 평균 지연 3.3초 기준
    **2.7 job/초**가 상한이므로, 배치 원가가 그보다 서너 자릿수 싸면 병목이 아니다.
    """
    from .cluster import PLACED

    counter = itertools.count()
    role = next(
        (r for r in parts.config.roles.values() if r.kind != "embed"),
        next(iter(parts.config.roles.values())),
    )

    def once() -> None:
        result = parts.cluster.place(
            job_id=f"loadtest-place-{next(counter)}", tenant_id="load",
            service_id="load-web", role=role,
            placement_snapshot=role.placement, prompt="가" * 200,
        )
        if result.outcome == PLACED and result.placement is not None:
            parts.cluster.release(result.placement)

    return once


async def measure_submit(parts, rounds: dict[int, int]) -> dict[str, Any]:
    """실제 ASGI 앱을 지나는 제출 처리량. 라우팅·인증·가드·저장이 전부 든다."""
    import httpx

    out: dict[str, Any] = {}
    transport = httpx.ASGITransport(app=parts.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://loadtest"
    ) as client:
        headers = {"Authorization": f"Bearer {parts.token}"}
        for size in SIZES:
            text = _prompt(size)
            label = f"{size // 1024}KB"
            body = {"role": "summarize", "prompt": text, "wait": 0}

            async def once() -> None:
                await client.post("/v1/generate", headers=headers, json=body)

            for _ in range(3):
                await once()
            samples: list[float] = []
            for _ in range(rounds[size]):
                started = time.perf_counter()
                await once()
                samples.append(time.perf_counter() - started)
            out[f"submit_{label}"] = _stats(samples)

        # 폴링 — 상태 조회는 제출과 다른 크기여야 한다.
        created = await client.post(
            "/v1/generate", headers=headers,
            json={"role": "summarize", "prompt": "짧은 프롬프트", "wait": 0},
        )
        job_id = created.json()["job_id"]
        samples = []
        for _ in range(300):
            started = time.perf_counter()
            await client.get(f"/v1/jobs/{job_id}", headers=headers)
            samples.append(time.perf_counter() - started)
        out["poll"] = _stats(samples)
    return out


async def measure_poll_under_queue_depth(
    parts, depths: tuple[int, ...] = (0, 100, 500, 1000), rounds: int = 30
) -> dict[str, Any]:
    """**폴 한 번의 원가가 큐 깊이에 비례하는가.**

    capacity §6.2-a 가 이 시스템의 유일한 파국 경로로 지목한 되먹임이다:

        클러스터 포화 → 큐 증가 → 대기 잡 증가 → 폴링 증가 → 컨트롤 플레인 과부하

    적응형 `retry_after` 는 폴링 **빈도**를 damp 한다. 그런데 폴 한 번의 **원가**가
    깊이에 비례하면 같은 되먹임에 gain 을 얹는 셈이라, 빈도를 줄인 만큼을 원가가
    도로 가져간다. 그래서 둘을 따로 재야 한다.
    """
    import httpx

    transport = httpx.ASGITransport(app=parts.app)
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://loadtest"
    ) as client:
        headers = {"Authorization": f"Bearer {parts.token}"}

        async def enqueue(prompt: str) -> str:
            response = await client.post(
                "/v1/generate", headers=headers,
                json={"role": "summarize", "prompt": prompt, "wait": 0},
            )
            return response.json()["job_id"]

        # **큐도 진짜 경로로 채운다.** 스토어에 직접 넣으면 프롬프트가 비어 있어
        # 행 크기가 실제와 다르고, 재려는 것이 바로 그 행 크기의 영향이다.
        watched = await enqueue("짧은 프롬프트")
        filler = _prompt(4_096)

        for depth in depths:
            while parts.store.count_queued("interactive") < depth:
                await enqueue(filler)
            for _ in range(3):
                await client.get(f"/v1/jobs/{watched}", headers=headers)
            samples: list[float] = []
            for _ in range(rounds):
                started = time.perf_counter()
                await client.get(f"/v1/jobs/{watched}", headers=headers)
                samples.append(time.perf_counter() - started)
            # **목표가 아니라 실제 깊이로 라벨을 단다.** 앞 단계가 남긴 잡이 있으면
            # 목표보다 깊고, 그 차이를 숨기면 표가 거짓말이 된다.
            actual = parts.store.count_queued("interactive")
            out[f"queue_depth_{actual}"] = _stats(samples)
    return out


async def measure_head_of_line(
    config_dir: str, *, window_seconds: float = 0.35, heavy_requests: int = 2,
    small_interval: float = 0.002, trials: int = 12,
) -> dict[str, Any]:
    """**200KB 제출이 도는 동안 작은 요청이 멈추는가.**

    capacity §6.2-b 의 주장이 여기서 갈린다. 큰 프롬프트의 1단을 이벤트 루프 위에서
    동기로 돌리면 그동안 다른 모든 요청이 선다. 임계를 크게 올려 오프로드를 끈
    대조군을 나란히 잰다 — 장치가 값을 하는지는 그 대조로만 알 수 있다.

    ### 한 번 재서는 답이 안 나온다 — 분포를 본다

    오프로드를 켠 쪽의 정지 시간은 **양봉으로 튄다.** 같은 코드로 46ms 가 나오기도
    하고 95ms 가 나오기도 한다 — 루프 스레드가 스캔 스레드에게서 GIL 을 되찾는
    경합의 결과라 실행마다 다르다. 한 번만 재면 그날 나온 표본으로 정반대 결론을
    쓰게 되므로, `trials` 회 반복해서 중앙값과 최댓값을 함께 낸다.

    ### 대조군은 부품 한 벌을 통째로 다시 짓는다

    앱만 다시 지으면 안 된다. `Pipeline` 이 생성 시점의 `guard` 를 들고 살기 때문에
    부품을 물려주면 **임계가 안 바뀐 가드가 그대로 따라오고**, 그러면 같은 구성을 두
    번 재게 된다. 처음에 그렇게 재서 "오프로드가 값을 안 한다" 는 결론이 나왔었다 —
    장치가 무의미한 게 아니라 **대조군이 대조군이 아니었다.**

    ### 1차 신호는 **루프 감시견**이다

    작은 요청의 지연으로만 판단하면 안 된다. 앞 요청이 끝나야 다음이 출발하는 구조라
    루프가 막힌 시간이 지연에서 새어 나가고(coordinated omission), 반대로 밀린 요청이
    몰리는 것까지 섞여 들어온다. 그래서 **2ms 마다 깨어나기만 하는 감시견**을 띄우고
    그 깨어남 간격의 최대치를 낸다 — "큰 프롬프트 한 건이 다른 모두를 세우는가" 에
    대한 직접적인 답은 그 값이다.

    작은 요청을 아예 안 넣으면 안 된다. 루프에 다른 일이 없으면 GIL 을 달라고 조르는
    스레드가 없어서, 오프로드를 켜도 스캔 스레드가 GIL 을 계속 쥔다 — 그렇게 재면
    양쪽이 똑같이 나온다(실측: 작은 요청 없이 148.6ms 대 144.4ms, 있으면 50.4ms 대
    152.2ms). **부하가 있어야 오프로드가 값을 한다**는 사실 자체가 결과의 일부다.

    무거운 요청이 창 안에 **몇 건 끝났는지**도 같이 낸다. 오프로드는 공짜가 아니라
    거래다 — 스캔 자체는 경합 때문에 5~10% 느려진다.
    """
    import httpx

    big = _prompt(204_800)

    async def run(parts) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {parts.token}"}
        transport = httpx.ASGITransport(app=parts.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://loadtest"
        ) as client:
            for _ in range(5):
                await client.get("/healthz")

            # **창은 무거운 일을 띄우는 순간부터다.** 여기서 먼저 `await` 을 하면
            # 막는 구성은 그 한 스텝 안에서 스캔을 통째로 끝내 버리고, 창은 정지가
            # 지나간 뒤에야 열린다 — 그러면 막는 쪽이 이겨 보인다.
            gaps: list[float] = []
            stop = asyncio.Event()

            async def watchdog() -> None:
                """2ms 마다 깨어나기만 한다. 못 깨어난 시간이 곧 루프가 선 시간이다."""
                last = time.perf_counter()
                while not stop.is_set():
                    await asyncio.sleep(0.002)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            samples: list[float] = []

            async def sampler() -> None:
                """작은 요청을 `small_interval` 간격으로 넣는다.

                **별도 태스크여야 한다.** 무거운 일을 기다리는 코루틴이 겸하면 그
                코루틴의 스케줄링이 측정에 섞인다 — 겸하게 했더니 양쪽 구성이 똑같이
                나왔고, 태스크로 떼어내자 3배 차이가 드러났다.

                간격을 **실제로 매번 자는** 것이 중요하다. 밀린 만큼 몰아치게 하면
                루프가 `select` 로 안 돌아가고, 그러면 GIL 이 루프 스레드에 붙어 있어
                오프로드한 스캔이 되레 굶는다 — 재려던 것과 다른 것을 재게 된다.
                """
                while not stop.is_set():
                    await asyncio.sleep(small_interval)
                    if stop.is_set():
                        break
                    started = time.perf_counter()
                    await client.get("/healthz")
                    samples.append(time.perf_counter() - started)

            # **작은 부하를 먼저 띄우고 자리를 잡게 한다.** 스캔이 시작되는 순간에
            # 루프 스레드가 이미 GIL 을 조르고 있어야 한다 — 스캔이 붙은 뒤에 부하를
            # 띄우면 루프가 `epoll` 에서 굶고, 오프로드가 값을 안 하는 것처럼 보인다.
            watcher = asyncio.create_task(watchdog())
            load = asyncio.create_task(sampler())
            await asyncio.sleep(0.02)

            heavy = [
                asyncio.create_task(
                    client.post(
                        "/v1/generate", headers=headers,
                        json={"role": "summarize", "prompt": big, "wait": 0},
                    )
                )
                for _ in range(heavy_requests)
            ]

            await asyncio.sleep(window_seconds)
            done = sum(1 for task in heavy if task.done())
            stop.set()
            await asyncio.gather(watcher, load)
            await asyncio.gather(*heavy)
            return {
                **_stats(samples),
                "loop_stall_max_ms": round(max(gaps) * 1000, 3) if gaps else 0.0,
                "heavy_done_in_window": done,
                "heavy_total": heavy_requests,
            }

    out: dict[str, Any] = {}
    for label, threshold in (("with_offload", None), ("without_offload", 10**9)):
        stalls: list[float] = []
        latest: dict[str, Any] = {}
        for _ in range(trials):
            parts = _build(config_dir, stage1_threshold=threshold)
            try:
                latest = await run(parts)
            finally:
                parts.store.close()
            stalls.append(latest["loop_stall_max_ms"])
        stalls.sort()
        out[label] = {
            **latest,
            "loop_stall_median_ms": round(statistics.median(stalls), 3),
            "loop_stall_max_ms": stalls[-1],
            "loop_stall_min_ms": stalls[0],
            "trials": trials,
        }
    return out


# ── 출력 ────────────────────────────────────────────────────────────────────


def _environment() -> dict[str, Any]:
    """**어디서 잰 값인지 없으면 숫자가 의미를 잃는다.**

    이 도구가 존재하는 이유가 "설치처 하드웨어에서 재라" 이므로, 결과에 환경을
    붙이지 않으면 남의 노트북 숫자를 자기 용량 산정에 쓰게 된다.
    """
    import os

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def _table(title: str, rows: dict[str, Any]) -> str:
    lines = [f"\n{title}", "-" * len(title)]
    lines.append(
        f"{'측정':<26}{'평균(ms)':>11}{'p50':>10}{'p99':>10}{'최대':>11}{'초당':>12}"
    )
    for name, stat in rows.items():
        lines.append(
            f"{name:<26}{stat['mean_ms']:>11.3f}{stat['p50_ms']:>10.3f}"
            f"{stat['p99_ms']:>10.3f}{stat['max_ms']:>11.3f}"
            f"{stat['per_second']:>12.1f}"
        )
        if "loop_stall_median_ms" in stat:
            lines.append(
                f"{'  └ 루프 정지':<25}중앙 {stat['loop_stall_median_ms']:>6.1f}ms"
                f"  (최소 {stat['loop_stall_min_ms']:.0f} ~ 최대 "
                f"{stat['loop_stall_max_ms']:.0f}, {stat['trials']}회)"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.loadtest",
        description="컨트롤 플레인 부하 측정 — 추정치를 자기 하드웨어의 사실로 바꾼다",
    )
    parser.add_argument("--config", default=None, help="설정 디렉터리")
    parser.add_argument("--json", action="store_true", help="기계용 JSON 으로 출력")
    parser.add_argument(
        "--quick", action="store_true", help="반복 수를 줄인다(연기 시험용)"
    )
    args = parser.parse_args(argv)

    from .cli_paths import bundled

    config_dir = args.config or str(bundled("config"))
    rounds = (
        {size: max(5, count // 10) for size, count in DEFAULT_ROUNDS.items()}
        if args.quick
        else dict(DEFAULT_ROUNDS)
    )

    parts = _build(config_dir)
    try:
        result: dict[str, Any] = {"environment": _environment()}
        result["stages"] = measure_stages(parts, rounds)
        result["submit"] = asyncio.run(measure_submit(parts, rounds))
        result["head_of_line"] = asyncio.run(
            measure_head_of_line(
                config_dir, trials=4 if args.quick else 12
            )
        )
        result["poll_depth"] = asyncio.run(
            measure_poll_under_queue_depth(
                parts,
                depths=(0, 100) if args.quick else (0, 100, 500, 1000),
                rounds=10 if args.quick else 30,
            )
        )
    finally:
        parts.store.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    env = result["environment"]
    print(f"환경: {env['platform']} · Python {env['python']} · 코어 {env['cpu_count']}")
    print(_table("단계별 원가", result["stages"]))
    print(_table("종단 (ASGI 앱 통과)", result["submit"]))
    print(_table("200KB 제출 중 작은 요청의 지연", result["head_of_line"]))
    print(_table("큐 깊이별 폴 원가", result["poll_depth"]))
    print(
        "\n주의: 목 프로바이더로 잰 **컨트롤 플레인의 한계**이지 클러스터 처리량이"
        " 아니다.\n클러스터 상한은 `노드 × 슬롯 ÷ 평균 지연` 이고, 그 둘의 비율이"
        " capacity §6 의 요지다."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
