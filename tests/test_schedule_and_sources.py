from datetime import datetime, timedelta

from schedviz.models import Job, Source
from schedviz.cronexpr import CronExpr
from schedviz.schedule import (compute_runs, upcoming_runs, find_collisions,
                               analyze, job_frequencies)
from schedviz.sources.cron import parse_cron_text
from schedviz.sources.systemd import parse_timer_unit
from schedviz.sources.ci import parse_ci_file
from schedviz.explain import describe_cron, humanize_delta


def cron_job(name, expr):
    return Job(name=name, source=Source.CRONTAB,
               schedule=CronExpr.parse(expr), raw=expr)


def cron_job_dur(name, expr, duration):
    from schedviz.models import parse_duration
    return Job(name=name, source=Source.CRONTAB,
               schedule=CronExpr.parse(expr), raw=expr,
               duration=parse_duration(duration))


def test_no_overlap_without_duration():
    # 03:00 and 03:05 do NOT collide when jobs are instantaneous.
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [cron_job("a", "0 3 * * *"), cron_job("b", "5 3 * * *")]
    runs = compute_runs(jobs, start, timedelta(days=1))
    assert find_collisions(runs) == []


def test_duration_causes_overlap_collision():
    # 'a' starts 03:00 and runs 20m; 'b' starts 03:05 -> overlap.
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [cron_job_dur("a", "0 3 * * *", "20m"),
            cron_job("b", "5 3 * * *")]
    runs = compute_runs(jobs, start, timedelta(days=1))
    cols = find_collisions(runs)
    assert len(cols) == 1
    assert {j.name for j in cols[0].jobs} == {"a", "b"}
    # The collision interval spans from 03:00 to at least 03:20.
    assert cols[0].start == datetime(2026, 8, 11, 3, 0)
    assert cols[0].end >= datetime(2026, 8, 11, 3, 20)


def test_duration_gap_no_collision():
    # 'a' runs 03:00-03:20; 'b' starts 03:25 -> no overlap.
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [cron_job_dur("a", "0 3 * * *", "20m"),
            cron_job("b", "25 3 * * *")]
    runs = compute_runs(jobs, start, timedelta(days=1))
    assert find_collisions(runs) == []


def test_parse_duration_forms():
    from schedviz.models import parse_duration
    assert parse_duration("20m") == timedelta(minutes=20)
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)
    assert parse_duration("90s") == timedelta(seconds=90)
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("45") == timedelta(seconds=45)
    assert parse_duration("nonsense") is None


def test_cron_duration_annotation():
    text = "# nightly backup duration=20m\n0 3 * * * /usr/bin/borg create\n"
    jobs = parse_cron_text(text)
    assert jobs[0].duration == timedelta(minutes=20)


def test_ci_duration_annotation():
    text = ("name: CI\non:\n  schedule:\n"
            "    - cron: '0 3 * * *'  # duration=15m\n")
    jobs = parse_ci_file(text, offset=timedelta(0))
    assert jobs[0].duration == timedelta(minutes=15)


def test_collision_two_backups_at_3am():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [
        cron_job("borg-backup", "0 3 * * *"),
        cron_job("restic-backup", "0 3 * * *"),
        cron_job("noon-report", "0 12 * * *"),
    ]
    runs = compute_runs(jobs, start, timedelta(days=1))
    collisions = find_collisions(runs)
    assert len(collisions) == 1
    col = collisions[0]
    assert col.start == datetime(2026, 8, 11, 3, 0)
    names = {j.name for j in col.jobs}
    assert names == {"borg-backup", "restic-backup"}


def test_no_self_collision():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [cron_job("frequent", "*/5 * * * *")]
    runs = compute_runs(jobs, start, timedelta(hours=1))
    assert find_collisions(runs) == []


def test_near_collision_with_threshold():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [
        cron_job("a", "0 3 * * *"),
        cron_job("b", "2 3 * * *"),
    ]
    runs = compute_runs(jobs, start, timedelta(days=1))
    assert find_collisions(runs, threshold=timedelta(0)) == []
    near = find_collisions(runs, threshold=timedelta(minutes=5))
    assert len(near) == 1


def test_analyze_ignores_frequent_jobs():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [
        cron_job("borg", "0 3 * * *"),        # daily -> notable
        cron_job("restic", "0 3 * * *"),      # daily -> notable
        cron_job("ping", "*/2 * * * *"),      # every 2 min -> frequent
        cron_job("scrape", "*/5 * * * *"),    # every 5 min -> frequent
    ]
    # With frequency filtering, only the two backups collide at 03:00.
    _, notable = analyze(jobs, start, timedelta(days=1))
    assert len(notable) == 1
    assert {j.name for j in notable[0].jobs} == {"borg", "restic"}

    # Including everything surfaces many ping/scrape overlaps too.
    _, everything = analyze(jobs, start, timedelta(days=1),
                            max_runs_per_day=None)
    assert len(everything) > 1


