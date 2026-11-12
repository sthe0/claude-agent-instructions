"""The reaper contract: what a reaper module provides and what the runner hands it.

Difficulty removed: each cleanup script hand-rolled its own throttle stamp, dry-run,
ownership check and log, so every new kind of residue meant another one-off hook that
decided alone. The runner (``reaper.runner``) owns those parts; a reaper only proves
what it can about its own kind of residue.

A reaper is a Python module defining

* ``NAME`` (str) and ``THROTTLE_HOURS`` (a finite number > 0, default 24);
* ``scan(ctx) -> list[Verdict]`` — one verdict per item it has an opinion on, with no
  side effect beyond best-effort bookkeeping that checks ``ctx.dry_run`` and ``ctx.due``;
* ``remove(path, ctx)`` — delete one item the runner approved. Raise ``RemovalRefused(reason)``
  when the item, judged again at removal time, must stay (not an error: the runner keeps it and
  records the reason); raise anything else (or return ``False``) when the removal failed;
* optionally ``summary(verdicts) -> str | None`` — one line for the SessionStart notice;
* optionally ``snapshot_extra(verdicts) -> dict`` — extra top-level fields of the runner's kept
  snapshot; optionally ``dry_run_notes(verdicts) -> list[str]`` — diagnostic lines a dry run
  prints to stderr after the summary. A keep verdict's ``reason=<slug>`` tag becomes the
  snapshot entry's ``reason``; its ``reason`` text becomes the entry's ``detail``;
* optionally ``upkeep(ctx) -> list[str]`` — non-destructive, idempotent, unthrottled
  maintenance (the ``git-worktrees`` reaper backs its branches up to origin), one report
  line per action. Only ``runner --upkeep-only`` calls it, under a lock and with errors
  isolated per reaper; ``scan`` and the SessionStart pass never do. When ``ctx.dry_run``
  is set it must not write, push or fetch, and returns the lines a live run would act on;
* optionally ``pressure_paths(ctx) -> list[str]`` — paths of the filesystems this reaper's
  residue fills. ``runner --on-pressure`` makes the reaper due (ignoring its throttle) when
  the lowest free percentage among them is below the config.md row
  ``reaper-pressure-free-percent``. It changes when the reaper runs, never what it may remove;
* optionally ``SESSION_SOURCES`` (a non-empty frozenset of SessionStart ``source`` values, e.g.
  ``frozenset({"startup", "clear"})``) — a session gate. In a SessionStart pass the reaper is due
  (prints its summary, spends its throttle) only when the hook payload names one of these sources
  and the session is a top-level, interactive one (``ReapContext.session``). Otherwise it is not due:
  its ``scan`` still runs with ``ctx.due`` False, it prints nothing and writes no stamp. A timer or
  pressure pass carries no payload, so a gated reaper is never due there. ``--dry-run``,
  ``--force-run`` and ``--only`` ignore the gate.

A plugin module must use ABSOLUTE imports (``from reaper.contract import Verdict``):
``lib.plugin_dir.load_plugin_module`` executes it under a synthetic package name.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from session_scope import registry

OWNER_HEARTBEAT_TTL_HOURS = 24.0
REMOVE = "remove"
KEEP = "keep"


class RemovalRefused(Exception):
    """``remove`` found, judging again, that the item must stay; the message is the reason.

    Difficulty removed: an item that turned live between scan and removal was reported as a
    failed removal with no reason, so a correct refusal looked like a pass error."""


@dataclass(frozen=True)
class Verdict:
    """One reaper's opinion on one item. ``report`` marks a kept item worth a notice;
    ``tags`` are labels the reaper reads back in its own ``summary`` (not compared);
    ``size`` is the item's disk bytes when the reaper measured them (0 when it did not),
    recorded in the runner's kept snapshot."""

    path: str
    action: str
    reason: str
    report: bool = False
    tags: "tuple[str, ...]" = field(default=(), compare=False)
    size: int = field(default=0, compare=False)


