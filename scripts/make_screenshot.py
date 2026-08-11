"""Render a still PNG of the schedviz demo (24h view, cursor near the pileup).

Usage:  python scripts/make_screenshot.py [out.png]
Requires: rsvg-convert (librsvg) and schedviz installed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

from schedviz.demo import demo_jobs
from schedviz.sources.ci import local_utc_offset
from schedviz.tui import SchedvizApp


async def capture(width=130, height=42):
    jobs = demo_jobs(ci_offset=local_utc_offset())
    now = datetime(2026, 8, 12, 2, 30)
    app = SchedvizApp(jobs, now=now, window=timedelta(hours=24))
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        for _ in range(20):
            await pilot.press("right")
        await pilot.pause()
        return app.export_screenshot(title="schedviz — demo")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "docs/demo.png"
    svg = asyncio.run(capture())
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as fh:
        fh.write(svg)
        svg_path = fh.name
    try:
        subprocess.run(["rsvg-convert", svg_path, "-o", out], check=True)
    finally:
        os.unlink(svg_path)
    print(f"wrote {out} ({os.path.getsize(out)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
