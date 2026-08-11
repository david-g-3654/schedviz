from datetime import datetime

import pytest

from schedviz.cronexpr import CronExpr, CronParseError


def N(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi)


def test_daily_fixed_time():
    c = CronExpr.parse("0 3 * * *")
    assert c.next_after(N(2026, 8, 11, 2, 0)) == N(2026, 8, 11, 3, 0)
    assert c.next_after(N(2026, 8, 11, 3, 0)) == N(2026, 8, 12, 3, 0)


def test_every_15_minutes():
    c = CronExpr.parse("*/15 * * * *")
    runs = c.next_runs(N(2026, 8, 11, 10, 0), 4)
    assert runs == [
        N(2026, 8, 11, 10, 15),
        N(2026, 8, 11, 10, 30),
        N(2026, 8, 11, 10, 45),
        N(2026, 8, 11, 11, 0),
    ]


def test_macro_daily():
    assert CronExpr.parse("@daily").next_after(N(2026, 8, 11, 5, 0)) == \
        N(2026, 8, 12, 0, 0)


def test_weekday_names():
    # Every Monday at 09:00. 2026-08-11 is a Tuesday.
    c = CronExpr.parse("0 9 * * mon")
    nxt = c.next_after(N(2026, 8, 11, 12, 0))
    assert nxt == N(2026, 8, 17, 9, 0)
    assert nxt.weekday() == 0  # Monday


def test_dom_dow_or_semantics():
    # Vixie rule: both restricted => fire if EITHER matches.
    # "0 0 13 * 5" => midnight on the 13th OR any Friday.
    c = CronExpr.parse("0 0 13 * 5")
    # 2026-08-11 Tue -> next Friday is Aug 14, and the 13th is Aug 13.
    runs = c.next_runs(N(2026, 8, 11, 0, 0), 2)
    assert runs[0] == N(2026, 8, 13, 0, 0)   # the 13th (Thursday)
    assert runs[1] == N(2026, 8, 14, 0, 0)   # Friday


def test_dom_only_restricted():
    c = CronExpr.parse("30 4 1 * *")  # 1st of every month at 04:30
    runs = c.next_runs(N(2026, 8, 11, 0, 0), 2)
    assert runs == [N(2026, 9, 1, 4, 30), N(2026, 10, 1, 4, 30)]


def test_month_restriction():
    c = CronExpr.parse("0 0 1 1 *")  # once a year, Jan 1
    assert c.next_after(N(2026, 8, 11, 0, 0)) == N(2027, 1, 1, 0, 0)


def test_impossible_date_returns_none():
    c = CronExpr.parse("0 0 30 2 *")  # Feb 30 never exists
    assert c.next_after(N(2026, 1, 1, 0, 0)) is None


def test_sunday_both_0_and_7():
    c0 = CronExpr.parse("0 0 * * 0")
    c7 = CronExpr.parse("0 0 * * 7")
    start = N(2026, 8, 11, 0, 0)
    assert c0.next_after(start) == c7.next_after(start)
    assert c0.next_after(start).weekday() == 6  # Sunday


def test_ranges_and_lists():
    c = CronExpr.parse("0 9-17 * * 1-5")  # top of the hour, 9-5, Mon-Fri
    runs = c.next_runs(N(2026, 8, 11, 8, 30), 3)
    assert runs == [
        N(2026, 8, 11, 9, 0),
        N(2026, 8, 11, 10, 0),
        N(2026, 8, 11, 11, 0),
    ]


@pytest.mark.parametrize("bad", [
    "", "* * * *", "60 * * * *", "* 24 * * *", "0 0 0 * *",
    "0 0 * 13 *", "*/0 * * * *", "@reboot", "@bogus",
])
def test_invalid_expressions(bad):
    with pytest.raises(CronParseError):
        CronExpr.parse(bad)


def test_six_field_seconds_dropped():
    c = CronExpr.parse("0 0 3 * * *")  # leading seconds field
    assert c.next_after(N(2026, 8, 11, 2, 0)) == N(2026, 8, 11, 3, 0)


def test_leap_year_feb_29():
    c = CronExpr.parse("0 0 29 2 *")
    # 2028 is the next leap year after 2026.
    assert c.next_after(N(2026, 3, 1, 0, 0)) == N(2028, 2, 29, 0, 0)
