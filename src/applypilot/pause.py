"""Pause and resume for long runs (local patch, 2026-11-05).

A pause is a marker file in ~/ApplyPilot/pause/<group>. Every per-job loop calls
`wait_if_paused(group)` between two jobs: while the marker exists the loop sleeps and the
process stays alive, so Resume continues exactly where the work stopped (the next search, the
next posting, the next application) without re-running anything. The job in hand when Pause is
pressed finishes first; nothing is killed. Stop still ends a paused run.

Groups: discover (searches), pipeline (enrich, score, tailor, pdf), apply, followups.
"round" means discover + pipeline + apply, "all" every group. Set from the Runs page (Pause /
Resume on each card), with `applypilot pause|resume <group>`, or by creating and deleting the
marker file by hand.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from applypilot import config

log = logging.getLogger(__name__)

PAUSE_DIR: Path = config.APP_DIR / "pause"
GROUPS = ("discover", "pipeline", "apply", "followups")
LABELS = {"discover": "Find jobs", "pipeline": "Score & tailor", "apply": "Apply", "followups": "Email follow-ups"}
TARGETS: dict[str, tuple[str, ...]] = {**{g: (g,) for g in GROUPS}, "round": ("discover", "pipeline", "apply"), "all": GROUPS}
POLL = 3.0   # seconds between looks at the marker while waiting


def _flag(group: str) -> Path:
    return PAUSE_DIR / group


def expand(target: str) -> tuple[str, ...]:
    """Groups behind a target name: a group, "round" or "all"."""
    if target not in TARGETS:
        raise ValueError(f"unknown pause target '{target}'; use one of {', '.join(TARGETS)}")
    return TARGETS[target]


def is_paused(group: str) -> bool:
    return _flag(group).exists()


def paused() -> list[str]:
    return [g for g in GROUPS if is_paused(g)]


def pause(target: str) -> list[str]:
    groups = expand(target)
    PAUSE_DIR.mkdir(parents=True, exist_ok=True)
    for g in groups:
        _flag(g).touch()
    return list(groups)


def resume(target: str) -> list[str]:
    groups = expand(target)
    for g in groups:
        _flag(g).unlink(missing_ok=True)
    return list(groups)


def waiting() -> dict[str, int]:
    """Processes sleeping at a gate right now, per group (markers of dead processes are removed)."""
    out = {g: 0 for g in GROUPS}
    if not PAUSE_DIR.exists():
        return out
    for marker in PAUSE_DIR.glob("*.waiting.*"):
        group, _, pid = marker.name.split(".", 2)
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError):
            marker.unlink(missing_ok=True)
            continue
        except PermissionError:
            pass
        if group in out:
            out[group] += 1
    return out


def wait_if_paused(group: str, stop_event: threading.Event | None = None,
                   on_pause: Callable[[], None] | None = None,
                   on_resume: Callable[[], None] | None = None) -> bool:
    """Sleep while `group` is paused. Returns True if it waited at all.

    Returns as soon as `stop_event` is set, so a Stop still ends a paused run; the caller
    checks the event afterwards. `on_pause` / `on_resume` replace the default log lines
    (the apply launcher shows them on its dashboard instead).
    """
    if not is_paused(group):
        return False
    (on_pause or (lambda: log.warning("Paused (%s): waiting for Resume", LABELS[group])))()
    marker = PAUSE_DIR / f"{group}.waiting.{os.getpid()}"
    try:
        marker.touch()
    except OSError:
        pass
    try:
        while is_paused(group):
            if stop_event is not None:
                if stop_event.wait(POLL):
                    return True
            else:
                time.sleep(POLL)
    finally:
        marker.unlink(missing_ok=True)
    (on_resume or (lambda: log.warning("Resumed (%s)", LABELS[group])))()
    return True
