#!/usr/bin/env python3
"""LLM ControlCenter 시연 시나리오 — 제품이 하는 일을 API 로 차례차례 보여 준다.

    python examples/showcase/showcase.py --url https://llmcc.example --token lcc_... [--end-user demo-user]
                                        [--admin-token lcc_...] [--only 2,3,4] [--seed 20] [--pace 1]

장면(`--only` 로 고른다):
  1 역할 — 이 토큰이 쓸 수 있는 역할·레인·한도 (GET /v1/roles)
  2 정상 생성 — 요약 한 건, 모델·노드·시도·지연
  3 개인정보 마스킹 — 이메일·카드번호가 노드로 가기 전에 가려진다 (guard_actions · 저장된 마스킹본)
  4 차단 — 주민등록번호는 제출 자체가 거부된다 (422 guard_blocked). 유예 모드면 마스킹으로 내려앉는다
  5 대화 — 두 턴. 서버는 기억하지 않고 턴 배열을 받는다 (POST /v1/chat)
  6 임베딩 — 동기 경로, 같은 관문 (POST /v1/embed)
  7 배치 레인 — wait=0 으로 넣고 큐 상태를 본 뒤 완료까지
  8 멱등성 — 같은 Idempotency-Key 로 두 번 보내면 같은 잡
  9 관리자 시야 — 사용량 추이·가드 미리보기·가드 사건·감사 (--admin-token 이 있을 때)

`--seed N` 은 대시보드를 채우는 트래픽이다 — 역할을 돌아가며 N 건을 끝까지 돌린다(개인정보 섞인 문장 포함).

표준 라이브러리 + 번들 SDK(`clients/client.py`)만 쓴다. 저장소 밖에서 쓰려면 호스트에서 SDK 를 받아 이 파일 옆에 둔다:
    curl -H "Authorization: Bearer $LCC_TOKEN" -o client.py https://llmcc.example/v1/client/client.py
비밀은 인자나 환경(LCC_URL · LCC_TOKEN · LCC_ADMIN_TOKEN)으로만 받고 화면에 찍지 않는다.
데모 프로파일(`python -m app serve --demo`, 목 프로바이더)에서도 같은 스크립트가 돈다 — 발표 전 예행은 거기서.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parents[1] / "clients"):
    if (candidate / "client.py").is_file():
        sys.path.insert(0, str(candidate))
        break
try:
    from client import Blocked, ControlCenter, ControlCenterError, RateLimited
except ImportError:  # pragma: no cover - 안내문이다
    sys.exit(
        "SDK(client.py) 를 못 찾았다 — 저장소 안에서 실행하거나, 호스트에서 받아 이 파일 옆에 두세요:\n"
        '  curl -H "Authorization: Bearer $LCC_TOKEN" -o client.py <URL>/v1/client/client.py'
    )

# 체크섬을 통과하는 샘플 — 가드는 검증기가 붙어 있어 아무 숫자나 안 걸린다. 실존 인물·실카드가 아니다.
SAMPLE_CARD = "4111 1111 1111 1111"
SAMPLE_RRN = "990101-1234563"
SAMPLE_EMAIL = "hong@example.com"

ARTICLE = (
    "지난 분기 고객지원팀은 문의 응답 시간을 평균 14시간에서 6시간으로 줄였다. 야간 문의를 자동 분류해 "
    "담당자에게 바로 배정한 것이 가장 큰 요인이었고, 반복 질문은 사내 지식베이스 답변으로 먼저 안내했다. "
    "다만 환불 관련 문의는 여전히 사람이 끝까지 처리해야 했고, 그 비중은 전체의 18% 였다."
)
SEED_PROMPTS = [
    ("summarize", "다음 글을 세 문장으로 요약해 줘.\n\n" + ARTICLE),
    ("classify", "다음 문의를 환불·배송·계정·기타 중 하나로 분류하고 이유를 한 줄로 적어 줘: '지난주에 주문한 상품이 아직 안 왔어요'"),
    ("summarize", f"고객 {SAMPLE_EMAIL} 가 보낸 메일을 한 줄로 요약해 줘: 결제 카드 {SAMPLE_CARD} 가 두 번 청구된 것 같다는 내용"),
    ("chat", "사내 위키에 올릴 'LLM 사용 가이드' 머리말을 두 문단으로 써 줘."),
    ("analyze", "다음 지표의 추세와 원인을 분석해 줘.\n\n" + ARTICLE),
    ("summarize", "다음 글의 핵심을 글머리표 세 개로 정리해 줘.\n\n" + ARTICLE),
    ("embed", ["환불 요청", "배송 지연 문의", "비밀번호 재설정"]),
    ("chat", "주간 보고서의 '다음 주 계획' 항목을 네 개 제안해 줘. 각 항목은 한 줄로."),
    ("blocked", f"주민등록번호 {SAMPLE_RRN} 고객의 가입 정보를 찾아 줘"),
    ("summarize", "아래 회의록에서 결정 사항만 뽑아 줘.\n\n" + ARTICLE),
]


# ── 출력 ─────────────────────────────────────────────────────────────────────


def say(line: str = "") -> None:
    print(line, flush=True)


def head(number: int, title: str) -> None:
    say()
    say(f"── {number}. {title} " + "─" * max(4, 60 - len(title) * 2))


def note(line: str) -> None:
    say(f"  · {line}")


def clip(text: str, limit: int = 240) -> str:
    text = (text or "").strip().replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def spark(values: list[int]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    top = max(values) if values and max(values) > 0 else 1
    return "".join(bars[min(7, int(v / top * 7.999))] if v else " " for v in values)


# ── 호출 ─────────────────────────────────────────────────────────────────────


class Show:
    def __init__(self, url: str, token: str, end_user: str, admin_token: str | None, quiet: bool) -> None:
        self.api = ControlCenter(url, token)
        self.admin = ControlCenter(url, admin_token) if admin_token else None
        self.end_user = end_user
        self.quiet = quiet
        self.roles: list[dict[str, Any]] = []

    # -- 역할 고르기: 이름이 아니라 종류로. 설치처마다 역할 이름이 다르다.
    def load_roles(self) -> None:
        self.roles = self.api.roles()

    def pick(self, kind: str, *, lane: str | None = None, prefer: tuple[str, ...] = ()) -> str | None:
        pool = [r for r in self.roles if r.get("kind", "generate") == kind and (lane is None or r.get("lane") == lane)]
        for name in prefer:
            if any(r["name"] == name for r in pool):
                return name
        return pool[0]["name"] if pool else None

    def run(self, role: str, prompt: str | None = None, **kwargs: Any):
        """끝까지 기다리되, 레이트리밋은 서버가 준 만큼 쉬고 다시 간다."""
        while True:
            try:
                return self.api.run(role, prompt, end_user=self.end_user, wait=30, **kwargs)
            except RateLimited as exc:
                delay = exc.retry_after or 5.0
                note(f"레이트리밋({exc.scope}) — {delay:.0f}초 뒤 다시")
                time.sleep(delay)

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """SDK 가 안 싣는 헤더(Idempotency-Key)를 붙일 때만 쓴다."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.api.base_url + path, data=data, method="POST",
            headers={"Authorization": f"Bearer {self.api.token}", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except Exception:
                return exc.code, {}

    def describe(self, result, started: float) -> None:
        elapsed = time.monotonic() - started
        note(f"status={result.status}  model={result.model or '—'}  node={result.node or '—'}  "
             f"attempts={result.attempts}  {elapsed:.1f}s")
        if result.guard_actions:
            note("guard_actions=" + json.dumps(result.guard_actions, ensure_ascii=False))
        if result.error:
            note(f"error={result.error_code or ''} {result.error}")
        if result.text and not self.quiet:
            note("응답: " + clip(result.text))

    def masked_copy(self, job_id: str) -> str | None:
        """기록 API 에 남은 마스킹본 — 노드와 저장소가 본 것이 이것이다."""
        try:
            body = self.api._request("GET", f"/v1/jobs?end_user={urllib.parse.quote(self.end_user)}&limit=20")
        except ControlCenterError:
            return None
        for row in body.get("jobs", []):
            if row.get("job_id") == job_id:
                return row.get("prompt_masked")
        return None


# ── 장면 ─────────────────────────────────────────────────────────────────────


def scene_roles(show: Show) -> None:
    head(1, "역할 — 이 토큰이 쓸 수 있는 것")
    for r in show.roles:
        note(f"{r['name']:<12} kind={r.get('kind', 'generate'):<8} lane={r.get('lane', '-'):<11} "
             f"timeout={r.get('timeout_seconds', '-')}s  max_prompt={r.get('max_prompt_chars', '-')}")
    if not show.roles:
        note("역할이 없다 — 서비스 토큰이 아닌 토큰이거나, 서비스에 허용 역할이 없다")


def scene_generate(show: Show) -> None:
    head(2, "정상 생성 — 요약 한 건")
    role = show.pick("generate", prefer=("summarize",))
    if not role:
        return note("generate 역할이 없어 건너뜀")
    note(f"역할 {role} 에 기사 한 토막을 보낸다")
    started = time.monotonic()
    show.describe(show.run(role, "다음 글을 세 문장으로 요약해 줘.\n\n" + ARTICLE), started)


def scene_masking(show: Show) -> None:
    head(3, "개인정보 마스킹 — 노드로 가기 전에 가려진다")
    role = show.pick("generate", prefer=("summarize",))
    if not role:
        return note("generate 역할이 없어 건너뜀")
    prompt = f"고객 {SAMPLE_EMAIL} 가 보낸 메일을 한 줄로 요약해 줘: 결제 카드 {SAMPLE_CARD} 가 두 번 청구된 것 같다는 내용"
    note("보내는 문장: " + clip(prompt))
    started = time.monotonic()
    result = show.run(role, prompt)
    show.describe(result, started)
    masked = show.masked_copy(result.job_id)
    if masked:
        note("저장된 마스킹본: " + clip(masked))
        # 등급이 말하는 대로 됐는가 — full/partial 은 값이 사라져야 하고, audit 은 그대로 나가되 기록만 남는다.
        actions = result.guard_actions
        for rule_ids, name, value in ((("credit_card", "card"), "카드번호", SAMPLE_CARD), (("email",), "이메일", SAMPLE_EMAIL)):
            action = next((actions[r] for r in rule_ids if r in actions), None)
            gone = value not in masked
            if action in ("full", "partial"):
                note(f"{name}: 등급 {action} → {'가려짐' if gone else '남음(!) — 등급과 다르다'}")
            elif action == "audit":
                note(f"{name}: 등급 audit → 그대로 나가고 탐지만 기록된다(베이스라인의 뜻 — 정상 업무 데이터인 경우가 많다)")
            else:
                note(f"{name}: 규칙에 안 걸림 → {'없음' if gone else '그대로'}")
    else:
        note("기록 API 에서 이 잡을 못 찾았다(엔드유저 표기나 보존 설정을 확인)")


def scene_block(show: Show) -> None:
    head(4, "차단 등급 — 베이스라인은 가리고, 차단은 승격으로")
    role = show.pick("generate", prefer=("summarize",))
    if not role:
        return note("generate 역할이 없어 건너뜀")
    prompt = f"주민등록번호 {SAMPLE_RRN} 고객의 가입 정보를 찾아 줘"
    note("보내는 문장: " + clip(prompt))
    started = time.monotonic()
    try:
        result = show.run(role, prompt)
    except Blocked as exc:
        note(f"422 guard_blocked — 걸린 규칙 {exc.rules}. 잡도 저장도 없다. ({time.monotonic() - started:.1f}s)")
        note("block 등급이 실제로 막았다 — 이 테넌트가 측정을 거쳐 승격해 둔 규칙이다")
        return
    show.describe(result, started)
    action = result.guard_actions.get("kr_rrn")
    if action in ("full", "partial"):
        note(f"주민등록번호는 노드로 가기 전에 가려졌다(등급 {action}). 제출은 거부되지 않았다 —")
        note("  번들 베이스라인은 어떤 패턴도 즉시 차단하지 않는다. 차단(block)은 audit 로 재서 오탐률을 본 뒤 승격 게이트를 지나야 켜진다(GUARD-12).")
        note("  이미 block 으로 올린 규칙이 있는데도 가려지기만 했다면 **가드 유예 모드**가 켜진 것이다 — 장면 9 의 미리보기가 그 사실을 말해 준다.")
    else:
        note("주민등록번호가 걸리지 않았다 — 이 테넌트의 로케일(ko-KR → 팩 ko_KR)과 규칙을 확인할 것")


def scene_chat(show: Show) -> None:
    head(5, "대화 — 서버는 기억하지 않고 턴 배열을 받는다")
    role = show.pick("chat")
    if not role:
        return note("chat 역할이 없어 건너뜀")
    turns = [{"role": "user", "content": "우리 팀이 LLM 을 안전하게 쓰려면 먼저 무엇을 정해야 할까? 세 가지만."}]
    started = time.monotonic()
    first = show.run(role, messages=turns)
    show.describe(first, started)
    if not first.ok:
        return
    turns += [{"role": "assistant", "content": first.text}, {"role": "user", "content": "두 번째 항목을 한 문단으로 풀어 줘."}]
    started = time.monotonic()
    show.describe(show.run(role, messages=turns), started)
    note(f"두 번째 요청은 턴 {len(turns)}개를 통째로 보냈다 — 대화 기억은 호출자 몫이다")


def scene_embed(show: Show) -> None:
    head(6, "임베딩 — 동기 경로, 같은 관문")
    role = show.pick("embed")
    if not role:
        return note("embed 역할이 없어 건너뜀")
    inputs = ["환불 요청", "배송 지연 문의", "비밀번호 재설정"]
    started = time.monotonic()
    try:
        body = show.api.embed(role, inputs, end_user=show.end_user)
    except ControlCenterError as exc:
        return note(f"실패 {exc.status} {exc.code}: {exc}")
    vectors = body.get("vectors") or []
    dims = len(vectors[0]) if vectors else 0
    note(f"{len(inputs)}건 → 벡터 {len(vectors)}개 × {dims}차원  model={body.get('model', '—')}  {time.monotonic() - started:.1f}s")


def scene_batch(show: Show) -> None:
    head(7, "배치 레인 — 큐에 넣고, 상태를 보고, 완료까지")
    role = show.pick("generate", lane="batch", prefer=("analyze",))
    if not role:
        return note("batch 레인 역할이 없어 건너뜀")
    started = time.monotonic()
    first = show.api.generate(role, "다음 지표의 추세와 원인을 분석해 줘.\n\n" + ARTICLE, end_user=show.end_user, wait=0)
    note(f"제출 직후: status={first.status} queue_position={first.queue_position} wait_reason={first.wait_reason or '-'} "
         f"retry_after={first.retry_after}")
    result = first
    while not result.done and time.monotonic() - started < 600:
        time.sleep(min(result.retry_after or 2.0, 10.0))
        result = show.api.job(result.job_id, wait=30)
    show.describe(result, started)


def scene_idempotency(show: Show) -> None:
    head(8, "멱등성 — 같은 키로 두 번 보내면 같은 잡")
    role = show.pick("generate", prefer=("summarize",))
    if not role:
        return note("generate 역할이 없어 건너뜀")
    key = f"showcase-{uuid.uuid4()}"
    body = {"role": role, "prompt": "한 문장으로: 멱등성 키는 왜 필요한가?", "end_user": show.end_user, "wait": 0}
    st1, first = show.post("/v1/generate", body, {"Idempotency-Key": key})
    st2, second = show.post("/v1/generate", body, {"Idempotency-Key": key})
    same = first.get("job_id") and first.get("job_id") == second.get("job_id")
    note(f"1회차 {st1} job={first.get('job_id', '?')}  2회차 {st2} job={second.get('job_id', '?')}  → {'같은 잡' if same else '다른 잡(!)'}")
    if first.get("job_id"):
        result = show.api.job(first["job_id"], wait=30)
        while not result.done:
            time.sleep(min(result.retry_after or 2.0, 10.0))
            result = show.api.job(first["job_id"], wait=30)
        note(f"그 잡의 결과: status={result.status} model={result.model or '—'}")


def scene_admin(show: Show) -> None:
    head(9, "관리자 시야 — 추이 · 미리보기 · 사건 · 감사")
    if not show.admin:
        return note("--admin-token 이 없어 건너뜀 (테넌트 관리자 토큰)")
    admin = show.admin
    now = time.time()
    # 보는 쪽 시간대(분) — 일 묶음의 자정을 맞추는 값. 서머타임 중이면 altzone 이 현재 오프셋이다.
    offset = -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone) // 60
    try:
        usage = admin._request("GET", f"/v1/admin/usage?series=hour&since={int(now - 86400)}&offset_minutes={offset}")
        rows = usage.get("rows", [])
        calls = sum(r["calls"] for r in rows)
        tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
        note(f"최근 30일: 호출 {calls} · 토큰 {tokens} · 지출 ${usage.get('spend_usd', 0):.4f} · "
             f"예산 소진율 {(usage.get('budget') or {}).get('burn_rate', 0) * 100:.1f}%")
        points = (usage.get("series") or {}).get("points", [])
        if points:
            note("최근 24시간 요청 추이: " + spark([p["calls"] for p in points]) + f"  (합 {sum(p['calls'] for p in points)})")
        for r in rows[:5]:
            note(f"  {r['key']:<16} 호출 {r['calls']:>4}  성공률 {r['success_rate'] * 100:5.1f}%  평균 {r['avg_duration_ms']} ms")
    except ControlCenterError as exc:
        note(f"사용량 조회 실패 {exc.status} {exc.code}")

    try:
        prompt = f"고객 {SAMPLE_EMAIL} 의 카드 {SAMPLE_CARD} 와 주민번호 {SAMPLE_RRN} 를 확인해 줘"
        preview = admin._request("POST", "/v1/admin/guard/preview", {"prompt": prompt})
        note("가드 미리보기 — 잡 없이 판정만:")
        note("  원문: " + clip(prompt))
        for boundary in ("internal", "external"):
            v = preview["boundaries"][boundary]
            note(f"  경계 {boundary:<8} {'통과' if v['allowed'] else '차단'}: " + (clip(v["prompt"]) if v["prompt"] else "(전송되지 않음)"))
        note("  걸린 규칙: " + ", ".join(f"{d['rule_id']}({d['actions']['internal']}/{d['actions']['external']})" for d in preview["detections"]))
        if preview.get("grace_mode"):
            note("  유예 모드가 켜져 있다 — 차단이 마스킹으로 낮춰진 결과다")
    except ControlCenterError as exc:
        note(f"미리보기 실패 {exc.status} {exc.code} (0.7.0 이상 호스트가 필요하다)")

    try:
        events = admin._request("GET", "/v1/admin/guard/events?limit=50").get("events", [])
        by_rule: dict[str, int] = {}
        for e in events:
            by_rule[e.get("rule_id", "?")] = by_rule.get(e.get("rule_id", "?"), 0) + 1
        note("가드 사건(최근 50건): " + (", ".join(f"{k} {v}건" for k, v in sorted(by_rule.items())) or "없음") + " — 값은 어디에도 없다")
        audit = admin._request("GET", "/v1/admin/audit?limit=8").get("audit", [])
        note("감사 최근 행: " + ", ".join(a.get("action", "?") for a in audit))
    except ControlCenterError as exc:
        note(f"사건·감사 조회 실패 {exc.status} {exc.code}")


