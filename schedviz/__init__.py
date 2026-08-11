"""schedviz — a terminal timeline for all your scheduled jobs at once.

Public API surface for programmatic use; the TUI and CLI build on top of these.
"""

from .cronexpr import CronExpr, CronParseError
from .systemd_calendar import SystemdCalendar, CalendarParseError
from .models import Job, Source, Schedule
from .schedule import Run, Collision, compute_runs, upcoming_runs, find_collisions
from .explain import describe_job, describe_cron, humanize_delta, format_run

__version__ = "0.1.0"

__all__ = [
    "CronExpr", "CronParseError",
    "SystemdCalendar", "CalendarParseError",
    "Job", "Source", "Schedule",
    "Run", "Collision", "compute_runs", "upcoming_runs", "find_collisions",
    "describe_job", "describe_cron", "humanize_delta", "format_run",
    "__version__",
]
