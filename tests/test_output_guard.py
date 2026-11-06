"""출력 축 — **나가는 것만 거르면 제품의 한 문장이 절반만 참이다.**

제품은 "나가는 프롬프트에서 개인정보를 걸러낸다" 고 말하는데, 순서 계약·2단 판정·
경계별 마스킹·원문 암호화·보존 기간이 전부 입력에만 있었다. 응답은 평문 TEXT 로
저장되고, 마스킹도 별도 보존도 열람 감사도 없었다.

응답에 민감정보가 실리는 경로는 가정이 아니다 — 요약·추출 작업의 산출물 자체가
개인정보이거나, 모델이 마스킹되지 않은 문맥을 재구성하거나, 인젝션이 시스템
프롬프트를 응답으로 끌어낸다.

여기 있는 테스트는 그 절반을 고정한다. 입력 축의 대응물이 `test_guard.py` ·
`test_pipeline.py` 에 있고, **같은 규칙이 양쪽에서 같게 동작하는지**가 요지다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.cluster import HEALTHY, Cluster
from app.crypto import KeyVault, Sealed, prompt_aad, response_aad
from app.guard import OUTPUT_BOUNDARY, STAGE_OUTPUT, Guard
from app.scheduler import Scheduler
from app.store import CIPHER_COLUMNS, SqliteStore, TenantScope
from tests.conftest import auth, seed_tenant

ACME = TenantScope("acme")

#: 체크섬을 통과하는 값들. 통과 못 하는 값을 쓰면 규칙이 안 걸려 테스트가
#: **아무것도 검증하지 않으면서 통과한다.**
VALID_RRN = "990101-1234563"
VALID_CARD = "4111 1111 1111 1111"
EMAIL = "hong@example.com"


@pytest.fixture
def acme(harness):
    return seed_tenant(harness, "acme", locale="ko-KR")


def reply_with(harness, text: str) -> None:
    """모든 목 노드가 이 텍스트로 답하게 한다 — 모델이 PII 를 뱉는 상황."""
    for state in harness.cluster.nodes.values():
        state.provider.reply = text


async def run_one(harness, *, role: str = "summarize", prompt: str = "요약해줘") -> str:
    """잡 하나를 끝까지 돌리고 잡 id 를 돌려준다."""
    job_id = harness.store.create_job(
        ACME, service_id="acme-web", role=role, lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked=prompt,
        placement=["internal", "external"],
    )
    for _ in range(6):
        await harness.scheduler.tick("interactive")
        await asyncio.sleep(0)
    return job_id


# ── 마스킹 ──────────────────────────────────────────────────────────────────


async def test_the_response_is_masked_before_it_is_stored(harness, acme):
    """**저장된 응답에 원문 값이 없다.**

    저장이 먼저고 마스킹이 나중이면 그 사이에 원문이 평문으로 DB 에 남는다 —
    입력에서 "필터가 저장보다 먼저" 를 계약으로 못박은 것과 같은 이유다.
    """
    reply_with(harness, f"담당자 연락처는 {EMAIL} 입니다")
    job_id = await run_one(harness)

    job = harness.store.get_job(ACME, job_id)
    assert job.status == "ok"
    assert EMAIL not in (job.response or ""), "응답에 원문 이메일이 그대로 남았다"
    assert "이메일" in job.response


async def test_the_consumer_receives_the_masked_response(harness, client, acme):
    """소비자가 받는 것도 마스킹본이다 — 저장만 가리고 전송은 안 가리면 무의미하다."""
    reply_with(harness, f"카드 {VALID_CARD} 로 결제되었습니다")

    submitted = client.post(
        "/v1/generate", headers=auth(acme["service"]),
        # `wait: 0` — 스케줄러를 이 테스트가 직접 돌리므로 요청 안에서 기다리면
        # 아무도 잡을 집어 가지 않아 그대로 멈춘다.
        json={"role": "summarize", "prompt": "요약해줘", "wait": 0},
    )
    assert submitted.status_code in (200, 202), submitted.text
    job_id = submitted.json()["job_id"]

    for _ in range(6):
        await harness.scheduler.tick("interactive")
        await asyncio.sleep(0)

    fetched = client.get(f"/v1/jobs/{job_id}", headers=auth(acme["service"]))
    body = fetched.json()
    assert body["status"] == "ok"
    assert VALID_CARD not in body["response"]
    assert "카드번호" in body["response"]


async def test_a_blocking_rule_masks_the_response_instead_of_dropping_it(harness, acme):
    """출력에서 `block` 은 **`full` 로 강등된다.**

    입력의 block 은 요청을 아예 처리하지 않는 것이라 아무것도 낭비되지 않는다.
    출력의 block 은 추론이 끝난 뒤다 — 응답을 통째로 버리면 소비자는 비용만 내고
    아무것도 못 받는다. 값은 가려지고 나머지는 쓸 수 있어야 한다.
    """
    reply_with(harness, f"신청자 주민번호는 {VALID_RRN} 입니다")
    job_id = await run_one(harness)

    job = harness.store.get_job(ACME, job_id)
    assert job.status == "ok", "출력 차단이 잡을 죽였다"
    assert VALID_RRN not in job.response
    assert "주민등록번호" in job.response
    assert "신청자" in job.response, "값만 가려야 하는데 응답을 통째로 버렸다"


async def test_a_clean_response_is_left_alone(harness, acme):
    """걸릴 것이 없으면 한 글자도 바뀌지 않는다. 오탐은 필터를 꺼지게 만든다."""
    reply_with(harness, "요약: 실적이 개선되었습니다.")
    job_id = await run_one(harness)

    assert harness.store.get_job(ACME, job_id).response == "요약: 실적이 개선되었습니다."


async def test_tenant_rules_apply_to_the_response_too(harness, acme):
    """테넌트가 조인 규칙이 **출력에도** 걸린다.

    입력에서만 걸리면 그 테넌트의 규칙이 절반만 동작하는 셈이고, 그 비대칭은
    어디에도 안 드러난다 — 관리자는 조였다고 믿는다.
    """
    harness.store.set_tenant_guard_rule(
        ACME,
        {
            "id": "internal_code", "kind": "pattern", "action": "full",
            "label": "[사내코드]", "pattern": r"\bACME-\d{4}\b",
            "keep_tail": 0, "locale_pack": "common",
        },
        updated_by="tenant_admin",
    )
    reply_with(harness, "관련 문서는 ACME-4821 입니다")
    job_id = await run_one(harness)

    job = harness.store.get_job(ACME, job_id)
    assert "ACME-4821" not in job.response
    assert "[사내코드]" in job.response


# ── 봉인 ────────────────────────────────────────────────────────────────────


async def test_the_raw_response_is_sealed_not_discarded(harness, acme):
    """마스킹본만 남기면 관리자가 무슨 일이 났는지 영영 못 본다 — 원문은 봉인한다."""
    reply_with(harness, f"연락처 {EMAIL}")
    job_id = await run_one(harness)

    job = harness.store.get_job(ACME, job_id)
    assert job.response_cipher, "응답 원문이 봉인되지 않았다"
    assert job.response_nonce
    assert EMAIL.encode() not in job.response_cipher, "암호문에 평문이 비친다"


async def test_the_response_ciphertext_cannot_be_transplanted_into_the_prompt(
    harness, acme
):
    """응답 암호문은 **자기 필드에** 묶인다.

    프롬프트와 같은 AAD 로 묶으면 응답 암호문을 프롬프트 컬럼에 옮겨 심어도 열린다 —
    관리자가 원문 열람을 눌렀을 때 감사에는 "프롬프트를 봤다" 고 남고 화면에는
    응답이 뜬다. 감사와 실제가 어긋나는 것이 열람 경로에서 가장 나쁜 실패다.
    """
    reply_with(harness, f"연락처 {EMAIL}")
    job_id = await run_one(harness)

    job = harness.store.get_job(ACME, job_id)
    tenant = harness.store.get_tenant("acme")
    sealed = Sealed(nonce=job.response_nonce, ciphertext=job.response_cipher)

    # 자기 AAD 로는 열린다.
    assert EMAIL in harness.vault.open(
        tenant["dek_wrapped"], sealed, aad=response_aad("acme", job_id)
    )

    # 프롬프트 AAD 로는 안 열린다 — 같은 잡, 같은 DEK 인데도.
    from app.crypto import CryptoError

    with pytest.raises(CryptoError):
        harness.vault.open(
            tenant["dek_wrapped"], sealed, aad=prompt_aad("acme", job_id)
        )


async def test_no_key_means_no_response_ciphertext(store, clock, config):
    """**KEK 가 없으면 암호문 자체를 안 만든다.** 평문 폴백 경로는 존재하지 않는다.

    마스킹은 여전히 돈다 — 키가 없다고 필터까지 꺼지면 그 설치처는 응답을 통째로
    무방비로 내보낸다.
    """
    store.create_tenant("acme", "Acme", locale="ko-KR", end_user_salt=b"s", dek_wrapped=None)
    store.create_service(ACME, "acme-web", "web")

    cluster = Cluster(config, store, now=clock)
    for name, state in cluster.nodes.items():
        state.models = frozenset(config.nodes[name].models)
        state.status = HEALTHY
        state.provider.reply = f"연락처 {EMAIL}"

    scheduler = Scheduler(
        config, store, cluster, now=clock,
        guard=Guard(config), vault=KeyVault(None),   # 금고가 꺼져 있다
    )
    job_id = store.create_job(
        ACME, service_id="acme-web", role="summarize", lane="interactive",
        kind="generate", status="queued", priority=0, prompt_masked="요약",
        placement=["internal", "external"],
    )
    for _ in range(6):
        await scheduler.tick("interactive")
        await asyncio.sleep(0)

    job = store.get_job(ACME, job_id)
    assert job.status == "ok"
    assert job.response_cipher is None, "키가 없는데 암호문이 생겼다"
    assert EMAIL not in job.response, "키가 없다고 마스킹까지 꺼졌다"


# ── 열람 ────────────────────────────────────────────────────────────────────


async def test_reading_the_raw_response_is_audited(harness, client, acme):
    """원문 열람은 감사에 남고, **무엇을 열었는지**까지 남는다."""
    reply_with(harness, f"연락처 {EMAIL}")
    job_id = await run_one(harness)

    read = client.get(
        f"/v1/admin/jobs/{job_id}/raw", headers=auth(acme["tenant_admin"])
    )
    assert read.status_code == 200, read.text
    assert EMAIL in read.json()["response"]

    entries = harness.store.list_audit(ACME, limit=50)
    reads = [e for e in entries if e["action"] == "read_raw_prompt"]
    assert reads, "원문 열람이 감사에 안 남았다"
    detail = json.loads(reads[0]["detail_json"])
    assert "response" in detail.get("fields", []), \
        "응답을 열었는데 감사에는 그 사실이 없다"
    assert EMAIL not in reads[0]["detail_json"], "감사에 원문이 실렸다"


async def test_the_masked_response_needs_no_special_permission(harness, client, acme):
    """마스킹본은 일반 조회로 보인다 — 원문만 별도 경로다."""
    reply_with(harness, f"연락처 {EMAIL}")
    await run_one(harness)

    listed = client.get("/v1/admin/jobs", headers=auth(acme["tenant_admin"]))
    assert listed.status_code == 200
    assert EMAIL not in listed.text, "목록 화면에 원문이 실렸다"


# ── 보존 · 반출 ─────────────────────────────────────────────────────────────


async def test_the_response_ciphertext_expires_with_the_prompt(harness, acme):
    """응답 원문은 프롬프트 원문과 **같은 보존 축**을 쓴다.

    프롬프트만 지우면 지워진 줄 알았던 원문이 응답 컬럼에 남는다 — 보관 기간이
    절반만 지켜지는 것이고, 그 사실은 아무 데도 안 드러난다.
    """
    reply_with(harness, f"연락처 {EMAIL}")
    job_id = await run_one(harness)
    assert harness.store.get_job(ACME, job_id).response_cipher

    harness.clock.advance(8 * 86400)
    harness.store.purge_expired(job_retention_days=30, raw_prompt_retention_days=7)

    job = harness.store.get_job(ACME, job_id)
    assert job.response_cipher is None, "응답 원문이 보존 기간을 넘겨 살아남았다"
    assert job.prompt_cipher is None
    assert job.response, "마스킹본까지 지웠다 — 그건 잡 보존 주기의 몫이다"


async def test_the_export_never_carries_response_ciphertext(harness, acme):
    """내보내기 파일이 원문을 나르면 보관 기간과 접근 감사가 그 파일 밖에서 무력화된다."""
    reply_with(harness, f"연락처 {EMAIL}")
    await run_one(harness)

    exported = harness.store.export_tenant(ACME)
    for job in exported["jobs"]:
        assert not (CIPHER_COLUMNS & set(job)), f"내보내기에 암호문이 실렸다: {job.keys()}"


def test_the_backup_strips_every_cipher_column(tmp_path):
    """백업도 같은 목록을 쓴다 — 한 곳에서만 빠지면 그쪽이 원문을 나른다."""
    from app.backup import snapshot

    source = tmp_path / "src.db"
    store = SqliteStore(source)
    store.create_tenant("acme", "Acme", end_user_salt=b"s")
    job_id = store.create_job(
        ACME, service_id="acme-web", role="r", lane="interactive",
        kind="generate", status="ok", priority=0, prompt_masked="x",
    )
    store.update_job(
        ACME, job_id,
        prompt_cipher=b"P" * 32, prompt_nonce=b"n" * 12,
        response_cipher=b"R" * 32, response_nonce=b"m" * 12,
    )
    store.close()

    target = tmp_path / "out.db"
    snapshot(source, target)

    import sqlite3

    conn = sqlite3.connect(target)
    row = conn.execute(
        "SELECT prompt_cipher, prompt_nonce, response_cipher, response_nonce FROM jobs"
    ).fetchone()
    conn.close()
    assert row == (None, None, None, None), f"백업에 암호문이 남았다: {row}"


# ── 감사 기록 ───────────────────────────────────────────────────────────────


async def test_output_hits_are_recorded_separately_from_input_hits(harness, acme):
    """출력 히트는 **다른 stage 로** 남는다.

    입력 히트는 소비자가 보낸 것이고 출력 히트는 모델이 만들어 낸 것이다. 후자가
    늘면 고칠 곳은 규칙이 아니라 프롬프트다 — 한 통계로 뭉치면 그 구분이 사라져
    아무도 원인을 못 찾는다.
    """
    reply_with(harness, f"연락처 {EMAIL}")
    job_id = await run_one(harness)

    events = harness.store.list_filter_events(ACME, limit=50)
    output = [e for e in events if e["stage"] == STAGE_OUTPUT]
    assert output, "출력 히트가 기록되지 않았다"
    assert output[0]["rule_id"] == "email"
    assert output[0]["job_id"] == job_id
    assert output[0]["boundary"] == OUTPUT_BOUNDARY


async def test_the_filter_event_never_carries_the_matched_value(harness, acme):
    """감사가 새 유출 경로가 되면 가드의 나머지 노력이 무의미해진다."""
    reply_with(harness, f"연락처 {EMAIL}")
    await run_one(harness)

    events = harness.store.list_filter_events(ACME, limit=50)
    assert events
    for event in events:
        assert EMAIL not in str(event), f"감사에 매칭된 값이 남았다: {event}"


# ── 계약 · 알림 ─────────────────────────────────────────────────────────────


def test_the_contract_tells_consumers_that_responses_are_masked(client, acme):
    """**소비자가 모르면 계약 위반이 된다.**

    응답이 가려질 수 있다는 사실을 안 알리면, 소비자는 응답 문자열을 파싱해 분기하는
    코드를 짜고 관리자가 규칙을 조이는 날 조용히 깨진다. 오류 코드로 분기하라고
    계약에 적어 둔 것과 같은 이유다.
    """
    guide = client.get("/v1/integration", headers=auth(acme["service"])).text

    assert "응답도 검사된다" in guide, "출력 필터가 통합 가이드에 없다"
    assert "마스킹본" in guide
    assert "작업은 실패하지 않는다" in guide, \
        "차단 등급이 응답에 걸렸을 때 어떻게 되는지가 계약에 없다"


def test_notifications_never_carry_response_text_or_ciphertext():
    """알림이 새 유출 경로가 되면 나머지 노력이 무의미해진다.

    암호문도 막는다 — 지금 못 여는 바이트라도 수신처에 쌓여 있으면 훗날 KEK 가
    새는 순간 그 이력이 통째로 열린다. 암호화는 유출을 **미루는** 것이지 없애는
    것이 아니다.
    """
    from app.notify import REDACTED_KEYS

    assert CIPHER_COLUMNS <= REDACTED_KEYS, "암호문 컬럼이 알림에서 안 걸러진다"
    assert "response" in REDACTED_KEYS
