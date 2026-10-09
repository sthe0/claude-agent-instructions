"""The reaper contract: what a reaper module provides and what the runner hands it.

Difficulty removed: each cleanup script hand-rolled its own throttle stamp, dry-run,
ownership check and log, so every new kind of residue meant another one-off hook that
decided alone. The runner (``reaper.runner``) owns those parts; a reaper only proves
what it can about its own kind of residue.

A reaper is a Python module defining

* ``NAME`` (str) and ``THROTTLE_HOURS`` (float, default 24);
* ``scan(ctx) -> list[Verdict]`` — one verdict per item it has an opinion on, with no
  side effect beyond best-effort bookkeeping that checks ``ctx.dry_run`` and ``ctx.due``;
* ``remove(path, ctx)`` — delete one item the runner approved. Raise (or return
  ``False``) when the removal did not happen;
* optionally ``summary(verdicts) -> str | None`` — one line for the SessionStart notice.

A plugin module must use ABSOLUTE imports (``from reaper.contract import Verdict``):
``lib.plugin_dir.load_plugin_module`` executes it under a synthetic package name.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from session_scope import registry

OWNER_HEARTBEAT_TTL_HOURS = 24.0
REMOVE = "remove"
KEEP = "keep"


@dataclass(frozen=True)
class Verdict:
    """One reaper's opinion on one item. ``report`` marks a kept item worth a notice."""

    path: str
    action: str
    reason: str
    report: bool = False


def _at_or_inside(container: str, candidate: str) -> bool:
    return candidate == container or candidate.startswith(container + os.sep)


def path_owned(
    path: str,
    records: "list[registry.ScopeRecord]",
    now_ts: float,
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS,
) -> bool:
    """True if a session-scope record's cwd or repo_root sits at/inside ``path`` and that
    session is live — a confirmed-alive pid, or a heartbeat within the floor.

    Absence of a live process is not proof of no owner: a paused session can still own
    a worktree across an idle gap, so the registry, not a /proc scan, is the oracle.
    Paths are compared by realpath, so a symlinked spelling on either side still matches.
    """
    real = os.path.realpath(path)
    ttl_s = heartbeat_ttl_hours * 3600.0
    for rec in records:
        if not _record_alive(rec, now_ts, ttl_s):
            continue
        for held in (rec.cwd, rec.repo_root):
            if held and _at_or_inside(real, os.path.realpath(held)):
                return True
    return False


def _record_alive(rec: "registry.ScopeRecord", now_ts: float, ttl_s: float) -> bool:
    alive = rec.pid is not None and registry.pid_alive(rec.pid)
    return alive or (now_ts - rec.heartbeat_ts) <= ttl_s


@dataclass
class ReapContext:
    """What the runner hands ``scan``/``remove``.

    ``due`` is true while this reaper's own throttle window has elapsed (or the run is
    forced); a ``scan`` that writes anything must check it and ``dry_run``.
    """

    now: float
    dry_run: bool
    project_dir: Path
    deadletter_dir: Path
    scope_records: "list[registry.ScopeRecord]" = field(default_factory=list)
    due: bool = True
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS

    def owned_path(self, path: str) -> bool:
        return path_owned(path, self.scope_records, self.now, self.heartbeat_ttl_hours)

    def owned_session(self, session_id: str) -> bool:
        ttl_s = self.heartbeat_ttl_hours * 3600.0
        return any(
            rec.session_id == session_id and _record_alive(rec, self.now, ttl_s)
            for rec in self.scope_records
        )
