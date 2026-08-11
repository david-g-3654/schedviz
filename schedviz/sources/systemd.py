"""Parse systemd ``.timer`` units into :class:`Job` objects.

A ``.timer`` file is INI-like. We read the ``[Timer]`` section, collect every
``OnCalendar=`` line (a timer may have several, and it fires on the *union*),
and note monotonic triggers (``OnBootSec=`` / ``OnUnitActiveSec=``) which have
no wall-clock schedule and so cannot be placed on the timeline.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional

from ..models import Job, Source, extract_duration_annotation
from ..systemd_calendar import CalendarParseError, SystemdCalendar

__all__ = ["parse_timer_unit", "parse_timer_dir"]


class _UnionSchedule:
    """Merge several :class:`SystemdCalendar` schedules; fires when any does."""

    def __init__(self, calendars: List[SystemdCalendar]):
        self._calendars = calendars

    def next_runs(self, start: datetime, count: int) -> List[datetime]:
        merged: List[datetime] = []
        seen = set()
        cur = start
        # Repeatedly take the earliest next-run across all calendars.
        while len(merged) < count:
            candidates = [c.next_after(cur) for c in self._calendars]
            candidates = [c for c in candidates if c is not None]
            if not candidates:
                break
            nxt = min(candidates)
            if nxt not in seen:
                seen.add(nxt)
                merged.append(nxt)
            cur = nxt
        return merged


def _parse_ini_timer(text: str):
    """Return ``(on_calendar_values, monotonic_keys, unit_override)``."""
    section = None
    on_calendar: List[str] = []
    monotonic: List[str] = []
    unit_override: Optional[str] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if section == "timer":
            if key.lower() == "oncalendar":
                if value:  # an empty "OnCalendar=" resets the list in systemd
                    on_calendar.append(value)
                else:
                    on_calendar.clear()
            elif key.lower() in ("onbootsec", "onunitactivesec",
                                 "onstartupsec", "onactivesec",
                                 "onunitinactivesec"):
                monotonic.append(key)
            elif key.lower() == "unit":
                unit_override = value
        elif section == "unit":
            pass

    return on_calendar, monotonic, unit_override


def parse_timer_unit(text: str, *, name: str = "",
                     source_path: str = "") -> Job:
    """Parse one ``.timer`` unit's text into a :class:`Job`."""

    on_calendar, monotonic, unit_override = _parse_ini_timer(text)

    unit_name = name or (os.path.basename(source_path) if source_path else "timer")
    if unit_name.endswith(".timer"):
        unit_name = unit_name[: -len(".timer")]

    triggered = unit_override or f"{unit_name}.service"

    calendars: List[SystemdCalendar] = []
    parse_notes: List[str] = []
    for expr in on_calendar:
        try:
            calendars.append(SystemdCalendar.parse(expr))
        except CalendarParseError as exc:
            parse_notes.append(f"OnCalendar={expr!r}: {exc}")

    tags = [f"triggers:{triggered}"]

    if calendars:
        schedule: Optional[_UnionSchedule] = (
            calendars[0] if len(calendars) == 1 else _UnionSchedule(calendars)
        )
        note = "; ".join(parse_notes)
    else:
        schedule = None
        if monotonic:
            note = (
                "monotonic timer (" + ", ".join(sorted(set(monotonic)))
                + ") — no wall-clock schedule"
            )
        else:
            note = "; ".join(parse_notes) or "no OnCalendar= found"

    return Job(
        name=unit_name,
        source=Source.SYSTEMD,
        schedule=schedule,
        raw="; ".join(on_calendar) if on_calendar else text.strip()[:120],
        command=triggered,
        source_path=source_path,
        note=note,
        tags=tags,
        duration=extract_duration_annotation(text),
    )


def parse_timer_dir(path: str) -> List[Job]:
    """Parse every ``*.timer`` file directly under ``path``."""
    jobs: List[Job] = []
    if not os.path.isdir(path):
        return jobs
    for entry in sorted(os.listdir(path)):
        if not entry.endswith(".timer"):
            continue
        full = os.path.join(path, entry)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        jobs.append(parse_timer_unit(text, name=entry, source_path=full))
    return jobs
