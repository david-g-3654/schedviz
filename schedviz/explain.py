"""Plain-English descriptions of schedules and human-friendly time deltas.

This is the "crontab.guru for all your jobs" part: turn a cron/systemd schedule
into a sentence, and turn a future timestamp into "in 3 hours".

The cron describer covers the common homelab shapes precisely (fixed times,
``*/N`` steps, weekday and day-of-month restrictions, month restrictions) and
degrades to an accurate-if-clunky field-by-field phrasing for exotic
expressions rather than lying.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from .cronexpr import CronExpr, CronParseError
from .models import Job, Source
from .systemd_calendar import SystemdCalendar

__all__ = ["describe_job", "describe_cron", "humanize_delta", "format_run"]

_DOW_FULL = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday"]
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 21: "21st", 22: "22nd", 23: "23rd",
             31: "31st"}


def _ordinal(n: int) -> str:
    return _ORDINALS.get(n, f"{n}th")


def _field_tokens(raw: str) -> str:
    return raw.strip()


def _is_star(tok: str) -> bool:
    return tok.strip() == "*"


def _step_of(tok: str) -> Optional[int]:
    """If ``tok`` is ``*/N`` or ``a-b/N``, return N, else None."""
    tok = tok.strip()
    if "/" in tok:
        base, _, step = tok.partition("/")
        if step.strip().isdigit():
            return int(step)
    return None


def _single_int(tok: str) -> Optional[int]:
    tok = tok.strip()
    return int(tok) if tok.isdigit() else None


def _list_ints(tok: str) -> Optional[List[int]]:
    tok = tok.strip()
    if all(p.strip().isdigit() for p in tok.split(",")) and tok:
        return [int(p) for p in tok.split(",")]
    return None


def _describe_time(minute: str, hour: str) -> str:
    m_val = _single_int(minute)
    h_val = _single_int(hour)
    m_step = _step_of(minute)
    h_step = _step_of(hour)

    # Every N minutes (optionally confined to a span of hours)
    if m_step:
        if _is_star(hour):
            return f"every {m_step} minutes"
        return f"every {m_step} minutes {_describe_hour_span(hour)}"
    if _is_star(minute) and _is_star(hour):
        return "every minute"
    # Fixed HH:MM
    if m_val is not None and h_val is not None:
        return f"at {h_val:02d}:{m_val:02d}"
    # N past every hour
    if m_val is not None and _is_star(hour):
        return f"at {m_val} minute{'s' if m_val != 1 else ''} past every hour"
    # Every N hours at fixed minute
    if m_val is not None and h_step:
        return f"at {m_val:02d} minutes past every {h_step}th hour"
    # Fixed minute, list of hours
    hours = _list_ints(hour)
    if m_val is not None and hours is not None:
        times = ", ".join(f"{h:02d}:{m_val:02d}" for h in hours)
        return f"at {times}"
    # Fallback
    parts = []
    parts.append(f"minute {minute}" if not _is_star(minute) else "every minute")
    if not _is_star(hour):
        parts.append(f"hour {hour}")
    return " ".join(parts)


def _describe_hour_span(hour: str) -> str:
    """A phrase for the hour field when qualifying a per-minute schedule,
    e.g. "between 09:00 and 17:59" or "in the 03:00 hour"."""
    hour = hour.strip()
    h_val = _single_int(hour)
    if h_val is not None:
        return f"in the {h_val:02d}:00 hour"
    if "-" in hour and "/" not in hour:
        a, _, b = hour.partition("-")
        if a.isdigit() and b.isdigit():
            return f"between {int(a):02d}:00 and {int(b):02d}:59"
    hours = _list_ints(hour)
    if hours is not None:
        return "during hours " + ", ".join(f"{h:02d}:00" for h in hours)
    return f"(hours {hour})"


def _describe_day(dom: str, month: str, dow: str) -> str:
    clauses: List[str] = []

    dom_star = _is_star(dom)
    dow_star = _is_star(dow)

    if not dow_star:
        clauses.append("on " + _describe_dow(dow))

    if not dom_star:
        dom_step = _step_of(dom)
        dom_val = _single_int(dom)
        doms = _list_ints(dom)
        if dom_step:
            clause = f"every {_ordinal(dom_step)} day of the month"
        elif dom_val is not None:
            clause = f"on the {_ordinal(dom_val)}"
        elif doms is not None:
            clause = "on the " + ", ".join(_ordinal(d) for d in doms)
        else:
            clause = f"on day-of-month {dom}"
        if not dow_star:
            # Vixie OR semantics when both restricted.
            clauses.append("or " + clause)
        else:
            clauses.append(clause)

    if not _is_star(month):
        months = _list_ints(month)
        m_val = _single_int(month)
        if m_val is not None:
            clauses.append("in " + _MONTHS[m_val])
        elif months is not None:
            clauses.append("in " + ", ".join(_MONTHS[m] for m in months))
        else:
            clauses.append(f"in month {month}")

    if not clauses:
        return "every day"
    return " ".join(clauses)


def _describe_dow(dow: str) -> str:
    dow = dow.strip()
    ints = _list_ints(dow)
    if ints is not None:
        names = [_DOW_FULL[i % 7] for i in ints]
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]
    if "-" in dow and "/" not in dow:
        a, _, b = dow.partition("-")
        if a.isdigit() and b.isdigit():
            return f"{_DOW_FULL[int(a) % 7]} through {_DOW_FULL[int(b) % 7]}"
    return f"weekday {dow}"


def describe_cron(expr_str: str) -> str:
    """Describe a raw cron expression string in plain English."""
    try:
        # Validate/normalise (also expands macros).
        CronExpr.parse(expr_str)
    except CronParseError as exc:
        return f"(unparseable: {exc})"

    raw = expr_str.strip()
    macros = {
        "@yearly": "at 00:00 on the 1st of January",
        "@annually": "at 00:00 on the 1st of January",
        "@monthly": "at 00:00 on the 1st of every month",
        "@weekly": "at 00:00 on Sunday",
        "@daily": "at 00:00 every day",
        "@midnight": "at 00:00 every day",
        "@hourly": "at minute 0 of every hour",
    }
    if raw.lower() in macros:
        return macros[raw.lower()]

    fields = raw.split()
    if len(fields) == 6:
        fields = fields[1:]
    if len(fields) != 5:
        return raw

    minute, hour, dom, month, dow = fields
    time_part = _describe_time(minute, hour)
    day_part = _describe_day(dom, month, dow)
    return f"{time_part}, {day_part}"


def describe_systemd(cal_raw: str) -> str:
    """Describe a systemd OnCalendar expression (best-effort)."""
    return f"OnCalendar: {cal_raw}"


def describe_job(job: Job) -> str:
    """Plain-English description of a job's schedule."""
    if job.schedule is None:
        return job.note or "no computable schedule"
    if job.source == Source.SYSTEMD:
        return describe_systemd(job.raw)
    # cron / cron.d / ci all carry a cron string in ``raw``.
    return describe_cron(job.raw)


# -- time humanisation ------------------------------------------------------

def humanize_delta(now: datetime, when: datetime) -> str:
    """"in 5 minutes", "in 2 hours", "3 days ago"."""
    delta = when - now
    past = delta.total_seconds() < 0
    secs = abs(delta.total_seconds())

    if secs < 60:
        val, unit = int(secs), "second"
    elif secs < 3600:
        val, unit = int(secs // 60), "minute"
    elif secs < 86400:
        val, unit = int(secs // 3600), "hour"
    elif secs < 86400 * 7:
        val, unit = int(secs // 86400), "day"
    else:
        val, unit = int(secs // (86400 * 7)), "week"

    plural = "" if val == 1 else "s"
    phrase = f"{val} {unit}{plural}"
    return f"{phrase} ago" if past else f"in {phrase}"


def format_run(now: datetime, when: datetime) -> str:
    """"Wed 13 Aug 03:00  (in 4 hours)" — the side-panel line format."""
    stamp = when.strftime("%a %d %b %H:%M")
    return f"{stamp}  ({humanize_delta(now, when)})"
