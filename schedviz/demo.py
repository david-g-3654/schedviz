"""A deterministic demo job set — the r/selfhosted scenario from the pitch.

Fourteen cron entries, three systemd timers, two CI schedules, with two backups
that both fire at 03:00 so the collision is visible the moment the TUI opens.
Used by ``schedviz --demo`` and by the screenshot/GIF tooling.
"""

from __future__ import annotations

from datetime import timedelta
from typing import List

from .cronexpr import CronExpr
from .models import Job, Source, parse_duration
from .sources.ci import _OffsetSchedule

# Estimated run durations for a few heavy jobs, so their timeline bars have
# width and interval-overlap collisions (not just same-minute starts) show up.
_DURATIONS = {
    "nightly borg backup": "25m",
    "restic backup to b2": "18m",
    "db dump": "10m",
    "reindex search": "30m",
    "prune docker images": "8m",
}

_CRON = [
    ("nightly borg backup", "0 3 * * *", "/usr/bin/borg create ::daily"),
    ("restic backup to b2", "0 3 * * *", "/usr/local/bin/restic backup /data"),
    ("renew certificates", "30 2 * * *", "certbot renew"),
    ("prune docker images", "0 4 * * 0", "docker system prune -af"),
    ("update dyndns", "*/5 * * * *", "curl https://dyndns/update"),
    ("sync photos", "15 */6 * * *", "rclone sync ~/photos remote:"),
    ("db dump", "0 1 * * *", "pg_dumpall > /backup/db.sql"),
    ("healthcheck ping", "*/2 * * * *", "curl https://hc-ping.com/xyz"),
    ("log rotate", "0 0 * * *", "logrotate /etc/logrotate.conf"),
    ("scrape metrics", "*/15 * * * *", "python scrape.py"),
    ("weekly report", "0 8 * * 1", "python report.py --weekly"),
    ("clean tmp", "30 3 * * *", "find /tmp -mtime +7 -delete"),
    ("reindex search", "0 5 1 * *", "python reindex.py"),
    ("check disk space", "0 */4 * * *", "df -h | mail admin"),
]

_TIMERS = [
    ("apt-daily", "*-*-* 06:00:00", "apt-daily.service"),
    ("fstrim", "Mon *-*-* 00:00:00", "fstrim.service"),
    ("man-db", "*-*-* 03:00:00", "man-db.service"),  # also collides at 03:00
]

_CI = [
    ("Nightly CI", "0 3 * * *"),     # UTC — offset applied
    ("Weekly deploy", "0 12 * * 1"),
]


def demo_jobs(ci_offset: timedelta = timedelta(0)) -> List[Job]:
    jobs: List[Job] = []

    for name, expr, cmd in _CRON:
        jobs.append(Job(name=name, source=Source.CRONTAB,
                        schedule=CronExpr.parse(expr), raw=expr, command=cmd,
                        source_path="crontab -l",
                        duration=parse_duration(_DURATIONS.get(name, ""))))

    from .systemd_calendar import SystemdCalendar
    for name, cal, svc in _TIMERS:
        jobs.append(Job(name=name, source=Source.SYSTEMD,
                        schedule=SystemdCalendar.parse(cal), raw=cal,
                        command=svc,
                        source_path=f"/etc/systemd/system/{name}.timer",
                        tags=[f"triggers:{svc}"]))

    # A monotonic timer, to show the "no wall-clock schedule" handling.
    jobs.append(Job(name="watchdog", source=Source.SYSTEMD, schedule=None,
                    raw="OnUnitActiveSec=5min",
                    note="monotonic timer (OnUnitActiveSec) — no wall-clock schedule",
                    source_path="/etc/systemd/system/watchdog.timer"))

    for name, expr in _CI:
        jobs.append(Job(name=name, source=Source.CI,
                        schedule=_OffsetSchedule(CronExpr.parse(expr), ci_offset),
                        raw=expr, command=name,
                        source_path=".github/workflows/ci.yml",
                        tags=["tz:UTC"]))

    return jobs