def session_sources_problem(module: object) -> "str | None":
    """Why a module's optional ``SESSION_SOURCES`` is unusable, else None (absent is fine)."""
    if not hasattr(module, "SESSION_SOURCES"):
        return None
    sources = module.SESSION_SOURCES  # type: ignore[attr-defined]
    if not isinstance(sources, frozenset) or not sources:
        return "SESSION_SOURCES is not a non-empty frozenset"
    if not all(isinstance(source, str) and source for source in sources):
        return "SESSION_SOURCES holds a value that is not a non-empty string"
    return None


def _at_or_inside(container: str, candidate: str) -> bool:
    return candidate == container or candidate.startswith(container + os.sep)


def owning_record(
    path: str,
    records: "list[registry.ScopeRecord]",
    now_ts: float,
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS,
    resolve: "Callable[[str], str]" = os.path.realpath,
) -> "registry.ScopeRecord | None":
    """The first session-scope record whose cwd or repo_root sits at/inside ``path`` and whose
    session is live — a confirmed-alive pid, or a heartbeat within the floor — else None.

    Absence of a live process is not proof of no owner: a paused session can still own
    a worktree across an idle gap, so the registry, not a /proc scan, is the oracle.
    Both sides are spelled by ``resolve`` (realpath by default, so a symlinked spelling on either
    side still matches); a caller that must not touch the filesystem passes a lexical one.
    """
    wanted = resolve(path)
    ttl_s = heartbeat_ttl_hours * 3600.0
    for rec in records:
        if not _record_alive(rec, now_ts, ttl_s):
            continue
        for held in (rec.cwd, rec.repo_root):
            if held and _at_or_inside(wanted, resolve(held)):
                return rec
    return None


def path_owned(
    path: str,
    records: "list[registry.ScopeRecord]",
    now_ts: float,
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS,
) -> bool:
    """True if ``owning_record`` finds a live session-scope record at/inside ``path`` (by realpath)."""
    return owning_record(path, records, now_ts, heartbeat_ttl_hours) is not None


def session_owned(
    session_id: str,
    records: "list[registry.ScopeRecord]",
    now_ts: float,
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS,
) -> bool:
    """True if a live session-scope record (same liveness rule as ``path_owned``) carries
    ``session_id``. Ids are compared in the sanitized form both stores use as file names.
    """
    wanted = registry._safe(session_id)
    ttl_s = heartbeat_ttl_hours * 3600.0
    return any(
        registry._safe(rec.session_id) == wanted and _record_alive(rec, now_ts, ttl_s)
        for rec in records
    )


def _record_alive(rec: "registry.ScopeRecord", now_ts: float, ttl_s: float) -> bool:
    alive = rec.pid is not None and registry.pid_alive(rec.pid)
    return alive or (now_ts - rec.heartbeat_ts) <= ttl_s


@dataclass
class ReapContext:
    """What the runner hands ``scan``/``remove``.

    ``due`` is true while this reaper's own throttle window has elapsed (or the run is
    forced); a ``scan`` that writes anything must check it and ``dry_run``.
    ``scope_registry_error`` is set when a session-scope record could not be read; the
    runner then removes nothing, since ``scope_records`` may be missing an owner.
    ``pressured`` names the reapers a ``--on-pressure`` pass made due; it is empty in every
    other pass.
    ``session`` is ``(source, top_level, interactive)`` of the SessionStart hook payload the runner
    read, else None (no payload, a manual pass, or a pass that no reaper gates).
    """

    now: float
    dry_run: bool
    project_dir: Path
    deadletter_dir: Path
    scope_records: "list[registry.ScopeRecord]" = field(default_factory=list)
    due: bool = True
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS
    scope_registry_error: "str | None" = None
    pressured: "frozenset[str]" = frozenset()
    session: "tuple[str, bool, bool] | None" = None

    def owned_path(self, path: str) -> bool:
        return path_owned(path, self.scope_records, self.now, self.heartbeat_ttl_hours)

    def owned_session(self, session_id: str) -> bool:
        return session_owned(session_id, self.scope_records, self.now, self.heartbeat_ttl_hours)
