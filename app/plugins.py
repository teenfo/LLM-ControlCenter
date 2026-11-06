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
from typing import Any, Callable, Iterable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .auth import ROLE_SERVICE, issue_token, rotate_token, service_is_active
from .bootstrap import PLATFORM_TENANT
from .config import INTERNAL
from .schedule import ScheduleError, next_after, parse_cron
from .store import JobRow, SqliteStore, TenantScope

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

#: 지금 지원하는 트리거.
#:   schedule — 시각이 원인. 컨트롤 플레인이 예정을 갖고 클레임만 준다(`claim_tick`)
#:   event    — 잡 종결이 원인. 모델 경계를 지난 것을 뒤에서 본다(`pull_events`)
SUPPORTED_TRIGGERS = ("schedule", "event")

#: `event` 트리거가 구독할 수 있는 것. 지금은 잡 종결 하나다 — 프롬프트가 나가고
#: 결과가 돌아온 그 경계가 훅을 원한 자리다. `notify` 의 운영 이벤트(node_offline·
#: budget_warn…)는 여기 없다: 그것은 사람에게 가는 알림이고 채널이 따로 있다.
SUPPORTED_EVENTS = ("job.finished",)

#: 한 번의 풀이 돌려주는 최대 건수. 플러그인이 `limit` 을 더 크게 불러도 여기서 자른다.
MAX_EVENT_BATCH = 200

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
    #: 스케줄 트리거. `None` 이면 예정이 없다.
    schedule: str | None = None
    schedule_tz: str = "UTC"
    #: 이벤트 트리거. `None` 이면 구독이 없다. `event_roles` 가 비면 모든 역할이다.
    #: 스케줄도 이벤트도 없으면 이 플러그인은 스스로 안 깨어난다.
    event: str | None = None
    event_roles: tuple[str, ...] = ()
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


@dataclass(frozen=True)
class Trigger:
    """`[trigger]` 절을 읽은 결과. 전부 비어 있으면 이 플러그인은 스스로 안 깨어난다."""

    schedule: str | None = None
    schedule_tz: str = "UTC"
    event: str | None = None
    event_roles: tuple[str, ...] = ()


def _parse_trigger(trigger: Any) -> Trigger:
    """`[trigger]` 절. 없으면 트리거가 없는 것이고, 그것이 기본이다.

    **스케줄 표현식은 여기서 실제로 계산해 본다.** 형식만 보고 통과시키면 `0 0 30 2 *`
    (2월 30일)처럼 문법은 맞는데 영원히 안 도는 스케줄이 설치된다. 그 플러그인은
    켜져 있고 화면에도 보이는데 아무 일도 안 하고, 그 상태를 아무도 못 읽는다.

    이벤트도 같은 태도다 — 모르는 이벤트 이름은 설치 시점에 거부한다. 받아 두면
    "구독은 했는데 아무것도 안 오는" 플러그인이 되고, 그 원인을 화면에서 못 읽는다.
    """
    if trigger is None:
        return Trigger()
    _require(isinstance(trigger, dict), "[trigger] 절은 표가 아닙니다")

    kind = str(trigger.get("kind", ""))
    _require(
        kind in SUPPORTED_TRIGGERS,
        f"지원하지 않는 트리거입니다: {kind!r} — 지금 되는 것은 {', '.join(SUPPORTED_TRIGGERS)}",
    )

    if kind == "event":
        event = trigger.get("event")
        _require(
            isinstance(event, str) and event in SUPPORTED_EVENTS,
            f'[trigger] kind = "event" 의 event 는 {", ".join(SUPPORTED_EVENTS)} 중 하나여야 '
            f"합니다: {event!r}",
        )
        roles = trigger.get("roles", [])
        _require(
            isinstance(roles, list) and all(isinstance(r, str) and r for r in roles),
            "[trigger].roles 는 문자열 목록이어야 합니다 — 비우면 모든 역할입니다",
        )
        _require(
            all(not r.startswith("_") for r in roles),
            "밑줄로 시작하는 역할은 내부 전용이라 구독할 수 없습니다",
        )
        return Trigger(event=str(event), event_roles=tuple(dict.fromkeys(roles)))

    expression = trigger.get("schedule")
    _require(
        isinstance(expression, str) and expression.strip(),
        '[trigger] kind = "schedule" 에는 schedule = "분 시 일 월 요일" 이 있어야 합니다',
    )
    timezone = str(trigger.get("timezone", "UTC"))

    try:
        spec = parse_cron(str(expression))
        next_after(spec, time.time(), timezone=timezone)
    except ScheduleError as exc:
        raise PluginError(f"스케줄을 쓸 수 없습니다: {exc}") from exc
    return Trigger(schedule=spec.source, schedule_tz=timezone)


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

    trigger = _parse_trigger(data.get("trigger"))

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
        schedule=trigger.schedule,
        schedule_tz=trigger.schedule_tz,
        event=trigger.event,
        event_roles=trigger.event_roles,
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


