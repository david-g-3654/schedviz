"""A parser + next-run engine for systemd ``OnCalendar=`` expressions.

systemd timers do **not** use cron syntax. The calendar form is::

    [DayOfWeek] Year-Month-Day Hour:Minute:Second

with ``*`` wildcards, ``a,b`` lists, ``a..b`` ranges, ``/step`` repetition, and a
set of shorthand keywords (``daily``, ``hourly``, ``weekly``, ...). Crucially,
unlike cron, systemd uses **AND** semantics: every specified component must match
(so ``Mon *-*-01`` fires only when the 1st of the month is a Monday).

We model second resolution because ``OnCalendar`` supports it, but the timeline
only ever reads minute granularity. Monotonic timers (``OnBootSec=`` /
``OnUnitActiveSec=``) are handled by the caller, not here — they have no
wall-clock schedule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Set

__all__ = ["SystemdCalendar", "CalendarParseError"]


class CalendarParseError(ValueError):
    """Raised when an OnCalendar expression cannot be parsed."""


_DOW_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_DOW_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# systemd's own normalisations of the shorthand keywords.
_KEYWORDS = {
    "minutely": "*-*-* *:*:00",
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00",
    "weekly": "Mon *-*-* 00:00:00",
    "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00",
    "quarterly": "*-01,04,07,10-01 00:00:00",
    "semiannually": "*-01,07-01 00:00:00",
}

_MAX_YEARS_AHEAD = 8


def _parse_numeric_field(raw: str, lo: int, hi: int, name: str) -> Optional[Set[int]]:
    """Parse one numeric component. Returns ``None`` for a bare ``*`` (means
    "any", i.e. unrestricted), otherwise the set of matching integers."""

    raw = raw.strip()
    if raw == "*":
        return None

    values: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part == "":
            raise CalendarParseError(f"empty item in {name}")

        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            if not re.fullmatch(r"\d+", step_str.strip()):
                raise CalendarParseError(f"bad step {step_str!r} in {name}")
            step = int(step_str)
            if step <= 0:
                raise CalendarParseError(f"step must be > 0 in {name}")
        else:
            base = part
        base = base.strip()

        if base == "*":
            start, end = lo, hi
        elif ".." in base:
            a, _, b = base.partition("..")
            start, end = int(a), int(b)
        else:
            if not re.fullmatch(r"\d+", base):
                raise CalendarParseError(f"bad value {base!r} in {name}")
            start = int(base)
            end = hi if "/" in part else start

        if start < lo or end > hi:
            raise CalendarParseError(
                f"value out of range {lo}-{hi} in {name}: {part!r}"
            )
        if start > end:
            raise CalendarParseError(f"reversed range in {name}: {part!r}")
        values.update(range(start, end + 1, step))

    return values


def _parse_dow_field(raw: str) -> Optional[Set[int]]:
    """Parse the optional day-of-week list into Python weekday ints
    (Mon=0..Sun=6). ``None`` means unrestricted."""

    raw = raw.strip()
    if raw == "" or raw == "*":
        return None

    values: Set[int] = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if part == "":
            continue
        if ".." in part:
            a, _, b = part.partition("..")
            if a not in _DOW_NAMES or b not in _DOW_NAMES:
                raise CalendarParseError(f"bad weekday range {part!r}")
            ai, bi = _DOW_ORDER.index(a[:3]), _DOW_ORDER.index(b[:3])
            span = range(ai, bi + 1) if ai <= bi else \
                list(range(ai, 7)) + list(range(0, bi + 1))
            values.update(span)
        else:
            if part not in _DOW_NAMES:
                raise CalendarParseError(f"bad weekday {part!r}")
            values.add(_DOW_NAMES[part])
    return values or None


@dataclass
class SystemdCalendar:
    """A parsed ``OnCalendar=`` expression. ``None`` in any numeric slot means
    "any value" (an unconstrained ``*``)."""

    dows: Optional[Set[int]]
    years: Optional[Set[int]]
    months: Optional[Set[int]]
    days: Optional[Set[int]]
    hours: Optional[Set[int]]
    minutes: Optional[Set[int]]
    seconds: Optional[Set[int]]
    raw: str = ""

    # -- construction -------------------------------------------------------

    @classmethod
    def parse(cls, expr: str) -> "SystemdCalendar":
        original = expr
        expr = expr.strip()
        if not expr:
            raise CalendarParseError("empty OnCalendar expression")

        key = expr.lower()
        if key in _KEYWORDS:
            expr = _KEYWORDS[key]

        tokens = expr.split()

        def _looks_like_dow(tok: str) -> bool:
            return all(
                part in _DOW_NAMES
                for part in re.split(r"[,.]+", tok.lower()) if part
            )

        dow_tok = date_tok = time_tok = None
        for tok in tokens:
            if ":" in tok:
                time_tok = tok
            elif "-" in tok:
                date_tok = tok
            elif tok == "*":
                # A bare '*' with no ':' or '-' is a date wildcard.
                date_tok = tok
            elif _looks_like_dow(tok):
                dow_tok = tok
            # Any other lone token (e.g. a trailing "UTC" timezone) is ignored;
            # we schedule in local wall clock like the rest of the tool.

        dows = _parse_dow_field(dow_tok) if dow_tok else None

        if date_tok and date_tok != "*":
            date_parts = date_tok.split("-")
            if len(date_parts) == 3:
                y, m, d = date_parts
            elif len(date_parts) == 2:
                y, m, d = "*", date_parts[0], date_parts[1]
            else:
                raise CalendarParseError(f"bad date component {date_tok!r}")
            years = _parse_year_field(y)
            months = _parse_numeric_field(m, 1, 12, "month")
            days = _parse_numeric_field(d, 1, 31, "day")
        else:
            years = months = days = None

        if time_tok:
            time_parts = time_tok.split(":")
            if len(time_parts) == 2:
                h, mi, s = time_parts[0], time_parts[1], "0"
            elif len(time_parts) == 3:
                h, mi, s = time_parts
            else:
                raise CalendarParseError(f"bad time component {time_tok!r}")
            hours = _parse_numeric_field(h, 0, 23, "hour")
            minutes = _parse_numeric_field(mi, 0, 59, "minute")
            seconds = _parse_numeric_field(s, 0, 59, "second")
        else:
            # No time given: systemd defaults to 00:00:00.
            hours = {0}
            minutes = {0}
            seconds = {0}

        return cls(dows, years, months, days, hours, minutes, seconds,
                   original.strip())

    # -- matching -----------------------------------------------------------

    @staticmethod
    def _in(value: int, allowed: Optional[Set[int]]) -> bool:
        return allowed is None or value in allowed

    def _day_matches(self, dt: datetime) -> bool:
        # systemd AND semantics: both day-of-month and day-of-week must match
        # when specified.
        return self._in(dt.day, self.days) and self._in(dt.weekday(), self.dows)

    def matches(self, dt: datetime) -> bool:
        return (
            self._in(dt.year, self.years)
            and self._in(dt.month, self.months)
            and self._day_matches(dt)
            and self._in(dt.hour, self.hours)
            and self._in(dt.minute, self.minutes)
            and self._in(dt.second, self.seconds)
        )

    # -- next-run computation ----------------------------------------------

    def next_after(self, after: datetime) -> Optional[datetime]:
        dt = after.replace(microsecond=0) + timedelta(seconds=1)
        horizon = dt.replace(year=dt.year + _MAX_YEARS_AHEAD)

        while dt < horizon:
            if self.years is not None and dt.year not in self.years:
                if dt.year > max(self.years):
                    return None
                dt = datetime(dt.year + 1, 1, 1)
                continue
            if not self._in(dt.month, self.months):
                dt = _advance_month(dt)
                continue
            if not self._day_matches(dt):
                dt = _advance_day(dt)
                continue
            if not self._in(dt.hour, self.hours):
                dt = _advance_hour(dt)
                continue
            if not self._in(dt.minute, self.minutes):
                dt = _advance_minute(dt)
                continue
            if not self._in(dt.second, self.seconds):
                dt = dt + timedelta(seconds=1)
                continue
            return dt

        return None

    def next_runs(self, start: datetime, count: int) -> List[datetime]:
        out: List[datetime] = []
        cur = start
        while len(out) < count:
            nxt = self.next_after(cur)
            if nxt is None:
                break
            out.append(nxt)
            cur = nxt
        return out


def _parse_year_field(raw: str) -> Optional[Set[int]]:
    raw = raw.strip()
    if raw == "*":
        return None
    return _parse_numeric_field(raw, 1970, 2200, "year")


def _advance_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1)
    return datetime(dt.year, dt.month + 1, 1)


def _advance_day(dt: datetime) -> datetime:
    return (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                            microsecond=0)


def _advance_hour(dt: datetime) -> datetime:
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _advance_minute(dt: datetime) -> datetime:
    return (dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
