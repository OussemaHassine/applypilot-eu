"""Shared job-location filter (local patch).

Reject patterns are checked first and always match whole words ("US", "Peru"),
so "Remote - US" is dropped even though it says remote, but "Perugia" is kept.
Remote markers then pass, and non-remote locations must match an accept pattern.
Places listed under `location_remote_only` in searches.yaml (Eastern Europe, local patch
2026-11-07) pass only when the location or the title says remote, freelance or B2B.
"""

from __future__ import annotations

import re

REMOTE_MARKERS = (
    "remote", "anywhere", "work from home", "wfh", "distributed",
    "télétravail", "teletravail", "da remoto", "smart working", "full remote",
)


# Title words that mean the job is not tied to that office: a `location_remote_only` place
# passes with them. "B2B" and "freelance" are how Polish and Czech postings say "contractor".
FLEX_MARKERS = ("freelance", "b2b", "contractor", "zdalnie", "zdalna", "home office", "homeoffice")

_REMOTE_ONLY: list[str] | None = None


def _remote_only_from_config() -> list[str]:
    global _REMOTE_ONLY
    if _REMOTE_ONLY is None:
        from applypilot import config
        _REMOTE_ONLY = list((config.load_search_config() or {}).get("location_remote_only") or [])
    return _REMOTE_ONLY


def _matches(pattern: str, loc: str, whole_word: bool = False) -> bool:
    p = pattern.lower().strip()
    if not p:
        return False
    if whole_word or len(p) <= 3:
        return re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", loc) is not None
    return p in loc


def location_ok(location: str | None, accept: list[str], reject: list[str],
                remote_only: list[str] | None = None, text: str = "") -> bool:
    """Return True when a job location passes the user's accept/reject filter.

    `text` (the title) may carry the remote / freelance / B2B markers; `remote_only`
    defaults to `location_remote_only` from searches.yaml."""
    if not location:
        return True  # unknown location -- keep it, let the scorer decide
    loc = location.lower()
    for r in reject:
        # Reject patterns always match whole words: "Peru" must not reject "Perugia".
        if _matches(r, loc, whole_word=True):
            return False
    extra = (text or "").lower()
    is_remote = any(m in loc for m in REMOTE_MARKERS) or any(m in extra for m in REMOTE_MARKERS + FLEX_MARKERS)
    for r in (remote_only if remote_only is not None else _remote_only_from_config()):
        if _matches(r, loc, whole_word=True):
            return is_remote   # a place wanted only for remote, freelance or B2B work
    if is_remote:
        return True
    for a in accept:
        if _matches(a, loc):
            return True
    return False