def test_collision_ranking_puts_rare_heavy_first():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [
        cron_job("pingA", "*/3 * * * *"),
        cron_job("pingB", "*/3 * * * *"),   # frequent pair, overlap often
        cron_job("backupA", "1 3 * * *"),   # 03:01 — off the */3 grid
        cron_job("backupB", "1 3 * * *"),   # rare pair, overlap once
    ]
    _, collisions = analyze(jobs, start, timedelta(days=1),
                            max_runs_per_day=None)
    top = collisions[0]
    assert {j.name for j in top.jobs} == {"backupA", "backupB"}


def test_job_frequencies():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [cron_job("hourly", "0 * * * *")]
    runs = compute_runs(jobs, start, timedelta(days=1))
    freqs = job_frequencies(runs, timedelta(days=1))
    # 24 runs in a day -> ~24/day.
    assert abs(list(freqs.values())[0] - 24.0) < 0.1


def test_upcoming_runs_merged_sorted():
    start = datetime(2026, 8, 11, 0, 0)
    jobs = [
        cron_job("hourly", "0 * * * *"),
        cron_job("daily3", "0 3 * * *"),
    ]
    runs = upcoming_runs(jobs, start, 5)
    times = [r.time for r in runs]
    assert times == sorted(times)
    assert times[0] == datetime(2026, 8, 11, 1, 0)


def test_parse_user_crontab():
    text = """
SHELL=/bin/bash
# nightly borg backup
0 3 * * * /usr/bin/borg create
*/15 * * * * curl http://localhost/ping
"""
    jobs = parse_cron_text(text)
    assert len(jobs) == 2
    assert jobs[0].command == "/usr/bin/borg create"
    # A comment on the line directly above becomes the job's name.
    assert jobs[0].name == "nightly borg backup"
    assert jobs[0].schedule is not None
    # No preceding comment -> name falls back to the command.
    assert jobs[1].command == "curl http://localhost/ping"


def test_parse_system_crontab_user_field():
    text = "0 3 * * * root /usr/bin/backup.sh\n"
    jobs = parse_cron_text(text, system=True)
    assert len(jobs) == 1
    assert jobs[0].command == "/usr/bin/backup.sh"
    assert "user:root" in jobs[0].tags


def test_parse_timer_unit():
    text = """
[Unit]
Description=Daily backup

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    job = parse_timer_unit(text, name="backup.timer", source_path="/x/backup.timer")
    assert job.name == "backup"
    assert job.source == Source.SYSTEMD
    assert job.schedule is not None
    nxt = job.next_run(datetime(2026, 8, 11, 0, 0))
    assert nxt == datetime(2026, 8, 11, 3, 0)


def test_monotonic_timer_unschedulable():
    text = "[Timer]\nOnBootSec=15min\nOnUnitActiveSec=1h\n"
    job = parse_timer_unit(text, name="watch.timer")
    assert job.schedule is None
    assert "monotonic" in job.note


def test_parse_ci_github_actions():
    text = """
name: Nightly
on:
  schedule:
    - cron: '0 3 * * *'
    - cron: "30 5 * * 0"
jobs:
  build:
    runs-on: ubuntu-latest
"""
    jobs = parse_ci_file(text, source_path=".github/workflows/nightly.yml",
                         offset=timedelta(0))
    assert len(jobs) == 2
    assert jobs[0].name == "Nightly"
    assert jobs[0].source == Source.CI
    # offset=0 => UTC == local for the test.
    assert jobs[0].next_run(datetime(2026, 8, 11, 0, 0)) == \
        datetime(2026, 8, 11, 3, 0)


def test_describe_cron():
    assert describe_cron("0 3 * * *") == "at 03:00, every day"
    assert describe_cron("*/15 * * * *") == "every 15 minutes, every day"
    assert "Monday" in describe_cron("0 9 * * 1")
    assert describe_cron("@daily") == "at 00:00 every day"


def test_humanize_delta():
    now = datetime(2026, 8, 11, 12, 0)
    assert humanize_delta(now, datetime(2026, 8, 11, 12, 5)) == "in 5 minutes"
    assert humanize_delta(now, datetime(2026, 8, 11, 15, 0)) == "in 3 hours"
    assert humanize_delta(now, datetime(2026, 8, 11, 11, 0)) == "1 hour ago"