@dataclass(frozen=True)
class Inspected:
    """검증을 통과한 번들. **설치는 이 결과만 믿고 푼다** — 사전 검사와 설치가 같은 것을 본다."""

    manifest: Manifest
    names: tuple[str, ...]
    signature_state: str
    bundle_sha256: str
    #: 검증을 통과한 바로 그 바이트. `install` 은 이것만 푼다.
    bundle: bytes

    @property
    def payload(self) -> tuple[str, ...]:
        """번들 안의 **내용 파일** — 체크섬·서명 같은 예약 파일은 뺀다."""
        return tuple(name for name in self.names if name not in _RESERVED)


def inspect_bundle(
    bundle: bytes,
    *,
    trust_dir: Path,
    host_version: str,
    require_signature: bool = True,
    known_roles: Iterable[str] | None = None,
) -> Inspected:
    """번들을 **설치하지 않고** 설치와 같은 순서로 검증한다.

    `install` 이 이 함수를 부른다 — 검증이 두 벌이면 사전 검사는 통과했는데 설치가
    거절하는(또는 그 반대) 날이 온다. 구조 검사가 `install` 이 직접 검증하지 않는 것을
    지킨다(`test_install_delegates_validation_to_inspect_bundle`). 사전 검사 라우트
    (`platform_plugin_inspect`)는 이 함수만 부르고 DB 도 디스크도 건드리지 않는다.
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

        if known_roles is not None and manifest.event_roles:
            unknown = sorted(set(manifest.event_roles) - set(known_roles))
            _require(
                not unknown,
                f"[trigger].roles 에 없는 역할이 있습니다: {', '.join(unknown)} — "
                "오타면 이 플러그인은 켜 놓아도 아무것도 못 받습니다",
            )

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

    return Inspected(
        manifest=manifest,
        names=tuple(names),
        signature_state=state,
        bundle_sha256=hashlib.sha256(bundle).hexdigest(),
        bundle=bundle,
    )


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
    known_roles: Iterable[str] | None = None,
) -> Installed:
    """번들을 검증하고 **서비스를 만들고 토큰을 발급한다.**

    설치가 곧 서비스 등록인 것이 요점이다. 이 함수가 끝나는 순간 플러그인은 아직
    한 줄도 안 돌았지만, 사용량 화면에 서비스 축으로 이미 잡히고 레이트리밋에 걸리고
    예산에 계산된다 — 배선을 따로 하지 않는다.

    `known_roles` 를 주면 이벤트 구독의 역할 필터를 그 목록에 대고 검사한다. 오타 난
    역할은 조용히 아무것도 안 받는 플러그인이 되므로 **설치 시점에** 거부한다.
    """
    inspected = inspect_bundle(
        bundle, trust_dir=trust_dir, host_version=host_version,
        require_signature=require_signature, known_roles=known_roles,
    )
    manifest, names, state = inspected.manifest, list(inspected.names), inspected.signature_state

    with zipfile.ZipFile(io.BytesIO(inspected.bundle)) as archive:
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
        "bundle_sha256": inspected.bundle_sha256,
        "signature_state": state,
        "last_error": None,
        "installed_by": actor,
        "installed_at": now(),
        "schedule": manifest.schedule,
        "schedule_tz": manifest.schedule_tz if manifest.schedule else None,
        "event": manifest.event,
        "event_roles": (
            json.dumps(list(manifest.event_roles), ensure_ascii=False) if manifest.event else None
        ),
    })
    # 설치는 켜는 것이 아니다. 모델 설치 요청이 승인과 나뉘어 있는 것과 같다.
    # 판올림도 같다 — 새 코드는 새 코드이므로 다시 꺼진다.
    store.set_service_status(scope, service_id, "inactive")
    # **꺼지면 예정도 없다.** 여기서 남겨 두면 판올림 뒤 꺼져 있는 플러그인의
    # 예정이 살아 있게 되고, `set_active` 가 끌 때 지우는 것과 규칙이 갈린다.
    # 사람이 다시 켜는 순간 그때부터 다시 잡힌다. 이벤트 커서도 같은 규칙이다.
    store.set_plugin_next_run(manifest.plugin_id, None)
    store.set_plugin_event_cursor(manifest.plugin_id, None)
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


def _next_run(expression: str, timezone: str | None, after: float) -> float | None:
    """다음 예정. **못 계산하면 예정을 없앤다** — 틀린 예정보다 없는 예정이 낫다.

    설치 때 한 번 계산해 봤으므로 여기서 실패하는 것은 그 사이에 시간대 데이터가
    바뀐 것 같은 경우다. 예외를 올려 보내면 켜는 것 자체가 실패하는데, 그러면
    스케줄 하나 때문에 플러그인 전체를 못 쓴다.
    """
    try:
        return next_after(parse_cron(expression), after, timezone=timezone or "UTC")
    except ScheduleError:
        return None


def set_active(
    store: SqliteStore, plugin_id: str, active: bool, *, actor: str,
    now: Callable[[], float] = time.time,
) -> bool:
    """플러그인을 켜고 끈다 — **실체는 그 서비스의 status 다.**

    별도 플래그를 두지 않으므로 여기서 상태가 갈릴 여지가 없다.
    """
    row = store.get_plugin(plugin_id)
    if row is None:
        return False
    store.set_service_status(
        TenantScope(row["tenant_id"]), row["service_id"], "active" if active else "inactive"
    )
    # **예정은 켤 때 지금부터 다시 잡는다.** 끄면 지운다.
    #
    # 껐다 켜는 사이에 지난 예정을 그대로 두면, 켜자마자 한 번 돈다 — 사흘 꺼 뒀다
    # 켠 사람이 원한 것은 사흘치도 즉시 실행도 아니고 "다음 예정부터" 다.
    if row["schedule"]:
        store.set_plugin_next_run(
            plugin_id,
            _next_run(row["schedule"], row["schedule_tz"], now()) if active else None,
        )
    if row["event"]:
        # **켜는 순간부터다.** 꺼져 있던 동안의 종결은 안 준다 — 스케줄이 밀린 것을
        # 몰아 돌리지 않는 것과 같은 이유다: 사흘 꺼 뒀다 켠 사람이 원한 것은 사흘치
        # 종결 이벤트가 아니다. 끄면 커서를 지운다 — 꺼진 플러그인의 「밀림」은 뜻이 없다.
        store.set_plugin_event_cursor(
            plugin_id, store.latest_plugin_event_id() if active else None,
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


@dataclass(frozen=True)
class RotatedToken:
    """플러그인 토큰 회전의 결과. `token` 원값은 **이때가 마지막이다.**"""

    plugin_id: str
    service_id: str
    token_id: str
    token: str
    grace_seconds: float
    #: 회전이 아니라 발급이었다 — 살아 있는 토큰이 하나도 없었다.
    reissued: bool
    old_token_id: str | None
    #: 유예를 주었을 때 옛 토큰이 죽는 시각. 즉시 폐기(유예 0)나 발급이면 `None`.
    old_token_expires_at: float | None


def rotate_plugin_token(
    store: SqliteStore,
    plugin_id: str,
    *,
    actor: str,
    actor_role: str,
    grace_seconds: float = 0.0,
    now: Callable[[], float] = time.time,
) -> RotatedToken | None:
    """플러그인의 토큰을 회전한다 — **살아 있는 토큰이 없으면 발급한다.**

    재설치는 토큰을 다시 주지 않고(`install`), 제거는 서비스 행을 남긴다(`uninstall`).
    그래서 토큰을 잃거나 폐기한 플러그인이 새 토큰을 받는 길은 여기뿐이다. 회전은
    발급이므로 `auth.rotate_token` 을 그대로 지난다 — 규칙을 두 곳에 적지 않는다.

    회전 대상은 **가장 최근에 산 토큰** 하나다. 유예 회전은 옛 토큰을 폐기하지 않고
    만료만 걸어 두므로, 살아 있는 토큰을 고를 때 `expires_at` 도 본다 — 안 그러면
    유예 중인 옛 토큰을 또 회전시켜 새 토큰을 두 장 만든다.
    `None` 은 플러그인이 없다는 뜻이다.
    """
    row = store.get_plugin(plugin_id)
    if row is None:
        return None
    scope = TenantScope(row["tenant_id"])
    moment = now()
    live = sorted(
        (
            t for t in store.list_tokens(scope)
            if t["service_id"] == row["service_id"] and t["role"] == ROLE_SERVICE
            and t["revoked_at"] is None
            and (t["expires_at"] is None or t["expires_at"] > moment)
        ),
        key=lambda t: t["created_at"], reverse=True,
    )
    if live:
        old = live[0]
        token_id, raw = rotate_token(
            store, scope, old["id"], actor=actor, actor_role=actor_role,
            grace_seconds=grace_seconds, now=now,
        )
        old_id: str | None = old["id"]
        old_expires = moment + grace_seconds if grace_seconds > 0 else None
        reissued = False
    else:
        token_id, raw = issue_token(
            store, scope, row["service_id"], role=ROLE_SERVICE,
            note=f"plugin {plugin_id}", actor=actor,
        )
        old_id = None
        old_expires = None
        reissued = True
    store.audit(
        actor, "rotate_plugin_token", tenant_id=scope.tenant_id, target=plugin_id,
        detail={
            "service": row["service_id"], "new_token_id": token_id,
            "grace_seconds": grace_seconds, "reissued": reissued,
        },
    )
    return RotatedToken(
        plugin_id=plugin_id, service_id=row["service_id"], token_id=token_id, token=raw,
        grace_seconds=grace_seconds, reissued=reissued,
        old_token_id=old_id, old_token_expires_at=old_expires,
    )


# ── 스케줄 클레임 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tick:
    """"지금 내 차례인가" 에 대한 답."""

    due: bool
    #: 가져갔을 때, 그 실행이 **예정돼 있던** 시각. 지금이 아니다 — 플러그인이
    #: 얼마나 늦게 왔는지 알아야 "어제 것" 인지 "오늘 것" 인지 판단할 수 있다.
    scheduled_for: float | None = None
    #: 다음 예정. `due` 든 아니든 알려 준다 — 플러그인이 폴링 간격을 정할 근거다.
    next_run_at: float | None = None


def claim_tick(
    store: SqliteStore, plugin_id: str, *, now: Callable[[], float] = time.time
) -> Tick:
    """플러그인이 "내 차례인가" 를 물으면 여기서 답한다.

    ### 왜 컨트롤 플레인이 플러그인을 부르지 않는가 (풀, 푸시 아님)

    `external` 은 **컨트롤 플레인이 프로세스를 안 띄운다**는 뜻이었다. 예정 시각에
    컨트롤 플레인이 플러그인의 `endpoint` 로 연결을 걸면 그 전제가 깨진다 — 그
    주소는 zip 파일이 들고 온 것이고, 그 주소로 서버가 연결을 거는 것은
    **플러그인이 스스로에게 준 권한**이다. `allow_roles` 나 예산처럼 플러그인이
    받아들이는 제약과는 방향이 반대다. (`notify` 의 웹훅 주소는 운영자가 `config/`
    에 직접 적은 것이라 출처가 다르다.)

    그래서 나가는 연결이 하나도 없다. 플러그인은 LLM 을 쓸 때도, "내 차례인가" 를
    물을 때도 같은 앞문으로 온다.

    ### 그러면 플러그인이 자기 cron 을 쓰는 것과 뭐가 다른가

    - **복제본이 셋이어도 한 번 돈다** — 클레임이 CAS 라서(`claim_plugin_tick`)
    - **끄면 선다** — 이 경로도 `auth.active_service` 를 지난다. 자기 cron 은
      관제 화면에서 꺼도 계속 때린다
    - **스케줄이 번들에 실려 서명된다** — 별도로 배선하는 것이 아니다
    - **안 돌고 있는 것이 보인다** — `last_run_at` 이 비어 있는 것으로

    ### 밀린 것을 몰아 돌리지 않는다

    다음 예정은 **지금 기준**으로 다시 잡는다. 사흘 꺼져 있었다면 사흘치가 아니라
    한 번 돌고 끝이다. `scheduled_for` 로 얼마나 늦었는지는 알려 주므로, 밀린 것을
    따라잡을지는 플러그인이 정한다 — 컨트롤 플레인이 정할 일이 아니다.
    """
    row = store.get_plugin(plugin_id)
    if row is None or not row["schedule"]:
        return Tick(due=False)

    moment = now()
    scheduled_for = row["next_run_at"]
    if scheduled_for is None or scheduled_for > moment:
        return Tick(due=False, next_run_at=scheduled_for)

    following = _next_run(row["schedule"], row["schedule_tz"], moment)
    if following is None:
        # 다음을 못 잡으면 이번도 안 준다. 예정을 비운 채로 주면 그 뒤로 영영
        # 안 돌면서 "한 번은 돌았다" 는 기록만 남는다.
        store.set_plugin_error(plugin_id, f"스케줄을 계산할 수 없습니다: {row['schedule']}")
        return Tick(due=False)

    if not store.claim_plugin_tick(plugin_id, now=moment, next_run_at=following):
        # 다른 복제본이 먼저 가져갔다. 진 쪽은 그냥 아니라고 듣는다.
        return Tick(due=False, next_run_at=store.get_plugin(plugin_id)["next_run_at"])
    return Tick(due=True, scheduled_for=scheduled_for, next_run_at=following)


# ── 재귀 방지 ────────────────────────────────────────────────────────────────


def may_wake_plugins(job: JobRow) -> bool:
    """이 잡의 완료가 플러그인을 깨워도 되는가. **플러그인이 만든 잡은 안 된다.**

    막으려는 고리는 이것이다:

        플러그인이 깨어난다 → `/v1/generate` 를 부른다 → 그 잡이 끝난다
        → 그 완료가 다시 플러그인을 깨운다 → …

    한 바퀴에 한 번씩 돈을 쓰면서 아무도 멈추라고 말하지 않는다. 레이트리밋과
    예산이 결국 세우기는 하지만, **예산이 다 탈 때까지 안 멈춘다** 는 것이 문제다.

    ### 왜 "그 플러그인" 이 아니라 "어떤 플러그인도" 인가

    자기 자신만 막으면 둘이 서로를 깨우는 고리(A→B→A)가 그대로 남는다. 두 플러그인이
    각자 정직해도 짝지어 놓으면 도는 고리라, 플러그인 하나를 심사해서는 볼 수 없다.
    그래서 판정에 플러그인 id 를 안 쓴다 — **플러그인이 만든 잡은 아무것도 안 깨운다.**

    그 결과 성질 하나가 선다: **플러그인이 하는 일은 사람이 시킨 일에서 딱 한 걸음이다.**
    사람이 요청 → 그 완료가 플러그인을 깨움 → 플러그인이 잡을 만듦 → 거기서 끝.

    ### 왜 깊이 카운터가 아닌가

    깊이를 세려면 "이 호출은 저 잡 때문이다" 를 서버가 알아야 하는데, 플러그인은
    앞문으로 **새 HTTP 요청**을 보내므로 서버는 그 인과를 볼 수 없다. 플러그인이
    상관 토큰을 되돌려 주게 하면 셀 수는 있지만, 그러면 재귀를 안 도는 것이
    **플러그인의 성의**에 달린다. 그건 방지가 아니다.

    ### 부르는 곳

    `pull_events` 하나다 — 이벤트 트리거가 종결을 플러그인에게 내주기 직전에 묻는다.
    표식(`jobs.origin_plugin`)은 **잡이 만들어지는 순간에만** 붙일 수 있어서 트리거보다
    먼저 들어왔고, `test_architecture` 가 `origin_plugin` 을 읽는 곳을 이 함수 하나로
    묶어 두므로 다음 트리거를 짜는 사람도 여기를 지날 수밖에 없다.
    """
    # `is None` 이다. 빈 문자열이 들어오는 경우를 "사람" 으로 읽으면 깨우지 말아야 할
    # 잡을 깨운다 — 애매하면 **안 깨우는 쪽**이 안전한 쪽이다.
    return job.origin_plugin is None


# ── 이벤트 풀 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pull:
    """한 번의 풀에 대한 답."""

    events: list[dict[str, Any]]
    #: 이번에 **훑은** 마지막 이벤트. 플러그인은 처리를 마치면 이 값을 그대로 ack 한다.
    #: 빈 배치라도 전진할 수 있다 — 플러그인이 만든 잡의 종결은 훑되 내주지 않는다.
    cursor: int
    #: 그 뒤에 남은 건수. 0 이 아니면 바로 다시 물어도 된다.
    pending: int


def visible_tenant(plugin_row: Mapping[str, Any]) -> str | None:
    """이 플러그인이 보는 테넌트.

    플러그인은 지금 플랫폼 테넌트에 산다(`docs/plugin-exploration.md` §3-4). 그 플러그인이
    자기 테넌트의 잡만 본다면 볼 것이 없다 — 소비자의 잡은 다른 테넌트에 있다. 그래서
    플랫폼 테넌트의 플러그인은 **전 테넌트를** 보고(`None`), 보통 테넌트에 설치된
    플러그인은 자기 테넌트만 본다. 설치처가 `plugin_tenant` 를 어느 테넌트로 두느냐가
    곧 이 범위다 — 별도 스위치를 두지 않는다.
    """
    return None if plugin_row["tenant_id"] == PLATFORM_TENANT else plugin_row["tenant_id"]


def subscribed_roles(plugin_row: Mapping[str, Any]) -> tuple[str, ...]:
    raw = plugin_row["event_roles"]
    return tuple(json.loads(raw)) if raw else ()


def event_payload(
    event_id: int, ts: float, status: str, job: JobRow, boundaries: Mapping[str, str],
) -> dict[str, Any]:
    """플러그인이 받는 한 건. **모델이 본 것을 준다 — 소비자가 보낸 원문이 아니다.**

    가드가 마스킹한 뒤의 프롬프트가 노드로 갔고, 경계 밖 노드였으면 더 가린 변형이
    갔다. 훅은 그 경계에 서 있으므로 그것을 본다. 원문 암호문은 여기 없다 — 원문은
    테넌트 관리자의 감사 남는 열람 경로(`tenant_job_raw`) 하나로만 열린다.

    응답도 같다 — 출력 가드를 지나 소비자에게 나간 것(`jobs.response`)이다.
    노드에 안 간 잡(배치 실패·큐에서 취소)은 프롬프트가 `None` 이다: 모델이 본
    것이 없다. 노드를 모르면(지워진 노드) 밖으로 친다 — 더 가린 쪽이 안전한 쪽이다.
    """
    boundary = boundaries.get(job.node) if job.node else None
    outbound = boundary != INTERNAL
    prompt = system = None
    if job.node is not None:
        prompt = job.prompt_external if outbound and job.prompt_external else job.prompt_masked
        system = job.system_external if outbound and job.system_external else job.system_masked
    return {
        "id": event_id,
        "kind": SUPPORTED_EVENTS[0],
        "ts": ts,
        "job_id": job.id,
        "tenant": job.tenant_id,
        "service": job.service_id,
        "end_user": job.end_user_hash,
        "job_kind": job.kind,
        "role": job.role,
        "route": job.route,
        "status": status,
        "error": job.error,
        "error_code": job.error_code,
        "model": job.model,
        "node": job.node,
        "boundary": boundary,
        "prompt": prompt,
        "system": system,
        "output": job.response if status == "ok" else None,
        "usage": {
            "input_tokens": job.input_tokens,
            "output_tokens": job.output_tokens,
            "cost_usd": job.cost_usd,
        },
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def pull_events(
    store: SqliteStore,
    plugin_id: str,
    *,
    ack: int | None,
    limit: int,
    now: float,
    boundaries: Mapping[str, str],
) -> Pull | None:
    """플러그인이 "내가 못 본 종결이 있나" 를 물으면 여기서 답한다.

    ### 관찰이지 개입이 아니다

    훅은 프롬프트가 나가고 결과가 돌아온 **뒤에** 본다. 나가기 전에 고치거나 막는
    자리가 아니다. 그 자리를 열면 요청 경로에 외부 프로세스가 끼고, 그 프로세스가
    죽었을 때 "막을 것인가 통과시킬 것인가" 를 정해야 한다 — 가드의 순서 계약
    ①~⑦ 은 그 결정을 이 제품 안에서 끝내려고 있는 것이다. 개입이 필요하면 그때
    `on_hook_error` 같은 정책과 함께 별도로 결정한다.

    ### 풀이다 — 스케줄과 같은 이유

    컨트롤 플레인이 플러그인을 부르러 나가지 않는다(`claim_tick` 의 근거 그대로).
    종결은 DB 트리거가 아웃박스(`plugin_events`)에 쌓고, 플러그인이 자기 토큰으로
    커서 다음 것을 가져간다. 그래서:

    - **at-least-once 다.** ack 하기 전에 죽으면 같은 배치를 다시 받는다. 이벤트 `id` 로
      중복을 거르는 것은 플러그인 몫이다
    - **복제본이 여럿이어도 커서는 앞으로만 간다** — ack 가 CAS 다(`ack_plugin_events`)
    - **끄면 선다** — 이 경로도 `auth.active_service` 를 지난다(라우트에서)
    - **켜는 순간부터다** — 커서가 켤 때 그 시점으로 잡힌다(`set_active`)

    ### 재귀 방지가 여기서 작동한다

    플러그인이 만든 잡의 종결은 **어떤 플러그인에게도** 안 준다(`may_wake_plugins`).
    A 가 받은 이벤트로 잡을 만들면 그 잡의 종결은 A 에게도 B 에게도 안 온다 — 그래서
    플러그인이 하는 일은 사람이 시킨 일에서 딱 한 걸음이다. 훑기는 하므로 커서는
    지나가고, 그래서 빈 배치라도 `cursor` 는 전진할 수 있다.

    ### 감사를 풀마다 남기지 않는다

    초 단위 폴링이 감사 사슬을 잡음으로 채운다. 동의의 기록은 `activate_plugin`
    감사(사람이 켰다)이고, 어디까지 읽었는지는 `plugins.event_cursor`·`last_event_at`
    이 화면에 보여 준다. 원문 열람과 달리 여기서 나가는 것은 마스킹본이다.

    `None` 은 "이 플러그인은 이벤트를 구독하지 않는다" 다 — 라우트가 409 로 바꾼다.
    """
    row = store.get_plugin(plugin_id)
    if row is None or not row["event"]:
        return None

    if ack is not None:
        store.ack_plugin_events(plugin_id, int(ack), now=now)
        row = store.get_plugin(plugin_id)

    cursor = row["event_cursor"]
    if cursor is None:
        # 켜져 있는데 커서가 없다 — 이 판 이전에 켜 둔 행이거나 손으로 만진 행이다.
        # 지금부터로 잡는다. 0 으로 잡으면 보존 기간 전체(한 달치)가 한꺼번에 나간다.
        cursor = store.latest_plugin_event_id()
        store.set_plugin_event_cursor(plugin_id, cursor)

    tenant = visible_tenant(row)
    roles = subscribed_roles(row) or None
    batch = store.plugin_events_after(
        # `limit: 0` 은 "ack 만" 이다 — 처리를 끝낸 배치를 확정하려고 한 건을 더 받을 필요가 없다.
        cursor, tenant_id=tenant, roles=roles, limit=max(0, min(int(limit), MAX_EVENT_BATCH)),
    )

    events: list[dict[str, Any]] = []
    last = int(cursor)
    for event_id, ts, status, job in batch:
        last = event_id
        if not may_wake_plugins(job):
            continue
        events.append(event_payload(event_id, ts, status, job, boundaries))

    pending = store.count_plugin_events_after(last, tenant_id=tenant, roles=roles)
    return Pull(events=events, cursor=last, pending=pending)


def snapshot(store: SqliteStore, *, data_dir: Path) -> list[dict[str, Any]]:
    """관제 화면이 그대로 그리는 목록.

    **디스크 상태를 함께 본다.** 행은 있는데 파일이 없거나 해시가 다르면 그것이
    곧 진단이다 — `doctor` 가 짚을 자리이기도 하다.
    """
    out: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for row in store.list_plugins():
        scope = TenantScope(row["tenant_id"])
        service = store.get_service(scope, row["service_id"])
        # 테넌트별로 한 번만 센다 — 플러그인마다 세면 목록 하나에 질의가 N 개 붙는다.
        if row["tenant_id"] not in counts:
            counts[row["tenant_id"]] = store.count_jobs_by_origin(scope)
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
            "active": service_is_active(service),
            "allow_roles": json.loads(service["allow_roles_json"]) if service else [],
            "files_present": path.is_dir(),
            # 이 플러그인이 만든 잡 수. **출처 칸을 사람이 볼 수 있게 하는 창이다** —
            # 쓰기만 하고 아무도 안 읽는 칸은 언젠가 틀린 채로 방치된다.
            "jobs_created": counts[row["tenant_id"]].get(row["id"], 0),
            # 스케줄. **안 돌고 있는 것이 보여야 한다** — 켜 두고 `last_run_at` 이
            # 비어 있으면 그것이 진단이다.
            "schedule": row["schedule"],
            "schedule_tz": row["schedule_tz"],
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            # 이벤트 구독. **켜 놓았는데 「밀림」이 늘기만 하면 플러그인이 안 돌고
            # 있는 것이다** — 스케줄의 `last_run_at` 이 비어 있는 것과 같은 진단이다.
            "event": row["event"],
            "event_roles": list(subscribed_roles(row)),
            "event_cursor": row["event_cursor"],
            "last_event_at": row["last_event_at"],
            "events_pending": (
                store.count_plugin_events_after(
                    row["event_cursor"], tenant_id=visible_tenant(row),
                    roles=subscribed_roles(row) or None,
                )
                if row["event"] and row["event_cursor"] is not None else None
            ),
            "last_error": row["last_error"],
            "installed_at": row["installed_at"],
        })
    return out
