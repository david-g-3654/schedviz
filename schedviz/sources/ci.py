"""Extract cron schedules from CI config files.

Primarily GitHub Actions (``.github/workflows/*.yml`` — ``on.schedule[].cron``),
but the extraction is a light-touch regex scan for ``cron:`` keys, so GitLab
``pipeline schedules`` YAML and similar formats come along for free.

**Timezone:** GitHub Actions cron runs in **UTC**. To place CI jobs on the same
wall-clock timeline as local cron/systemd jobs, we compute their runs in UTC and
shift by the local UTC offset. This is exact except across DST boundaries, where
it can be off by an hour — good enough for spotting collisions, and the offset
used is recorded in the job note.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import List, Optional

from ..cronexpr import CronExpr, CronParseError
from ..models import Job, Source, extract_duration_annotation

__all__ = ["parse_ci_file", "local_utc_offset"]

# Matches a `cron:` value, quoted or bare, capturing any trailing `# comment`
# (which may carry a `duration=` annotation).
_CRON_RE = re.compile(
    r"""cron\s*:\s*(?:(['"])(?P<q>[^'"]+)\1|(?P<bare>[^#\n]+?))"""
    r"""\s*(?:\#(?P<comment>.*))?$""",
    re.MULTILINE,
)


def local_utc_offset() -> timedelta:
    """The current local offset from UTC (local = UTC + offset)."""
    off = datetime.now().astimezone().utcoffset()
    return off or timedelta(0)


class _OffsetSchedule:
    """Wrap a cron schedule expressed in some base timezone and present its runs
    in local wall-clock time by applying a fixed offset."""

    def __init__(self, expr: CronExpr, offset: timedelta):
        self._expr = expr
        self._offset = offset

    def next_runs(self, start: datetime, count: int) -> List[datetime]:
        base_start = start - self._offset
        runs = self._expr.next_runs(base_start, count)
        return [r + self._offset for r in runs]


def parse_ci_file(text: str, *, source_path: str = "",
                  offset: Optional[timedelta] = None) -> List[Job]:
    """Parse every ``cron:`` schedule found in a CI config file's ``text``."""

    if offset is None:
        offset = local_utc_offset()

    workflow = _guess_workflow_name(text, source_path)
    jobs: List[Job] = []

    for idx, match in enumerate(_CRON_RE.finditer(text), start=1):
        expr_str = (match.group("q") or match.group("bare") or "").strip()
        if not expr_str:
            continue

        off_hours = offset.total_seconds() / 3600.0
        offset_note = f"UTC schedule, shown at local UTC{off_hours:+g}h"

        try:
            expr = CronExpr.parse(expr_str)
            schedule: Optional[_OffsetSchedule] = _OffsetSchedule(expr, offset)
            note = offset_note
        except CronParseError as exc:
            schedule = None
            note = f"unparsed cron {expr_str!r}: {exc}"

        name = workflow if idx == 1 else f"{workflow} #{idx}"
        duration = extract_duration_annotation(match.group("comment"))
        jobs.append(Job(
            name=name,
            source=Source.CI,
            schedule=schedule,
            raw=expr_str,
            command=workflow,
            source_path=source_path,
            note=note,
            tags=["tz:UTC"],
            duration=duration,
        ))

    return jobs


def _guess_workflow_name(text: str, source_path: str) -> str:
    m = re.search(r"^\s*name\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"")
    if source_path:
        base = os.path.basename(source_path)
        return os.path.splitext(base)[0]
    return "ci-schedule"
