"""Session -> filesystem-scope record store.

Difficulty removed: no session today records where it is working, so no other
session (and no online conflict detector) can see it. This module is the
storage primitive that fixes that — a small JSON-per-session file under
DEFAULT_SCOPES_DIR (or an injected scopes_dir in tests), written atomically.

Every function here is deterministic given its arguments: no function reads
the wall clock (now_ts is always caller-supplied) and none calls out to a
model or the network. The only side effect is filesystem I/O against the
scopes directory, made testable by accepting scopes_dir as a parameter (mirrors
agentctl/store.py's FileStateStore.root and project_entry/projects.py's
injected-I/O style).

A missing or corrupt scope file is always treated as absent — one session's
malformed record must never raise into another session's read path.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from lib import config_root

DEFAULT_SCOPES_DIR = config_root.agentctl_scopes_dir()

# Cap on touched_paths so a long-running session's record can't grow unbounded.
# Oldest entries are dropped first (FIFO) when the cap is exceeded.
MAX_TOUCHED_PATHS = 500


def _safe(session_id: str) -> str:
    """Sanitize a session_id to alnum/-/_ — mirrors agentctl/store.py's _safe
    and hook-state-gate.py's _safe so the same session_id maps to the same
    filename across the coordination state store and this scope store."""
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


@dataclass
class ScopeRecord:
    """One session's recorded filesystem scope.

    touched_paths holds normalized absolute paths, deduped and capped at
    MAX_TOUCHED_PATHS. vcs is one of "git" / "arc" / "none".
    """

    session_id: str
    heartbeat_ts: float = 0.0
    cwd: "str | None" = None
    repo_root: "str | None" = None
    vcs: str = "none"
    touched_paths: "list[str]" = field(default_factory=list)
    pid: "int | None" = None
    # Ancestor session ids of this session (its spawn lineage), so the online
    # conflict detector can treat a parent and its synchronously-spawned
    # descendants as one write-lineage. Empty for a top-level session and for
    # any legacy record written before lineage tracking existed.
    lineage_ids: "list[str]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "ScopeRecord":
        pid = d.get("pid")
        return cls(
            session_id=str(d["session_id"]),
            heartbeat_ts=float(d.get("heartbeat_ts", 0.0)),
            cwd=d.get("cwd"),
            repo_root=d.get("repo_root"),
            vcs=str(d.get("vcs", "none")),
            touched_paths=[str(p) for p in d.get("touched_paths", [])],
            pid=int(pid) if pid is not None else None,
            lineage_ids=[str(x) for x in d.get("lineage_ids", [])],
        )

    @classmethod
    def from_json(cls, text: str) -> "ScopeRecord":
        return cls.from_dict(json.loads(text))


def parse_lineage(raw: "str | None") -> "list[str]":
    """Split a comma-joined lineage-ids string into an ordered, deduped list;
    empty/whitespace entries are dropped. The inverse of format_lineage. Shared
    by spawn-specialist (building the child's lineage), hook-scope-track (persisting
    it) and hook-scope-conflict (reading the writer's lineage), so the env-var wire
    format has one definition."""
    seen: set = set()
    out: "list[str]" = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return out


def format_lineage(ids: "list[str]") -> str:
    """Join lineage ids into the comma-separated env-var form parse_lineage reads."""
    return ",".join(ids)


def scope_path(scopes_dir: "str | Path", session_id: str) -> Path:
    return Path(scopes_dir) / f"{_safe(session_id)}.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically (tmp file + rename) so a reader never
    observes a partially-written scope file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load(scopes_dir: "str | Path", session_id: str) -> "ScopeRecord | None":
    """Load one session's record. A missing or corrupt file is absent, never raised."""
    path = scope_path(scopes_dir, session_id)
    if not path.exists():
        return None
    try:
        return ScopeRecord.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(scopes_dir: "str | Path", record: ScopeRecord) -> None:
    _atomic_write(scope_path(scopes_dir, record.session_id), record.to_json())


def record_touch(
    session_id: str, abspath: str, scopes_dir: "str | Path" = DEFAULT_SCOPES_DIR
) -> ScopeRecord:
    """Add abspath to the session's touched_paths (deduped, capped, FIFO-trimmed).

    Does not touch heartbeat_ts — a caller that also wants to mark the session
    live calls heartbeat() separately, keeping the clock injection isolated to
    that one function.
    """
    rec = load(scopes_dir, session_id) or ScopeRecord(session_id=session_id)
    norm = os.path.normpath(abspath)
    if norm not in rec.touched_paths:
        rec.touched_paths.append(norm)
        if len(rec.touched_paths) > MAX_TOUCHED_PATHS:
            rec.touched_paths = rec.touched_paths[-MAX_TOUCHED_PATHS:]
        save(scopes_dir, rec)
    return rec


def set_context(
    session_id: str,
    cwd: "str | None",
    repo_root: "str | None",
    vcs: str,
    scopes_dir: "str | Path" = DEFAULT_SCOPES_DIR,
) -> ScopeRecord:
    """Record/refresh the session's cwd, repo_root and VCS kind."""
    rec = load(scopes_dir, session_id) or ScopeRecord(session_id=session_id)
    rec.cwd = cwd
    rec.repo_root = repo_root
    rec.vcs = vcs
    save(scopes_dir, rec)
    return rec


def record_lineage(
    session_id: str,
    lineage_ids: "list[str]",
    scopes_dir: "str | Path" = DEFAULT_SCOPES_DIR,
) -> ScopeRecord:
    """Persist the session's ancestor lineage onto its record (create-or-update).

    Idempotent: writes only when the stored lineage actually differs, so the
    PostToolUse track hook can call it on every fire without churning the file.
    Loads the existing record first so other fields (touched_paths, heartbeat,
    pid) are preserved, mirroring set_context/heartbeat's read-modify-write.
    """
    rec = load(scopes_dir, session_id) or ScopeRecord(session_id=session_id)
    if rec.lineage_ids != list(lineage_ids):
        rec.lineage_ids = list(lineage_ids)
        save(scopes_dir, rec)
    return rec


def heartbeat(
    session_id: str,
    now_ts: float,
    scopes_dir: "str | Path" = DEFAULT_SCOPES_DIR,
    pid: "int | None" = None,
) -> ScopeRecord:
    """Mark the session live as of now_ts (caller-supplied clock).

    pid, when given, is recorded onto the session's process id (used by
    live_pid_check to narrow heartbeat-freshness with an actual liveness
    probe). Omitting pid (the default) leaves any previously recorded pid
    untouched — a caller that heartbeats without knowing the pid never erases
    one a prior call already resolved.
    """
    rec = load(scopes_dir, session_id) or ScopeRecord(session_id=session_id)
    rec.heartbeat_ts = now_ts
    if pid is not None:
        rec.pid = pid
    save(scopes_dir, rec)
    return rec


def pid_alive(pid: int) -> bool:
    """Probe process liveness via os.kill(pid, 0) semantics.

    True if the process exists (signal delivery would succeed) or if we lack
    permission to signal it (EPERM implies it exists under another uid, e.g. a
    root-owned process) — permission failure is not evidence of absence.
    False only on a confirmed-absent process (ESRCH / ProcessLookupError).
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def live_pid_check(records: "list[ScopeRecord]") -> "Callable[[str], bool]":
    """Build an extra_live_check callable (for live_sessions/detect_conflicts)
    that probes each session's recorded pid via pid_alive.

    A session with no recorded pid (a legacy record, or a session whose pid
    capture never resolved) is treated as alive by this check alone — pid
    liveness only NARROWS what heartbeat freshness already allows through, it
    never widens it. That asymmetry matters: a false "dead" verdict here
    silently disables conflict detection for a session that is actually live,
    while a false "alive" verdict merely falls back to today's heartbeat-only
    behavior.
    """
    pid_by_session = {r.session_id: r.pid for r in records}

    def check(session_id: str) -> bool:
        pid = pid_by_session.get(session_id)
        return pid is None or pid_alive(pid)

    return check


def load_all(scopes_dir: "str | Path" = DEFAULT_SCOPES_DIR) -> "list[ScopeRecord]":
    """Load every session's record under scopes_dir. Corrupt files are skipped,
    not raised — one broken record must not hide the rest of the registry."""
    scopes_dir = Path(scopes_dir)
    if not scopes_dir.is_dir():
        return []
    records = []
    for path in sorted(scopes_dir.glob("*.json")):
        try:
            records.append(ScopeRecord.from_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return records


def live_sessions(
    records: "list[ScopeRecord]",
    now_ts: float,
    ttl_s: float,
    extra_live_check: "Callable[[str], bool] | None" = None,
) -> "list[ScopeRecord]":
    """Filter to records heartbeat-fresh within ttl_s of now_ts.

    extra_live_check, when given, is consulted as an additional liveness gate
    (e.g. cross-checking an agentctl state file) without coupling this module
    to agentctl's schema.
    """
    out = []
    for rec in records:
        if now_ts - rec.heartbeat_ts > ttl_s:
            continue
        if extra_live_check is not None and not extra_live_check(rec.session_id):
            continue
        out.append(rec)
    return out


def delete(scopes_dir: "str | Path", session_id: str) -> None:
    """Remove one session's scope file, if present.

    No-op when the file is already absent (never written, already pruned, or
    deregistered twice) — mirrors load's absent-is-not-an-error contract, so
    a caller (e.g. spawn-specialist's child-exit deregistration) never has to
    special-case "already gone".
    """
    try:
        scope_path(scopes_dir, session_id).unlink()
    except FileNotFoundError:
        pass


def prune_stale(
    scopes_dir: "str | Path", now_ts: float, ttl_s: float
) -> "list[str]":
    """Delete scope files whose heartbeat is older than ttl_s. Returns the
    session_ids removed."""
    removed = []
    for rec in load_all(scopes_dir):
        if now_ts - rec.heartbeat_ts > ttl_s:
            try:
                scope_path(scopes_dir, rec.session_id).unlink()
            except OSError:
                continue
            removed.append(rec.session_id)
    return removed