SCENES = {
    1: scene_roles, 2: scene_generate, 3: scene_masking, 4: scene_block, 5: scene_chat,
    6: scene_embed, 7: scene_batch, 8: scene_idempotency, 9: scene_admin,
}


# ── 대시보드 채우기 ───────────────────────────────────────────────────────────


def seed(show: Show, count: int, pace: float) -> None:
    head(0, f"시연 데이터 적재 — {count}건")
    done = ok = 0
    started_all = time.monotonic()
    for index in range(count):
        kind, payload = SEED_PROMPTS[index % len(SEED_PROMPTS)]
        started = time.monotonic()
        label = kind
        try:
            if kind == "embed":
                role = show.pick("embed")
                if not role:
                    continue
                body = show.api.embed(role, payload, end_user=show.end_user)
                status, model = "ok", body.get("model") or "—"
            elif kind == "chat":
                role = show.pick("chat")
                if not role:
                    continue
                result = show.run(role, messages=[{"role": "user", "content": payload}], deadline=240)
                status, model = result.status, result.model or "—"
            else:
                want = "generate"
                role = show.pick(want, lane="batch", prefer=("analyze",)) if kind == "analyze" else show.pick(want, prefer=(kind, "summarize"))
                if not role:
                    continue
                label = role if kind != "blocked" else "blocked"
                result = show.run(role, payload, deadline=240)
                status, model = result.status, result.model or "—"
        except Blocked as exc:
            status, model = "blocked(" + ",".join(exc.rules) + ")", "—"
        except ControlCenterError as exc:
            status, model = f"error {exc.status} {exc.code}", "—"
        done += 1
        ok += status == "ok"
        say(f"  #{index + 1:<3} {label:<12} {status:<28} {model:<22} {time.monotonic() - started:5.1f}s")
        if pace:
            time.sleep(pace)
    note(f"끝 — {done}건 중 성공 {ok}건, {time.monotonic() - started_all:.0f}초")


