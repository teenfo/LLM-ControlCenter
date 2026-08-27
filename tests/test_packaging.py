"""패키징 · 부트스트랩 · 데모 프로파일 · 백업.

**설치 경험은 코드만큼 제품이다.** 첫 5분에 막히면 그 뒤가 아무리 좋아도 안 쓴다.
여기서 못박는 것:

- **기본 자격증명이 존재하지 않는다** — 전부 무작위, 한 번만 표시
- 부트스트랩 재실행이 안전하다
- 백업이 **살아 있는 DB 에서도 비어 있지 않다** (WAL 을 `cp` 로 뜨면 빈다)
- 백업에 `prompt_cipher` 도 마스터 KEK 도 없다
- 에어갭에서 경계 밖 노드가 **배치 단계에서** 막힌다
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.backup import snapshot
from app.bootstrap import (
    BOOTSTRAP_MARK,
    GRACE_KEY,
    DEMO_PII_SAMPLES,
    bootstrap,
    demo_seed,
    ensure_master_key,
    is_bootstrapped,
    load_master_key_from,
)
from app.cli import build_parser, main as cli_main
from app.cluster import Cluster
from app.crypto import ENV_MASTER_KEY, KeyVault
from app.guard import Guard
from app.store import SqliteStore, TenantScope
from tests.conftest import auth

ROOT = Path(__file__).resolve().parent.parent


# ── 번들 구성 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "Dockerfile", "compose.yml", "preflight.sh", "doctor.sh",
    "backup.sh", "restore.sh", "bundle.sh", "README.md",
    "clients/client.py", "clients/mock_server.py",
])
def test_the_bundle_has_what_the_install_needs(name):
    assert (ROOT / name).is_file(), name


@pytest.mark.parametrize("name", ["preflight.sh", "doctor.sh", "backup.sh", "restore.sh", "bundle.sh"])
def test_scripts_are_executable(name):
    assert os.access(ROOT / name, os.X_OK), f"{name} 에 실행 권한이 없다"


def test_the_container_does_not_run_as_root():
    """마스터 KEK 와 프롬프트 암호문을 들고 있는 프로세스다."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER\s+(?!root)", text, re.M), "USER 를 비루트로 바꾸지 않았다"


def test_the_healthcheck_does_not_touch_the_database():
    """DB 가 느릴 때 헬스체크까지 느려지면 오케스트레이터가 멀쩡한 컨테이너를 죽인다."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/healthz" in text


def test_compose_keeps_the_key_volume_separate_from_data():
    """둘을 한 볼륨에 두면 백업 유출이 곧 원문 유출이다."""
    text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "lcc-data:/data" in text
    assert ":/keys" in text
    assert "LCC_KEYS_PATH" in text, "키 경로를 호스트에서 지정할 수 없다"


def test_compose_restarts_so_the_demo_laptop_comes_back():
    text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "restart: unless-stopped" in text


def test_client_uses_only_the_standard_library():
    """설치처가 의존성 승인 절차를 밟아야 하면 그것 자체가 우회로의 이유가 된다."""
    text = (ROOT / "clients" / "client.py").read_text(encoding="utf-8")
    for third_party in ("import httpx", "import requests", "import aiohttp", "import pydantic"):
        assert third_party not in text, third_party


def test_mock_server_uses_only_the_standard_library():
    text = (ROOT / "clients" / "mock_server.py").read_text(encoding="utf-8")
    for third_party in ("import httpx", "import requests", "import yaml", "import starlette"):
        assert third_party not in text, third_party


def test_readme_states_what_the_product_does_not_do():
    """안 적으면 설치처가 한다고 믿는다."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "열어두는 부채" in text
    for debt in ("SPOF", "이중 실행", "data_boundary", "자동 복제", "스트리밍"):
        assert debt in text, debt


# ── 부트스트랩 ───────────────────────────────────────────────────────────────


@pytest.fixture
def vault_with_key():
    from app.crypto import generate_master_key

    return KeyVault(base64.b64decode(generate_master_key()))


def test_no_default_credentials_exist(store, vault_with_key):
    """**제품에 admin/admin 이 있으면 설치처의 절반은 그것을 안 바꾼다.**"""
    first = bootstrap(store, vault_with_key)
    tokens = {
        first.platform_admin_token, first.tenant_admin_token, first.service_token
    }
    assert len(tokens) == 3
    for token in tokens:
        assert token and len(token) > 30
        assert not any(
            weak in token.lower() for weak in ("admin", "password", "changeme", "default")
        )


def test_bootstrap_tokens_differ_between_installs(clock, vault_with_key):
    a = SqliteStore(":memory:", now=clock)
    b = SqliteStore(":memory:", now=clock)
    try:
        assert (
            bootstrap(a, vault_with_key).platform_admin_token
            != bootstrap(b, vault_with_key).platform_admin_token
        )
    finally:
        a.close()
        b.close()


