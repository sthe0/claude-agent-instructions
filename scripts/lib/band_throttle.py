"""Per-session band throttling for advisory hooks: say a thing once per band.

Difficulty removed
------------------

An advisory ``UserPromptSubmit`` hook runs on EVERY prompt of every session. A
nudge that re-emits each time is worse than useless — ``hook-context-growth-
reminder.py``'s whole subject is context bloat, and re-printing its own warning
every turn is bloat it caused. So each such hook remembers the highest band it
has already spoken at, per session, and stays quiet until the situation gets
worse.

That is one rule, and it was written twice: ``hook-context-growth-reminder.py``
and ``hook-burn-rate-guard.py`` carried byte-similar ``state_path`` /
``already_fired`` / ``record_fired`` triples differing only in where the stamps
live. The next hook that needs it would have copied a third. It lives here now.

Two properties every caller depends on
--------------------------------------

FAIL-OPEN, ALWAYS. An unwritable state directory, a stamp file replaced by a
directory, a session id full of path separators — none of these may raise, and
none may be treated as "already fired". Losing the memo degrades to a repeated
nudge, which is a nuisance; raising puts a traceback in front of the user on
their own prompt, which is a defect.

PRUNING IS THE STORE'S JOB. A ``/tmp`` root is cleaned by the OS; a
``~/.local/state`` root is not, so a long-lived one accumulates a stamp per
session forever. ``record_band`` prunes entries older than ``MAX_AGE_DAYS`` on
write — opportunistically, best-effort, and never in a way that can fail a call.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# How long a stamp stays interesting. A session that has not been heard from in
# this long will not be resumed into the same band decision, and the file is only
# a few bytes — this bounds the directory, it does not need to be exact.
MAX_AGE_DAYS = 30

# Cheap upper bound on the pruning scan, so a directory that somehow grew huge
# cannot turn a prompt into a long listdir. Whatever is missed is pruned next time.
_PRUNE_SCAN_LIMIT = 500


def safe_name(session_id: str) -> str:
    """A session id reduced to a single safe filename component.

    Everything outside ``[A-Za-z0-9-_]`` is dropped rather than escaped: the
    result only has to be stable and confined to one directory, and dropping
    makes a traversal attempt (``../..``) collapse to a harmless name instead of
    a path.
    """
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def stamp_path(session_id: str, root: Path | str, prefix: str = "") -> Path:
    """The stamp file for one session.

    ``prefix`` exists so a caller can keep its stamps as named files in a SHARED
    directory (``/tmp/cc-context-nudge-<id>``) instead of owning a directory of
    its own. On a multi-user ``/tmp`` an owned directory is a directory the next
    user cannot write into, so this is not merely cosmetic.
    """
    return Path(root) / f"{prefix}{safe_name(session_id)}"


def fired_band(session_id: str, root: Path | str, prefix: str = "") -> int:
    """The highest band already announced for this session; 0 when unknown.

    Unknown and never-fired are deliberately the same answer: on a lost or
    unreadable stamp the hook should speak again, not fall silent.
    """
    try:
        path = stamp_path(session_id, root, prefix)
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def record_band(
    session_id: str, band: int, root: Path | str, prefix: str = "",
    *, prune: bool = True,
) -> None:
    """Remember that ``band`` has been announced. Never raises."""
    path = stamp_path(session_id, root, prefix)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(band), encoding="utf-8")
    except OSError:
        return
    if prune:
        _prune(path.parent, prefix)


def _prune(root: Path, prefix: str = "") -> None:
    """Drop stamps older than ``MAX_AGE_DAYS``. Best-effort; never raises.

    Only files matching this caller's ``prefix`` are considered, so a caller
    sharing a directory (``/tmp``) prunes its own stamps and nobody else's. A
    caller with no prefix owns its directory and prunes all of it.
    """
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    try:
        entries = os.scandir(root)
    except OSError:
        return
    with entries:
        for i, entry in enumerate(entries):
            if i >= _PRUNE_SCAN_LIMIT:
                return
            if prefix and not entry.name.startswith(prefix):
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                continue
