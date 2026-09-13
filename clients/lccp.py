"""LLM ControlCenter 플러그인 패키징 CLI — `lccp` (단일 파일).

플러그인을 **만드는 쪽**의 도구다. 호스트(컨트롤 플레인)가 설치 때 대는 규칙과 **같은 코드**로
매니페스트를 판정하고, 호스트가 푸는 것과 같은 모양의 `.lccp` 번들을 만들고, Ed25519 로 서명한다.

    python lccp.py keygen acme                          # acme.key(비밀, 0600) · acme.pub(공개, hex)
    python lccp.py init my-plugin --id acme.my-plugin --trigger event
    python lccp.py check my-plugin                      # 호스트 규칙으로 판정 — 오프라인
    python lccp.py build my-plugin --sign acme.key      # acme.my-plugin-0.1.0.lccp (재현 가능)
    python lccp.py verify acme.my-plugin-0.1.0.lccp --trust ./trust
    python lccp.py inspect acme.my-plugin-0.1.0.lccp --json

### 약속

**`check` 가 통과하면 호스트의 사전 검사(`POST /v1/platform/plugins/inspect`)도 매니페스트 규칙에서는
통과한다.** 규칙 코드(`parse_manifest`·`_parse_trigger`·`host_satisfies`·`safe_names`·cron 계산)를
호스트 저장소의 `app/plugins.py`·`app/schedule.py` 에서 **그대로 옮겨 왔고**, 호스트의 테스트가 이름마다
AST 동일성으로 묶는다(`tests/test_plugin_kit.py`). 여기서 못 보는 것은 하나뿐이다 — **역할이 그 호스트에
실재하는가**(`allow_roles`·`[trigger].roles`). 그것은 호스트의 사전 검사가 본다.

### 의존성

표준 라이브러리 + `cryptography`(서명·검증에만, 함수 안에서 import). `init`·`check`·`build`(무서명)·
`inspect` 는 `cryptography` 없이 돈다. Python 3.11 이상(`tomllib`) — 호스트 이미지의 python 으로
실행해도 된다: `docker run --rm -v "$PWD:/w" -w /w --entrypoint python llm-controlcenter:<판> /app/clients/lccp.py …`

종료 코드: 0 통과 · 1 거부(규칙·서명·해시) · 2 사용법·환경(파일 없음 · cryptography 없음 · 키 덮어쓰기 거부).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 이 도구가 따라가는 호스트 판. `check --host-version` 의 기본값이고 `init` 의 `requires_host` 근거다.
#: 호스트의 버전 문자열 일치 테스트(`test_every_version_string_agrees`)가 같이 본다.
HOST_VERSION = "0.6.0"

# ═══ 호스트와 같은 규칙 ═══════════════════════════════════════════════════════
# 아래 블록은 호스트 저장소의 app/schedule.py 와 app/plugins.py 에서 **그대로** 옮긴 것이다.
# 호스트의 tests/test_plugin_kit.py 가 이름마다 AST 동일성으로 묶는다 — 여기를 고치려면 호스트를 고친다.

# ── app/schedule.py ──

#: (최솟값, 최댓값, 이름) — 다섯 칸의 순서 그대로.
_FIELDS = (
    (0, 59, "분"),
    (0, 23, "시"),
    (1, 31, "일"),
    (1, 12, "월"),
    (0, 7, "요일"),  # 7 도 일요일로 받는다(관행). 저장은 0 으로 정규화한다.
)
_TERM = re.compile(r"^(?:\*|(\d+)(?:-(\d+))?)(?:/(\d+))?$")
#: 다음 시각을 찾을 때 앞으로 볼 최대 일수. 4년 + 여유 — `29 2`(2월 29일)가
#: 윤년에만 있어서 4년을 봐야 한다. 여기서 못 찾으면 그 표현식은 **영원히 안 도는
#: 표현식**이고(`0 0 30 2 *` 같은), 설치 시점에 그것을 거부하는 근거가 된다.
MAX_LOOKAHEAD_DAYS = 366 * 4 + 2


class ScheduleError(ValueError):
    """표현식을 사람이 읽고 고칠 수 있는 문장으로 거부한다."""


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: 일·요일 둘 다 제한됐는가. cron 의 오랜 함정이라 **판정에 필요해서** 들고 있다.
    both_day_fields_restricted: bool
    source: str

    def matches_day(self, moment: datetime) -> bool:
        """이 날짜에 도는가.

        **일과 요일이 둘 다 제한되면 OR 다.** cron 의 표준 동작이고, 이것을 AND 로
        읽으면 `0 0 1 * 1`("매월 1일과 매주 월요일")이 "1일이면서 월요일" 이 되어
        몇 년에 한 번 돈다. 한쪽만 제한되면 그쪽만 본다.
        """
        if moment.month not in self.months:
            return False
        # `weekday()` 는 월=0, cron 은 일=0 이다. 이 한 줄이 어긋나면 하루씩 밀린다.
        weekday = (moment.weekday() + 1) % 7
        day_hit = moment.day in self.days
        weekday_hit = weekday in self.weekdays
        if self.both_day_fields_restricted:
            return day_hit or weekday_hit
        return day_hit and weekday_hit


def _parse_field(raw: str, low: int, high: int, label: str) -> tuple[frozenset[int], bool]:
    """한 칸을 값 집합으로. 두 번째 값은 "제한됐는가"(`*` 가 아닌가)."""
    values: set[int] = set()
    restricted = False
    for term in raw.split(","):
        term = term.strip()
        match = _TERM.match(term)
        if not match or not term:
            raise ScheduleError(f"{label} 칸을 읽을 수 없습니다: '{term}'")
        start_raw, end_raw, step_raw = match.groups()
        step = int(step_raw) if step_raw else 1
        if step < 1:
            raise ScheduleError(f"{label} 칸의 간격은 1 이상이어야 합니다: '{term}'")

        if start_raw is None:  # `*` 또는 `*/n`
            start, end = low, high
        else:
            restricted = True
            start = int(start_raw)
            end = int(end_raw) if end_raw is not None else start
        if not (low <= start <= high) or not (low <= end <= high):
            raise ScheduleError(f"{label} 칸은 {low}~{high} 범위입니다: '{term}'")
        if start > end:
            raise ScheduleError(f"{label} 칸의 범위가 거꾸로입니다: '{term}'")
        values.update(range(start, end + 1, step))

    if not values:
        raise ScheduleError(f"{label} 칸이 비었습니다")
    return frozenset(values), restricted


def parse_cron(expression: str) -> CronSpec:
    """다섯 칸 cron 을 판정 가능한 형태로. 못 읽으면 거부한다 — 조용히 안 돌면 안 된다."""
    fields = expression.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"cron 은 다섯 칸입니다(분 시 일 월 요일). 받은 것: {len(fields)}칸 — '{expression}'"
        )

    parsed = [
        _parse_field(raw, low, high, label)
        for raw, (low, high, label) in zip(fields, _FIELDS)
    ]
    (minutes, _), (hours, _), (days, day_restricted), (months, _), (weekdays, dow_restricted) = parsed
    # 7 은 일요일이다. 정규화해 두지 않으면 `matches_day` 가 0 만 보고 놓친다.
    weekdays = frozenset(0 if value == 7 else value for value in weekdays)

    return CronSpec(
        minutes=minutes, hours=hours, days=days, months=months, weekdays=weekdays,
        both_day_fields_restricted=day_restricted and dow_restricted,
        source=" ".join(fields),
    )


def resolve_timezone(name: str) -> ZoneInfo:
    """IANA 시간대. 없는 이름을 조용히 UTC 로 떨어뜨리지 않는다.

    떨어뜨리면 "왜 9시간 일찍 도느냐" 가 되고, 그때 원인을 아무도 못 찾는다.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ScheduleError(f"알 수 없는 시간대입니다: '{name}'") from exc


