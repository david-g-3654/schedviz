"""Parse crontab text into :class:`Job` objects.

Handles both flavours:

* **User crontabs** (``crontab -l``): ``m h dom mon dow  command``
* **System crontabs** (``/etc/crontab``, ``/etc/cron.d/*``): an extra *user*
  field sits between the schedule and the command:
  ``m h dom mon dow  user  command``

Environment-assignment lines (``SHELL=/bin/bash``) and comments are recognised
and skipped. A ``#``-comment immediately above an entry is captured as its
description.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..cronexpr import CronExpr, CronParseError
from ..models import (Job, Source, extract_duration_annotation,
                      strip_duration_annotation)

__all__ = ["parse_cron_text"]

_ENV_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=")
_MACRO_RE = re.compile(r"^@\w+")


def parse_cron_text(text: str, *, system: bool = False,
                    source_path: str = "") -> List[Job]:
    """Parse ``text`` (the contents of a crontab) into jobs.

    ``system=True`` expects the extra user field used by ``/etc/crontab`` and
    ``/etc/cron.d`` files. Unparseable schedule lines become jobs with
    ``schedule=None`` and a ``note`` explaining the error, so nothing is
    silently dropped.
    """

    jobs: List[Job] = []
    pending_comment: Optional[str] = None

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if not stripped:
            pending_comment = None
            continue
        if stripped.startswith("#"):
            pending_comment = stripped.lstrip("#").strip() or None
            continue
        if _ENV_RE.match(stripped):
            # Environment assignment — not a job.
            pending_comment = None
            continue

        src = Source.CRON_D if system else Source.CRONTAB
        job = _parse_entry(stripped, system=system, source_path=source_path,
                           comment=pending_comment, lineno=lineno, source=src)
        if job is not None:
            jobs.append(job)
        pending_comment = None

    return jobs


def _split_schedule(stripped: str, *, system: bool):
    """Return ``(schedule_expr, remainder)`` or raise CronParseError.

    ``remainder`` still contains the (optional) user field and the command.
    """
    if _MACRO_RE.match(stripped):
        parts = stripped.split(None, 1)
        expr = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""
        return expr, remainder

    # 5 whitespace-separated schedule fields, then the rest.
    parts = stripped.split(None, 5)
    if len(parts) < 6:
        raise CronParseError("not enough fields for a cron entry")
    expr = " ".join(parts[:5])
    remainder = parts[5]
    return expr, remainder


def _parse_entry(stripped: str, *, system: bool, source_path: str,
                 comment: Optional[str], lineno: int, source: Source
                 ) -> Optional[Job]:
    try:
        expr_str, remainder = _split_schedule(stripped, system=system)
    except CronParseError as exc:
        return Job(
            name=stripped[:60],
            source=source,
            schedule=None,
            raw=stripped,
            source_path=source_path,
            note=f"unparsed cron line {lineno}: {exc}",
        )

    user = ""
    command = remainder
    if system:
        # First token of the remainder is the user the job runs as.
        bits = remainder.split(None, 1)
        user = bits[0] if bits else ""
        command = bits[1] if len(bits) > 1 else ""

    try:
        schedule = CronExpr.parse(expr_str)
        note = ""
    except CronParseError as exc:
        schedule = None
        note = f"unparsed schedule {expr_str!r}: {exc}"

    duration = extract_duration_annotation(comment)
    clean_comment = strip_duration_annotation(comment) if comment else ""
    name = clean_comment or (command.strip() or stripped)[:60]
    tags = [f"user:{user}"] if user else []

    return Job(
        name=name,
        source=source,
        schedule=schedule,
        raw=stripped,
        command=command.strip(),
        source_path=source_path,
        note=note,
        tags=tags,
        duration=duration,
    )
