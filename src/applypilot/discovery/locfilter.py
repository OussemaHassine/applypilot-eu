"""Shared job-location filter (local patch).

Reject patterns are checked first and always match whole words ("US", "Peru"),
so "Remote - US" is dropped even though it says remote, but "Perugia" is kept.
Remote markers then pass, and non-remote locations must match an accept pattern.
"""

from __future__ import annotations

import re

REMOTE_MARKERS = (
    "remote", "anywhere", "work from home", "wfh", "distributed",
    "télétravail", "teletravail", "da remoto", "smart working", "full remote",
)


def _matches(pattern: str, loc: str, whole_word: bool = False) -> bool:
    p = pattern.lower().strip()
    if not p:
        return False
    if whole_word or len(p) <= 3:
        return re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", loc) is not None
    return p in loc


def location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Return True when a job location passes the user's accept/reject filter."""
    if not location:
        return True  # unknown location -- keep it, let the scorer decide
    loc = location.lower()
    for r in reject:
        # Reject patterns always match whole words: "Peru" must not reject "Perugia".
        if _matches(r, loc, whole_word=True):
            return False
    if any(m in loc for m in REMOTE_MARKERS):
        return True
    for a in accept:
        if _matches(a, loc):
            return True
    return False