def next_after(spec: CronSpec, after: float, *, timezone: str = "UTC") -> float:
    """`after`(epoch 초) **다음** 시각을 epoch 초로.

    하루 단위로 훑고, 맞는 날 안에서 시·분을 훑는다. 분 단위로 훑으면 4년치가
    210만 번이라 못 쓴다 — 이렇게 하면 최악이 1500번 남짓이다.

    반환값은 **반드시 `after` 보다 크다.** 서머타임으로 같은 벽시계 시각이 두 번
    오는 날(가을) 두 번째는 건너뛰고 다음 날로 간다 — 두 번 도는 것보다 한 번
    거르는 쪽이 낫다. 없는 시각이 되는 날(봄)은 파이썬이 전이 직후로 밀어 주고,
    그 결과가 `after` 보다 크면 그대로 쓴다.
    """
    zone = resolve_timezone(timezone)
    start = datetime.fromtimestamp(after, tz=zone)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)

    for offset in range(MAX_LOOKAHEAD_DAYS):
        day = (start + timedelta(days=offset)).date()
        probe = datetime(day.year, day.month, day.day, tzinfo=zone)
        if not spec.matches_day(probe):
            continue
        for hour in hours:
            for minute in minutes:
                moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                stamp = moment.timestamp()
                if stamp > after:
                    return stamp

    raise ScheduleError(
        f"'{spec.source}' 은(는) 앞으로 {MAX_LOOKAHEAD_DAYS // 366}년 안에 한 번도 돌지 "
        "않습니다 — 2월 30일처럼 없는 날짜인지 확인하세요"
    )


# ── app/plugins.py ──

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
#: 역DNS. 점이 하나는 있어야 한다 — 일반 서비스 id(`acme-web`)와 섞이지 않게 한다.
_ID = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9][a-z0-9_-]*)+$")
_VERSION = re.compile(r"^\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.-]+)?$")
_CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


