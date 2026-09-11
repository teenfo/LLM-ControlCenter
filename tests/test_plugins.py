"""플러그인 — **앞문으로 지나는 소비자다.**

이 파일이 고정하는 것은 하나로 요약된다: 플러그인의 권한은 새로 만든 것이 아니라
`services` 행이고, 켜고 끄는 것은 그 서비스의 `status` 다. 그래서 강제 지점이
`pipeline` 의 제출 경로 한 곳뿐이고, 여기서 상태가 갈릴 여지가 없다.

배경은 `docs/plugin-exploration.md`.
"""

from __future__ import annotations

import asyncio
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
from app.main import VERSION
from app.store import JobRow, TenantScope

from tests.conftest import auth

PLATFORM = "_platform"

#: 테스트 번들이 요구하는 호스트 범위 — **지금 판의 마이너 범위**를 따라간다.
#: 0.1.0 → 0.2.0 으로 올릴 때 `>=0.1,<0.2` 로 박아 둔 번들이 진짜로 거절됐다(그것이 이
#: 필드의 뜻이다). 범위를 박아 두면 판을 올릴 때마다 여기가 먼저 깨진다.
_MAJOR, _MINOR = (int(part) for part in VERSION.split(".")[:2])
HOST_RANGE = f">={_MAJOR}.{_MINOR},<{_MAJOR}.{_MINOR + 1}"

