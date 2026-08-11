"""Discover jobs from explicit paths or by auto-scanning the local system."""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from ..models import Job
from .ci import parse_ci_file
from .cron import parse_cron_text
from .systemd import parse_timer_dir, parse_timer_unit

__all__ = ["discover_jobs", "auto_discover", "parse_path"]

# Standard locations checked during auto-discovery.
_SYSTEM_CRON_FILES = ["/etc/crontab"]
_SYSTEM_CRON_DIRS = ["/etc/cron.d"]
_SYSTEMD_TIMER_DIRS = [
    "/etc/systemd/system",
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    os.path.expanduser("~/.config/systemd/user"),
]
_CI_DIRS = [".github/workflows"]


def parse_path(path: str) -> List[Job]:
    """Parse a single file or directory, choosing the parser by its
    name/location/extension."""

    if os.path.isdir(path):
        # A directory: treat as a cron.d dir and/or a systemd timer dir.
        jobs: List[Job] = []
        jobs.extend(parse_timer_dir(path))
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if os.path.isfile(full) and not entry.endswith(".timer"):
                jobs.extend(parse_path(full))
        return jobs

    base = os.path.basename(path)
    lower = base.lower()

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return [Job(name=base, source=_guess_source(path), schedule=None,
                    source_path=path, note=f"could not read: {exc}")]

    if lower.endswith(".timer"):
        return [parse_timer_unit(text, name=base, source_path=path)]
    if lower.endswith((".yml", ".yaml")) or "workflow" in path:
        return parse_ci_file(text, source_path=path)
    if "cron.d" in path or base in ("crontab",) or path == "/etc/crontab":
        return parse_cron_text(text, system=True, source_path=path)
    # Default: a user crontab dump.
    return parse_cron_text(text, system=False, source_path=path)


def _guess_source(path: str):
    from ..models import Source
    lower = path.lower()
    if lower.endswith(".timer"):
        return Source.SYSTEMD
    if lower.endswith((".yml", ".yaml")) or "workflow" in lower:
        return Source.CI
    if "cron.d" in lower:
        return Source.CRON_D
    return Source.CRONTAB


def _read_current_crontab() -> Optional[str]:
    """Return the invoking user's crontab via ``crontab -l``, or ``None``."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def auto_discover(*, include_user_crontab: bool = True,
                  include_system: bool = True,
                  include_systemd: bool = True,
                  include_ci: bool = True,
                  cwd: Optional[str] = None) -> List[Job]:
    """Scan the standard system locations and the current project for jobs."""

    jobs: List[Job] = []

    if include_user_crontab:
        text = _read_current_crontab()
        if text:
            jobs.extend(parse_cron_text(text, system=False,
                                        source_path="crontab -l"))

    if include_system:
        for f in _SYSTEM_CRON_FILES:
            if os.path.isfile(f):
                jobs.extend(parse_path(f))
        for d in _SYSTEM_CRON_DIRS:
            if os.path.isdir(d):
                for entry in sorted(os.listdir(d)):
                    full = os.path.join(d, entry)
                    if os.path.isfile(full):
                        try:
                            with open(full, encoding="utf-8",
                                      errors="replace") as fh:
                                jobs.extend(parse_cron_text(
                                    fh.read(), system=True, source_path=full))
                        except OSError:
                            continue

    if include_systemd:
        for d in _SYSTEMD_TIMER_DIRS:
            jobs.extend(parse_timer_dir(d))

    if include_ci:
        root = cwd or os.getcwd()
        for rel in _CI_DIRS:
            d = os.path.join(root, rel)
            if os.path.isdir(d):
                for entry in sorted(os.listdir(d)):
                    if entry.lower().endswith((".yml", ".yaml")):
                        jobs.extend(parse_path(os.path.join(d, entry)))

    return jobs


def discover_jobs(paths: Optional[List[str]] = None, **auto_kwargs) -> List[Job]:
    """Top-level entry: parse explicit ``paths`` if given, else auto-discover."""
    if paths:
        jobs: List[Job] = []
        for p in paths:
            jobs.extend(parse_path(p))
        return jobs
    return auto_discover(**auto_kwargs)
