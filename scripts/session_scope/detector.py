"""Cross-session filesystem-conflict detector — the online counterpart to the
session_scope.registry storage primitive.

Difficulty removed: no live session today can see whether the file it is about
to touch is already held by another live session, so collisions are caught only
after the fact (reactively, via git status / stash diffing). This module makes
that decidable at write time from observable inputs alone: session records
(from the registry), a candidate set of paths, and the current time.

Every function here is pure and deterministic: no wall-clock read (now_ts is
always caller-supplied, mirroring registry.live_sessions), no network/model
call, no filesystem I/O of its own — it only reasons over ScopeRecord objects
already loaded by the caller via registry.load_all.

VCS-agnosticism: path_overlaps operates on normalized filesystem paths alone,
never on a VCS's own diff/status. A git working tree and an arc mount are both,
at this layer, just directories — two sessions rooted in physically distinct
worktrees/mounts naturally produce non-overlapping paths with no VCS-specific
branch needed, which is what makes isolate-not-serialize automatic: isolating a
task into its own worktree/mount is what stops the detector from firing again.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentctl.exempt_paths import is_gated_path
from session_scope.registry import ScopeRecord, live_sessions


def path_overlaps(a: str, b: str) -> bool:
    """True iff one normalized absolute path is an ancestor of, or equal to,
    the other. Symmetric (order of a/b does not matter) and reflexive
    (path_overlaps(x, x) is True: a session's own held path is itself a
    conflict candidate for another session writing that same path)."""
    a_parts = Path(os.path.normpath(os.path.abspath(a))).parts
    b_parts = Path(os.path.normpath(os.path.abspath(b))).parts
    shorter, longer = (a_parts, b_parts) if len(a_parts) <= len(b_parts) else (b_parts, a_parts)
    return longer[: len(shorter)] == shorter


@dataclass(frozen=True)
class Conflict:
    """One candidate path a live OTHER session already holds in its scope."""

    other_session: str
    held_path: str
    candidate: str


def detect_conflicts(
    records: "list[ScopeRecord]",
    this_session: str,
    candidate_paths: "list[str]",
    now_ts: float,
    ttl_s: float,
    extra_live_check: "Callable[[str], bool] | None" = None,
) -> "list[Conflict]":
    """Conflicts between candidate_paths and OTHER live sessions' held paths.

    A session never conflicts with itself (this_session is excluded before
    overlap is even checked). Liveness is delegated entirely to
    registry.live_sessions, so a stale record — or, with extra_live_check, one
    whose backing process is gone — never produces a conflict. Two sessions
    rooted in distinct worktrees/mounts naturally hold disjoint paths, so no
    repo_root/vcs special-casing is needed here for isolate-not-serialize.
    """
    conflicts: "list[Conflict]" = []
    for rec in live_sessions(records, now_ts, ttl_s, extra_live_check=extra_live_check):
        if rec.session_id == this_session:
            continue
        for held in rec.touched_paths:
            for candidate in candidate_paths:
                if path_overlaps(held, candidate):
                    conflicts.append(
                        Conflict(other_session=rec.session_id, held_path=held, candidate=candidate)
                    )
    return conflicts


def classify_severity(candidate: str, held_by_other_live: bool) -> str:
    """'block' iff candidate is a gated path (agentctl/exempt_paths.is_gated_path
    is the only source of that designation — no hardcoded repo path) already
    held by another live session; 'warn' otherwise. A hard block on every
    overlap would serialize sessions, so blocking is reserved for the case
    where a second writer risks corrupting a path the coordination engine
    itself governs."""
    if held_by_other_live and is_gated_path(candidate):
        return "block"
    return "warn"