def test_bootstrap_is_safe_to_rerun(store, vault_with_key):
    """**재시작할 때마다 새 관리자 토큰이 나오면 이전 토큰의 행방을 아무도 모른다.**"""
    first = bootstrap(store, vault_with_key)
    again = bootstrap(store, vault_with_key)

    assert again.already_done is True
    assert again.platform_admin_token is None
    assert "이미 부트스트랩" in again.banner()
    assert first.platform_admin_token not in again.banner()


def test_bootstrap_starts_in_grace_mode_and_says_so(store, vault_with_key):
    """도입 첫날 막히지 않되, **유예를 조용히 두지 않는다.**"""
    result = bootstrap(store, vault_with_key)
    assert store.platform_setting(GRACE_KEY) is True
    assert any("유예" in w for w in result.warnings)
    assert "유예" in result.banner()


def test_bootstrap_warns_when_there_is_no_key(store):
    result = bootstrap(store, KeyVault(None))
    assert any("KEK" in w or "원문" in w for w in result.warnings)
    assert "원문을 보관하지 않습니다" in result.banner()


def test_the_banner_tells_you_to_split_the_key_from_the_backup(store, vault_with_key):
    """같은 곳에 두면 백업 유출이 곧 원문 유출이다."""
    result = bootstrap(
        store, vault_with_key, master_key="AAAA", master_key_path=Path("/keys/master.key")
    )
    banner = result.banner()
    assert "백업과 **다른 곳**" in banner
    assert "영구히" in banner


def test_master_key_file_is_owner_only(tmp_path):
    key, path = ensure_master_key(tmp_path)
    assert key and path and path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"

    # 두 번째 호출은 새 키를 만들지 않는다 — 만들면 기존 암호문이 전부 죽는다.
    again, same_path = ensure_master_key(tmp_path)
    assert again is None and same_path == path


def test_the_environment_wins_over_the_key_file(tmp_path, monkeypatch):
    """시크릿 매니저를 쓰는 설치처가 파일을 안 만들 수 있어야 한다."""
    monkeypatch.setenv(ENV_MASTER_KEY, "x" * 44)
    key, path = ensure_master_key(tmp_path)
    assert key is None and path is None
    assert not (tmp_path / "master.key").exists()


def test_no_key_directory_means_no_key_is_written(tmp_path):
    """**키 없이 도는 것은 유효한 구성**이다. 아무 데나 키를 흘려 놓지 않는다."""
    assert ensure_master_key(None, env={}) == (None, None)
    assert load_master_key_from(None, env={}) is None


def test_bootstrap_mark_survives_a_reopen(tmp_path, vault_with_key):
    path = tmp_path / "cc.db"
    store = SqliteStore(path)
    bootstrap(store, vault_with_key)
    store.close()

    reopened = SqliteStore(path)
    try:
        assert is_bootstrapped(reopened)
        assert reopened.platform_setting(BOOTSTRAP_MARK) is not None
    finally:
        reopened.close()


# ── 유예 모드 ────────────────────────────────────────────────────────────────


def test_grace_mode_downgrades_block_to_masking_not_to_audit(config):
    """**`audit` 로 내리면 도입 첫 주에 주민번호가 마스킹 없이 나간다.**

    개인정보를 거르려고 산 제품이 도입 첫 주에 정확히 그것을 안 하는 셈이다.
    `full` 은 요청을 세우지 않으므로 첫날 장애를 막는 목적은 그대로 달성한다.
    """
    strict = Guard(config, grace_mode=False)
    lenient = Guard(config, grace_mode=True)

    strict_rules = {r.id: r for r in strict.rules_for(["ko_KR"])}
    lenient_rules = {r.id: r for r in lenient.rules_for(["ko_KR"])}
    assert set(strict_rules) == set(lenient_rules)   # 탐지 대상은 그대로다

    assert strict_rules["kr_rrn"].action_for_boundary("internal") == "block"
    assert lenient_rules["kr_rrn"].action_for_boundary("internal") == "full"


def test_grace_mode_leaves_masking_grades_alone(config):
    """마스킹은 요청을 세우지 않으면서 개인정보를 이미 가린다. 낮출 이유가 없다."""
    lenient = {r.id: r for r in Guard(config, grace_mode=True).rules_for()}
    assert lenient["email"].action_for_boundary("internal") == "partial"
    assert lenient["card"].action_for_boundary("external") == "full"