class PluginError(Exception):
    """설치를 거부한 이유. **사람이 읽고 고칠 수 있는 문장이어야 한다.**"""


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


# ── 여기부터 도구 고유 코드 ─────────────────────────────────────────────────


class CryptoUnavailable(RuntimeError):
    """`cryptography` 가 없다. 서명·검증·키 생성에만 필요하다."""


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover — 설치 환경에 달렸다
        raise CryptoUnavailable(
            "cryptography 패키지가 없다 — `pip install cryptography` (서명·검증·keygen 에만 필요하다)"
        ) from exc
    return InvalidSignature, Ed25519PrivateKey, Ed25519PublicKey


def _ed25519_sign(private_key: bytes, block: bytes) -> bytes:
    _, private_cls, _ = _crypto()
    return private_cls.from_private_bytes(private_key).sign(block)


def _ed25519_verify(public_key: bytes, signature: bytes, block: bytes) -> bool:
    invalid, _, public_cls = _crypto()
    try:
        public_cls.from_public_bytes(public_key).verify(signature, block)
        return True
    except (invalid, ValueError):
        return False


def read_public_key(path: Path) -> bytes:
    """`.pub` — 호스트 신뢰 디렉터리와 같은 규칙(날 32바이트 또는 hex 64자)."""
    for candidate in _key_candidates(Path(path).read_bytes()):
        if len(candidate) == 32:
            return candidate
    raise PluginError(f"공개 키로 읽을 수 없습니다: {path} (날 32바이트 또는 hex 64자여야 합니다)")


def read_private_key(path: Path) -> bytes:
    """`.key` — `keygen` 이 쓴 hex 64자(날 32바이트도 받는다)."""
    for candidate in _key_candidates(Path(path).read_bytes()):
        if len(candidate) == 32:
            return candidate
    raise PluginError(f"비밀 키로 읽을 수 없습니다: {path} (keygen 이 만든 .key 파일이어야 합니다)")


def load_trusted_keys(trust_dir: Path | None) -> list[bytes]:
    """`<dir>/*.pub` 의 공개 키들 — 호스트의 `keys/plugin-trust/` 와 같은 읽기 규칙."""
    keys: list[bytes] = []
    if trust_dir is None or not Path(trust_dir).is_dir():
        return keys
    for path in sorted(Path(trust_dir).glob("*.pub")):
        for candidate in _key_candidates(path.read_bytes()):
            if len(candidate) == 32:
                keys.append(candidate)
                break
    return keys


