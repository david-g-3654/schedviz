"""The Textual TUI: a scrubable timeline of every scheduled job at once.

Keys
----
  ← / →   scrub the cursor across the timeline
  , / .   pan the window earlier / later
  d       24-hour view      w   7-day view      h   6-hour view
  n       jump back to now
  f       cycle the collision fuzz window (exact / 1m / 5m / 15m)
  ↑ / ↓   select a job (dims the rest)
  q       quit
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from .explain import describe_job, format_run
from .models import Job
from .render import (build_layout, collision_cells, collision_columns,
                     color_for, render_axis, render_collision_row,
                     render_job_row)
from .schedule import analyze, job_frequencies, upcoming_runs

LABEL_WIDTH = 22


def _fmt_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return "".join(parts) or "0s"

_WINDOWS = {
    "h": ("6h", timedelta(hours=6)),
    "d": ("24h", timedelta(hours=24)),
    "w": ("7d", timedelta(days=7)),
}

_FUZZ_STEPS = [
    (timedelta(0), "exact"),
    (timedelta(minutes=1), "±1m"),
    (timedelta(minutes=5), "±5m"),
    (timedelta(minutes=15), "±15m"),
]


class TimelineWidget(Static):
    """Renders the axis, a collision strip, and one row per job."""

    def render(self) -> Text:
        app: "SchedvizApp" = self.app  # type: ignore[assignment]
        width = self.size.width or 80
        n_cols = max(10, width - LABEL_WIDTH)
        app._last_cols = n_cols

        layout = build_layout(app.start, app.window, n_cols)
        col_cursor = app.cursor_col if app.cursor_col is not None else None
        if col_cursor is not None:
            col_cursor = max(0, min(n_cols - 1, col_cursor))

        ccols = collision_columns(layout, app.collisions)
        ccells = collision_cells(layout, app.collisions)

        out = Text()
        out.append(render_axis(layout, LABEL_WIDTH))
        out.append("\n")
        out.append(render_collision_row(layout, ccols, col_cursor,
                                        app.flash_on, LABEL_WIDTH))
        out.append("\n")

        for idx, job in enumerate(app.jobs):
            if job.schedule is None:
                continue
            job_runs = [r for r in app.runs if r.job is job]
            selected = (app.selected_index == idx)
            dimmed = (app.selected_index is not None and not selected)
            color = "grey42" if dimmed else color_for(idx)
            row = render_job_row(job, color, layout, job_runs, ccells,
                                 col_cursor, app.flash_on, LABEL_WIDTH,
                                 selected=selected)
            out.append(row)
            out.append("\n")

        # Unschedulable jobs listed at the bottom for completeness.
        unsched = [j for j in app.jobs if j.schedule is None]
        if unsched:
            out.append("\n")
            out.append(Text("  no wall-clock schedule:", style="dim italic"))
            out.append("\n")
            for job in unsched:
                out.append(Text(f"  · {job.name} — {job.note}", style="dim"))
                out.append("\n")

        return out


class SidePanel(VerticalScroll):
    """The "next runs" explainer panel, in plain English."""

    def compose(self) -> ComposeResult:
        yield Static(id="side-content")

    def refresh_content(self) -> None:
        app: "SchedvizApp" = self.app  # type: ignore[assignment]
        content = self.query_one("#side-content", Static)
        content.update(self._build(app))

    def _build(self, app: "SchedvizApp") -> Text:
        out = Text()
        now = app.now

        # Collision summary first — the headline.
        if app.collisions:
            out.append("⚠  COLLISIONS\n", style="bold red")
            for col in app.collisions[:8]:
                names = ", ".join(j.name for j in col.jobs)
                out.append(f"  {col.start:%a %d %b %H:%M}  ", style="red")
                out.append(f"{col.size} jobs\n", style="bold red")
                out.append(f"    {names}\n", style="red")
            out.append("\n")
        else:
            out.append("✓  no collisions in view\n\n", style="green")

        # Cursor context.
        if app.cursor_col is not None:
            layout = build_layout(app.start, app.window, app._last_cols or 60)
            ctime = layout.time_of(max(0, min((app._last_cols or 60) - 1,
                                              app.cursor_col)))
            out.append(f"▮ cursor: {ctime:%a %d %b %H:%M}\n\n",
                       style="bright_white")

        # Next 50 runs, merged across jobs. By default we hide high-frequency
        # jobs (pings/scrapes) so the useful entries aren't buried; [a] shows all.
        panel_jobs = app.jobs
        header = "NEXT 50 RUNS"
        if not app.include_frequent:
            freqs = job_frequencies(app.runs, app.window)
            panel_jobs = [j for j in app.jobs
                          if freqs.get(id(j), 0.0) <= 6.0]
            header = "NEXT 50 RUNS (notable)"
        out.append(header + "\n", style="bold")
        runs = upcoming_runs(panel_jobs, now, 50)
        for idx, run in enumerate(runs):
            job_idx = app.jobs.index(run.job)
            color = color_for(job_idx)
            out.append("● ", style=color)
            dur = run.job.duration
            suffix = f"  · ~{_fmt_duration(dur)}" if dur else ""
            out.append(f"{run.job.name}{suffix}\n", style="bold " + color)
            out.append(f"   {format_run(now, run.time)}\n", style="dim")
            out.append(f"   {describe_job(run.job)}\n", style="italic dim")
        if not runs:
            out.append("  (nothing scheduled)\n", style="dim")
        return out


class SchedvizApp(App):
    """Top-level app holding view state and the computed schedule."""

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #timeline { width: 2fr; padding: 1 1; }
    SidePanel { width: 1fr; border-left: solid $accent; padding: 0 1; }
    #status { height: 1; background: $panel; color: $text; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("left", "scrub(-1)", "◀ scrub"),
        ("right", "scrub(1)", "scrub ▶"),
        ("comma,less_than_sign", "pan(-1)", "◀ pan"),
        ("full_stop,greater_than_sign", "pan(1)", "pan ▶"),
        ("d", "set_window('d')", "24h"),
        ("w", "set_window('w')", "7d"),
        ("h", "set_window('h')", "6h"),
        ("n", "now", "Now"),
        ("f", "fuzz", "Fuzz"),
        ("a", "toggle_frequent", "All jobs"),
        ("up", "select(-1)", "▲ job"),
        ("down", "select(1)", "▼ job"),
    ]

    flash_on: reactive[bool] = reactive(True)

    def __init__(self, jobs: List[Job], now: Optional[datetime] = None,
                 window: timedelta = timedelta(hours=24)):
        super().__init__()
        self.jobs = jobs
        self.now = now or datetime.now().replace(second=0, microsecond=0)
        self.start = self.now - timedelta(minutes=30)
        self.window = window
        self.fuzz_index = 0
        self.threshold = _FUZZ_STEPS[0][0]
        self.include_frequent = False
        self.cursor_col: Optional[int] = None
        self.selected_index: Optional[int] = None
        self._last_cols = 60
        self.runs = []
        self.collisions = []

    # -- lifecycle ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield TimelineWidget(id="timeline")
            yield SidePanel()
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "schedviz"
        self.recompute()
        self.set_interval(0.6, self._toggle_flash)
        self._refresh_all()

    def _toggle_flash(self) -> None:
        if self.collisions:
            self.flash_on = not self.flash_on
            self.query_one(TimelineWidget).refresh()

    # -- data ---------------------------------------------------------------

    def recompute(self) -> None:
        max_per_day = None if self.include_frequent else 6.0
        self.runs, self.collisions = analyze(
            self.jobs, self.start, self.window, self.threshold,
            max_runs_per_day=max_per_day,
        )

    def _refresh_all(self) -> None:
        self.query_one(TimelineWidget).refresh()
        self.query_one(SidePanel).refresh_content()
        self._update_status()

    def _update_status(self) -> None:
        wname = {v[1]: v[0] for v in _WINDOWS.values()}.get(self.window, "?")
        fuzz = _FUZZ_STEPS[self.fuzz_index][1]
        n_sched = sum(1 for j in self.jobs if j.schedule is not None)
        scope = "all" if self.include_frequent else "notable"
        status = (
            f" {n_sched} jobs  ·  window {wname} from {self.start:%a %d %b %H:%M}"
            f"  ·  collisions: {len(self.collisions)} ({scope})  ·  fuzz {fuzz}"
            f"  ·  [←→] scrub  [,.] pan  [d/w/h] zoom  [f] fuzz  [a] all"
            f"  [n] now  [q] quit"
        )
        self.query_one("#status", Static).update(status)

    # -- actions ------------------------------------------------------------

    def action_scrub(self, direction: int) -> None:
        cols = self._last_cols or 60
        if self.cursor_col is None:
            self.cursor_col = cols // 2
        else:
            self.cursor_col = max(0, min(cols - 1, self.cursor_col + direction))
        self._refresh_all()

    def action_pan(self, direction: int) -> None:
        self.start += self.window * 0.25 * direction
        self.recompute()
        self._refresh_all()

    def action_set_window(self, key: str) -> None:
        _, win = _WINDOWS[key]
        self.window = win
        self.recompute()
        self._refresh_all()

    def action_now(self) -> None:
        self.now = datetime.now().replace(second=0, microsecond=0)
        self.start = self.now - timedelta(minutes=30)
        self.cursor_col = None
        self.recompute()
        self._refresh_all()

    def action_fuzz(self) -> None:
        self.fuzz_index = (self.fuzz_index + 1) % len(_FUZZ_STEPS)
        self.threshold = _FUZZ_STEPS[self.fuzz_index][0]
        self.recompute()
        self._refresh_all()

    def action_toggle_frequent(self) -> None:
        self.include_frequent = not self.include_frequent
        self.recompute()
        self._refresh_all()

    def action_select(self, direction: int) -> None:
        sched_indices = [i for i, j in enumerate(self.jobs)
                         if j.schedule is not None]
        if not sched_indices:
            return
        if self.selected_index is None:
            self.selected_index = sched_indices[0]
        else:
            pos = sched_indices.index(self.selected_index) \
                if self.selected_index in sched_indices else 0
            pos = (pos + direction) % len(sched_indices)
            self.selected_index = sched_indices[pos]
        self._refresh_all()


def run_app(jobs: List[Job], **kwargs) -> None:
    SchedvizApp(jobs, **kwargs).run()