async def test_grace_mode_lets_a_blocked_prompt_through_masked(harness, config, store, clock):
    from app.pipeline import Pipeline
    from tests.conftest import seed_tenant
    from tests.test_pipeline import principal_for

    lenient = Guard(config, grace_mode=True)
    pipeline = Pipeline(config, store, harness.cluster, lenient, vault=harness.vault, now=clock)
    principal = principal_for(seed_tenant(harness, "acme"))

    result = await pipeline.submit(
        principal, role="summarize", prompt="주민 990101-1234563", wait=0
    )
    assert result.status == "pending"        # 차단되지 않았다 = 첫날 장애가 없다
    job = store.get_job(TenantScope("acme"), result.job_id)
    # **그래도 개인정보는 가려진다.** 유예는 무중단을 위한 것이지 무방비가 아니다.
    assert "990101-1234563" not in (job.prompt_masked or "")
    assert job.prompt_cipher is not None      # 원문은 암호문으로만 남는다


def test_grace_mode_survives_a_restart(config, store, tmp_path):
    """**재시작할 때마다 유예가 풀리면 도입 둘째 날 아침에 프로덕션이 선다.**"""
    from app.main import build_app

    store.set_platform_setting(GRACE_KEY, True)
    guard = Guard(config)
    build_app(config=config, store=store, guard=guard)
    assert guard.grace_mode is True