def verify_bundle(archive: zipfile.ZipFile, names: Sequence[str], keys: Sequence[bytes] | None) -> str:
    """`signed` · `unsigned` · `invalid` · `unverified` 중 하나.

    앞의 셋은 호스트의 `verify_bundle` 과 같은 판정이다(같은 순서로 같은 것을 본다). `unverified` 는 이 도구에만
    있다 — 서명은 있는데 대 볼 신뢰 키가 없거나(`keys is None`) `cryptography` 가 없을 때다. 호스트는 그 경우를
    `invalid` 로 읽는다(신뢰 키가 없으면 어떤 서명도 못 믿는다). 그 차이를 이름으로 드러낸다.
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
        return "invalid"
    for name in payload:
        if hashlib.sha256(_read(archive, name)).hexdigest() != listed[name]:
            return "invalid"

    if not has_sig:
        return "unsigned"
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
    if keys is None:
        return "unverified"
    try:
        for signature in candidates:
            for key in keys:
                if _ed25519_verify(key, signature, block):
                    return "signed"
    except CryptoUnavailable:
        return "unverified"
    return "invalid"


# ── 디렉터리 → 번들 ─────────────────────────────────────────────────────────

#: 번들에 담지 않는 디렉터리. 개발 잔재와 저장소 메타데이터다.
IGNORED_DIRS = frozenset({
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "dist", "build",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})
#: 담지 않는 파일. `.key` 는 **비밀 키** — 번들에 실리면 서명이 의미를 잃는다.
IGNORED_SUFFIXES = (".pyc", ".pyo", ".lccp", ".key")
IGNORED_FILES = frozenset({".DS_Store", "Thumbs.db"})


def files_under(root: Path) -> tuple[dict[str, bytes], list[str]]:
    """디렉터리를 번들 파일 맵으로. 돌려주는 둘째는 **뺀 것**의 목록이다(사람이 봐야 한다).

    **심볼릭 링크는 거부한다** — 따라가면 링크 너머의 파일(`~/.ssh/id_ed25519` 같은)이 번들에 실린다.
    호스트가 zip 안의 링크 항목을 거부하는 것과 같은 태도다(`safe_names`). 예약 파일(`MANIFEST.sha256`·
    `SIGNATURE`)은 만들 때 새로 쓰므로 디렉터리에 있어도 담지 않는다.
    """
    root = Path(root)
    if not root.is_dir():
        raise PluginError(f"디렉터리가 아닙니다: {root}")
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        for name in sorted(dirnames):
            if (here / name).is_symlink():
                raise PluginError(f"심볼릭 링크는 담을 수 없습니다: {(rel_dir / name).as_posix()}")
        kept = [name for name in dirnames if name not in IGNORED_DIRS]
        skipped.extend((rel_dir / name).as_posix() + "/" for name in dirnames if name in IGNORED_DIRS)
        dirnames[:] = sorted(kept)
        for name in sorted(filenames):
            path = here / name
            rel = (rel_dir / name).as_posix()
            if path.is_symlink():
                raise PluginError(f"심볼릭 링크는 담을 수 없습니다: {rel}")
            if name in IGNORED_FILES or name.endswith(IGNORED_SUFFIXES) or rel in _RESERVED:
                skipped.append(rel)
                continue
            files[rel] = path.read_bytes()
    if MANIFEST_NAME not in files:
        raise PluginError(f"{root} 에 {MANIFEST_NAME} 이 없습니다")
    return files, skipped


def check_limits(files: Mapping[str, bytes]) -> None:
    """호스트가 zip 헤더로 재는 상한을 **만들기 전에** 같은 값으로 잰다."""
    _require(len(files) <= MAX_FILES, f"번들 항목이 너무 많습니다: {len(files)} > {MAX_FILES}")
    total = 0
    for name, data in sorted(files.items()):
        total += len(data)
        _require(
            total <= MAX_UNCOMPRESSED_BYTES,
            f"해제 크기가 상한을 넘습니다: {total} > {MAX_UNCOMPRESSED_BYTES}",
        )
    _require(bool(files), "빈 번들입니다")


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    # 고정 시각·고정 권한 — 같은 입력이면 같은 바이트다. 운영자가 sha256 을 비교할 수 있다.
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, data)


def pack(files: Mapping[str, bytes], *, sign_key: bytes | None = None) -> bytes:
    """`.lccp` 바이트 — 호스트의 `build_bundle` 과 같은 배치(정렬 · `MANIFEST.sha256` · hex `SIGNATURE`)에
    **재현 가능성**을 더했다. 호스트 상한은 여기서 먼저 잰다."""
    payload = {name: data for name, data in files.items() if name not in _RESERVED}
    check_limits(payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            _write_entry(archive, name, data)
        if sign_key is not None:
            block = checksum_block(payload)
            _write_entry(archive, CHECKSUMS_NAME, block)
            _write_entry(archive, SIGNATURE_NAME, _ed25519_sign(sign_key, block).hex().encode("ascii"))
    raw = buffer.getvalue()
    _require(len(raw) <= MAX_BUNDLE_BYTES, f"번들이 너무 큽니다: {len(raw)} > {MAX_BUNDLE_BYTES}")
    return raw


# ── 검사 ────────────────────────────────────────────────────────────────────


@dataclass
class Report:
    manifest: Manifest
    files: tuple[str, ...]
    signature: str
    sha256: str | None
    size: int
    notes: list[str] = field(default_factory=list)

    def trigger_text(self) -> str:
        m = self.manifest
        if m.schedule:
            return f"schedule {m.schedule} ({m.schedule_tz})"
        if m.event:
            roles = ", ".join(m.event_roles) if m.event_roles else "모든 역할"
            return f"event {m.event} ({roles})"
        return "없음 — 스스로 깨어나지 않는다"

    def as_dict(self) -> dict[str, Any]:
        m = self.manifest
        return {
            "id": m.plugin_id, "version": m.version, "name": m.name, "description": m.description,
            "requires_host": m.requires_host, "kind": m.kind, "endpoint": m.endpoint,
            "trigger": {
                "kind": "schedule" if m.schedule else ("event" if m.event else None),
                "schedule": m.schedule, "timezone": m.schedule_tz if m.schedule else None,
                "event": m.event, "roles": list(m.event_roles),
            },
            "service": m.service_fields(),
            "files": list(self.files), "signature": self.signature,
            "sha256": self.sha256, "size": self.size, "notes": list(self.notes),
        }


def _host_range(manifest: Manifest, host_version: str | None) -> None:
    if host_version is not None and not host_satisfies(host_version, manifest.requires_host):
        raise PluginError(
            f"이 호스트({host_version})는 플러그인이 요구하는 범위"
            f"({manifest.requires_host})에 없습니다"
        )


def inspect_bytes(raw: bytes, *, keys: Sequence[bytes] | None, host_version: str | None) -> Report:
    """번들을 **호스트의 `inspect_bundle` 과 같은 순서로** 본다: 크기 → zip → 항목 → 매니페스트 → 호스트 범위 → 서명.

    `invalid` 는 호스트와 같은 문장으로 거부한다. `unsigned` 는 거부하지 않고 노트로 남긴다 — 호스트 설치는
    서명을 요구하지만, 그것은 `build --sign` 단계의 일이지 규칙 위반이 아니다.
    """
    _require(len(raw) <= MAX_BUNDLE_BYTES, f"번들이 너무 큽니다: {len(raw)} > {MAX_BUNDLE_BYTES}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise PluginError(f"zip 이 아닙니다: {exc}") from exc
    with archive:
        names = safe_names(archive)
        _require(MANIFEST_NAME in names, f"번들 루트에 {MANIFEST_NAME} 이 없습니다")
        manifest = parse_manifest(_read(archive, MANIFEST_NAME))
        _host_range(manifest, host_version)
        state = verify_bundle(archive, names, keys)
    if state == "invalid":
        raise PluginError(
            "번들 검증에 실패했습니다 — 서명이나 파일 해시가 맞지 않습니다. "
            "받은 파일이 손상됐거나 손을 탄 것입니다"
        )
    report = Report(
        manifest=manifest, files=tuple(n for n in names if n not in _RESERVED),
        signature=state, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
    )
    _common_notes(report)
    if state == "unsigned":
        report.notes.append("서명이 없다 — 호스트 설치는 서명을 요구한다: `build --sign <키>.key`")
    elif state == "unverified":
        report.notes.append("서명은 있지만 판정하지 않았다 — `--trust <디렉터리>` 나 `--pub <키>.pub` 으로 신뢰 키를 준다")
    return report


def inspect_dir(root: Path, *, host_version: str | None) -> Report:
    """디렉터리를 **만들지 않고** 본다 — 번들이 될 파일 맵에 같은 규칙을 댄다."""
    files, skipped = files_under(root)
    check_limits(files)
    manifest = parse_manifest(files[MANIFEST_NAME])
    _host_range(manifest, host_version)
    report = Report(
        manifest=manifest, files=tuple(sorted(files)), signature="unsigned",
        sha256=None, size=sum(len(d) for d in files.values()),
    )
    _common_notes(report)
    for name in skipped:
        report.notes.append(f"뺐다: {name}")
    return report


def _common_notes(report: Report) -> None:
    report.notes.append(
        "역할이 이 호스트에 실재하는지(allow_roles · [trigger].roles)는 여기서 못 본다 — "
        "호스트의 사전 검사 POST /v1/platform/plugins/inspect 가 본다"
    )
    if report.manifest.schedule:
        stamp = next_after(parse_cron(report.manifest.schedule), time.time(), timezone=report.manifest.schedule_tz)
        when = datetime.fromtimestamp(stamp, tz=ZoneInfo(report.manifest.schedule_tz)).strftime("%Y-%m-%d %H:%M %Z")
        report.notes.append(f"다음 예정(지금 기준): {when}")


# ── 출력 ────────────────────────────────────────────────────────────────────


def _say(text: str = "") -> None:
    print(text)


def _fail(text: str, code: int = 1) -> int:
    print(f"거부: {text}" if code == 1 else text, file=sys.stderr)
    return code


def _print_report(report: Report, *, path: Path) -> None:
    m = report.manifest
    _say(f"  {path}")
    _say(f"  id            {m.plugin_id}  v{m.version}  ({m.name})")
    _say(f"  requires_host {m.requires_host or '(제한 없음)'}")
    _say(f"  trigger       {report.trigger_text()}")
    service = m.service_fields()
    _say(
        f"  service       roles={','.join(service['allow_roles'])}"
        f"  rate/min={service['rate_limit_per_min'] if service['rate_limit_per_min'] is not None else '-'}"
        f"  budget/month={service['budget_usd_per_month'] if service['budget_usd_per_month'] is not None else '-'}"
    )
    _say(f"  files         {len(report.files)} ({report.size} bytes)")
    _say(f"  signature     {report.signature}")
    if report.sha256:
        _say(f"  sha256        {report.sha256}")
    for note in report.notes:
        _say(f"  · {note}")


# ── 부명령 ──────────────────────────────────────────────────────────────────


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(text)


def cmd_keygen(args: argparse.Namespace) -> int:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", args.name):
        return _fail("키 이름은 글자·숫자·. _ - 만 됩니다", 2)
    try:
        _, private_cls, _ = _crypto()
    except CryptoUnavailable as exc:
        return _fail(str(exc), 2)
    out = Path(args.out or ".")
    key_path, pub_path = out / f"{args.name}.key", out / f"{args.name}.pub"
    for path in (key_path, pub_path):
        if path.exists():
            return _fail(f"이미 있습니다: {path} — 키는 덮어쓰지 않는다", 2)
    out.mkdir(parents=True, exist_ok=True)
    key = private_cls.generate()
    _write_private(key_path, key.private_bytes_raw().hex() + "\n")
    pub_path.write_text(key.public_key().public_bytes_raw().hex() + "\n", encoding="ascii")
    _say(f"  비밀 키  {key_path}  (0600 — 서명하는 기계 밖으로 내보내지 않는다)")
    _say(f"  공개 키  {pub_path}  → 호스트의 keys/plugin-trust/{pub_path.name} 으로 (uid 10001 이 읽어야 한다)")
    return 0


_MANIFEST_TEMPLATE = """\
# {id} — LLM ControlCenter 플러그인 매니페스트
# 규칙은 `lccp check` 가 호스트와 같은 코드로 판정한다.

