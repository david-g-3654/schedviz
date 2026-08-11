"""Render an animated GIF of schedviz scrubbing into the 03:00 collision.

Drives the Textual app headlessly, exports one SVG per frame (moving the cursor
and toggling the collision flash), rasterises each with ``rsvg-convert``, and
assembles them into a looping GIF with Pillow.

Usage:  python scripts/make_gif.py [out.gif]
Requires: rsvg-convert (librsvg) and Pillow, plus schedviz installed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

from PIL import Image

from schedviz.demo import demo_jobs
from schedviz.sources.ci import local_utc_offset
from schedviz.tui import SchedvizApp, TimelineWidget

WIDTH, HEIGHT = 118, 34
FRAME_MS = 320  # per-frame delay in the GIF


async def capture_frames():
    jobs = demo_jobs(ci_offset=local_utc_offset())
    # Pin "now" just before the 03:00 pile-up so it sits in a 6h window.
    now = datetime(2026, 8, 12, 2, 15)
    app = SchedvizApp(jobs, now=now, window=timedelta(hours=6))

    svgs = []
    async with app.run_test(size=(WIDTH, HEIGHT)) as pilot:
        await pilot.press("h")          # 6-hour view: 02:00–08:00
        await pilot.pause()

        # Establish the cursor on the right, then scrub left toward 03:00.
        await pilot.press("right")
        await pilot.pause()
        cols = app._last_cols or 90
        app.cursor_col = cols - 2
        app.query_one(TimelineWidget).refresh()
        await pilot.pause()
        svgs.append(app.export_screenshot(title="schedviz"))

        # Walk the cursor across the strip to the 03:00 column.
        steps = 16
        target = int(cols * (timedelta(hours=1) / app.window))  # ~03:00
        for i in range(steps):
            frac = (i + 1) / steps
            app.cursor_col = int((cols - 2) + (target - (cols - 2)) * frac)
            # Keep the collision flashing while we move.
            app.flash_on = bool(i % 2)
            app.query_one(TimelineWidget).refresh()
            app.query_one("SidePanel").refresh_content()
            await pilot.pause()
            svgs.append(app.export_screenshot(title="schedviz"))

        # Park on the collision and let it flash a few times.
        app.cursor_col = target
        for i in range(6):
            app.flash_on = bool(i % 2)
            app.query_one(TimelineWidget).refresh()
            await pilot.pause()
            svgs.append(app.export_screenshot(title="schedviz"))

    return svgs


def svgs_to_gif(svgs, out_path):
    with tempfile.TemporaryDirectory() as tmp:
        pngs = []
        for i, svg in enumerate(svgs):
            svg_path = os.path.join(tmp, f"f{i:03d}.svg")
            png_path = os.path.join(tmp, f"f{i:03d}.png")
            with open(svg_path, "w") as fh:
                fh.write(svg)
            subprocess.run(
                ["rsvg-convert", "-z", "1", svg_path, "-o", png_path],
                check=True,
            )
            pngs.append(png_path)

        frames = [Image.open(p).convert("RGBA") for p in pngs]
        # Flatten onto the first frame's size.
        base = frames[0].size
        frames = [f.resize(base) if f.size != base else f for f in frames]
        # Hold the final (parked-on-collision) frame a little longer.
        durations = [FRAME_MS] * len(frames)
        durations[-1] = 1200

        rgb = [f.convert("P", palette=Image.ADAPTIVE) for f in frames]
        rgb[0].save(
            out_path, save_all=True, append_images=rgb[1:],
            duration=durations, loop=0, optimize=True, disposal=2,
        )


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "docs/demo.gif"
    svgs = asyncio.run(capture_frames())
    svgs_to_gif(svgs, out)
    size = os.path.getsize(out)
    print(f"wrote {out} ({len(svgs)} frames, {size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
