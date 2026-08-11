from datetime import datetime, timedelta

from schedviz.cronexpr import CronExpr
from schedviz.models import Job, Source
from schedviz.render import build_layout, collision_cells
from schedviz.schedule import analyze


def job(name, expr):
    return Job(name=name, source=Source.CRONTAB,
               schedule=CronExpr.parse(expr), raw=expr)


def test_collision_cells_only_mark_participants():
    """A high-frequency bystander that merely fires in a busy column must NOT
    be marked as colliding — only the actual collision participants are."""
    start = datetime(2026, 8, 12, 2, 0)
    borg = job("borg", "0 3 * * *")
    restic = job("restic", "0 3 * * *")
    ping = job("ping", "*/5 * * * *")   # also fires at 03:00, but excluded
    jobs = [borg, restic, ping]

    runs, collisions = analyze(jobs, start, timedelta(hours=6))
    # Only borg + restic collide (ping is high-frequency, filtered out).
    assert len(collisions) == 1
    assert {j.name for j in collisions[0].jobs} == {"borg", "restic"}

    layout = build_layout(start, timedelta(hours=6), 90)
    cells = collision_cells(layout, collisions)
    job_ids = {jid for jid, _ in cells}
    assert id(borg) in job_ids
    assert id(restic) in job_ids
    # The bystander ping must not appear in any collision cell.
    assert id(ping) not in job_ids