[plugin]
id = "{id}"
name = "{name}"
version = "0.1.0"
requires_host = "{requires_host}"
description = ""

[service]
# 이 플러그인의 토큰이 부를 수 있는 역할 — 호스트의 config/roles.yaml 에 있어야 한다(호스트 사전 검사가 본다).
allow_roles = [{roles}]
rate_limit_per_min = 10
budget_usd_per_month = 5.0

[run]
# external — 컨트롤 플레인이 프로세스를 띄우지 않는다. 운영자가 systemd·컨테이너로 띄운다.
kind = "external"
{trigger}"""

_TRIGGER_TEMPLATES = {
    "schedule": """
[trigger]
kind = "schedule"
# 분 시 일 월 요일 — 이름·@daily 는 안 받는다. 밀린 예정은 몰아서 돌지 않는다.
schedule = "{schedule}"
timezone = "{timezone}"
""",
    "event": """
[trigger]
kind = "event"
event = "job.finished"
# 비우면 모든 역할의 종결을 받는다.
roles = []
""",
    "none": "",
}

_MAIN_TEMPLATE = '''\
#!/usr/bin/env python3
"""{id} — LLM ControlCenter 플러그인.

`client.py` 와 `plugin.py` 를 이 디렉터리에 둔다(호스트의 GET /v1/client/client.py · /v1/client/plugin.py) —
또는 `LCC_SDK_DIR` 로 그 위치를 준다. 토큰은 설치 응답이나 토큰 회전에서 **한 번만** 나온다.

    LCC_URL=https://llmcc.example.com LCC_TOKEN=lcc_... python3 main.py
