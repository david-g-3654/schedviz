"""Pure rendering helpers that turn schedule data into Rich renderables.

Kept free of any Textual/event machinery so the layout logic can be unit-tested
and reused. The Textual app in :mod:`schedviz.tui` is a thin shell over these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

from rich.text import Text

from .models import Job
from .schedule import Collision, Run

__all__ = [
    "PALETTE", "color_for", "TimelineLayout", "build_layout",
    "render_axis", "render_job_row", "render_collision_row",
    "collision_columns", "collision_cells",
]

# A colour-blind-friendly-ish cycle; distinct hues that read on dark terminals.
PALETTE = [
    "cyan", "green", "yellow", "magenta", "blue", "bright_red",
    "bright_green", "bright_yellow", "bright_magenta", "bright_cyan",
    "orange3", "spring_green2", "deep_pink3", "gold3", "turquoise2",
]

EMPTY_CHAR = "·"
RUN_CHAR = "█"
COLLISION_CHAR = "█"
CURSOR_CHAR = "│"


def color_for(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


@dataclass
class TimelineLayout:
    """Precomputed column mapping for a render pass."""

    start: datetime
    window: timedelta
    n_cols: int

    def col_of(self, when: datetime) -> Optional[int]:
        frac = (when - self.start) / self.window
        if frac < 0 or frac >= 1:
            return None
        return min(self.n_cols - 1, int(frac * self.n_cols))

    def time_of(self, col: int) -> datetime:
        frac = (col + 0.5) / self.n_cols
        return self.start + self.window * frac


def build_layout(start: datetime, window: timedelta, n_cols: int) -> TimelineLayout:
    return TimelineLayout(start, window, max(1, n_cols))


def render_axis(layout: TimelineLayout, label_width: int) -> Text:
    """A time axis line with hour/day ticks aligned to the columns."""
    total_hours = layout.window.total_seconds() / 3600
    line = [" "] * layout.n_cols

    # Choose a tick spacing that yields ~8-12 labels.
    if total_hours <= 26:
        tick = timedelta(hours=3)
        fmt = "%H"
    elif total_hours <= 24 * 3:
        tick = timedelta(hours=12)
        fmt = "%a%H"
    else:
        tick = timedelta(days=1)
        fmt = "%a"

    text = Text(" " * label_width, style="dim")
    # Build a label string across the columns.
    labels = [" "] * layout.n_cols
    t = _ceil_to(layout.start, tick)
    while t < layout.start + layout.window:
        col = layout.col_of(t)
        if col is not None:
            label = t.strftime(fmt)
            for i, ch in enumerate(label):
                if col + i < layout.n_cols:
                    labels[col + i] = ch
        t += tick

    axis = Text(" " * label_width)
    axis.append("".join(labels), style="dim")
    return axis


def _ceil_to(dt: datetime, tick: timedelta) -> datetime:
    """Round ``dt`` up to the next multiple of ``tick`` from midnight."""
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = dt - midnight
    steps = int(elapsed / tick)
    candidate = midnight + tick * steps
    if candidate < dt:
        candidate += tick
    return candidate


def render_job_row(job: Job, color: str, layout: TimelineLayout,
                   runs: Sequence[Run], collision_cells: Set[Tuple[int, int]],
                   cursor_col: Optional[int], flash_on: bool,
                   label_width: int, selected: bool = False) -> Text:
    """One job's timeline row: label + colored cells.

    A cell is painted red only if *this* job actually participates in a
    collision at that column — ``collision_cells`` holds the exact
    ``(id(job), col)`` pairs — so a bystander job that merely happens to fire in
    a busy column is not mislabelled as colliding.
    """

    cells = [None] * layout.n_cols  # None => empty
    for run in runs:
        for col in _span_cols(layout, run):
            cells[col] = True

    jid = id(job)
    label = job.name[: label_width - 2]
    label = label.ljust(label_width - 1)
    row = Text()
    row.append(label + " ", style=("bold " + color) if selected else color)

    for col in range(layout.n_cols):
        is_cursor = cursor_col is not None and col == cursor_col
        if cells[col]:
            if (jid, col) in collision_cells:
                style = "bold white on red" if flash_on else "bold red"
                row.append(COLLISION_CHAR, style=style)
            else:
                row.append(RUN_CHAR, style=color)
        elif is_cursor:
            row.append(CURSOR_CHAR, style="bright_white")
        else:
            row.append(EMPTY_CHAR, style="grey30")
    return row


def render_collision_row(layout: TimelineLayout,
                         collision_cols: Dict[int, int],
                         cursor_col: Optional[int], flash_on: bool,
                         label_width: int) -> Text:
    """The dedicated top strip that marks collision columns in red."""
    row = Text()
    row.append("COLLISIONS".ljust(label_width - 1) + " ",
               style="bold red" if collision_cols else "dim")
    for col in range(layout.n_cols):
        is_cursor = cursor_col is not None and col == cursor_col
        if col in collision_cols:
            style = "bold white on red" if flash_on else "bold red"
            row.append("▼", style=style)
        elif is_cursor:
            row.append(CURSOR_CHAR, style="bright_white")
        else:
            row.append(" ")
    return row


def _span_cols(layout: TimelineLayout, run: Run) -> range:
    """Columns covered by a run's ``[time, end)`` interval (clamped to view)."""
    start_col = layout.col_of(run.time)
    if start_col is None:
        return range(0, 0)
    end_col = layout.col_of(run.end)
    if end_col is None:
        # Interval extends past the window; fill to the edge.
        end_col = layout.n_cols - 1
    if end_col < start_col:
        end_col = start_col
    return range(start_col, end_col + 1)


def collision_columns(layout: TimelineLayout,
                      collisions: List[Collision]) -> Dict[int, int]:
    """Map collision column -> number of jobs involved (spanning durations).
    Used for the summary strip at the top of the timeline."""
    cols: Dict[int, int] = {}
    for col in collisions:
        for run in col.runs:
            for c in _span_cols(layout, run):
                cols[c] = max(cols.get(c, 0), col.size)
    return cols


def collision_cells(layout: TimelineLayout,
                    collisions: List[Collision]) -> Set[Tuple[int, int]]:
    """The exact ``(id(job), col)`` pairs that are part of a collision, so each
    job row can paint only its own colliding cells."""
    cells: Set[Tuple[int, int]] = set()
    for col in collisions:
        for run in col.runs:
            jid = id(run.job)
            for c in _span_cols(layout, run):
                cells.add((jid, c))
    return cells
