"""Source-agnostic job model shared by every parser and the TUI.

A :class:`Job` is anything that fires on a wall-clock schedule: a crontab line,
an ``/etc/cron.d`` entry, a systemd ``.timer``, or a CI cron trigger. Each Job
carries a ``schedule`` implementing :class:`Schedule` — a tiny protocol with a
single ``next_runs`` method — so the timeline and collision code never has to
care *where* a job came from.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Protocol, runtime_checkable

__all__ = ["Source", "Schedule", "Job", "parse_duration",
           "extract_duration_annotation", "strip_duration_annotation"]

# Matches a `duration=20m` / `duration: 1h30m` / `runtime ~ 90s` annotation
# inside a comment. The value is the run's estimated wall-clock length.
_ANNOTATION_RE = re.compile(
    r"(?:duration|runtime|runs?[- ]?for|takes)\s*[=:~]?\s*([0-9][0-9dhms ]*)",
    re.IGNORECASE,
)

_DURATION_RE = re.compile(
    r"(\d+)\s*(d|days?|h|hours?|hrs?|min|minutes?|m|s|sec|secs?|seconds?)",
    re.IGNORECASE,
)
_DURATION_UNITS = {
    "d": 86400, "day": 86400, "days": 86400,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "min": 60, "minute": 60, "minutes": 60, "m": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse a human duration like ``20m``, ``1h30m``, ``90s``, ``2h``.

    A bare integer is treated as seconds. Returns ``None`` if nothing parses,
    so callers can distinguish "no duration given" from a real value.
    """
    text = text.strip().lower()
    if not text:
        return None
    if text.isdigit():
        return timedelta(seconds=int(text))
    total = 0
    matched = False
    for value, unit in _DURATION_RE.findall(text):
        mult = _DURATION_UNITS.get(unit.lower())
        if mult is None:
            continue
        total += int(value) * mult
        matched = True
    return timedelta(seconds=total) if matched else None


def extract_duration_annotation(text: Optional[str]) -> Optional[timedelta]:
    """Pull a ``duration=20m`` style annotation out of a comment string.

    Recognises ``duration``, ``runtime``, ``runs for`` and ``takes`` followed by
    a duration (e.g. ``# schedviz: duration=20m``). Returns ``None`` if there is
    no such annotation.
    """
    if not text:
        return None
    m = _ANNOTATION_RE.search(text)
    if not m:
        return None
    return parse_duration(m.group(1))


def strip_duration_annotation(text: Optional[str]) -> str:
    """Remove a duration annotation (and a leading ``schedviz:`` marker) from a
    comment so it can be used as a clean display name."""
    if not text:
        return ""
    cleaned = _ANNOTATION_RE.sub("", text)
    cleaned = re.sub(r"(?i)^\s*schedviz\s*[:\-]?\s*", "", cleaned)
    # Tidy leftover separators/whitespace.
    cleaned = re.sub(r"[\s,;:\-]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


class Source(enum.Enum):
    """Where a job was discovered."""

    CRONTAB = "crontab"
    CRON_D = "cron.d"
    SYSTEMD = "systemd"
    CI = "ci"

    @property
    def label(self) -> str:
        return {
            Source.CRONTAB: "crontab",
            Source.CRON_D: "cron.d",
            Source.SYSTEMD: "systemd",
            Source.CI: "ci",
        }[self]


@runtime_checkable
class Schedule(Protocol):
    """Anything that can enumerate its own future run times."""

    def next_runs(self, start: datetime, count: int) -> List[datetime]:
        ...


@dataclass
class Job:
    """A single scheduled unit, normalised across all sources."""

    name: str
    source: Source
    schedule: Optional[Schedule]
    raw: str = ""
    command: str = ""
    source_path: str = ""
    # Human note explaining why a job has no computable schedule
    # (e.g. a monotonic systemd timer), or any parse caveat.
    note: str = ""
    tags: List[str] = field(default_factory=list)
    # Estimated run duration, if annotated. None => modelled as instantaneous
    # (a start-time point). When set, collisions use interval-overlap.
    duration: Optional[timedelta] = None

    @property
    def schedulable(self) -> bool:
        return self.schedule is not None

    def next_runs(self, start: datetime, count: int) -> List[datetime]:
        if self.schedule is None:
            return []
        return self.schedule.next_runs(start, count)

    def next_run(self, start: datetime) -> Optional[datetime]:
        runs = self.next_runs(start, 1)
        return runs[0] if runs else None
