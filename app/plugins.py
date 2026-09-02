"""플러그인 — **앞문으로 지나는 소비자다.**

플러그인이 LLM 을 쓸 때는 이 제품의 API(`POST /v1/generate`)를 지난다. 시스템
안쪽에서 노드에 직접 붙지 않는다. 그 한 문장이 이 모듈의 설계 전부를 정한다.

    플러그인 = `services` 행 하나 + 서비스 토큰 하나 + (어딘가에서 도는) 프로그램

권한 모델을 새로 만들지 않는다. 매니페스트의 `[service]` 절은 `create_service()` 의
인자 이름 그대로이고, 관리자가 설치 화면에서 읽는 문장과 DB 에 들어가는 값과 런타임에
강제되는 것이 **같은 것**이다.

같은 이유로 **활성/비활성을 여기 따로 두지 않는다.** 플러그인을 끈다는 것은 그
플러그인의 서비스를 끄는 것이고, 그 판정은 `pipeline` 의 제출 경로 한 곳에서만
일어난다(`services.status != "active"` → 401). 토글을 두 곳에 두면 둘은 반드시
어긋나고, 그때 "껐는데 왜 도느냐" 가 된다.

지금 지원하는 실행 형태는 `external` 하나다 — **컨트롤 플레인이 프로세스를 띄우지
않는다.** 운영자가 별도 컨테이너·호스트에서 띄우고, 이 제품은 신원(서비스+토큰)과
카탈로그와 스위치만 갖는다. 그래서 언어가 자유롭고(Go·Node·무엇이든) 프로세스
감독 코드가 필요 없다. 자세한 배경은 `docs/plugin-exploration.md`.

새 의존성이 없다 — `zipfile`·`hashlib`·`tomllib` 은 표준 라이브러리이고, 서명 검증은
이미 의존성인 `cryptography` 의 Ed25519 를 쓴다.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .auth import ROLE_SERVICE, issue_token
from .store import SqliteStore, TenantScope

#: 번들 안에서 이름이 정해진 파일 셋.
MANIFEST_NAME = "plugin.toml"
CHECKSUMS_NAME = "MANIFEST.sha256"
SIGNATURE_NAME = "SIGNATURE"
_RESERVED = frozenset({CHECKSUMS_NAME, SIGNATURE_NAME})

#: 상한들. **압축 해제 전에 건다** — 해제하고 나서 재는 것은 이미 늦다.
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_FILES = 512

#: 지금 지원하는 실행 형태. 늘릴 때는 그 형태의 감독 코드도 같이 온다.
SUPPORTED_KINDS = ("external",)

#: 역DNS. 점이 하나는 있어야 한다 — 일반 서비스 id(`acme-web`)와 섞이지 않게 한다.
_ID = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9][a-z0-9_-]*)+$")
_VERSION = re.compile(r"^\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.-]+)?$")
_CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


class PluginError(Exception):
    """설치를 거부한 이유. **사람이 읽고 고칠 수 있는 문장이어야 한다.**"""


# ── 매니페스트 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Manifest:
    plugin_id: str
    name: str
    version: str
    kind: str
    description: str = ""
    requires_host: str = ""
    endpoint: str | None = None
    allow_roles: tuple[str, ...] = ()
    rate_limit_per_min: int | None = None
    budget_usd_per_month: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def service_fields(self) -> dict[str, Any]:
        """`create_service()` 로 그대로 넘어가는 것들."""
        return {
            "allow_roles": list(self.allow_roles),
            "rate_limit_per_min": self.rate_limit_per_min,
            "budget_usd_per_month": self.budget_usd_per_month,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PluginError(message)


def parse_manifest(raw: bytes) -> Manifest:
    """`plugin.toml` 을 읽고 **거부할 것은 여기서 거부한다.**

    TOML 을 쓰는 이유는 이것이 **남이 준 파일**이기 때문이다. `config/` 가 YAML 인
    것은 그쪽이 운영자 소유라서다. `tomllib` 은 읽기 전용 stdlib 파서라 앵커·별칭이
    없고 공격 표면이 작다.
    """
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PluginError(f"{MANIFEST_NAME} 을 읽을 수 없습니다: {exc}") from exc

    plugin = data.get("plugin")
    _require(isinstance(plugin, dict), f"{MANIFEST_NAME} 에 [plugin] 절이 없습니다")

    plugin_id = str(plugin.get("id", ""))
    _require(
        bool(_ID.match(plugin_id)),
        f"플러그인 id 가 역DNS 형식이 아닙니다: {plugin_id!r} (예: acme.daily-digest)",
    )
    version = str(plugin.get("version", ""))
    _require(bool(_VERSION.match(version)), f"버전 형식이 아닙니다: {version!r}")

    kind = str(data.get("run", {}).get("kind", ""))
    _require(
        kind in SUPPORTED_KINDS,
        f"지원하지 않는 실행 형태입니다: {kind!r} — 지금 되는 것은 {', '.join(SUPPORTED_KINDS)}",
    )

    run = data.get("run", {})
    runtime = str(run.get("requires_runtime", "none"))
    _require(
        runtime == "none",
        "external 플러그인은 컨트롤 플레인이 띄우지 않으므로 런타임을 제공할 수 없습니다 "
        f"(requires_runtime={runtime!r}). 실행 환경은 운영자가 준비합니다",
    )
    endpoint = run.get("endpoint")
    if endpoint is not None:
        endpoint = str(endpoint)
        _require(
            endpoint.startswith(("http://", "https://")),
            f"endpoint 는 http(s) 여야 합니다: {endpoint!r}",
        )

    service = data.get("service")
    _require(
        isinstance(service, dict),
        f"{MANIFEST_NAME} 에 [service] 절이 없습니다 — 플러그인의 권한은 서비스가 갖습니다",
    )
    roles = service.get("allow_roles")
    _require(
        isinstance(roles, list) and roles and all(isinstance(r, str) and r for r in roles),
        "[service].allow_roles 는 비어 있지 않은 문자열 목록이어야 합니다 "
        "— 역할이 없으면 그 토큰으로 할 수 있는 일이 없습니다",
    )
    _require(
        all(not r.startswith("_") for r in roles),
        "밑줄로 시작하는 역할은 내부 전용이라 플러그인이 요청할 수 없습니다",
    )

    rate = service.get("rate_limit_per_min")
    _require(rate is None or (isinstance(rate, int) and rate > 0), "rate_limit_per_min 은 양의 정수여야 합니다")
    budget = service.get("budget_usd_per_month")
    _require(
        budget is None or (isinstance(budget, (int, float)) and budget >= 0),
        "budget_usd_per_month 는 0 이상이어야 합니다",
    )

    return Manifest(
        plugin_id=plugin_id,
        name=str(plugin.get("name") or plugin_id),
        version=version,
        kind=kind,
        description=str(plugin.get("description", "")),
        requires_host=str(plugin.get("requires_host", "")),
        endpoint=endpoint,
        allow_roles=tuple(roles),
        rate_limit_per_min=rate,
        budget_usd_per_month=float(budget) if budget is not None else None,
        raw=data,
    )


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = re.split(r"[.\-+]", text)
    out: list[int] = []
    for part in parts:
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out) or (0,)


def host_satisfies(host_version: str, spec: str) -> bool:
    """`>=0.1,<0.2` 같은 범위를 판정한다. **비어 있으면 통과다.**

    `packaging` 을 끌어오지 않는 이유는 의존성 5개 원칙이다. 지원하는 것은
    `>=` `>` `<=` `<` `==` 뿐이고, 그 밖의 문법은 **모르는 채 통과시키지 않고 거부**한다.
    """
    if not spec.strip():
        return True
    current = _version_tuple(host_version)
    for clause in spec.split(","):
        clause = clause.strip()
        match = re.match(r"^(>=|<=|==|>|<)\s*(\d[\w.\-+]*)$", clause)
        if match is None:
            raise PluginError(f"requires_host 를 해석할 수 없습니다: {clause!r}")
        op, want = match.group(1), _version_tuple(match.group(2))
        # 자릿수를 맞춘 뒤 비교한다 — 안 그러면 `1.0.0 == 1.0` 이 거짓이 되고,
        # 매니페스트를 쓴 사람은 자기가 왜 거부당했는지 알 수 없다.
        width = max(len(current), len(want))
        current_padded = current + (0,) * (width - len(current))
        want = want + (0,) * (width - len(want))
        current = current_padded
        ok = {
            ">=": current >= want, "<=": current <= want, "==": current == want,
            ">": current > want, "<": current < want,
        }[op]
        if not ok:
            return False
    return True


# ── 번들 안전 처리 ───────────────────────────────────────────────────────────


def _path_parts(name: str) -> list[str]:
    return [part for part in name.split("/") if part not in ("", ".")]


def safe_names(archive: zipfile.ZipFile) -> list[str]:
    """풀어도 되는 항목 이름들. **경로 순회와 zip bomb 을 여기서 막는다.**

    해제 후에 재는 것은 늦다 — 이미 디스크를 채운 뒤다. 그래서 zip 의 헤더가
    말하는 크기로 **풀기 전에** 판정한다(헤더가 거짓말할 수 있으므로 해제할 때도
    실제 바이트를 센다).
    """
    infos = archive.infolist()
    _require(len(infos) <= MAX_FILES, f"번들 항목이 너무 많습니다: {len(infos)} > {MAX_FILES}")

    total = 0
    names: list[str] = []
    for info in infos:
        name = info.filename
        if name.endswith("/"):
            continue
        _require(not name.startswith("/"), f"절대 경로 항목: {name!r}")
        _require("\\" not in name, f"역슬래시 경로 항목: {name!r}")
        parts = _path_parts(name)
        _require(".." not in parts, f"상위 디렉터리를 가리키는 항목: {name!r}")
        # zip 은 심볼릭 링크를 외부 속성 상위 16비트에 담는다. 링크는 풀지 않는다 —
        # `/keys/master.key` 를 가리키는 링크 하나면 번들이 키를 읽어 간다.
        mode = info.external_attr >> 16
        _require(not (mode & 0o170000 == 0o120000), f"심볼릭 링크는 담을 수 없습니다: {name!r}")
        total += info.file_size
        _require(
            total <= MAX_UNCOMPRESSED_BYTES,
            f"해제 크기가 상한을 넘습니다: {total} > {MAX_UNCOMPRESSED_BYTES}",
        )
        names.append(name)
    _require(bool(names), "빈 번들입니다")
    return names


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as handle:
        data = handle.read(MAX_UNCOMPRESSED_BYTES + 1)
    _require(len(data) <= MAX_UNCOMPRESSED_BYTES, f"항목이 너무 큽니다: {name}")
    return data


# ── 서명 ────────────────────────────────────────────────────────────────────


def load_trusted_keys(trust_dir: Path) -> list[Ed25519PublicKey]:
    """`keys/plugin-trust/*.pub` 의 공개 키들. 없으면 빈 목록이다.

    **번들 안의 키로 번들 안의 서명을 검증하지 않는다** — 그건 서명이 아니다.
    """
    keys: list[Ed25519PublicKey] = []
    if not trust_dir.is_dir():
        return keys
    for path in sorted(trust_dir.glob("*.pub")):
        for candidate in _key_candidates(path.read_bytes()):
            try:
                keys.append(Ed25519PublicKey.from_public_bytes(candidate))
                break
            except Exception:
                continue                      # 깨진 키 하나가 나머지를 막지 않는다
    return keys


def _key_candidates(data: bytes) -> list[bytes]:
    """원본 바이트에서 키가 될 수 있는 후보들. **날바이트를 strip 하지 않는다.**

    처음엔 `data.strip()` 을 먼저 걸고 길이로 갈랐다. 그런데 Ed25519 공개 키는
    **어떤 32바이트도 될 수 있고**, 그중 5%는 공백 문자에 해당하는 바이트
    (0x20·0x09·0x0a·0x0d·0x0b·0x0c)로 시작하거나 끝난다. 그런 키는 31바이트로
    깎여 검증이 실패했다 — 실행할 때마다 다른 테스트가 실패하는 플레이크였다.

    **날바이트와 텍스트를 갈라서 다룬다.** 32바이트면 그대로 키이고, 그게 아닐
    때만 공백을 털어 hex 로 읽어 본다.
    """
    out: list[bytes] = []
    if len(data) == 32:
        out.append(data)
    text = data.strip()
    if len(text) == 64:
        try:
            out.append(bytes.fromhex(text.decode("ascii")))
        except (ValueError, UnicodeDecodeError):
            pass
    return out


def checksum_block(files: Mapping[str, bytes]) -> bytes:
    """`MANIFEST.sha256` 본문. 서명이 덮는 것은 이 바이트다."""
    lines = [
        f"{hashlib.sha256(data).hexdigest()}  {name}"
        for name, data in sorted(files.items())
        if name not in _RESERVED
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def verify_bundle(archive: zipfile.ZipFile, names: Sequence[str], trust_dir: Path) -> str:
    """`signed` · `unsigned` · `invalid` 중 하나를 돌려준다.

    서명은 파일마다 걸지 않고 `MANIFEST.sha256` **한 장에** 건다. 그 한 장이 나머지
    파일의 해시를 들고 있으므로, 서명 하나로 번들 전체가 고정된다. 어느 파일이든
    바뀌면 해시가 어긋나고, 목록에 없는 파일이 끼어들어도 잡힌다.
    """
    has_sums = CHECKSUMS_NAME in names
    has_sig = SIGNATURE_NAME in names
    if not has_sums and not has_sig:
        return "unsigned"
    if has_sig and not has_sums:
        return "invalid"

    block = _read(archive, CHECKSUMS_NAME)
    listed: dict[str, str] = {}
    for line in block.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        match = _CHECKSUM_LINE.match(line)
        if match is None:
            return "invalid"
        listed[match.group(2)] = match.group(1)

    payload = [name for name in names if name not in _RESERVED]
    if set(payload) != set(listed):
        return "invalid"                      # 목록에 없는 파일 · 목록에만 있는 파일
    for name in payload:
        if hashlib.sha256(_read(archive, name)).hexdigest() != listed[name]:
            return "invalid"

    if not has_sig:
        return "unsigned"                     # 해시는 맞지만 서명이 없다
    # 서명도 같은 함정이 있다 — 64바이트 날서명은 strip 하면 안 된다(`_key_candidates` 참고).
    raw_signature = _read(archive, SIGNATURE_NAME)
    candidates: list[bytes] = []
    if len(raw_signature) == 64:
        candidates.append(raw_signature)
    text = raw_signature.strip()
    if len(text) == 128:
        try:
            candidates.append(bytes.fromhex(text.decode("ascii")))
        except (ValueError, UnicodeDecodeError):
            pass
    keys = load_trusted_keys(trust_dir)
    for signature in candidates:
        for key in keys:
            try:
                key.verify(signature, block)
                return "signed"
            except InvalidSignature:
                continue
    return "invalid"


# ── 번들 만들기 (개발자·테스트용) ────────────────────────────────────────────


def build_bundle(
    files: Mapping[str, bytes], *, sign_key: Ed25519PrivateKey | None = None
) -> bytes:
    """`.lccp` 번들 바이트를 만든다. 사내 개발자가 CI 에서 부르는 함수다."""
    payload = {name: data for name, data in files.items() if name not in _RESERVED}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            archive.writestr(name, data)
        if sign_key is not None:
            block = checksum_block(payload)
            archive.writestr(CHECKSUMS_NAME, block)
            archive.writestr(SIGNATURE_NAME, sign_key.sign(block).hex())
    return buffer.getvalue()


# ── 설치 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Installed:
    plugin_id: str
    version: str
    service_id: str
    signature_state: str
    #: 새로 발급한 토큰. **이때가 마지막이다.** 재설치에서는 `None`(기존 토큰 유지).
    token: str | None
    upgraded: bool


def plugin_root(data_dir: Path) -> Path:
    """설치본이 사는 곳. `config/` 는 읽기 전용 마운트라 쓸 수 없다."""
    return Path(data_dir) / "plugins"


def install(
    store: SqliteStore,
    bundle: bytes,
    *,
    actor: str,
    data_dir: Path,
    trust_dir: Path,
    tenant_id: str,
    host_version: str,
    require_signature: bool = True,
    now: Callable[[], float] = time.time,
) -> Installed:
    """번들을 검증하고 **서비스를 만들고 토큰을 발급한다.**

    설치가 곧 서비스 등록인 것이 요점이다. 이 함수가 끝나는 순간 플러그인은 아직
    한 줄도 안 돌았지만, 사용량 화면에 서비스 축으로 이미 잡히고 레이트리밋에 걸리고
    예산에 계산된다 — 배선을 따로 하지 않는다.
    """
    _require(
        len(bundle) <= MAX_BUNDLE_BYTES,
        f"번들이 너무 큽니다: {len(bundle)} > {MAX_BUNDLE_BYTES}",
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise PluginError(f"zip 이 아닙니다: {exc}") from exc

    with archive:
        names = safe_names(archive)
        _require(MANIFEST_NAME in names, f"번들 루트에 {MANIFEST_NAME} 이 없습니다")
        manifest = parse_manifest(_read(archive, MANIFEST_NAME))

        if not host_satisfies(host_version, manifest.requires_host):
            raise PluginError(
                f"이 호스트({host_version})는 플러그인이 요구하는 범위"
                f"({manifest.requires_host})에 없습니다"
            )

        state = verify_bundle(archive, names, trust_dir)
        if state == "invalid":
            raise PluginError(
                "번들 검증에 실패했습니다 — 서명이나 파일 해시가 맞지 않습니다. "
                "받은 파일이 손상됐거나 손을 탄 것입니다"
            )
        if require_signature and state != "signed":
            raise PluginError(
                "서명되지 않은 번들입니다. 사내 키로 서명하고 그 공개 키를 "
                f"{trust_dir} 에 `<이름>.pub` 으로 두세요"
            )

        target = plugin_root(data_dir) / manifest.plugin_id / manifest.version
        staging = target.with_name(target.name + ".incoming")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            for name in names:
                destination = staging.joinpath(*_path_parts(name))
                # 정규화한 결과가 여전히 안쪽인지 **쓰기 직전에** 다시 본다.
                resolved = destination.resolve()
                _require(
                    resolved.is_relative_to(staging.resolve()),
                    f"대상 디렉터리 밖을 가리키는 항목: {name!r}",
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(_read(archive, name))
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    scope = TenantScope(tenant_id)
    existing = store.get_plugin(manifest.plugin_id)
    service_id = existing["service_id"] if existing else manifest.plugin_id
    token: str | None = None

    if store.get_service(scope, service_id) is None:
        store.create_service(
            scope, service_id, manifest.name, **manifest.service_fields()
        )
        _, token = issue_token(
            store, scope, service_id, role=ROLE_SERVICE,
            note=f"plugin {manifest.plugin_id}", actor=actor,
        )

    store.save_plugin({
        "id": manifest.plugin_id,
        "version": manifest.version,
        "name": manifest.name,
        "kind": manifest.kind,
        "tenant_id": tenant_id,
        "service_id": service_id,
        "endpoint": manifest.endpoint,
        "manifest_json": json.dumps(manifest.raw, ensure_ascii=False, sort_keys=True),
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "signature_state": state,
        "last_error": None,
        "installed_by": actor,
        "installed_at": now(),
    })
    # 설치는 켜는 것이 아니다. 모델 설치 요청이 승인과 나뉘어 있는 것과 같다.
    store.set_service_status(scope, service_id, "inactive")
    store.audit(
        actor, "install_plugin", target=manifest.plugin_id,
        detail={"version": manifest.version, "signature": state, "service": service_id},
    )
    return Installed(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        service_id=service_id,
        signature_state=state,
        token=token,
        upgraded=existing is not None,
    )


def set_active(store: SqliteStore, plugin_id: str, active: bool, *, actor: str) -> bool:
    """플러그인을 켜고 끈다 — **실체는 그 서비스의 status 다.**

    별도 플래그를 두지 않으므로 여기서 상태가 갈릴 여지가 없다.
    """
    row = store.get_plugin(plugin_id)
    if row is None:
        return False
    store.set_service_status(
        TenantScope(row["tenant_id"]), row["service_id"], "active" if active else "inactive"
    )
    store.audit(
        actor, "activate_plugin" if active else "deactivate_plugin", target=plugin_id,
    )
    return True


def uninstall(store: SqliteStore, plugin_id: str, *, actor: str, data_dir: Path) -> bool:
    """플러그인을 지운다. **서비스 행은 남긴다.**

    지우면 그 서비스로 집계된 사용량·감사가 이름을 잃는다. 대신 `inactive` 로
    내려 두면 과거는 읽히고 미래는 막힌다.
    """
    row = store.get_plugin(plugin_id)
    if row is None:
        return False
    store.set_service_status(TenantScope(row["tenant_id"]), row["service_id"], "inactive")
    store.delete_plugin(plugin_id)
    shutil.rmtree(plugin_root(data_dir) / plugin_id, ignore_errors=True)
    store.audit(actor, "uninstall_plugin", target=plugin_id)
    return True


def snapshot(store: SqliteStore, *, data_dir: Path) -> list[dict[str, Any]]:
    """관제 화면이 그대로 그리는 목록.

    **디스크 상태를 함께 본다.** 행은 있는데 파일이 없거나 해시가 다르면 그것이
    곧 진단이다 — `doctor` 가 짚을 자리이기도 하다.
    """
    out: list[dict[str, Any]] = []
    for row in store.list_plugins():
        scope = TenantScope(row["tenant_id"])
        service = store.get_service(scope, row["service_id"])
        path = plugin_root(data_dir) / row["id"] / row["version"]
        out.append({
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "kind": row["kind"],
            "endpoint": row["endpoint"],
            "service_id": row["service_id"],
            "signature": row["signature_state"],
            # 활성 여부의 유일한 출처는 서비스다.
            "active": bool(service is not None and service["status"] == "active"),
            "allow_roles": json.loads(service["allow_roles_json"]) if service else [],
            "files_present": path.is_dir(),
            "last_error": row["last_error"],
            "installed_at": row["installed_at"],
        })
    return out
