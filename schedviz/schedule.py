"""Cross-job timeline computation and collision detection.

This is the analytical heart the TUI renders. Given a set of jobs and a time
window it produces:

* :func:`compute_runs` — every run event inside the window, sorted by time.
* :func:`upcoming_runs` — the merged "next N runs" across all jobs.
* :func:`find_collisions` — clusters of runs from *different* jobs that fire at
  (or within ``threshold`` of) the same moment. The default threshold is zero,
  i.e. exact same-minute collisions like two 03:00 backups.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from .models import Job

__all__ = ["Run", "Collision", "compute_runs", "upcoming_runs",
           "find_collisions", "job_frequencies", "analyze"]


@dataclass(order=True)
class Run:
    """A single firing of one job at a specific time.

    If the job carries a ``duration`` the run occupies the half-open interval
    ``[time, end)``; otherwise it is a zero-width point at ``time``.
    """

    time: datetime
    job: Job = field(compare=False)

    @property
    def end(self) -> datetime:
        return self.time + (self.job.duration or timedelta(0))


@dataclass
class Collision:
    """Two or more runs from distinct jobs clustered within ``threshold``."""

    start: datetime
    end: datetime
    runs: List[Run]
    score: float = 0.0  # higher = more notable (rare, heavy jobs colliding)

    @property
    def jobs(self) -> List[Job]:
        seen = []
        for r in self.runs:
            if r.job not in seen:
                seen.append(r.job)
        return seen

    @property
    def size(self) -> int:
        return len(self.jobs)


def compute_runs(jobs: List[Job], start: datetime, window: timedelta,
                 *, max_per_job: int = 5000) -> List[Run]:
    """All run events in ``[start, start + window]``, sorted by time.

    ``max_per_job`` bounds pathological high-frequency jobs (``* * * * *`` over a
    week is 10 080 runs) so the timeline stays responsive.
    """
    end = start + window
    runs: List[Run] = []
    for job in jobs:
        if job.schedule is None:
            continue
        count = 0
        for t in job.schedule.next_runs(start, max_per_job):
            if t > end:
                break
            runs.append(Run(t, job))
            count += 1
    runs.sort(key=lambda r: r.time)
    return runs


def upcoming_runs(jobs: List[Job], start: datetime, count: int) -> List[Run]:
    """The next ``count`` runs across all jobs, merged and time-sorted.

    Uses a lazy heap merge so a distant-future job never forces a huge
    per-job enumeration.
    """
    # Prime the heap with each job's first upcoming run, then keep pulling the
    # earliest and refilling from that job.
    iters = []
    heap: List = []
    for idx, job in enumerate(jobs):
        if job.schedule is None:
            continue
        it = _run_iter(job, start)
        iters.append(it)
        first = next(it, None)
        if first is not None:
            heapq.heappush(heap, (first, idx, job))

    out: List[Run] = []
    while heap and len(out) < count:
        t, idx, job = heapq.heappop(heap)
        out.append(Run(t, job))
        nxt = next(iters[idx], None)
        if nxt is not None:
            heapq.heappush(heap, (nxt, idx, job))
    return out


def _run_iter(job: Job, start: datetime):
    """Yield a job's runs lazily in chunks (avoids materialising thousands)."""
    cur = start
    chunk = 64
    while True:
        batch = job.schedule.next_runs(cur, chunk)
        if not batch:
            return
        for t in batch:
            yield t
        cur = batch[-1]


def job_frequencies(runs: List[Run], window: timedelta) -> Dict[int, float]:
    """Runs-per-day for each job (keyed by ``id(job)``) over ``window``."""
    days = max(window.total_seconds() / 86400.0, 1e-9)
    counts: Dict[int, int] = {}
    for r in runs:
        counts[id(r.job)] = counts.get(id(r.job), 0) + 1
    return {jid: c / days for jid, c in counts.items()}


def find_collisions(runs: List[Run],
                    threshold: timedelta = timedelta(0),
                    *, freqs: Optional[Dict[int, float]] = None
                    ) -> List[Collision]:
    """Cluster ``runs`` (already or not-yet sorted) into collisions.

    Runs are merged by **interval overlap**: a run occupies ``[time, end)``
    (a point when the job has no duration), and a run joins the current cluster
    while it starts no later than the cluster's running end plus ``threshold``.
    With no durations this reduces to start-time clustering, so behaviour is
    unchanged for jobs that are modelled as instantaneous. A cluster is a
    collision only if it involves at least two *distinct* jobs — a single
    frequent job firing repeatedly is not a collision with itself.

    Each collision gets a ``score``: rare, heavy jobs colliding score high;
    two monitoring pings overlapping score near zero. Results are returned
    sorted by score (then time), so the meaningful pile-ups surface first.
    ``freqs`` (from :func:`job_frequencies`) drives the rarity weighting; if
    omitted it is derived from ``runs`` alone.
    """
    if not runs:
        return []

    ordered = sorted(runs, key=lambda r: r.time)
    if freqs is None:
        counts: Dict[int, int] = {}
        for r in ordered:
            counts[id(r.job)] = counts.get(id(r.job), 0) + 1
        freqs = {jid: float(c) for jid, c in counts.items()}

    collisions: List[Collision] = []
    cluster: List[Run] = [ordered[0]]
    cluster_end = ordered[0].end
    for cur in ordered[1:]:
        if cur.time <= cluster_end + threshold:
            cluster.append(cur)
            if cur.end > cluster_end:
                cluster_end = cur.end
        else:
            _maybe_add(collisions, cluster, freqs)
            cluster = [cur]
            cluster_end = cur.end
    _maybe_add(collisions, cluster, freqs)

    collisions.sort(key=lambda c: (-c.score, c.start))
    return collisions


def _rarity(freq_per_day: float) -> float:
    """Weight a job by how rare it is: a daily job counts ~1.0, a job firing
    every couple of minutes counts ~0. Rare + heavy jobs are what actually
    hurt when they collide."""
    return 1.0 / (1.0 + max(0.0, freq_per_day))


def _maybe_add(collisions: List[Collision], cluster: List[Run],
               freqs: Dict[int, float]) -> None:
    distinct = {id(r.job): r.job for r in cluster}
    if len(distinct) < 2:
        return
    rarity = sum(_rarity(freqs.get(jid, 1.0)) for jid in distinct)
    # Scale by how many distinct jobs pile up, so a 3-way rare collision
    # outranks a 2-way one.
    score = rarity * len(distinct)
    collisions.append(Collision(
        start=min(r.time for r in cluster),
        end=max(r.end for r in cluster),
        runs=list(cluster),
        score=score,
    ))


def analyze(jobs: List[Job], start: datetime, window: timedelta,
            threshold: timedelta = timedelta(0), *,
            max_runs_per_day: Optional[float] = 6.0):
    """Compute runs and notable collisions in one pass.

    Returns ``(runs, collisions)``. Jobs firing more often than
    ``max_runs_per_day`` (default: more than every 4h) are excluded from
    *collision* detection — two health-check pings overlapping is expected, not
    a problem — but they still appear on the timeline. Pass ``None`` to treat
    every job as collision-eligible.
    """
    runs = compute_runs(jobs, start, window)
    freqs = job_frequencies(runs, window)
    if max_runs_per_day is None:
        candidate = runs
    else:
        candidate = [r for r in runs
                     if freqs.get(id(r.job), 0.0) <= max_runs_per_day]
    collisions = find_collisions(candidate, threshold, freqs=freqs)
    return runs, collisions
