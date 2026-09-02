"""플러그인 — **앞문으로 지나는 소비자다.**

이 파일이 고정하는 것은 하나로 요약된다: 플러그인의 권한은 새로 만든 것이 아니라
`services` 행이고, 켜고 끄는 것은 그 서비스의 `status` 다. 그래서 강제 지점이
`pipeline` 의 제출 경로 한 곳뿐이고, 여기서 상태가 갈릴 여지가 없다.

배경은 `docs/plugin-exploration.md`.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import plugins
from app.plugins import (
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    SIGNATURE_NAME,
    PluginError,
    build_bundle,
    host_satisfies,
)
from app.identity import new_salt
from app.store import TenantScope

from tests.conftest import auth

PLATFORM = "_platform"

MANIFEST = """
[plugin]
id = "acme.daily-digest"
name = "일일 요약"
version = "1.0.0"
requires_host = ">=0.1,<0.2"
description = "전날 사용량을 요약해 보낸다"

[service]
allow_roles = ["summarize"]
rate_limit_per_min = 10
budget_usd_per_month = 5.0

[run]
kind = "external"
endpoint = "http://acme-digest:9000"
"""


@pytest.fixture
def signing_key(harness):
    """사내 서명 키. 공개 키를 신뢰 디렉터리에 둔다.

    **번들 안의 키로 번들을 검증하지 않는다** — 그건 서명이 아니다.
    """
    key = Ed25519PrivateKey.generate()
    harness.trust_dir.mkdir(parents=True, exist_ok=True)
    (harness.trust_dir / "acme.pub").write_bytes(key.public_key().public_bytes_raw())
    return key


def bundle(manifest: str = MANIFEST, *, key=None, extra: dict[str, bytes] | None = None) -> bytes:
    files = {MANIFEST_NAME: manifest.encode("utf-8")}
    files.update(extra or {})
    return build_bundle(files, sign_key=key)


def do_install(harness, raw: bytes, **overrides):
    kwargs = dict(
        actor="tester", data_dir=harness.data_dir, trust_dir=harness.trust_dir,
        tenant_id=PLATFORM, host_version="0.1.0", now=harness.clock,
    )
    kwargs.update(overrides)
    return plugins.install(harness.store, raw, **kwargs)


@pytest.fixture
def platform_tenant(harness):
    """플러그인의 서비스가 사는 테넌트. bootstrap 이 만드는 것과 같은 자리다."""
    harness.store.create_tenant(
        PLATFORM, "플랫폼", end_user_salt=new_salt(),
        dek_wrapped=harness.vault.create_dek(),
    )
    return TenantScope(PLATFORM)


# ── 번들 검증 ───────────────────────────────────────────────────────────────


def test_a_signed_bundle_verifies(harness, signing_key):
    """서명 하나가 번들 전체를 고정한다 — 서명은 `MANIFEST.sha256` 한 장에 걸린다."""
    raw = bundle(key=signing_key)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = plugins.safe_names(archive)
        assert plugins.verify_bundle(archive, names, harness.trust_dir) == "signed"


def test_an_unsigned_bundle_is_unsigned_not_invalid(harness, signing_key):
    """무서명과 변조는 다른 사건이다. 섞으면 어느 쪽도 못 고친다."""
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        names = plugins.safe_names(archive)
        assert plugins.verify_bundle(archive, names, harness.trust_dir) == "unsigned"


def test_tampering_with_a_signed_bundle_is_detected(harness, signing_key):
    """매니페스트 한 글자만 바꿔도 해시가 어긋난다."""
    raw = bundle(key=signing_key)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == MANIFEST_NAME:
                data = data.replace(b'"summarize"', b'"analyze"')
            dst.writestr(name, data)
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        names = plugins.safe_names(archive)
        assert plugins.verify_bundle(archive, names, harness.trust_dir) == "invalid"


def test_an_extra_file_not_in_the_checksums_is_detected(harness, signing_key):
    """목록에 없는 파일이 끼어드는 것도 변조다."""
    raw = bundle(key=signing_key)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("payload/sneaky.sh", b"rm -rf /")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        names = plugins.safe_names(archive)
        assert plugins.verify_bundle(archive, names, harness.trust_dir) == "invalid"


def test_a_signature_from_an_untrusted_key_is_invalid(harness):
    """신뢰 목록에 없는 키의 서명은 서명이 아니다."""
    stranger = Ed25519PrivateKey.generate()
    harness.trust_dir.mkdir(parents=True, exist_ok=True)
    raw = bundle(key=stranger)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = plugins.safe_names(archive)
        assert plugins.verify_bundle(archive, names, harness.trust_dir) == "invalid"


def test_a_signature_without_checksums_is_invalid(harness, signing_key):
    """서명만 있고 무엇을 서명했는지가 없으면 아무것도 보증하지 못한다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as dst:
        dst.writestr(MANIFEST_NAME, MANIFEST)
        dst.writestr(SIGNATURE_NAME, signing_key.sign(b"whatever").hex())
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        names = plugins.safe_names(archive)
        assert plugins.verify_bundle(archive, names, harness.trust_dir) == "invalid"


