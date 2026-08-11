"""Command-line entry point for schedviz.

    schedviz                 auto-discover jobs and open the timeline TUI
    schedviz FILE...         parse specific crontab / .timer / CI files
    schedviz --demo          open the built-in demo scenario
    schedviz --list          print the merged "next N runs" (no TUI)
    schedviz --collisions    print detected collisions only
    schedviz --explain EXPR  describe one cron / OnCalendar expression
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from typing import List, Optional

from . import __version__
from .explain import describe_cron, describe_job, format_run
from .models import Job
from .schedule import analyze, upcoming_runs

_WINDOWS = {
    "1h": timedelta(hours=1), "6h": timedelta(hours=6),
    "12h": timedelta(hours=12), "24h": timedelta(hours=24),
    "48h": timedelta(hours=48), "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

_FUZZ = {
    "exact": timedelta(0), "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schedviz",
        description="A terminal timeline for all your scheduled jobs at once — "
                    "cron, /etc/cron.d, systemd timers, and CI cron.",
    )
    p.add_argument("paths", nargs="*",
                   help="crontab / .timer / CI files or dirs to parse "
                        "(default: auto-discover the local system)")
    p.add_argument("--demo", action="store_true",
                   help="open the built-in demo scenario (no system access)")
    p.add_argument("-l", "--list", action="store_true",
                   help="print the merged next-N runs and exit (no TUI)")
    p.add_argument("-c", "--collisions", action="store_true",
                   help="print detected collisions and exit (no TUI)")
    p.add_argument("--explain", metavar="EXPR",
                   help="describe a single cron or OnCalendar expression")
    p.add_argument("-n", "--next", type=int, default=20, metavar="N",
                   help="how many upcoming runs to list (default: 20)")
    p.add_argument("--window", choices=list(_WINDOWS), default="24h",
                   help="timeline / collision window (default: 24h)")
    p.add_argument("--fuzz", choices=list(_FUZZ), default="exact",
                   help="treat runs within this window as colliding "
                        "(default: exact)")
    p.add_argument("--all-collisions", action="store_true",
                   help="include high-frequency jobs (pings, scrapes) in "
                        "collision detection instead of ignoring them")
    p.add_argument("--top", type=int, default=15, metavar="N",
                   help="max collisions to print in --collisions mode "
                        "(default: 15; 0 = all)")
    p.add_argument("--assume-duration", metavar="DUR",
                   help="assume this run duration (e.g. 20m, 1h30m) for jobs "
                        "with no duration annotation, so overlaps — not just "
                        "same-minute starts — count as collisions")
    p.add_argument("--no-user", action="store_true",
                   help="skip the invoking user's crontab during auto-discovery")
    p.add_argument("--version", action="version",
                   version=f"schedviz {__version__}")
    return p


def _load_jobs(args) -> List[Job]:
    from .sources import discover_jobs
    if args.demo:
        from .demo import demo_jobs
        from .sources.ci import local_utc_offset
        return demo_jobs(ci_offset=local_utc_offset())
    return discover_jobs(
        args.paths or None,
        include_user_crontab=not args.no_user,
    )


def _print_explain(expr: str) -> int:
    from .cronexpr import CronExpr, CronParseError
    from .systemd_calendar import CalendarParseError, SystemdCalendar

    now = datetime.now().replace(second=0, microsecond=0)
    # Try cron first, then systemd OnCalendar.
    try:
        c = CronExpr.parse(expr)
        print(f"cron:  {expr}")
        print(f"  → {describe_cron(expr)}")
        runs = c.next_runs(now, 5)
    except CronParseError:
        try:
            s = SystemdCalendar.parse(expr)
            print(f"OnCalendar:  {expr}")
            runs = s.next_runs(now, 5)
        except CalendarParseError as exc:
            print(f"could not parse {expr!r} as cron or OnCalendar: {exc}",
                  file=sys.stderr)
            return 2
    print("  next runs:")
    for r in runs:
        print(f"    {format_run(now, r)}")
    return 0


def _print_list(jobs: List[Job], count: int) -> int:
    now = datetime.now().replace(second=0, microsecond=0)
    runs = upcoming_runs(jobs, now, count)
    n_sched = sum(1 for j in jobs if j.schedule is not None)
    print(f"{n_sched} schedulable job(s); next {len(runs)} runs:\n")
    for r in runs:
        print(f"  {format_run(now, r.time)}  {r.job.name}")
        print(f"      {r.job.source.label}: {describe_job(r.job)}")
    unsched = [j for j in jobs if j.schedule is None]
    if unsched:
        print("\nno wall-clock schedule:")
        for j in unsched:
            print(f"  · {j.name} — {j.note}")
    return 0


def _print_collisions(jobs: List[Job], window: timedelta,
                      threshold: timedelta, *, include_frequent: bool,
                      top: int) -> int:
    now = datetime.now().replace(second=0, microsecond=0)
    max_per_day = None if include_frequent else 6.0
    _, collisions = analyze(jobs, now, window, threshold,
                            max_runs_per_day=max_per_day)
    if not collisions:
        print(f"No collisions in the next {_fmt_td(window)}. ✓")
        return 0

    shown = collisions if top <= 0 else collisions[:top]
    print(f"{len(collisions)} collision(s) in the next {_fmt_td(window)}"
          f" (most notable first):\n")
    for col in shown:
        names = ", ".join(j.name for j in col.jobs)
        when = f"{col.start:%a %d %b %H:%M}"
        if col.end != col.start:
            when += f"–{col.end:%H:%M}"
        print(f"  ⚠  {when}  ({col.size} jobs)")
        print(f"       {names}")
    if len(collisions) > len(shown):
        print(f"\n  … and {len(collisions) - len(shown)} more "
              f"(use --top 0 to show all).")
    if not include_frequent:
        print("\n  (high-frequency jobs excluded; --all-collisions to include)")
    return 1  # non-zero so CI can gate on "no new collisions"


def _fmt_td(td: timedelta) -> str:
    hours = td.total_seconds() / 3600
    if hours >= 24 and hours % 24 == 0:
        return f"{int(hours // 24)}d"
    return f"{int(hours)}h"


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.explain:
        return _print_explain(args.explain)

    jobs = _load_jobs(args)
    window = _WINDOWS[args.window]
    threshold = _FUZZ[args.fuzz]

    if args.assume_duration:
        from .models import parse_duration
        assumed = parse_duration(args.assume_duration)
        if assumed is None:
            print(f"could not parse --assume-duration {args.assume_duration!r}",
                  file=sys.stderr)
            return 2
        for j in jobs:
            if j.duration is None:
                j.duration = assumed

    if not jobs and not args.list and not args.collisions:
        print("No scheduled jobs found. Try `schedviz --demo`, or pass a "
              "crontab / .timer / CI file explicitly.", file=sys.stderr)
        return 1

    if args.collisions:
        return _print_collisions(jobs, window, threshold,
                                 include_frequent=args.all_collisions,
                                 top=args.top)
    if args.list:
        return _print_list(jobs, args.next)

    # Default: launch the TUI.
    from .tui import SchedvizApp
    app = SchedvizApp(jobs, window=window)
    app.threshold = threshold
    app.fuzz_index = list(_FUZZ).index(args.fuzz)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