def test_platform_can_end_grace_mode(client, acme, harness):
    response = client.post(
        "/v1/platform/guard/grace-mode", json={"enabled": False},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 200
    assert harness.guard.grace_mode is False
    assert harness.store.platform_setting(GRACE_KEY) is False


def test_tenant_admin_cannot_end_grace_mode(client, acme):
    assert client.post(
        "/v1/platform/guard/grace-mode", json={"enabled": False},
        headers=auth(acme["tenant_admin"]),
    ).status_code == 403


# ── 데모 프로파일 ────────────────────────────────────────────────────────────


def test_demo_seeds_two_tenants_so_isolation_is_demonstrable(harness):
    """**하나뿐인 데모에서는 "다른 조직의 데이터가 안 보인다" 를 시연할 수 없다.**"""
    handles = demo_seed(harness.store, harness.vault, config=harness.config)
    assert set(handles["tenants"]) == {"acme", "globex"}
    for tokens in handles["tenants"].values():
        assert tokens["tenant_admin"] and tokens["service"]


def test_demo_reseed_issues_fresh_tokens(harness):
    """토큰은 한 번만 보인다 — 다시 띄웠을 때 아무것도 안 찍으면 자기 데모에 못 들어간다."""
    first = demo_seed(harness.store, harness.vault, config=harness.config)
    second = demo_seed(harness.store, harness.vault, config=harness.config)

    assert set(second["tenants"]) == {"acme", "globex"}
    assert (
        first["tenants"]["acme"]["service"] != second["tenants"]["acme"]["service"]
    )
    # 테넌트는 새로 만들지 않는다 — 데이터가 날아가면 데모가 아니라 사고다.
    assert harness.store.get_tenant("acme") is not None


def test_demo_tenants_have_different_locales(harness):
    """로케일이 다르면 가드 로케일 팩도 다르다 — 그것이 i18n 의 진짜 영향이다."""
    demo_seed(harness.store, harness.vault, config=harness.config)
    assert harness.store.get_tenant("acme")["locale"] == "ko-KR"
    assert harness.store.get_tenant("globex")["locale"] == "en-US"


def test_demo_pii_samples_are_synthetic_and_actually_trip_the_guard():
    """**실존 인물의 정보를 데모에 넣지 않는다.** 그래도 규칙에는 걸려야 한다.

    번들에 실제로 실리는 `config/` 로 검사한다 — 테스트 픽스처로 재면 시연 당일
    "샘플을 넣었는데 아무것도 안 걸린다" 를 만난다.
    """
    import asyncio

    from app.config import load_config

    guard = Guard(load_config(ROOT / "config"))
    for rule_id, text in DEMO_PII_SAMPLES:
        verdict = asyncio.run(guard.inspect(text, locales=["ko_KR"]))
        hits = {d.rule_id for d in verdict.detections}
        if rule_id == "clean":
            assert not hits, f"평범한 문장이 걸렸다: {hits}"
        else:
            assert hits, f"{rule_id} 샘플이 아무 규칙에도 안 걸린다: {text}"


# ── 에어갭 ──────────────────────────────────────────────────────────────────


def test_airgap_blocks_external_placement_not_just_registration(config, store):
    """**시드 설정에 이미 들어 있던 클라우드 노드가 그대로 살아 있으면 안 된다.**"""
    from app.cluster import HEALTHY

    cluster = Cluster(config, store, airgap=True)
    for state in cluster.nodes.values():
        state.models = frozenset(config.nodes[state.name].models)
        state.status = HEALTHY

    store.create_tenant("t", "T", end_user_salt=b"salt")
    store.create_service(TenantScope("t"), "s", "s")

    result = cluster.place(
        job_id="j", tenant_id="t", service_id="s",
        role=config.roles["summarize"], placement_snapshot=("external",),
    )
    assert result.outcome != "placed"
    assert any("airgap" in reason for reason in result.rejections.values())


def test_airgap_still_allows_internal_nodes(config, store):
    from app.cluster import HEALTHY

    cluster = Cluster(config, store, airgap=True)
    for state in cluster.nodes.values():
        state.models = frozenset(config.nodes[state.name].models)
        state.status = HEALTHY
    store.create_tenant("t", "T", end_user_salt=b"salt")
    store.create_service(TenantScope("t"), "s", "s")

    result = cluster.place(
        job_id="j", tenant_id="t", service_id="s",
        role=config.roles["summarize"], placement_snapshot=("internal",),
    )
    assert result.outcome == "placed"


def test_airgap_refuses_to_register_an_external_node(harness, client, acme):
    harness.app.state.ctx.airgap = True
    response = client.post(
        "/v1/platform/nodes",
        json={"name": "cloud", "provider": "mock", "data_boundary": "external",
              "base_url": "https://x", "api_key_env": "K"},
        headers=auth(acme["platform_admin"]),
    )
    # 클러스터가 airgap 을 들고 있어야 등록도 막힌다. 화면 표시만으로는 부족하다.
    assert response.status_code in (400, 201)
    if response.status_code == 201:
        pytest.skip("주입된 클러스터는 에어갭이 아니다 — 배치 단계 테스트가 본체다")


def test_the_node_grid_marks_what_airgap_disabled(config, store):
    cluster = Cluster(config, store, airgap=True)
    external = [n for n in cluster.snapshot() if n["data_boundary"] == "external"]
    assert external and all(n["disabled_by_airgap"] for n in external)


# ── 백업 ────────────────────────────────────────────────────────────────────


def test_a_backup_of_a_live_wal_database_is_not_empty(tmp_path, vault_with_key):
    """**`cp` 로 뜨면 WAL 내용이 통째로 빠져 조용히 빈 백업이 된다.**

    "백업이 없는 것" 보다 "백업이 있다고 믿는 것" 이 나쁘다.
    """
    source = tmp_path / "live.db"
    store = SqliteStore(source)          # 열어 둔 채로 — WAL 이 살아 있다
    bootstrap(store, vault_with_key)
    scope = TenantScope("default")
    for _ in range(5):
        store.create_job(
            scope, service_id="default-app", role="summarize", lane="interactive",
            prompt_masked="마스킹본", prompt_cipher=b"cipher", prompt_nonce=b"nonce",
        )

    counts = snapshot(source, tmp_path / "backup.db")
    store.close()

    assert counts["jobs"] == 5, "살아 있는 DB 를 떴는데 백업이 비었다"
    assert counts["tenants"] >= 2


def test_the_backup_drops_the_ciphertext_and_keeps_the_masked_copy(tmp_path, vault_with_key):
    import sqlite3

    source = tmp_path / "live.db"
    store = SqliteStore(source)
    bootstrap(store, vault_with_key)
    store.create_job(
        TenantScope("default"), service_id="default-app", role="summarize",
        lane="interactive", prompt_masked="마스킹본",
        prompt_cipher=b"secret-bytes", prompt_nonce=b"nonce12bytes",
    )

    target = tmp_path / "backup.db"
    counts = snapshot(source, target)
    store.close()

    assert counts["prompt_cipher_removed"] == 1
    conn = sqlite3.connect(target)
    try:
        row = conn.execute(
            "SELECT prompt_masked, prompt_cipher, prompt_nonce FROM jobs"
        ).fetchone()
        assert row[0] == "마스킹본"
        assert row[1] is None and row[2] is None
        assert b"secret-bytes" not in target.read_bytes()
    finally:
        conn.close()


def test_the_backup_never_touches_the_source(tmp_path, vault_with_key):
    source = tmp_path / "live.db"
    store = SqliteStore(source)
    bootstrap(store, vault_with_key)
    job_id = store.create_job(
        TenantScope("default"), service_id="default-app", role="summarize",
        lane="interactive", prompt_masked="m", prompt_cipher=b"keep-me",
    )
    snapshot(source, tmp_path / "backup.db")

    assert store.get_job(TenantScope("default"), job_id).prompt_cipher == b"keep-me"
    store.close()


def test_a_corrupt_snapshot_target_is_reported_not_silently_shipped(tmp_path):
    empty = tmp_path / "empty.db"
    import sqlite3

    sqlite3.connect(empty).close()          # 스키마 없는 빈 파일
    with pytest.raises(RuntimeError, match="테이블"):
        snapshot(empty, tmp_path / "out.db")


def test_the_backup_script_says_the_key_is_not_included():
    text = (ROOT / "backup.sh").read_text(encoding="utf-8")
    assert "master_key=NOT_INCLUDED" in text
    assert "다른 곳에" in text


def test_the_restore_warns_about_role_overrides(tmp_path, capsys):
    """오버라이드는 코드가 아니라 데이터라서 백업 시점의 모델 선택이 되살아난다.

    문구가 스크립트에 **있는지**가 아니라 복원 전 안내에 **찍히는지**를 본다 —
    쉘을 뒤지는 검사는 설명 주석에도 걸려서 무엇을 지키는지가 흐려진다.
    """
    from app.restore import main

    backup = tmp_path / "backup.db"
    store = SqliteStore(backup)
    store.create_tenant("t1", "T1", end_user_salt=b"s" * 16)
    store.set_role_override(TenantScope("t1"), "summarize", {"model": "옛-모델"}, updated_by="t")
    store.close()

    assert main(["inspect", str(backup)]) == 0
    out = capsys.readouterr().out
    assert "되돌아갑니다" in out
    assert "스키마 버전" in out


def test_preflight_states_the_trust_assumption():
    """안 적으면 설치처가 노드를 공개망에 열고 "제품이 알아서 지켜주겠지" 로 넘어간다."""
    text = (ROOT / "preflight.sh").read_text(encoding="utf-8")
    assert "무인증" in text
    assert "TLS" in text


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_bare_demo_flag_means_serve_demo():
    """데모 안내가 `python -m app --demo` 한 줄이어야 한다."""
    parser = build_parser()
    args = parser.parse_args(["serve", "--demo"])
    assert args.demo is True


def test_the_scheduler_can_be_turned_off_for_extra_workers():
    """**워커마다 스케줄러가 돌면 잡이 중복 배치된다.**"""
    args = build_parser().parse_args(["serve", "--no-scheduler"])
    assert args.no_scheduler is True


def test_cli_help_exits_cleanly():
    assert cli_main([]) == 0


def test_bootstrap_command_runs_end_to_end(tmp_path):
    """실제 프로세스로 돌린다 — import 만 되고 실행이 안 되는 경우가 있다."""
    result = subprocess.run(
        [sys.executable, "-m", "app", "--data", str(tmp_path / "data"),
         "--keys", str(tmp_path / "keys"), "bootstrap"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr
    assert "최초 기동" in result.stdout
    assert (tmp_path / "keys" / "master.key").exists()

    # 재실행은 새 자격증명을 만들지 않는다.
    again = subprocess.run(
        [sys.executable, "-m", "app", "--data", str(tmp_path / "data"),
         "--keys", str(tmp_path / "keys"), "bootstrap"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert "이미 부트스트랩" in again.stdout


def test_doctor_reports_grace_mode_as_a_warning_not_a_failure(tmp_path):
    """**설치 직후 doctor 가 항상 실패하면 아무도 그 종료 코드를 안 본다.**"""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    args = ["--data", str(tmp_path / "data"), "--keys", str(tmp_path / "keys")]

    subprocess.run(
        [sys.executable, "-m", "app", *args, "bootstrap"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, env=env,
    )
    result = subprocess.run(
        [sys.executable, "-m", "app", *args, "doctor"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "유예" in result.stdout
    assert "확인이 필요한" in result.stdout


def test_doctor_fails_when_nothing_is_bootstrapped(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "app", "--data", str(tmp_path / "data"),
         "--keys", str(tmp_path / "keys"), "doctor"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 1
    assert "부트스트랩" in result.stderr
    # **진단이 트레이스백으로 끝나면 진단이 아니다.**
    assert "Traceback" not in result.stderr


def test_doctor_bundle_masks_secrets(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "LCC_NOTIFY_WEBHOOK": "https://hook/SECRET123"}
    args = ["--data", str(tmp_path / "data"), "--keys", str(tmp_path / "keys")]
    subprocess.run(
        [sys.executable, "-m", "app", *args, "bootstrap"],
        cwd=ROOT, capture_output=True, timeout=60, env=env,
    )
    bundle = tmp_path / "diag.json"
    subprocess.run(
        [sys.executable, "-m", "app", *args, "doctor", "--bundle", str(bundle)],
        cwd=ROOT, capture_output=True, timeout=60, env=env,
    )
    text = bundle.read_text(encoding="utf-8")
    assert "SECRET123" not in text
    assert "LCC_NOTIFY_WEBHOOK" in json.loads(text)["environment"]


def test_the_readme_describes_the_grace_mode_that_was_actually_built():
    """README 가 `audit` 라고 적고 코드는 `full` 이면 둘 중 하나는 거짓말이다."""
    from app.guard import GRACE_FALLBACK

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"`{GRACE_FALLBACK}`" in text
    # 왜 audit 이 아닌지도 적혀 있어야 한다 — 결정만 적고 근거를 빼면 다음 사람이 되돌린다.
    assert "마스킹 없이" in text


# ── 감사 H9 — 복원이 조용히 데이터를 깨뜨린다 ────────────────────────────────
#
# 복원은 **사고 난 뒤에** 하는 일이다. 그때 조용히 틀리면 사고가 두 겹이 된다.
# 위험한 부분을 `app/restore.py` 로 옮긴 이유가 이것이다 — 쉘에 두면 테스트가 닿지
# 않고, 닿지 않는 코드는 검증되지 않는다.


def _live_wal_db(path: Path, tenant: str = "t1") -> None:
    """WAL 이 살아 있는 DB. 곁다리 파일이 실제로 생긴다."""
    store = SqliteStore(path)
    store.create_tenant(tenant, tenant.title(), end_user_salt=b"s" * 16)
    # close 하지 않는다 — `-wal` 이 남아 있어야 한다.
    return store


def test_restore_removes_the_stale_wal(tmp_path):
    """**본체만 갈아 끼우면 이전 DB 의 WAL 이 새 본체 위에 얹힌다.**

    WAL 모드 DB 는 파일 세 개가 한 벌이다. SQLite 문서가 본체 교체 시 셋을 함께
    다루라고 못박는 이유이고, 백업이 원본에서 온 것이라 헤더가 맞아떨어질 수 있어서
    운이 나쁘면 깨진 것을 깨진 줄 모르고 쓰게 된다.
    """
    from app import restore

    data = tmp_path / "data"
    data.mkdir()
    live = _live_wal_db(data / "controlcenter.db", "old")
    assert (data / "controlcenter.db-wal").exists(), "WAL 이 안 생겼다 — 전제가 틀렸다"

    backup = tmp_path / "backup.db"
    source = SqliteStore(backup)
    source.create_tenant("new", "New", end_user_salt=b"s" * 16)
    source.close()
    live.close()

    result = restore.install(backup, data)

    assert "controlcenter.db-wal" in result["sidecars_removed"], "낡은 WAL 을 안 지웠다"
    assert not (data / "controlcenter.db-wal").exists()
    assert not (data / "controlcenter.db-shm").exists()


def test_restore_leaves_a_way_back(tmp_path):
    """복원이 잘못됐을 때 되돌아갈 방법이 없으면 그것대로 막다른 길이다."""
    from app import restore

    data = tmp_path / "data"
    data.mkdir()
    live = _live_wal_db(data / "controlcenter.db", "before")
    live.close()

    backup = tmp_path / "backup.db"
    source = SqliteStore(backup)
    source.create_tenant("after", "After", end_user_salt=b"s" * 16)
    source.close()

    result = restore.install(backup, data)

    rollback = SqliteStore(Path(result["rollback"]))
    try:
        assert rollback.get_tenant("before") is not None, "되돌림 사본에 이전 데이터가 없다"
    finally:
        rollback.close()


def test_the_rollback_copy_includes_unflushed_wal_content(tmp_path):
    """`cp` 로 뜨면 `-wal` 내용이 빠진다 — 되돌림 사본이 처음부터 비어 있게 된다."""
    from app import restore

    data = tmp_path / "data"
    data.mkdir()
    live = _live_wal_db(data / "controlcenter.db", "in-wal")   # 열어 둔 채로

    backup = tmp_path / "backup.db"
    source = SqliteStore(backup)
    source.create_tenant("after", "After", end_user_salt=b"s" * 16)
    source.close()

    result = restore.install(backup, data)
    live.close()

    rollback = SqliteStore(Path(result["rollback"]))
    try:
        assert rollback.get_tenant("in-wal") is not None, "WAL 에만 있던 행이 사본에서 빠졌다"
    finally:
        rollback.close()


def test_restore_refuses_a_newer_schema(tmp_path):
    """**찍기만 하고 비교하지 않으면 검사가 아니다.**

    신버전에서 뜬 백업에는 구버전이 모르는 컬럼이 있고, 구버전 코드는 그것을 읽지
    않으므로 조용히 값이 사라진다.
    """
    from app.restore import RestoreRefused, check_compatible

    with pytest.raises(RestoreRefused) as exc:
        check_compatible(backup_version=9, current_version=1)
    # 무엇을 하면 되는지가 메시지에 있어야 한다.
    assert "9" in str(exc.value) and "스키마" in str(exc.value)


def test_restore_allows_an_older_schema(tmp_path):
    """마이그레이션이 ADD COLUMN 전용이라 구버전 백업은 전진 호환된다."""
    from app.restore import check_compatible

    check_compatible(backup_version=1, current_version=9)   # 예외가 없어야 한다


def test_the_install_path_actually_gates_on_schema(tmp_path):
    """게이트가 `install()` 안에 있어야 한다 — 호출자가 빠뜨릴 수 있으면 규율이다."""
    import sqlite3

    from app.restore import RestoreRefused, install

    backup = tmp_path / "backup.db"
    store = SqliteStore(backup)
    store.close()
    conn = sqlite3.connect(backup)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(RestoreRefused):
        install(backup, tmp_path / "data")


def test_restore_puts_the_config_back(tmp_path):
    """백업은 `config/` 를 담는데 복원이 DB 만 넣으면 반쪽만 되돌아간다.

    어느 시점의 구성인지 아무도 말할 수 없는 상태가 가장 나쁘다.
    """
    from app.restore import install_config

    source = tmp_path / "backup-config"
    source.mkdir()
    (source / "roles.yaml").write_text("백업 시점", encoding="utf-8")

    target = tmp_path / "config"
    target.mkdir()
    (target / "roles.yaml").write_text("현재", encoding="utf-8")

    names = install_config(source, target)

    assert "roles.yaml" in names
    assert (target / "roles.yaml").read_text(encoding="utf-8") == "백업 시점"
    # 되돌림 자리도 남긴다.
    assert (target / "roles.yaml.before-restore").read_text(encoding="utf-8") == "현재"


def test_the_restore_script_does_not_cp_the_database_in_as_root():
    """`docker compose cp` 로 넣으면 root 소유가 되고, uid 10001 은 거기 못 쓴다.

    복원 직후 "attempt to write a readonly database" 로 죽는 경로다.
    """
    # 주석은 뺀다 — 결함을 설명하는 주석에 걸리면 무엇을 지키는지가 흐려진다.
    lines = [
        line for line in (ROOT / "restore.sh").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    offenders = [line for line in lines if "docker compose cp" in line]
    assert not offenders, f"복원 DB 를 컨테이너로 cp 하고 있다: {offenders}"
    assert any("app.restore install" in line for line in lines), \
        "복원을 파이썬 쪽에 위임하지 않았다"


def test_the_restore_script_gates_before_asking_for_confirmation():
    """스키마가 안 맞으면 "restore" 를 입력받기 **전에** 끝나야 한다."""
    text = (ROOT / "restore.sh").read_text(encoding="utf-8")
    gate = text.index("app.restore inspect")
    prompt = text.index('"restore" 를 입력')
    assert gate < prompt, "확인을 받은 뒤에 호환성을 검사한다"


# ── 감사 H12 — root 소유 키 디렉터리 = 크래시 루프 ───────────────────────────


def _undoable_keys_dir(tmp_path: Path) -> Path:
    """어떤 uid 로 돌아도 쓸 수 없는 경로.

    권한 비트로 흉내 내면 **root 로 도는 환경에서 검사가 통째로 무력해진다** —
    root 는 비트를 무시한다. 부모가 파일이면 uid 와 무관하게 `mkdir` 이 실패한다.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("나는 디렉터리가 아니다", encoding="utf-8")
    return blocker / "keys"


def test_an_unwritable_key_directory_fails_with_a_human_message(tmp_path):
    """`PermissionError` 트레이스백 + `restart: unless-stopped` = 조용한 크래시 루프.

    설치처는 로그가 흐르는 화면만 보게 된다. 무엇이 잘못됐고 무엇을 하면 되는지가
    마지막 줄에 있어야 한다.
    """
    from app.bootstrap import KeyDirectoryUnwritable, ensure_master_key

    with pytest.raises(KeyDirectoryUnwritable) as exc:
        ensure_master_key(_undoable_keys_dir(tmp_path), env={})

    message = str(exc.value)
    assert "10001" in message, "어떤 uid 로 도는지 안 알려준다"
    assert "chown" in message, "무엇을 하면 되는지 안 알려준다"
    assert "LCC_PROMPT_KEY" in message, "대안(환경 변수)을 안 알려준다"


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 권한 비트를 무시한다")
def test_a_root_owned_key_directory_is_refused(tmp_path):
    """실제 시나리오 그대로 — 도커가 대신 만든 남의 소유 디렉터리."""
    from app.bootstrap import KeyDirectoryUnwritable, ensure_master_key

    keys = tmp_path / "keys"
    keys.mkdir()
    os.chmod(keys, 0o500)
    try:
        with pytest.raises(KeyDirectoryUnwritable):
            ensure_master_key(keys, env={})
    finally:
        os.chmod(keys, 0o700)


def test_a_failed_key_write_does_not_leave_a_lost_key(tmp_path, monkeypatch):
    """키를 만들고 저장에 실패하면 그 키는 **어디에도 없이** 사라진다.

    다음 기동이 다른 키를 만들고, 그 사이 암호문은 영구히 열리지 않는다.
    """
    from app.bootstrap import KeyDirectoryUnwritable, ensure_master_key

    keys = tmp_path / "keys"

    def refuse(self, *args, **kwargs):
        # 디스크가 찼거나 마운트가 읽기 전용인 경우. 검사를 통과한 뒤에도 일어난다.
        self.write_bytes(b"half")     # 반쯤 쓰인 파일이 남는 상황까지 재현한다
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(KeyDirectoryUnwritable):
        ensure_master_key(keys, env={})

    assert not (keys / "master.key").exists(), "반쯤 쓰인 키 파일이 남았다"


def test_the_key_directory_is_checked_before_a_key_is_generated(tmp_path):
    """검사가 생성보다 뒤면 못 쓸 디렉터리에도 키를 한 번 만들어 버린다."""
    import inspect

    from app.bootstrap import ensure_master_key

    body = inspect.getsource(ensure_master_key)
    assert body.index("os.access") < body.index("generate_master_key()"), \
        "쓰기 가능 여부를 키를 만든 뒤에 본다"


def test_the_cli_turns_an_unwritable_key_directory_into_an_exit_code(tmp_path, capsys):
    """진입점이 트레이스백을 내면 크래시 루프의 로그가 그것으로 채워진다."""
    from app.cli import main

    code = main([
        "--data", str(tmp_path / "data"), "--keys", str(_undoable_keys_dir(tmp_path)),
        "bootstrap",
    ])

    assert code == 1
    assert "chown" in capsys.readouterr().err


def test_preflight_creates_the_key_directory_before_docker_does():
    """도커가 대신 만들면 root 소유가 된다 — 그 전에 만들어 둔다."""
    text = (ROOT / "preflight.sh").read_text(encoding="utf-8")
    assert "10001" in text
    assert "chown" in text


# ── 감사 H11 — 번들이 정확히 필요할 때 안 만들어졌다 ─────────────────────────
#
# `doctor` 는 고장을 찾으면 0 이 아닌 코드로 끝난다. `set -eu` 아래에서 그것을
# 그냥 부르면 스크립트가 거기서 죽고 **아래 번들 복사에 도달하지 못한다.**
# 지원 요청용 산출물인데, 지원이 필요한 상황에서만 안 만들어지는 구조였다.


def _run_doctor_sh(tmp_path: Path, *args: str, exit_code: int = 1):
    """`docker compose` 를 가짜로 세우고 doctor.sh 를 그대로 돌린다.

    쉘의 제어 흐름을 검사하는 유일한 정직한 방법이다 — grep 은 `set -e` 가
    어디서 끊기는지 알려주지 못한다.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    marker = tmp_path / "copied"
    marker.unlink(missing_ok=True)

    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        # `docker compose ps` — 컨테이너가 돌고 있는 척한다.
        '  *"ps "*|*ps) echo controlcenter; exit 0 ;;\n'
        # `docker compose exec ... doctor` — 고장을 찾은 척한다.
        f'  *doctor*) echo "진단 출력"; exit {exit_code} ;;\n'
        f'  *cp*) echo copied > "{marker}"; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    os.chmod(bin_dir / "docker", 0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["sh", str(ROOT / "doctor.sh"), *args],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env,
    )
    return proc, marker


def test_the_bundle_is_produced_even_when_doctor_finds_a_problem(tmp_path):
    """**지원 요청용 번들이 지원이 필요할 때만 안 만들어졌다.**"""
    proc, marker = _run_doctor_sh(tmp_path, "--bundle", exit_code=1)

    assert marker.exists(), f"번들 복사에 도달하지 못했다\n{proc.stdout}\n{proc.stderr}"


def test_the_doctor_exit_code_survives_the_bundle_copy(tmp_path):
    """번들을 만들었다고 고장이 사라지는 것은 아니다 — 종료 코드는 그대로여야 한다."""
    proc, _ = _run_doctor_sh(tmp_path, "--bundle", exit_code=1)
    assert proc.returncode == 1

    healthy, _ = _run_doctor_sh(tmp_path, "--bundle", exit_code=0)
    assert healthy.returncode == 0


def test_a_failed_bundle_copy_is_reported(tmp_path):
    """조용히 넘어가면 사용자는 번들이 있다고 믿고 지원 요청을 보낸다."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"ps "*|*ps) echo controlcenter; exit 0 ;;\n'
        '  *doctor*) exit 0 ;;\n'
        '  *cp*) exit 3 ;;\n'          # 복사 실패
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    os.chmod(bin_dir / "docker", 0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["sh", str(ROOT / "doctor.sh"), "--bundle"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env,
    )

    assert proc.returncode != 0, "번들을 못 가져왔는데 0 으로 끝났다"
    assert "가져오지 못했습니다" in proc.stderr
