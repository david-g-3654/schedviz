from datetime import datetime

import pytest

from schedviz.systemd_calendar import CalendarParseError, SystemdCalendar


def N(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s)


def test_daily_keyword():
    c = SystemdCalendar.parse("daily")
    assert c.next_after(N(2026, 8, 11, 5, 0)) == N(2026, 8, 12, 0, 0)


def test_explicit_daily_time():
    c = SystemdCalendar.parse("*-*-* 03:00:00")
    assert c.next_after(N(2026, 8, 11, 2, 0)) == N(2026, 8, 11, 3, 0)


def test_hourly_keyword():
    c = SystemdCalendar.parse("hourly")
    assert c.next_after(N(2026, 8, 11, 10, 30)) == N(2026, 8, 11, 11, 0)


def test_weekly_monday():
    c = SystemdCalendar.parse("weekly")  # Mon 00:00
    nxt = c.next_after(N(2026, 8, 11, 12, 0))  # Tue
    assert nxt == N(2026, 8, 17, 0, 0)
    assert nxt.weekday() == 0


def test_and_semantics_dow_and_dom():
    # systemd AND: the 1st AND a Wednesday.
    c = SystemdCalendar.parse("Wed *-*-01 00:00:00")
    nxt = c.next_after(N(2026, 8, 11, 0, 0))
    assert nxt.day == 1 and nxt.weekday() == 2  # Wednesday the 1st


def test_minute_step():
    c = SystemdCalendar.parse("*-*-* *:0/15:00")
    runs = c.next_runs(N(2026, 8, 11, 10, 0), 3)
    assert runs == [N(2026, 8, 11, 10, 15),
                    N(2026, 8, 11, 10, 30),
                    N(2026, 8, 11, 10, 45)]


def test_monthly_keyword():
    c = SystemdCalendar.parse("monthly")  # *-*-01 00:00:00
    assert c.next_after(N(2026, 8, 11, 0, 0)) == N(2026, 9, 1, 0, 0)


def test_specific_seconds():
    c = SystemdCalendar.parse("*-*-* 12:00:30")
    assert c.next_after(N(2026, 8, 11, 12, 0, 0)) == N(2026, 8, 11, 12, 0, 30)


def test_dow_range():
    c = SystemdCalendar.parse("Mon..Fri *-*-* 08:00:00")
    # 2026-08-15 is a Saturday; next weekday run from Fri evening is Monday.
    nxt = c.next_after(N(2026, 8, 14, 9, 0))  # Friday after 8am
    assert nxt == N(2026, 8, 17, 8, 0)  # Monday
    assert nxt.weekday() == 0


def test_yearly():
    c = SystemdCalendar.parse("yearly")  # *-01-01 00:00:00
    assert c.next_after(N(2026, 8, 11, 0, 0)) == N(2027, 1, 1, 0, 0)


def test_empty_raises():
    with pytest.raises(CalendarParseError):
        SystemdCalendar.parse("")