"""
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("LCC_SDK_DIR") or str(HERE))
try:
    from plugin import Plugin
except ImportError as exc:
    sys.exit(f"SDK 를 찾지 못했다: {{exc}} — client.py 와 plugin.py 를 {{HERE}} 에 두거나 LCC_SDK_DIR 로 위치를 준다")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("{id}")

{body}

if __name__ == "__main__":
    sys.exit(main())
'''

_BODIES = {
    "schedule": '''\
def on_tick(tick):
    """예정이 지났고 이 프로세스가 그 실행을 가져왔다 — 복제본이 여럿이어도 한 번만 불린다."""
    log.info("tick: scheduled_for=%s late_by=%.0fs", tick.scheduled_for, tick.late_by or 0.0)
    result = plugin.llm.generate("{role}", "여기에 프롬프트를 쓴다", wait=180)
    if result.ok:
        log.info("결과: %s", result.text[:200])
    else:
        log.warning("실패: %s %s", result.status, result.error_code)


def main() -> int:
    global plugin
    plugin = Plugin.from_env(name="{id}")
    report = plugin.run(on_tick=on_tick, once="--once" in sys.argv)
    log.info("끝: %s", report)
    return 0
''',
    "event": '''\
def on_event(event):
    """잡 하나의 종결 — 모델이 본 것(마스킹 뒤)과 나간 응답이다. 이 함수가 예외 없이 끝나야 ack 된다."""
    log.info("event %s: role=%s status=%s model=%s latency=%s", event.id, event.role, event.status,
             event.model, event.latency)


def main() -> int:
    plugin = Plugin.from_env(name="{id}")
    report = plugin.run(on_event=on_event, once="--once" in sys.argv)
    log.info("끝: %s", report)
    return 0
''',
    "none": '''\
def main() -> int:
    """트리거가 없는 플러그인은 스스로 깨어나지 않는다 — 자기 토큰으로 API 를 부르는 프로그램이다."""
    plugin = Plugin.from_env(name="{id}")
    result = plugin.llm.generate("{role}", "여기에 프롬프트를 쓴다", wait=180)
    log.info("%s %s", result.status, result.text[:200])
    return 0 if result.ok else 1
''',
}

_README_TEMPLATE = """\
# {id}

`lccp init` 이 만든 골격이다.

```sh
python lccp.py check .                          # 호스트 규칙으로 판정
LCC_URL=http://localhost:8610 LCC_TOKEN=... python3 main.py   # 목 서버나 데모 호스트에 붙여 본다
python lccp.py build . --sign <키>.key          # 서명 번들 → 운영자에게 .lccp 와 <키>.pub 을 넘긴다
```