# ── 진입 ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM ControlCenter 시연 시나리오", formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("장면(")[0].strip())
    parser.add_argument("--url", default=os.environ.get("LCC_URL", "http://127.0.0.1:8610"), help="호스트 주소 (기본: $LCC_URL 또는 로컬 데모)")
    parser.add_argument("--token", default=os.environ.get("LCC_TOKEN"), help="서비스 토큰 (기본: $LCC_TOKEN)")
    parser.add_argument("--admin-token", default=os.environ.get("LCC_ADMIN_TOKEN"), help="테넌트 관리자 토큰 — 장면 9 (기본: $LCC_ADMIN_TOKEN)")
    parser.add_argument("--end-user", default="showcase", help="엔드유저 표기 (기본: showcase)")
    parser.add_argument("--only", help="돌릴 장면 번호, 쉼표 구분 (예: 2,3,4)")
    parser.add_argument("--seed", type=int, default=0, help="대시보드를 채울 요청 수 — 장면 대신 적재만 한다")
    parser.add_argument("--pace", type=float, default=0.0, help="적재 요청 사이 쉬는 초")
    parser.add_argument("--quiet", action="store_true", help="응답 본문을 찍지 않는다")
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("--token 또는 $LCC_TOKEN 이 필요하다")

    show = Show(args.url, args.token, args.end_user, args.admin_token, args.quiet)
    try:
        show.load_roles()
    except ControlCenterError as exc:
        say(f"역할을 못 읽었다 — {exc.status} {exc.code}: {exc}")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        say(f"호스트에 닿지 못했다 — {args.url}: {exc}")
        return 1
    say(f"호스트 {args.url} · 역할 {len(show.roles)}개 · 엔드유저 {args.end_user}")

    if args.seed:
        seed(show, args.seed, args.pace)
        return 0

    wanted = sorted(SCENES) if not args.only else [int(n) for n in args.only.split(",") if n.strip()]
    for number in wanted:
        scene = SCENES.get(number)
        if scene is None:
            note(f"장면 {number} 은 없다")
            continue
        try:
            scene(show)
        except ControlCenterError as exc:
            note(f"실패 {exc.status} {exc.code}: {exc}")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
