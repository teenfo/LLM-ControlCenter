"""cron 표현식 — 파싱과 "다음 시각".

플러그인이 `[trigger] schedule = "0 8 * * *"` 을 선언하면 그 다음 시각을 여기서
계산한다. **순수 함수다** — DB 도 시계도 안 만진다. 스케줄 계산이 틀렸을 때
그것이 계산 문제인지 배관 문제인지 가려낼 수 있어야 한다.

새 의존성이 없다. `zoneinfo` 는 3.9 부터 표준 라이브러리다.

### 왜 간격(`every = "24h"`)이 아니라 cron 인가

"매일 아침 8시" 를 간격으로는 못 쓴다. 24시간 간격은 설치한 시각에 매여서, 재설치
한 번에 새벽 3시로 옮겨 간다. 운영자가 기대하는 것은 시각이지 간격이 아니다.

### 안 하는 것

- 이름(`JAN`·`MON`)·특수 표현(`@daily`·`L`·`W`·`#`)을 안 받는다. 숫자만이다 —
  받는 문법이 늘수록 "이게 언제 도는 거냐" 를 사람이 못 읽는다
- 밀린 것을 몰아서 실행하지 않는다. 그 판단은 여기가 아니라 클레임 쪽에 있다
  (`plugins.claim_tick`) — 사흘 꺼져 있었다고 사흘치를 한꺼번에 돌리면 그게 사고다
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