- SDK: `client.py`·`plugin.py` 를 이 디렉터리에 둔다 — 호스트의 `GET /v1/client/` 에서 내려받는다.
- 목 서버: `python mock_server.py --plugin-events --plugin-tick-every 30` 이 tick·events 를 흉내 낸다.
- 데모 호스트: `python -m app serve --demo --plugin-dev .` 가 이 디렉터리를 무서명으로 설치하고 켜고 토큰을 찍는다.
"""


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir)
    if target.exists() and any(target.iterdir()):
        return _fail(f"비어 있지 않습니다: {target}", 2)
    if not _ID.match(args.id):
        return _fail(f"플러그인 id 가 역DNS 형식이 아닙니다: {args.id!r} (예: acme.daily-digest)", 2)
    roles = [r.strip() for r in (args.roles or "summarize").split(",") if r.strip()]
    if not roles:
        return _fail("--roles 가 비었습니다", 2)
    major, minor = (int(part) for part in (args.host_version or HOST_VERSION).split(".")[:2])
    trigger = _TRIGGER_TEMPLATES[args.trigger].format(
        schedule=args.schedule or "0 8 * * *", timezone=args.timezone or "UTC",
    )
    manifest = _MANIFEST_TEMPLATE.format(
        id=args.id, name=args.name or args.id, requires_host=f">={major}.{minor},<{major}.{minor + 1}",
        roles=", ".join(json.dumps(r) for r in roles), trigger=trigger,
    )
    target.mkdir(parents=True, exist_ok=True)
    (target / MANIFEST_NAME).write_text(manifest, encoding="utf-8")
    (target / "main.py").write_text(
        _MAIN_TEMPLATE.format(id=args.id, body=_BODIES[args.trigger].format(id=args.id, role=roles[0])),
        encoding="utf-8",
    )
    (target / "README.md").write_text(_README_TEMPLATE.format(id=args.id), encoding="utf-8")
    try:
        report = inspect_dir(target, host_version=args.host_version or HOST_VERSION)
    except PluginError as exc:
        return _fail(str(exc))
    _say(f"  만들었다: {target}/  ({MANIFEST_NAME} · main.py · README.md)")
    _print_report(report, path=target)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        return _fail(f"없습니다: {path}", 2)
    host_version = None if args.any_host else (args.host_version or HOST_VERSION)
    try:
        if path.is_dir():
            report = inspect_dir(path, host_version=host_version)
        else:
            keys = _keys_from(args)
            report = inspect_bytes(path.read_bytes(), keys=keys, host_version=host_version)
    except PluginError as exc:
        return _fail(str(exc))
    if args.json:
        _say(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _say("  통과 — 매니페스트 규칙 · 번들 상한" + (" · 항목 이름" if path.is_file() else ""))
        _print_report(report, path=path)
    return 0


def _keys_from(args: argparse.Namespace) -> list[bytes] | None:
    keys: list[bytes] = []
    if getattr(args, "trust", None):
        keys.extend(load_trusted_keys(Path(args.trust)))
    if getattr(args, "pub", None):
        keys.append(read_public_key(Path(args.pub)))
    return keys or None


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    try:
        files, skipped = files_under(root)
        sign_key = read_private_key(Path(args.sign)) if args.sign else None
        raw = pack(files, sign_key=sign_key)
        # 만들었으면 곧바로 호스트 순서로 다시 읽어 본다 — 서명은 방금 쓴 키의 공개 키로 대 본다.
        keys = None
        if sign_key is not None:
            _, private_cls, _ = _crypto()
            keys = [private_cls.from_private_bytes(sign_key).public_key().public_bytes_raw()]
        report = inspect_bytes(raw, keys=keys, host_version=args.host_version or HOST_VERSION)
    except PluginError as exc:
        return _fail(str(exc))
    except CryptoUnavailable as exc:
        return _fail(str(exc), 2)
    out = Path(args.out) if args.out else Path(f"{report.manifest.plugin_id}-{report.manifest.version}.lccp")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    for name in skipped:
        report.notes.append(f"뺐다: {name}")
    _say(f"  만들었다: {out}  ({len(raw)} bytes)")
    _print_report(report, path=out)
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    path = Path(args.bundle)
    if not path.is_file():
        return _fail(f"없습니다: {path}", 2)
    try:
        raw = path.read_bytes()
        _require(len(raw) <= MAX_BUNDLE_BYTES, f"번들이 너무 큽니다: {len(raw)} > {MAX_BUNDLE_BYTES}")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise PluginError(f"zip 이 아닙니다: {exc}") from exc
        with archive:
            names = safe_names(archive)
            files = {name: _read(archive, name) for name in names if name not in _RESERVED}
        sign_key = read_private_key(Path(args.key))
        signed = pack(files, sign_key=sign_key)
        _, private_cls, _ = _crypto()
        keys = [private_cls.from_private_bytes(sign_key).public_key().public_bytes_raw()]
        report = inspect_bytes(signed, keys=keys, host_version=None)
    except PluginError as exc:
        return _fail(str(exc))
    except CryptoUnavailable as exc:
        return _fail(str(exc), 2)
    out = Path(args.out) if args.out else path
    out.write_bytes(signed)
    _say(f"  서명했다: {out}")
    _print_report(report, path=out)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.bundle)
    if not path.is_file():
        return _fail(f"없습니다: {path}", 2)
    try:
        keys = _keys_from(args)
        report = inspect_bytes(path.read_bytes(), keys=keys, host_version=None)
    except PluginError as exc:
        return _fail(str(exc))
    _print_report(report, path=path)
    if report.signature == "signed":
        return 0
    if report.signature == "unverified":
        return _fail("판정 불가 — 신뢰 키(--trust/--pub)나 cryptography 가 없다", 2)
    return _fail("서명되지 않은 번들입니다. 사내 키로 서명하고 그 공개 키를 호스트의 keys/plugin-trust/ 에 두세요")


def cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.bundle)
    if not path.is_file():
        return _fail(f"없습니다: {path}", 2)
    try:
        report = inspect_bytes(path.read_bytes(), keys=_keys_from(args), host_version=None)
    except PluginError as exc:
        return _fail(str(exc))
    if args.json:
        _say(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(report, path=path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lccp", description="LLM ControlCenter 플러그인 패키징 — 호스트와 같은 규칙으로 검사하고 서명한다",
    )
    sub = parser.add_subparsers(dest="command")

    keygen = sub.add_parser("keygen", help="Ed25519 서명 키 한 쌍을 만든다 (<이름>.key · <이름>.pub)")
    keygen.add_argument("name")
    keygen.add_argument("--out", help="쓸 디렉터리 (기본: 현재 디렉터리)")
    keygen.set_defaults(func=cmd_keygen)

    init = sub.add_parser("init", help="플러그인 골격을 만든다 (plugin.toml · main.py · README.md)")
    init.add_argument("dir")
    init.add_argument("--id", required=True, help="역DNS id (예: acme.daily-digest)")
    init.add_argument("--name", help="표시 이름 (기본: id)")
    init.add_argument("--trigger", choices=("schedule", "event", "none"), default="event")
    init.add_argument("--roles", help="allow_roles, 쉼표 구분 (기본: summarize)")
    init.add_argument("--schedule", help='schedule 트리거의 cron (기본: "0 8 * * *")')
    init.add_argument("--timezone", help="schedule 트리거의 IANA 시간대 (기본: UTC)")
    init.add_argument("--host-version", help=f"requires_host 의 근거 (기본: {HOST_VERSION})")
    init.set_defaults(func=cmd_init)

    check = sub.add_parser("check", help="디렉터리나 .lccp 를 호스트 규칙으로 판정한다 — 오프라인")
    check.add_argument("path")
    check.add_argument("--host-version", help=f"이 판의 호스트라고 가정한다 (기본: {HOST_VERSION})")
    check.add_argument("--any-host", action="store_true", help="requires_host 범위를 보지 않는다")
    check.add_argument("--trust", help="신뢰 키 디렉터리 (*.pub) — 번들의 서명을 판정할 때")
    check.add_argument("--pub", help="공개 키 파일 하나")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

    build = sub.add_parser("build", help="디렉터리를 .lccp 로 만든다 (재현 가능 · --sign 으로 서명)")
    build.add_argument("dir")
    build.add_argument("-o", "--out", help="출력 경로 (기본: <id>-<version>.lccp)")
    build.add_argument("--sign", metavar="KEY", help="keygen 이 만든 .key 로 서명한다")
    build.add_argument("--host-version", help=f"requires_host 를 이 판에 대 본다 (기본: {HOST_VERSION})")
    build.set_defaults(func=cmd_build)

    sign = sub.add_parser("sign", help="이미 만든 .lccp 에 서명한다 (기본은 제자리)")
    sign.add_argument("bundle")
    sign.add_argument("--key", required=True, help="keygen 이 만든 .key")
    sign.add_argument("-o", "--out", help="다른 경로에 쓴다")
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="서명을 판정한다: signed 0 · unsigned/invalid 1 · 판정 불가 2")
    verify.add_argument("bundle")
    verify.add_argument("--trust", help="신뢰 키 디렉터리 (*.pub)")
    verify.add_argument("--pub", help="공개 키 파일 하나")
    verify.set_defaults(func=cmd_verify)

    inspect = sub.add_parser("inspect", help="번들 요약을 찍는다 (--json)")
    inspect.add_argument("bundle")
    inspect.add_argument("--trust", help="신뢰 키 디렉터리 (*.pub)")
    inspect.add_argument("--pub", help="공개 키 파일 하나")
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
