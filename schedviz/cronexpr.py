"""Deterministic cron-expression engine.

Parses a 5-field cron expression (the classic Vixie/POSIX form, plus the common
``@daily``-style macros and a few forgiving extensions) and computes the next
run times after a given wall-clock ``datetime``.

Design notes
------------
* Everything works on **naive** ``datetime`` objects interpreted as local wall
  clock, which is exactly how ``cron`` itself schedules jobs. This keeps the
  engine deterministic and trivially testable — no timezone/DST ambiguity leaks
  into the unit tests.
* ``next_after`` steps by *field* (jump to the next matching month, then day,
  then hour, then minute) rather than minute-by-minute, so sparse schedules like
  ``0 3 1 1 *`` (once a year) resolve in microseconds.
* Day-of-month vs day-of-week follows the standard Vixie rule: when **both**
  fields are restricted, a run fires if *either* matches; when only one is
  restricted, only that one applies.

This module has **no third-party dependencies** on purpose — it is the piece the
whole product's correctness rests on, and the test suite exercises it directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, List, Optional, Set

__all__ = ["CronExpr", "CronParseError"]


class CronParseError(ValueError):
    """Raised when a cron expression cannot be parsed."""


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

# name -> canonical 5-field expression
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# A search this far ahead without a match means the expression never fires
# (e.g. Feb 30). Cron's own horizon; we mirror it.
_MAX_YEARS_AHEAD = 8


@dataclass(frozen=True)
class _Field:
    """A parsed cron field: the set of values it matches, and whether it was
    restricted (i.e. not a bare ``*``)."""

    values: frozenset
    restricted: bool

    def matches(self, value: int) -> bool:
        return value in self.values


def _parse_field(raw: str, lo: int, hi: int, names: Optional[dict] = None,
                 field_name: str = "field") -> _Field:
    """Parse one cron field into the set of integers it matches within
    ``[lo, hi]``. Supports ``*``, ``a-b``, ``a-b/step``, ``*/step``, ``a,b,c``
    combinations, and symbolic names (Jan, Mon, ...)."""

    raw = raw.strip()
    if raw == "":
        raise CronParseError(f"empty {field_name}")

    restricted = raw != "*"
    values: Set[int] = set()

    def resolve(token: str) -> int:
        token = token.strip().lower()
        if names and token in names:
            return names[token]
        if not re.fullmatch(r"\d+", token):
            raise CronParseError(f"bad value {token!r} in {field_name}")
        return int(token)

    for part in raw.split(","):
        part = part.strip()
        if part == "":
            raise CronParseError(f"empty item in {field_name}")

        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            if not re.fullmatch(r"\d+", step_str.strip()):
                raise CronParseError(f"bad step {step_str!r} in {field_name}")
            step = int(step_str)
            if step <= 0:
                raise CronParseError(f"step must be > 0 in {field_name}")
        else:
            base = part

        base = base.strip()
        if base == "*":
            start, end = lo, hi
        elif "-" in base and not (base.startswith("-")):
            a, _, b = base.partition("-")
            start, end = resolve(a), resolve(b)
        else:
            start = resolve(base)
            # ``a/step`` (no range) means "from a to the max, every step"
            end = hi if "/" in part else start

        if start < lo or end > hi:
            raise CronParseError(
                f"value out of range {lo}-{hi} in {field_name}: {part!r}"
            )
        if start > end:
            raise CronParseError(f"reversed range in {field_name}: {part!r}")

        values.update(range(start, end + 1, step))

    return _Field(frozenset(values), restricted)


class CronExpr:
    """A parsed cron expression.

    Construct with :meth:`parse` (which accepts macros and symbolic names) and
    query with :meth:`next_after` / :meth:`iter_runs`.
    """

    __slots__ = ("minute", "hour", "dom", "month", "dow", "raw")

    def __init__(self, minute: _Field, hour: _Field, dom: _Field,
                 month: _Field, dow: _Field, raw: str):
        self.minute = minute
        self.hour = hour
        self.dom = dom
        self.month = month
        self.dow = dow
        self.raw = raw

    # -- construction -------------------------------------------------------

    @classmethod
    def parse(cls, expr: str) -> "CronExpr":
        original = expr
        expr = expr.strip()
        if not expr:
            raise CronParseError("empty expression")

        if expr.startswith("@"):
            key = expr.lower()
            if key == "@reboot":
                raise CronParseError("@reboot has no wall-clock schedule")
            if key not in _MACROS:
                raise CronParseError(f"unknown macro {expr!r}")
            expr = _MACROS[key]

        fields = expr.split()
        if len(fields) == 6:
            # Some crons (and lots of examples online) use a leading seconds
            # field. We don't model sub-minute resolution, so we require the
            # seconds field to be a plain 0/*/single value and drop it.
            fields = fields[1:]
        if len(fields) != 5:
            raise CronParseError(
                f"expected 5 fields, got {len(fields)}: {original!r}"
            )

        minute = _parse_field(fields[0], 0, 59, field_name="minute")
        hour = _parse_field(fields[1], 0, 23, field_name="hour")
        dom = _parse_field(fields[2], 1, 31, field_name="day-of-month")
        month = _parse_field(fields[3], 1, 12, _MONTH_NAMES, "month")
        dow = _parse_dow(fields[4])

        return cls(minute, hour, dom, month, dow, original.strip())

    # -- matching -----------------------------------------------------------

    def _day_matches(self, dt: datetime) -> bool:
        # cron dow: 0=Sunday..6=Saturday. Python weekday(): 0=Monday..6=Sunday.
        py = dt.weekday()
        cron_dow = (py + 1) % 7
        dom_ok = self.dom.matches(dt.day)
        dow_ok = self.dow.matches(cron_dow)

        if self.dom.restricted and self.dow.restricted:
            return dom_ok or dow_ok
        if self.dom.restricted:
            return dom_ok
        if self.dow.restricted:
            return dow_ok
        return True

    def matches(self, dt: datetime) -> bool:
        """True if a run fires exactly at ``dt`` (to the minute)."""
        return (
            self.minute.matches(dt.minute)
            and self.hour.matches(dt.hour)
            and self.month.matches(dt.month)
            and self._day_matches(dt)
        )

    # -- next-run computation ----------------------------------------------

    def next_after(self, after: datetime) -> Optional[datetime]:
        """Return the first run strictly after ``after`` (minute resolution),
        or ``None`` if the expression can never fire (e.g. ``0 0 30 2 *``)."""

        # Start at the next whole minute after ``after``.
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        horizon = dt.replace(year=dt.year + _MAX_YEARS_AHEAD)

        while dt < horizon:
            if not self.month.matches(dt.month):
                dt = _advance_month(dt)
                continue
            if not self._day_matches(dt):
                dt = _advance_day(dt)
                continue
            if not self.hour.matches(dt.hour):
                dt = _advance_hour(dt)
                continue
            if not self.minute.matches(dt.minute):
                dt = _advance_minute(dt)
                continue
            return dt

        return None

    def iter_runs(self, start: datetime) -> Iterator[datetime]:
        """Yield successive runs strictly after ``start`` (infinite generator;
        stops silently if the expression can never fire again)."""
        cur = start
        while True:
            nxt = self.next_after(cur)
            if nxt is None:
                return
            yield nxt
            cur = nxt

    def next_runs(self, start: datetime, count: int) -> List[datetime]:
        """Return up to ``count`` runs strictly after ``start``."""
        out: List[datetime] = []
        for run in self.iter_runs(start):
            out.append(run)
            if len(out) >= count:
                break
        return out

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CronExpr({self.raw!r})"


def _parse_dow(raw: str) -> _Field:
    """Day-of-week is special: it accepts 0-7 where both 0 and 7 mean Sunday.
    We normalise everything to 0-6 (0=Sunday)."""
    field = _parse_field(raw, 0, 7, _DOW_NAMES, "day-of-week")
    normalised = frozenset((v % 7) for v in field.values)
    return _Field(normalised, field.restricted)


# -- field-stepping helpers (jump to the next candidate cheaply) -----------

def _advance_month(dt: datetime) -> datetime:
    """Jump to 00:00 on day 1 of the next month."""
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1)
    return datetime(dt.year, dt.month + 1, 1)


def _advance_day(dt: datetime) -> datetime:
    """Jump to 00:00 of the next day."""
    return (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)


def _advance_hour(dt: datetime) -> datetime:
    """Jump to :00 of the next hour."""
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _advance_minute(dt: datetime) -> datetime:
    return dt + timedelta(minutes=1)