# ── 압축 해제 방어 ──────────────────────────────────────────────────────────


def test_a_path_traversal_entry_is_refused(harness, signing_key):
    """`../../keys/master.key` 로 나가는 항목은 풀기 전에 막는다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as dst:
        dst.writestr(MANIFEST_NAME, MANIFEST)
        dst.writestr("../../escaped.txt", b"nope")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        with pytest.raises(PluginError, match="상위 디렉터리"):
            plugins.safe_names(archive)


def test_an_absolute_path_entry_is_refused(harness):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as dst:
        dst.writestr(MANIFEST_NAME, MANIFEST)
        info = zipfile.ZipInfo("/etc/cron.d/evil")
        dst.writestr(info, b"nope")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        with pytest.raises(PluginError, match="절대 경로"):
            plugins.safe_names(archive)


def test_a_symlink_entry_is_refused(harness):
    """링크 하나면 번들이 마스터 KEK 를 읽어 간다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as dst:
        dst.writestr(MANIFEST_NAME, MANIFEST)
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        dst.writestr(info, b"/keys/master.key")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        with pytest.raises(PluginError, match="심볼릭 링크"):
            plugins.safe_names(archive)


def test_a_zip_bomb_is_refused_before_extraction(harness):
    """해제하고 나서 재는 것은 늦다 — 이미 디스크를 채운 뒤다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
        dst.writestr(MANIFEST_NAME, MANIFEST)
        dst.writestr("payload/big", b"\0" * (plugins.MAX_UNCOMPRESSED_BYTES + 1))
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        with pytest.raises(PluginError, match="해제 크기"):
            plugins.safe_names(archive)


def test_too_many_files_is_refused(harness):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as dst:
        dst.writestr(MANIFEST_NAME, MANIFEST)
        for index in range(plugins.MAX_FILES + 1):
            dst.writestr(f"payload/{index}", b"x")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        with pytest.raises(PluginError, match="항목이 너무 많"):
            plugins.safe_names(archive)


# ── 매니페스트 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "edit, message",
    [
        ('id = "acme.daily-digest"', "역DNS"),
        ('kind = "external"', "지원하지 않는 실행 형태"),
        ('allow_roles = ["summarize"]', "allow_roles"),
    ],
)
def test_a_malformed_manifest_says_what_is_wrong(edit, message):
    """거부 사유는 사람이 읽고 고칠 수 있어야 한다 — 코드만 던지면 못 고친다."""
    broken = MANIFEST.replace(edit, {
        'id = "acme.daily-digest"': 'id = "notreversedns"',
        'kind = "external"': 'kind = "native"',
        'allow_roles = ["summarize"]': "allow_roles = []",
    }[edit])
    with pytest.raises(PluginError, match=message):
        plugins.parse_manifest(broken.encode("utf-8"))


def test_an_internal_role_cannot_be_requested():
    """밑줄 역할은 소비자에게 존재하지 않는다 — 플러그인도 소비자다."""
    broken = MANIFEST.replace('["summarize"]', '["_guard_classify"]')
    with pytest.raises(PluginError, match="내부 전용"):
        plugins.parse_manifest(broken.encode("utf-8"))


def test_external_plugins_cannot_demand_a_runtime():
    """컨트롤 플레인이 안 띄우므로 런타임을 줄 수도 없다. 조용히 통과시키지 않는다."""
    broken = MANIFEST.replace("[run]\n", '[run]\nrequires_runtime = "node20"\n')
    with pytest.raises(PluginError, match="런타임을 제공할 수 없"):
        plugins.parse_manifest(broken.encode("utf-8"))


@pytest.mark.parametrize(
    "host, spec, ok",
    [
        ("0.1.0", ">=0.1,<0.2", True),
        ("0.2.0", ">=0.1,<0.2", False),
        ("0.1.5", "", True),
        ("1.0.0", "==1.0", True),
    ],
)
def test_host_version_ranges(host, spec, ok):
    assert host_satisfies(host, spec) is ok


def test_an_unparseable_range_is_refused_not_ignored():
    """모르는 문법을 통과시키면 호환성 검사가 장식이 된다."""
    with pytest.raises(PluginError, match="해석할 수 없"):
        host_satisfies("0.1.0", "~=0.1")


def test_a_bundle_for_another_host_version_is_refused(harness, signing_key, platform_tenant):
    raw = bundle(key=signing_key)
    with pytest.raises(PluginError, match="요구하는 범위"):
        do_install(harness, raw, host_version="0.9.0")


def test_a_non_zip_upload_is_refused(harness, platform_tenant):
    with pytest.raises(PluginError, match="zip 이 아닙니다"):
        do_install(harness, b"not a zip at all")


# ── 설치가 곧 서비스 등록이다 ───────────────────────────────────────────────


def test_installing_creates_a_service_and_a_token(harness, signing_key, platform_tenant):
    """**설치가 곧 서비스 등록이다.** 권한 모델을 새로 만들지 않는다."""
    result = do_install(harness, bundle(key=signing_key))

    assert result.signature_state == "signed"
    assert result.token, "발급 토큰의 원값은 이 응답이 마지막이다"

    service = harness.store.get_service(platform_tenant, result.service_id)
    assert service is not None
    # 매니페스트 [service] 절이 그대로 서비스 필드가 된다.
    assert json.loads(service["allow_roles_json"]) == ["summarize"]
    assert service["rate_limit_per_min"] == 10
    assert service["budget_usd_per_month"] == 5.0


def test_a_freshly_installed_plugin_is_not_active(harness, signing_key, platform_tenant):
    """설치는 켜는 것이 아니다 — 모델 설치 요청이 승인과 나뉜 것과 같다."""
    result = do_install(harness, bundle(key=signing_key))
    service = harness.store.get_service(platform_tenant, result.service_id)
    assert service["status"] == "inactive"


def test_an_unsigned_bundle_is_refused_by_default(harness, signing_key, platform_tenant):
    """기본은 서명 강제다. 거부 문구가 무엇을 해야 하는지까지 말한다."""
    with pytest.raises(PluginError, match="서명되지 않은"):
        do_install(harness, bundle())


def test_a_tampered_bundle_is_refused_even_when_signatures_are_optional(
    harness, signing_key, platform_tenant
):
    """무서명은 허용할 수 있어도 **변조는 절대 아니다.**"""
    raw = bundle(key=signing_key)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == CHECKSUMS_NAME:
                data = data.replace(b"0", b"1", 1)
            dst.writestr(name, data)
    with pytest.raises(PluginError, match="검증에 실패"):
        do_install(harness, buffer.getvalue(), require_signature=False)


def test_the_payload_lands_under_the_data_dir(harness, signing_key, platform_tenant):
    """`config/` 는 읽기 전용 마운트다. 설치본은 데이터 디렉터리로 간다."""
    do_install(harness, bundle(key=signing_key, extra={"payload/notes.md": b"hi"}))
    root = plugins.plugin_root(harness.data_dir) / "acme.daily-digest" / "1.0.0"
    assert (root / MANIFEST_NAME).is_file()
    assert (root / "payload" / "notes.md").read_bytes() == b"hi"


def test_reinstalling_does_not_reissue_the_token(harness, signing_key, platform_tenant):
    """업그레이드가 토큰을 갈아 치우면 도는 플러그인이 조용히 죽는다."""
    first = do_install(harness, bundle(key=signing_key))
    upgraded = MANIFEST.replace('version = "1.0.0"', 'version = "1.1.0"')
    second = do_install(harness, bundle(upgraded, key=signing_key))

    assert second.upgraded is True
    assert second.token is None
    assert second.service_id == first.service_id


def test_an_upgrade_lands_inactive(harness, signing_key, platform_tenant):
    """새 코드는 새 코드다. 켜는 것은 사람의 별도 결정으로 남긴다."""
    do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")

    upgraded = MANIFEST.replace('version = "1.0.0"', 'version = "1.1.0"')
    do_install(harness, bundle(upgraded, key=signing_key))

    row = harness.store.get_plugin("acme.daily-digest")
    service = harness.store.get_service(platform_tenant, row["service_id"])
    assert service["status"] == "inactive"


# ── 토글의 실체 ─────────────────────────────────────────────────────────────


def test_the_toggle_is_the_service_status(harness, signing_key, platform_tenant):
    """**활성 여부의 출처는 하나다.** 별도 플래그가 없으므로 갈릴 수 없다."""
    result = do_install(harness, bundle(key=signing_key))

    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")
    assert harness.store.get_service(platform_tenant, result.service_id)["status"] == "active"

    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t")
    assert harness.store.get_service(platform_tenant, result.service_id)["status"] == "inactive"


def test_the_plugins_table_has_no_active_column():
    """토글을 두 곳에 두면 **반드시 어긋난다.** 컬럼 자체를 두지 않는다.

    구조로 막는 것이지 규율로 막는 것이 아니다 — 컬럼이 있으면 언젠가 누군가 그것을
    읽고, 그 순간 강제 지점이 둘이 된다.
    """
    from app import store as store_mod

    schema = store_mod._SCHEMA
    body = schema.split("CREATE TABLE IF NOT EXISTS plugins", 1)[1].split(");", 1)[0]
    assert "active" not in body, "plugins 테이블에 active 컬럼이 생겼다"


def test_deactivating_a_plugin_stops_its_token_at_the_pipeline(
    harness, client, signing_key, platform_tenant
):
    """**이 파일에서 가장 중요한 테스트다.**

    비활성이 무엇을 뜻하는지가 여기서 정해진다 — 플러그인의 토큰으로 앞문을 두드리면
    401 이다. 강제는 `pipeline` 의 제출 경로 한 곳에서만 일어난다.
    """
    result = do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")

    payload = {"role": "summarize", "prompt": "요약할 내용", "wait": 0}
    allowed = client.post("/v1/generate", json=payload, headers=auth(result.token))
    assert allowed.status_code in (200, 202), allowed.text

    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t")
    refused = client.post("/v1/generate", json=payload, headers=auth(result.token))
    assert refused.status_code == 401


def test_a_plugin_cannot_use_a_role_it_did_not_declare(
    harness, client, signing_key, platform_tenant
):
    """`allow_roles` 는 문서가 아니라 강제 대상이다 (AUTH-6).

    `inside` 는 설정에 **실재하는** 역할이다 — 없는 역할을 쓰면 404 가 나와서
    "선언 안 한 역할이 막힌다" 를 확인하지 못하고 "없는 역할이 없다" 만 확인하게 된다.
    """
    result = do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")

    response = client.post(
        "/v1/generate", json={"role": "inside", "prompt": "x", "wait": 0},
        headers=auth(result.token),
    )
    assert response.status_code == 403


# ── 제거 ────────────────────────────────────────────────────────────────────


def test_uninstalling_keeps_the_service_row(harness, signing_key, platform_tenant):
    """서비스를 지우면 그 서비스로 집계된 사용량·감사가 이름을 잃는다."""
    result = do_install(harness, bundle(key=signing_key))
    assert plugins.uninstall(
        harness.store, "acme.daily-digest", actor="t", data_dir=harness.data_dir
    )

    assert harness.store.get_plugin("acme.daily-digest") is None
    service = harness.store.get_service(platform_tenant, result.service_id)
    assert service is not None, "과거는 읽히고 미래는 막힌다"
    assert service["status"] == "inactive"


def test_uninstalling_removes_the_payload(harness, signing_key, platform_tenant):
    do_install(harness, bundle(key=signing_key))
    plugins.uninstall(harness.store, "acme.daily-digest", actor="t", data_dir=harness.data_dir)
    assert not (plugins.plugin_root(harness.data_dir) / "acme.daily-digest").exists()


# ── 관제 화면이 보는 것 ─────────────────────────────────────────────────────


def test_the_snapshot_shows_missing_files(harness, signing_key, platform_tenant):
    """행은 있는데 파일이 없는 상태가 드러나야 한다 — 백업 복원 뒤가 그렇다."""
    do_install(harness, bundle(key=signing_key))
    assert plugins.snapshot(harness.store, data_dir=harness.data_dir)[0]["files_present"]

    import shutil
    shutil.rmtree(plugins.plugin_root(harness.data_dir) / "acme.daily-digest")
    view = plugins.snapshot(harness.store, data_dir=harness.data_dir)[0]
    assert view["files_present"] is False


@pytest.mark.parametrize("edge", [b"\x20", b"\x0a", b"\x09", b"\x0d"])
def test_a_public_key_starting_or_ending_with_whitespace_bytes_still_loads(harness, edge):
    """**Ed25519 공개 키는 어떤 32바이트도 될 수 있다.**

    처음 구현은 파일을 `strip()` 한 뒤 길이로 갈랐다. 그런데 무작위 공개 키의 약
    5%가 공백에 해당하는 바이트로 시작하거나 끝나고, 그런 키는 31바이트로 깎여
    검증이 실패했다 — 실행할 때마다 **다른 테스트가** 실패하는 플레이크였다.

    무작위로 뽑아 확인하면 그 5%를 기다리는 테스트가 되므로, 경계 바이트를 직접
    넣어 결정론적으로 고정한다.
    """
    harness.trust_dir.mkdir(parents=True, exist_ok=True)
    (harness.trust_dir / "edge.pub").write_bytes(edge + b"\x11" * 30 + edge)

    assert len(plugins.load_trusted_keys(harness.trust_dir)) == 1


def test_a_hex_encoded_public_key_is_also_accepted(harness, signing_key):
    """사람이 손으로 넣기 쉬운 형태도 받는다 — 다만 날바이트를 깎지 않으면서."""
    other = Ed25519PrivateKey.generate()
    (harness.trust_dir / "hex.pub").write_text(
        other.public_key().public_bytes_raw().hex() + "\n"
    )
    assert len(plugins.load_trusted_keys(harness.trust_dir)) == 2


def test_a_broken_trust_key_does_not_stop_the_others(harness, signing_key):
    """깨진 키 하나가 나머지를 막으면 그 키를 고칠 방법도 같이 사라진다."""
    (harness.trust_dir / "garbage.pub").write_bytes(b"not a key")
    assert len(plugins.load_trusted_keys(harness.trust_dir)) == 1


# ── HTTP 표면 ───────────────────────────────────────────────────────────────


def test_the_routes_need_platform_admin(client, acme):
    """플러그인은 설치처 전체에 영향을 준다 — 테넌트 관리자 권한으로는 못 만진다."""
    assert client.get("/v1/platform/plugins", headers=auth(acme["tenant_admin"])).status_code == 403
    assert client.post(
        "/v1/platform/plugins", content=b"x", headers=auth(acme["tenant_admin"])
    ).status_code == 403


def test_install_activate_and_deactivate_over_http(client, harness, acme, signing_key, platform_tenant):
    """관리자가 실제로 하는 왕복 — 업로드는 raw body 다(멀티파트는 6번째 의존성)."""
    admin = auth(acme["platform_admin"])
    created = client.post("/v1/platform/plugins", content=bundle(key=signing_key), headers=admin)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["active"] is False and body["signature"] == "signed" and body["token"]

    listed = client.get("/v1/platform/plugins", headers=admin).json()["plugins"]
    assert [p["id"] for p in listed] == ["acme.daily-digest"]
    assert listed[0]["active"] is False

    on = client.post(
        "/v1/platform/plugins/acme.daily-digest/activate", json={"active": True}, headers=admin
    )
    assert on.status_code == 200 and on.json()["active"] is True
    assert client.get("/v1/platform/plugins", headers=admin).json()["plugins"][0]["active"]

    off = client.post(
        "/v1/platform/plugins/acme.daily-digest/activate", json={"active": False}, headers=admin
    )
    assert off.status_code == 200 and off.json()["active"] is False


def test_a_rejected_bundle_says_why(client, acme, signing_key, platform_tenant):
    """400 만 던지면 설치처가 무엇을 고쳐야 하는지 모른다."""
    response = client.post(
        "/v1/platform/plugins", content=b"definitely not a zip",
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 400
    assert "zip" in json.dumps(response.json(), ensure_ascii=False)


def test_toggling_an_unknown_plugin_is_404(client, acme):
    response = client.post(
        "/v1/platform/plugins/nope.nope/activate", json={"active": True},
        headers=auth(acme["platform_admin"]),
    )
    assert response.status_code == 404


def test_the_lifecycle_is_audited(client, harness, acme, signing_key, platform_tenant):
    """설치·활성·비활성·제거가 전부 해시 사슬에 남는다."""
    admin = auth(acme["platform_admin"])
    client.post("/v1/platform/plugins", content=bundle(key=signing_key), headers=admin)
    client.post(
        "/v1/platform/plugins/acme.daily-digest/activate", json={"active": True}, headers=admin
    )
    client.delete("/v1/platform/plugins/acme.daily-digest", headers=admin)

    actions = {
        row["action"]
        for row in harness.store._conn.execute("SELECT action FROM admin_audit")
    }
    assert {"install_plugin", "activate_plugin", "uninstall_plugin"} <= actions