MANIFEST = f"""
[plugin]
id = "acme.daily-digest"
name = "일일 요약"
version = "1.0.0"
requires_host = "{HOST_RANGE}"
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
        tenant_id=PLATFORM, host_version=VERSION, now=harness.clock,
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


# ── 재귀 방지 ────────────────────────────────────────────────────────────────


def test_a_job_a_plugin_created_does_not_wake_that_plugin(
    harness, client, signing_key, platform_tenant
):
    """**계획서(§11-4)가 이름으로 지정한 테스트다.**

    막으려는 고리: 플러그인이 깨어난다 → `/v1/generate` → 그 잡이 끝난다 →
    그 완료가 다시 그 플러그인을 깨운다 → … 한 바퀴마다 돈을 쓰면서 아무도
    멈추라고 말하지 않는다.

    표식은 잡이 만들어지는 순간에만 붙일 수 있으므로, 트리거보다 **먼저** 있어야 한다.
    """
    result = do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")

    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "요약할 내용", "wait": 0},
        headers=auth(result.token),
    )
    assert response.status_code in (200, 202), response.text

    job = harness.store.get_job(platform_tenant, response.json()["job_id"])
    assert job.origin_plugin == "acme.daily-digest"
    assert plugins.may_wake_plugins(job) is False


def test_a_job_a_person_created_does_wake_plugins(harness, client, acme):
    """반대 방향도 본다. 전부 막으면 트리거는 영원히 안 도는 기능이 된다."""
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "요약할 내용", "wait": 0},
        headers=auth(acme["service"]),
    )
    assert response.status_code in (200, 202), response.text

    job = harness.store.get_job(TenantScope("acme"), response.json()["job_id"])
    assert job.origin_plugin is None
    assert plugins.may_wake_plugins(job) is True


def test_a_plugins_job_wakes_no_plugin_at_all_not_just_its_own(
    harness, signing_key, platform_tenant
):
    """**자기 자신만 막으면 A→B→A 고리가 그대로 남는다.**

    두 플러그인이 각자 정직해도 짝지어 놓으면 도는 고리라, 하나씩 심사해서는
    보이지 않는다. 그래서 판정에 플러그인 id 를 쓰지 않는다 — 판정 함수의 인자에
    깨우려는 대상이 아예 없다는 것이 그 설계다.
    """
    import inspect

    signature = inspect.signature(plugins.may_wake_plugins)
    assert list(signature.parameters) == ["job"], (
        "판정이 '어느 플러그인을 깨우는가' 를 받기 시작하면 자기 고리만 막게 된다"
    )

    do_install(harness, bundle(key=signing_key))
    job_of_a = JobRow(
        id="j", tenant_id=PLATFORM, service_id="acme.daily-digest",
        role="summarize", lane="interactive", status="ok",
        origin_plugin="acme.daily-digest",
    )
    assert plugins.may_wake_plugins(job_of_a) is False


def test_the_origin_survives_uninstalling_the_plugin(
    harness, client, signing_key, platform_tenant
):
    """**표식이 잡 행에 있는 이유다.**

    `plugins` 를 되짚어 출처를 알아내면, 플러그인을 지우는 순간 그 플러그인이 만든
    잡이 전부 "사람이 만든 잡" 으로 바뀐다 — 지웠다 다시 깔면 그 사이 잡들이
    트리거를 깨우게 된다.
    """
    result = do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")
    response = client.post(
        "/v1/generate",
        json={"role": "summarize", "prompt": "요약할 내용", "wait": 0},
        headers=auth(result.token),
    )
    job_id = response.json()["job_id"]

    plugins.uninstall(harness.store, "acme.daily-digest", actor="t", data_dir=harness.data_dir)

    job = harness.store.get_job(platform_tenant, job_id)
    assert job.origin_plugin == "acme.daily-digest"
    assert plugins.may_wake_plugins(job) is False


def test_an_ambiguous_origin_is_read_as_a_plugin_not_a_person(harness):
    """애매하면 **안 깨우는 쪽**이다. 깨우는 쪽으로 틀리면 그것이 고리다."""
    def job(origin):
        return JobRow(
            id="j", tenant_id=PLATFORM, service_id="s", role="summarize",
            lane="interactive", status="ok", origin_plugin=origin,
        )

    assert plugins.may_wake_plugins(job("")) is False
    assert plugins.may_wake_plugins(job(None)) is True


def test_the_admin_list_shows_how_many_jobs_each_plugin_made(
    harness, client, signing_key, platform_tenant
):
    """출처 칸을 사람이 볼 수 있어야 한다 — 아무도 안 읽는 칸은 언젠가 틀린다."""
    result = do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t")

    [row] = plugins.snapshot(harness.store, data_dir=harness.data_dir)
    assert row["jobs_created"] == 0

    for _ in range(2):
        client.post(
            "/v1/generate",
            json={"role": "summarize", "prompt": "요약할 내용", "wait": 0},
            headers=auth(result.token),
        )

    [row] = plugins.snapshot(harness.store, data_dir=harness.data_dir)
    assert row["jobs_created"] == 2


# ── 스케줄 트리거 ────────────────────────────────────────────────────────────

SCHEDULED = MANIFEST + """
[trigger]
kind = "schedule"
schedule = "0 8 * * *"
timezone = "Asia/Seoul"
"""


def scheduled_bundle(**overrides):
    manifest = SCHEDULED
    for old, new in overrides.items():
        manifest = manifest.replace(old.replace("__", " "), new)
    return manifest


def install_scheduled(harness, signing_key, manifest: str = SCHEDULED):
    return do_install(harness, bundle(manifest, key=signing_key))


def test_a_plugin_without_a_trigger_section_has_no_schedule(harness, signing_key, platform_tenant):
    """**스케줄이 없는 것이 기본이다.** 선언하지 않은 플러그인이 돌기 시작하면 안 된다."""
    do_install(harness, bundle(key=signing_key))
    row = harness.store.get_plugin("acme.daily-digest")
    assert row["schedule"] is None
    assert row["next_run_at"] is None


def test_a_declared_schedule_is_stored_with_its_timezone(harness, signing_key, platform_tenant):
    install_scheduled(harness, signing_key)
    row = harness.store.get_plugin("acme.daily-digest")
    assert row["schedule"] == "0 8 * * *"
    assert row["schedule_tz"] == "Asia/Seoul"


@pytest.mark.parametrize("edit,fragment", [
    ('schedule = "0 8 * * *"', "다섯 칸"),          # 아래에서 값만 갈아 끼운다
    ('timezone = "Asia/Seoul"', "시간대"),
])
def test_a_schedule_that_cannot_be_computed_is_refused_at_install(
    harness, signing_key, platform_tenant, edit, fragment
):
    """**설치 시점에 실제로 계산해 본다.** 형식만 보면 안 도는 스케줄이 설치된다."""
    broken = SCHEDULED.replace(edit, {
        'schedule = "0 8 * * *"': 'schedule = "0 8 * *"',
        'timezone = "Asia/Seoul"': 'timezone = "Asia/Seuol"',
    }[edit])
    with pytest.raises(PluginError) as caught:
        do_install(harness, bundle(broken, key=signing_key))
    assert fragment in str(caught.value)


def test_a_schedule_that_never_fires_is_refused(harness, signing_key, platform_tenant):
    """`0 0 30 2 *` — 2월 30일. 문법은 맞고 영원히 안 돈다.

    받아 두면 켜져 있고 화면에도 보이는데 아무 일도 안 하는 플러그인이 되고,
    그 상태를 아무도 못 읽는다.
    """
    never = SCHEDULED.replace('"0 8 * * *"', '"0 0 30 2 *"')
    with pytest.raises(PluginError) as caught:
        do_install(harness, bundle(never, key=signing_key))
    assert "없는 날짜" in str(caught.value)


def test_an_unsupported_trigger_kind_is_refused(harness, signing_key, platform_tenant):
    """`daemon` 은 배관이 없다. 받아 두고 안 도는 것보다 거부가 낫다."""
    daemon = SCHEDULED.replace('kind = "schedule"', 'kind = "daemon"')
    with pytest.raises(PluginError) as caught:
        do_install(harness, bundle(daemon, key=signing_key))
    assert "트리거" in str(caught.value)


# ── 예정은 켤 때 잡는다 ──────────────────────────────────────────────────────


def test_installing_does_not_schedule_anything(harness, signing_key, platform_tenant):
    """설치는 켜는 것이 아니다 — 예정도 안 잡는다.

    설치 시점에 잡아 두면, 설치해 두고 한 달 뒤에 켠 플러그인이 **켜자마자** 한 번 돈다.
    """
    install_scheduled(harness, signing_key)
    assert harness.store.get_plugin("acme.daily-digest")["next_run_at"] is None


def test_activating_schedules_from_now(harness, signing_key, platform_tenant):
    install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)

    next_run = harness.store.get_plugin("acme.daily-digest")["next_run_at"]
    assert next_run is not None and next_run > harness.clock()


def test_deactivating_clears_the_schedule(harness, signing_key, platform_tenant):
    """끄면 예정이 없어진다. 남겨 두면 다시 켤 때 지난 예정이 즉시 터진다."""
    install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t", now=harness.clock)
    assert harness.store.get_plugin("acme.daily-digest")["next_run_at"] is None


# ── 클레임 ──────────────────────────────────────────────────────────────────


def activated(harness, signing_key):
    install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    return harness.store.get_plugin("acme.daily-digest")["next_run_at"]


def test_before_the_scheduled_time_the_answer_is_no(harness, signing_key, platform_tenant):
    due_at = activated(harness, signing_key)
    tick = plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock)
    assert tick.due is False
    assert tick.next_run_at == due_at


def test_after_the_scheduled_time_the_answer_is_yes_exactly_once(
    harness, signing_key, platform_tenant
):
    """**이 파일에서 스케줄 쪽의 핵심 테스트다.**

    두 번째 물음에 또 "예" 라고 하면, 폴링하는 플러그인이 하루 종일 돈다.
    """
    due_at = activated(harness, signing_key)
    harness.clock.now = due_at + 1

    first = plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock)
    assert first.due is True
    assert first.scheduled_for == due_at
    assert first.next_run_at > harness.clock()

    second = plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock)
    assert second.due is False


def test_only_one_replica_wins_the_same_tick(harness, signing_key, platform_tenant):
    """복제본이 셋이면 셋이 동시에 묻는다. **클레임이 CAS 라서** 하나만 가져간다.

    이것이 플러그인이 자기 cron 을 쓰는 것과 다른 점 하나다 — 자기 cron 은
    복제본 수만큼 돈다.
    """
    due_at = activated(harness, signing_key)
    harness.clock.now = due_at + 1

    answers = [
        plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock)
        for _ in range(3)
    ]
    assert sum(1 for a in answers if a.due) == 1


def test_a_long_outage_fires_once_not_a_backlog(harness, signing_key, platform_tenant):
    """사흘 꺼져 있었다고 사흘치를 몰아 돌리지 않는다. **그게 사고다.**

    얼마나 늦었는지는 `scheduled_for` 로 알려 주므로, 따라잡을지는 플러그인이 정한다.
    """
    due_at = activated(harness, signing_key)
    harness.clock.now = due_at + 3 * 86400 + 60

    first = plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock)
    assert first.due is True
    assert first.scheduled_for == due_at              # 늦었다는 사실은 그대로 전한다
    assert first.next_run_at > harness.clock()        # 다음은 **지금** 기준

    assert plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock).due is False


def test_a_plugin_without_a_schedule_is_told_no_not_an_error(
    harness, signing_key, platform_tenant
):
    do_install(harness, bundle(key=signing_key))
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    assert plugins.claim_tick(harness.store, "acme.daily-digest", now=harness.clock).due is False


def test_an_upgrade_clears_the_schedule_because_it_lands_inactive(
    harness, signing_key, platform_tenant
):
    """판올림은 이미 **비활성으로 내려간다**(`test_an_upgrade_lands_inactive`).

    그러면 예정도 같이 없어져야 규칙이 하나로 남는다 — "꺼진 플러그인은 예정이 없다".
    남겨 두면 꺼져 있는데 예정이 살아 있는 상태가 생기고, `set_active` 가 끌 때
    지우는 것과 갈린다. 사람이 다시 켜면 그때부터 다시 잡힌다.
    """
    activated(harness, signing_key)
    upgraded = SCHEDULED.replace('version = "1.0.0"', 'version = "1.1.0"')
    do_install(harness, bundle(upgraded, key=signing_key))
    assert harness.store.get_plugin("acme.daily-digest")["next_run_at"] is None

    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    assert harness.store.get_plugin("acme.daily-digest")["next_run_at"] > harness.clock()


# ── 앞문에서 ────────────────────────────────────────────────────────────────


def test_the_tick_route_answers_the_plugins_own_token(
    harness, client, signing_key, platform_tenant
):
    result = install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)

    early = client.post("/v1/plugin/tick", headers=auth(result.token))
    assert early.status_code == 200, early.text
    assert early.json()["due"] is False

    harness.clock.now = harness.store.get_plugin("acme.daily-digest")["next_run_at"] + 1
    due = client.post("/v1/plugin/tick", headers=auth(result.token))
    assert due.json()["due"] is True
    assert due.json()["id"] == "acme.daily-digest"


def test_turning_the_plugin_off_stops_its_schedule_at_the_same_choke_point(
    harness, client, signing_key, platform_tenant
):
    """**끄면 스케줄도 선다.**

    제출 경로와 **같은 함수**(`auth.active_service`)를 지나므로 두 곳이 갈릴 수 없다.
    자기 cron 을 쓰는 플러그인은 관제 화면에서 꺼도 계속 때린다 — 그 차이가 이
    기능이 존재하는 이유의 절반이다.
    """
    result = install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    assert client.post("/v1/plugin/tick", headers=auth(result.token)).status_code == 200

    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t", now=harness.clock)
    assert client.post("/v1/plugin/tick", headers=auth(result.token)).status_code == 401


def test_an_ordinary_service_token_is_not_a_plugin(harness, client, acme):
    """플러그인이 아닌 서비스에게 "예정 없음" 이라고 답하면 거짓말이 된다."""
    assert client.post("/v1/plugin/tick", headers=auth(acme["service"])).status_code == 404


def test_a_plugin_can_only_claim_its_own_tick(harness, client, signing_key, platform_tenant):
    """**남의 차례를 가져갈 수 있는 인자가 없다.**

    플러그인 id 를 요청 본문에서 받으면 언젠가 남의 것을 가져간다. 토큰에서
    유도하므로 그 경로가 존재하지 않는다.
    """
    result = install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)

    body = client.post(
        "/v1/plugin/tick", json={"id": "someone.else"}, headers=auth(result.token)
    ).json()
    assert body["id"] == "acme.daily-digest"


# ── 이벤트 트리거 ────────────────────────────────────────────────────────────
#
# 훅은 **모델 경계 뒤에 선다.** 프롬프트가 나가고 결과가 돌아온 뒤에 보고, 나가기 전에
# 고치거나 막지 않는다. 여기서 고정하는 것은 넷이다 — 종결마다 한 번 · 모델이 본 것만 ·
# 플러그인이 만든 잡은 안 줌 · 커서는 앞으로만.

EVENTED = MANIFEST + """
[trigger]
kind = "event"
event = "job.finished"
"""

EVENTED_SUMMARIZE_ONLY = EVENTED + 'roles = ["summarize"]\n'

WATCHER = EVENTED.replace('id = "acme.daily-digest"', 'id = "acme.watcher"')

#: 가드가 **가리는** 값(이메일)과 **경계에 따라** 가리는 값(카드 — 안에서는 보이고 밖으로는
#: 가린다). 주민번호는 차단이라 여기 못 쓴다 — 차단된 프롬프트는 잡이 되지 않는다.
EMAIL = "hong@example.com"
VALID_CARD = "4111 1111 1111 1111"


def drive(harness, lane="interactive", rounds=6):
    """스케줄러를 손으로 돌린다 — 테스트는 배경 루프를 켜지 않는다."""

    async def run() -> None:
        for _ in range(rounds):
            await harness.scheduler.tick(lane)
            await asyncio.sleep(0)

    asyncio.run(run())


def boundaries(harness):
    return {name: state.node.data_boundary for name, state in harness.cluster.nodes.items()}


def install_evented(harness, signing_key, manifest=EVENTED, *, plugin_id="acme.daily-digest", **overrides):
    result = do_install(harness, bundle(manifest, key=signing_key), **overrides)
    plugins.set_active(harness.store, plugin_id, True, actor="t", now=harness.clock)
    return result


def submit(client, token, prompt="요약할 내용", role="summarize"):
    response = client.post(
        "/v1/generate", json={"role": role, "prompt": prompt, "wait": 0}, headers=auth(token),
    )
    assert response.status_code in (200, 202), response.text
    return response.json()["job_id"]


def pull(harness, plugin_id="acme.daily-digest", *, ack=None, limit=50):
    return plugins.pull_events(
        harness.store, plugin_id, ack=ack, limit=limit, now=harness.clock(),
        boundaries=boundaries(harness),
    )


def test_a_declared_event_subscription_is_stored(harness, signing_key, platform_tenant):
    do_install(harness, bundle(EVENTED_SUMMARIZE_ONLY, key=signing_key))
    row = harness.store.get_plugin("acme.daily-digest")
    assert row["event"] == "job.finished"
    assert json.loads(row["event_roles"]) == ["summarize"]
    assert row["event_cursor"] is None, "설치는 켜는 것이 아니다 — 커서도 없다"


def test_an_unknown_event_is_refused_at_install(harness, signing_key, platform_tenant):
    """구독은 했는데 아무것도 안 오는 플러그인이 되면 그 원인을 화면에서 못 읽는다."""
    with pytest.raises(PluginError) as caught:
        do_install(harness, bundle(EVENTED.replace("job.finished", "job.started"), key=signing_key))
    assert "event" in str(caught.value)


def test_a_role_filter_with_a_typo_is_refused_when_the_roles_are_known(
    harness, signing_key, platform_tenant
):
    """오타 난 역할은 조용히 아무것도 안 받는 플러그인이 된다 — 설치 시점에 거른다."""
    typo = EVENTED + 'roles = ["sumarize"]\n'
    with pytest.raises(PluginError) as caught:
        do_install(harness, bundle(typo, key=signing_key), known_roles=["summarize", "inside"])
    assert "sumarize" in str(caught.value)


def test_an_internal_role_cannot_be_subscribed(harness, signing_key, platform_tenant):
    with pytest.raises(PluginError):
        do_install(harness, bundle(EVENTED + 'roles = ["_guard_classify"]\n', key=signing_key))


def test_a_finished_job_is_delivered_once_with_what_the_model_saw(
    harness, client, signing_key, platform_tenant, acme
):
    """**이 절의 핵심이다.** 훅은 모델이 본 것을 본다 — 소비자가 보낸 원문이 아니다.

    가드가 가린 뒤의 프롬프트가 노드로 갔고, 출력 가드를 지난 응답이 소비자에게 갔다.
    훅은 그 둘을 받는다. 원문은 여기 없다.
    """
    install_evented(harness, signing_key)
    job_id = submit(
        client, acme["service"], prompt=f"담당자 {EMAIL} 의 카드 {VALID_CARD} 관련 문의를 요약",
    )

    assert pull(harness).events == [], "큐에 있는 잡은 아직 종결이 아니다"

    drive(harness)
    job = harness.store.get_job(TenantScope("acme"), job_id)
    assert job.status == "ok", job.error

    first = pull(harness)
    [event] = first.events
    assert event["job_id"] == job_id
    assert event["kind"] == "job.finished" and event["status"] == "ok"
    assert event["tenant"] == "acme" and event["role"] == "summarize"
    # 노드의 경계에 따라 나간 변형이 다르다 — 훅은 **나간 그것**을 준다.
    sent = job.prompt_masked if boundaries(harness)[job.node] == "internal" else job.prompt_external
    assert event["prompt"] == sent and event["boundary"] == boundaries(harness)[job.node]
    assert EMAIL not in event["prompt"], "가드가 가린 값이 훅에서 다시 보이면 안 된다"
    assert event["output"] == job.response
    assert event["model"] == job.model and event["node"] == job.node
    assert first.pending == 0

    again = pull(harness)
    assert [e["id"] for e in again.events] == [event["id"]], "ack 전에는 같은 배치를 다시 준다"

    acked = pull(harness, ack=first.cursor)
    assert acked.events == [] and acked.pending == 0
    assert harness.store.get_plugin("acme.daily-digest")["event_cursor"] == first.cursor


def test_a_failed_job_carries_its_error_and_no_output(harness, signing_key, platform_tenant, acme):
    install_evented(harness, signing_key)
    scope = TenantScope("acme")
    job_id = harness.store.create_job(
        scope, service_id="acme-web", role="summarize", lane="interactive",
        status="running", prompt_masked="가린 프롬프트",
    )
    harness.store.update_job(
        scope, job_id, node="in-1", status="failed", error="노드가 죽었다",
        error_code="backend_unavailable", finished_at=harness.clock(),
    )
    [event] = pull(harness).events
    assert event["status"] == "failed" and event["error_code"] == "backend_unavailable"
    assert event["output"] is None
    assert event["prompt"] == "가린 프롬프트", "노드까지 간 잡이다 — 모델이 본 프롬프트는 있다"


def test_a_job_that_never_reached_a_node_has_no_prompt_in_its_event(
    harness, client, signing_key, platform_tenant, acme
):
    """큐에서 취소된 잡도 종결이지만 모델이 본 것이 없다 — 훅은 그 경계에 서 있다."""
    install_evented(harness, signing_key)
    job_id = submit(client, acme["service"])
    cancelled = client.delete(f"/v1/jobs/{job_id}", headers=auth(acme["service"]))
    assert cancelled.status_code == 200, cancelled.text

    [event] = pull(harness).events
    assert event["status"] == "cancelled"
    assert event["node"] is None and event["prompt"] is None and event["output"] is None


def test_a_plugins_own_job_is_delivered_to_no_plugin(harness, client, signing_key, platform_tenant):
    """**재귀 방지가 실제로 작동하는 자리다.**

    플러그인이 받은 이벤트로 잡을 만들면 그 잡의 종결은 자기에게도 남에게도 안 온다.
    훑기는 하므로 커서는 지나간다 — 안 지나가면 그 이벤트가 영원히 「밀림」으로 남는다.
    """
    digest = install_evented(harness, signing_key)
    install_evented(harness, signing_key, WATCHER, plugin_id="acme.watcher")

    job_id = submit(client, digest.token)
    drive(harness)
    finished = harness.store.get_job(TenantScope(PLATFORM), job_id)
    assert finished.status == "ok", finished.error

    for plugin_id in ("acme.daily-digest", "acme.watcher"):
        result = pull(harness, plugin_id)
        assert result.events == [], f"{plugin_id} 가 플러그인이 만든 잡의 종결을 받았다"
        assert result.cursor == harness.store.latest_plugin_event_id(), "훑었으므로 커서는 지나간다"
        assert result.pending == 0


def test_a_platform_plugin_sees_every_tenant_and_a_tenant_plugin_only_its_own(
    harness, client, signing_key, platform_tenant, acme, globex
):
    """플러그인이 사는 테넌트가 곧 보는 범위다 — 별도 스위치가 없다."""
    install_evented(harness, signing_key)
    install_evented(harness, signing_key, WATCHER, plugin_id="acme.watcher", tenant_id="acme")

    submit(client, acme["service"])
    submit(client, globex["service"])
    drive(harness)

    assert {e["tenant"] for e in pull(harness).events} == {"acme", "globex"}
    assert {e["tenant"] for e in pull(harness, "acme.watcher").events} == {"acme"}


def test_the_role_filter_narrows_what_is_delivered(harness, client, signing_key, platform_tenant, acme):
    install_evented(harness, signing_key, EVENTED_SUMMARIZE_ONLY)
    summarize = submit(client, acme["service"], role="summarize")
    inside = submit(client, acme["service"], role="inside")
    drive(harness)
    for job_id in (summarize, inside):
        assert harness.store.get_job(TenantScope("acme"), job_id).status == "ok"

    delivered = pull(harness).events
    assert [e["job_id"] for e in delivered] == [summarize]


def test_the_cursor_only_moves_forward_and_never_past_what_exists(
    harness, client, signing_key, platform_tenant, acme
):
    """뒤처진 복제본의 ack 가 커서를 되돌리면 앞선 복제본이 처리한 것이 다시 나간다."""
    install_evented(harness, signing_key)
    for _ in range(3):
        submit(client, acme["service"])
    drive(harness)

    first = pull(harness, limit=2)
    assert len(first.events) == 2 and first.pending == 1
    second = pull(harness, ack=first.cursor, limit=2)
    assert len(second.events) == 1 and second.pending == 0
    third_id = second.events[0]["id"]

    stale = pull(harness, ack=first.cursor - 1)
    assert [e["id"] for e in stale.events] == [third_id], "뒤처진 ack 가 커서를 되돌렸다"
    bogus = pull(harness, ack=second.cursor + 1000)
    assert [e["id"] for e in bogus.events] == [third_id], "없는 이벤트를 ack 해 미래를 건너뛰었다"


def test_events_before_activation_are_not_replayed(harness, client, signing_key, platform_tenant, acme):
    """켜는 순간부터다 — 사흘 꺼 뒀다 켠 사람이 원한 것은 사흘치 종결이 아니다."""
    do_install(harness, bundle(EVENTED, key=signing_key))
    submit(client, acme["service"])
    drive(harness)

    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)
    assert pull(harness).events == []

    fresh = submit(client, acme["service"])
    drive(harness)
    assert [e["job_id"] for e in pull(harness).events] == [fresh]

    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t", now=harness.clock)
    assert harness.store.get_plugin("acme.daily-digest")["event_cursor"] is None


def test_a_review_verdict_is_not_a_new_finish_but_a_second_run_is(
    harness, signing_key, platform_tenant, acme
):
    """종결에서 종결로(검토 판정)는 같은 종결의 정정이다. 큐로 돌아갔다 다시 끝나면 새 종결이다."""
    install_evented(harness, signing_key)
    scope = TenantScope("acme")
    job_id = harness.store.create_job(
        scope, service_id="acme-web", role="summarize", lane="interactive",
        status="running", prompt_masked="p",
    )
    harness.store.update_job(scope, job_id, status="needs_review", finished_at=1.0)
    harness.store.update_job(scope, job_id, status="failed", finished_at=2.0)
    assert [e["status"] for e in pull(harness).events] == ["needs_review"]

    harness.store.update_job(scope, job_id, status="queued")
    harness.store.update_job(scope, job_id, status="running")
    harness.store.update_job(scope, job_id, status="ok", finished_at=3.0, response="r")
    assert [e["status"] for e in pull(harness).events] == ["needs_review", "ok"]


def test_purging_the_job_takes_its_event_with_it(harness, client, signing_key, platform_tenant, acme):
    """이벤트 행은 잡을 가리키기만 한다 — 그래서 보존·파기가 잡과 함께 간다."""
    install_evented(harness, signing_key)
    submit(client, acme["service"])
    drive(harness)
    assert len(pull(harness).events) == 1

    harness.clock.advance(31 * 86400)
    purged = harness.store.purge_expired(job_retention_days=30)
    assert purged["jobs"] == 1

    after = pull(harness)
    assert after.events == [] and after.pending == 0
    left = harness.store._conn.execute("SELECT COUNT(*) AS n FROM plugin_events").fetchone()["n"]
    assert left == 0


def test_the_prompt_in_the_event_is_the_variant_the_node_boundary_received():
    """경계 밖 노드에는 더 가린 변형이 갔다. 노드를 모르면 밖으로 친다 — 더 가린 쪽이 안전한 쪽이다."""
    job = JobRow(
        id="j", tenant_id="acme", service_id="s", role="summarize", lane="interactive",
        status="ok", node="n", prompt_masked="덜 가린", prompt_external="더 가린",
    )

    def prompt_for(node_boundaries):
        return plugins.event_payload(1, 0.0, "ok", job, node_boundaries)["prompt"]

    assert prompt_for({"n": "internal"}) == "덜 가린"
    assert prompt_for({"n": "external"}) == "더 가린"
    assert prompt_for({}) == "더 가린"


def test_an_evented_upgrade_lands_inactive_and_forgets_its_cursor(harness, signing_key, platform_tenant):
    install_evented(harness, signing_key)
    assert harness.store.get_plugin("acme.daily-digest")["event_cursor"] is not None

    do_install(harness, bundle(EVENTED.replace('version = "1.0.0"', 'version = "1.1.0"'), key=signing_key))
    row = harness.store.get_plugin("acme.daily-digest")
    assert row["version"] == "1.1.0"
    assert row["event_cursor"] is None
    [shown] = plugins.snapshot(harness.store, data_dir=harness.data_dir)
    assert shown["active"] is False and shown["events_pending"] is None


# ── 이벤트 — 앞문에서 ────────────────────────────────────────────────────────


def test_the_events_route_answers_the_plugins_own_token(
    harness, client, signing_key, platform_tenant, acme
):
    result = install_evented(harness, signing_key)
    submit(client, acme["service"])
    drive(harness)

    first = client.post("/v1/plugin/events", headers=auth(result.token))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["id"] == "acme.daily-digest"
    assert len(body["events"]) == 1 and body["pending"] == 0
    assert isinstance(body["cursor"], int)

    acked = client.post(
        "/v1/plugin/events", json={"ack": body["cursor"], "limit": 10}, headers=auth(result.token),
    )
    assert acked.status_code == 200 and acked.json()["events"] == []


def test_turning_the_plugin_off_stops_its_events_at_the_same_choke_point(
    harness, client, signing_key, platform_tenant
):
    """제출·스케줄과 **같은 함수**(`auth.active_service`)를 지난다."""
    result = install_evented(harness, signing_key)
    assert client.post("/v1/plugin/events", headers=auth(result.token)).status_code == 200

    plugins.set_active(harness.store, "acme.daily-digest", False, actor="t", now=harness.clock)
    assert client.post("/v1/plugin/events", headers=auth(result.token)).status_code == 401


def test_a_plugin_without_an_event_subscription_is_told_so(
    harness, client, signing_key, platform_tenant
):
    """빈 목록을 주면 "아직 없다" 로 읽는다 — 구독을 안 했다는 것은 다른 사실이다."""
    result = install_scheduled(harness, signing_key)
    plugins.set_active(harness.store, "acme.daily-digest", True, actor="t", now=harness.clock)

    response = client.post("/v1/plugin/events", headers=auth(result.token))
    assert response.status_code == 409
    assert response.json()["code"] == "plugin_no_event_trigger"


def test_an_ordinary_service_token_gets_no_events(harness, client, acme):
    assert client.post("/v1/plugin/events", headers=auth(acme["service"])).status_code == 404


@pytest.mark.parametrize(
    "body", [{"ack": -1}, {"ack": "x"}, {"ack": True}, {"limit": 0}, {"limit": "many"}],
)
def test_a_malformed_ack_or_limit_is_rejected(harness, client, signing_key, platform_tenant, body):
    result = install_evented(harness, signing_key)
    response = client.post("/v1/plugin/events", json=body, headers=auth(result.token))
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_field"


def test_the_admin_list_shows_the_subscription_and_the_backlog(
    harness, client, signing_key, platform_tenant, acme
):
    """켜 놓았는데 「밀림」이 늘기만 하면 플러그인이 안 돌고 있는 것이다 — 화면이 그것을 읽어야 한다."""
    result = install_evented(harness, signing_key, EVENTED_SUMMARIZE_ONLY)
    submit(client, acme["service"])
    drive(harness)

    [row] = client.get("/v1/platform/plugins", headers=auth(acme["platform_admin"])).json()["plugins"]
    # 화면의 트리거 칸(`triggerCell`)이 읽는 것 전부.
    assert {"event", "event_roles", "events_pending", "last_event_at", "schedule", "last_run_at"} <= set(row)
    assert row["event"] == "job.finished" and row["event_roles"] == ["summarize"]
    assert row["events_pending"] == 1 and row["last_event_at"] is None

    cursor = client.post("/v1/plugin/events", headers=auth(result.token)).json()["cursor"]
    client.post("/v1/plugin/events", json={"ack": cursor}, headers=auth(result.token))
    [row] = client.get("/v1/platform/plugins", headers=auth(acme["platform_admin"])).json()["plugins"]
    assert row["events_pending"] == 0 and row["last_event_at"] is not None


def test_a_plugin_owned_service_is_not_editable_by_hand(harness, client, signing_key, platform_tenant):
    """플러그인 서비스의 정본은 매니페스트다 — 콘솔·API 로 고치면 다음 설치·갱신과 어긋난다(409)."""
    from app.auth import ROLE_TENANT_ADMIN, issue_token

    result = do_install(harness, bundle(key=signing_key))
    _, admin = issue_token(harness.store, platform_tenant, result.service_id, role=ROLE_TENANT_ADMIN)

    response = client.put(
        f"/v1/admin/services/{result.service_id}", json={"allow_roles": ["*"]},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "plugin_managed"
    assert json.loads(harness.store.get_service(platform_tenant, result.service_id)["allow_roles_json"]) == ["summarize"]
