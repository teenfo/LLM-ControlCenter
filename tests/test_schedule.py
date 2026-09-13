"""cron 계산 — 순수 함수라 여기서 다 잡는다.

스케줄이 틀리면 증상이 **몇 시간 뒤에** 나타난다. "왜 안 돌았지" 를 배관에서
찾기 시작하면 하루가 간다. 그래서 계산은 배관과 분리해 두고, 여기서 다 못박는다.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.schedule import (
    MAX_LOOKAHEAD_DAYS,
    ScheduleError,
    next_after,
    parse_cron,
    resolve_timezone,
)

UTC = ZoneInfo("UTC")
SEOUL = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


def fires(expression: str, after: datetime, *, timezone: str = "UTC", count: int = 1):
    """다음 `count` 번의 시각을 그 시간대의 datetime 으로."""
    spec = parse_cron(expression)
    zone = ZoneInfo(timezone)
    stamp = after.timestamp()
    out = []
    for _ in range(count):
        stamp = next_after(spec, stamp, timezone=timezone)
        out.append(datetime.fromtimestamp(stamp, zone))
    return out


# ── 파싱 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("expression", [
    "0 8 * * *", "*/15 * * * *", "0 0 1 * *", "30 2 29 2 *",
    "0 9-17 * * 1-5", "0,30 * * * *", "0 */4 * * *",
])
def test_ordinary_expressions_parse(expression):
    assert parse_cron(expression).source == expression


@pytest.mark.parametrize("expression,fragment", [
    ("0 8 * *", "다섯 칸"),
    ("0 8 * * * *", "다섯 칸"),
    ("60 8 * * *", "0~59"),
    ("0 24 * * *", "0~23"),
    ("0 8 0 * *", "1~31"),
    ("0 8 32 * *", "1~31"),
    ("0 8 * 13 *", "1~12"),
    ("0 8 * * 8", "0~7"),
    ("a b c d e", "읽을 수 없"),
    ("0 8 * * 5-1", "거꾸로"),
    ("*/0 * * * *", "1 이상"),
    ("@daily", "다섯 칸"),
    ("0 8 * JAN *", "읽을 수 없"),
])
def test_broken_expressions_are_refused_with_a_readable_reason(expression, fragment):
    """**조용히 안 도는 것이 최악이다.** 못 읽으면 설치 시점에 거부한다."""
    with pytest.raises(ScheduleError) as caught:
        parse_cron(expression)
    assert fragment in str(caught.value)


def test_sunday_can_be_written_as_seven():
    """관행이다. 0 으로 정규화하지 않으면 `* * * * 7` 이 영원히 안 돈다."""
    assert 0 in parse_cron("0 0 * * 7").weekdays
    assert fires("0 0 * * 7", datetime(2026, 9, 3, tzinfo=UTC))[0].weekday() == 6


# ── 다음 시각 ────────────────────────────────────────────────────────────────


def test_a_daily_schedule_walks_forward_one_day_at_a_time():
    got = fires("0 8 * * *", datetime(2026, 9, 3, 10, 30, tzinfo=UTC), count=3)
    assert [d.strftime("%m-%d %H:%M") for d in got] == ["09-04 08:00", "09-05 08:00", "09-06 08:00"]


def test_the_result_is_always_strictly_after_the_given_moment():
    """정각에 물어봤다고 그 정각을 다시 주면, 클레임이 무한히 돈다."""
    exact = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert fires("0 8 * * *", exact)[0] == datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def test_a_leap_day_schedule_finds_the_next_leap_year():
    """4년을 내다보지 않으면 여기서 "안 도는 표현식" 으로 오판한다."""
    got = fires("30 2 29 2 *", datetime(2026, 9, 3, tzinfo=UTC), count=2)
    assert [d.strftime("%Y-%m-%d") for d in got] == ["2028-02-29", "2032-02-29"]


def test_a_schedule_that_never_fires_is_refused_not_returned():
    """`0 0 30 2 *` — 2월 30일. 설치가 이걸 받아 두면 영원히 안 도는 플러그인이 된다."""
    spec = parse_cron("0 0 30 2 *")
    with pytest.raises(ScheduleError) as caught:
        next_after(spec, datetime(2026, 9, 3, tzinfo=UTC).timestamp())
    assert "없는 날짜" in str(caught.value)


def test_the_lookahead_is_long_enough_for_a_leap_year():
    """상한을 줄이면 위 두 테스트의 관계가 조용히 깨진다."""
    assert MAX_LOOKAHEAD_DAYS >= 366 * 4


# ── cron 의 오랜 함정: 일과 요일 ───────────────────────────────────────────────


def test_day_and_weekday_are_or_ed_when_both_are_restricted():
    """**AND 로 읽으면 몇 년에 한 번 돈다.**

    `0 0 1 * 1` 은 "매월 1일 **또는** 매주 월요일" 이다. cron 의 표준 동작이고,
    이걸 뒤집으면 "1일이면서 월요일" 이 되어 사실상 안 도는 것과 같아진다.
    """
    got = fires("0 0 1 * 1", datetime(2026, 9, 3, tzinfo=UTC), count=6)
    labels = {d.strftime("%m-%d") for d in got}
    assert "10-01" in labels, "매월 1일이 빠졌다 (AND 로 읽고 있다)"
    assert "09-07" in labels, "월요일이 빠졌다"


def test_only_one_restricted_day_field_means_only_that_field_matters():
    monthly = fires("0 0 1 * *", datetime(2026, 9, 3, tzinfo=UTC), count=2)
    assert [d.strftime("%m-%d") for d in monthly] == ["10-01", "11-01"]

    weekly = fires("0 0 * * 1", datetime(2026, 9, 3, tzinfo=UTC), count=2)
    assert all(d.weekday() == 0 for d in weekly)


def test_cron_sunday_is_zero_not_python_sunday():
    """`weekday()` 는 월=0 이고 cron 은 일=0 이다. 이 변환이 틀리면 하루씩 밀린다."""
    [sunday] = fires("0 0 * * 0", datetime(2026, 9, 3, tzinfo=UTC))
    assert sunday.weekday() == 6  # 파이썬에서 일요일


# ── 시간대 ──────────────────────────────────────────────────────────────────


def test_the_schedule_is_read_in_its_own_timezone():
    """"아침 8시" 는 그 나라의 8시다. UTC 로 고정하면 한국에서 오후 5시에 돈다."""
    [seoul] = fires("0 8 * * *", datetime(2026, 9, 3, 0, 0, tzinfo=UTC), timezone="Asia/Seoul")
    assert seoul.astimezone(SEOUL).strftime("%H:%M") == "08:00"
    assert seoul.astimezone(UTC).strftime("%H:%M") == "23:00"  # 전날 23시 UTC


def test_an_unknown_timezone_is_refused_not_silently_utc():
    """조용히 UTC 로 떨어뜨리면 "왜 9시간 일찍 도느냐" 의 원인을 아무도 못 찾는다."""
    with pytest.raises(ScheduleError):
        resolve_timezone("Asia/Seuol")
    with pytest.raises(ScheduleError):
        next_after(parse_cron("0 8 * * *"), 0.0, timezone="Mars/Olympus")


def test_a_nonexistent_local_time_still_fires_once_shifted():
    """서머타임 봄 전이 — 2027-03-14 의 02:30 은 존재하지 않는 시각이다.

    안 돌면 그날 하루가 조용히 사라진다. 밀어서라도 한 번 돈다.
    """
    got = fires("30 2 * * *", datetime(2027, 3, 13, 12, 0, tzinfo=NY),
                timezone="America/New_York", count=2)
    assert got[0].strftime("%m-%d %H:%M") == "03-14 03:30"
    assert got[1].strftime("%m-%d %H:%M") == "03-15 02:30"


def test_a_repeated_local_time_fires_only_once():
    """가을 전이 — 01:30 이 두 번 온다. **두 번 도는 것보다 한 번이 낫다.**"""
    got = fires("30 1 * * *", datetime(2026, 10, 31, 12, 0, tzinfo=NY),
                timezone="America/New_York", count=2)
    assert [d.strftime("%m-%d %H:%M") for d in got] == ["11-01 01:30", "11-02 01:30"]


def test_time_never_goes_backwards_across_a_dst_transition():
    """거꾸로 가면 클레임이 이미 지난 시각을 다시 예정으로 잡고 폭주한다."""
    spec = parse_cron("*/10 * * * *")
    stamp = datetime(2026, 10, 31, 20, 0, tzinfo=NY).timestamp()
    for _ in range(400):
        nxt = next_after(spec, stamp, timezone="America/New_York")
        assert nxt > stamp
        stamp = nxt
