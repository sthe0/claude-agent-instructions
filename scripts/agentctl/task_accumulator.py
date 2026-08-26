"""Cross-session per-task effort accumulator.

Difficulty this removes: `round_release.py` bounds a review/re-run loop WITHIN
a session, and `effort.py`'s REPLANS scale bounds a session's own replan count
against `effort-replan-absolute` -- but both budgets reset to zero on a fresh
session. A task stuck across a session restart (crash, `/clear`, a new
terminal) gets a brand-new budget each time, so two sessions on the SAME
task_id can each independently pay up to the Rule-of-Three threshold before
either one's own valve fires. Concrete incident: task
`hook-guard-permission-self-grant`, sessions `18fb6860` and `2442a5ac`, same
evening, each hit the 3-replan wall on its own.

This module is the ONLY filesystem seam for cross-session task data (mirrors
store.py's role for session state): `effort.py` and `gates.py` stay pure
(AST-purity contract, no file/subprocess/socket/clock reach); a caller such as
`cli.py` reads a task's accumulated totals with `get()`, folds session-local
counts in with `add()`, and passes the resulting numbers to `effort.py` as
plain data -- the same shape `refresh_spend(state, rows, path)` already uses
for the cost ledger.

Schema (``schema_version=1``), one JSON file per `task_id` under
``config_root.agentctl_task_accumulator_dir() / "<sha256(task_id)>.json"``::

    {
      "schema_version": 1,
      "task_id": "<original id, for humans skimming the directory>",
      "per_axis_totals": {
        "replan_count": 0,
        "plan_review_rounds": 0,
        "plan_enumerate_rounds": 0,
        "code_review_rounds": 0
      },
      "session_ids_contributing": ["18fb6860", "2442a5ac"],
      "last_updated": "<caller-supplied timestamp, e.g. an ISO8601 string>"
    }

Concurrency: two live sessions on the same `task_id` may call `add()` at the
same time. `add()` and `reset()` both hold an exclusive `fcntl.flock` on a
sibling `.lock` file across the read-modify-write, so concurrent increments
never drop one another; the visible file itself is replaced atomically
(`os.replace`) so a reader never observes a half-written file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from lib import config_root

#: The four axes this accumulator tracks. `replan_count` is the one currently
#: consumed by `effort.divergence` (the REPLANS scale); the other three are
#: recorded for the same cross-session visibility but are not, in this stage,
#: wired into any live gate -- `plan_review_rounds`/`code_review_rounds`/
#: `plan_enumerate_rounds`'s own round-release valves stay session-local
#: (`round_release.py`, `gates.py`), unchanged by this module.
AXES = ("replan_count", "plan_review_rounds", "plan_enumerate_rounds", "code_review_rounds")

SCHEMA_VERSION = 1

DEFAULT_ROOT = config_root.agentctl_task_accumulator_dir()


def _hash_task_id(task_id: str) -> str:
    return hashlib.sha256((task_id or "").encode("utf-8")).hexdigest()


def _root(root: Path | None) -> Path:
    return Path(root) if root is not None else DEFAULT_ROOT


def _path(task_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"{_hash_task_id(task_id)}.json"


def _empty(task_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "per_axis_totals": {axis: 0 for axis in AXES},
        "session_ids_contributing": [],
        "last_updated": None,
    }


def _coerce(raw: str, task_id: str) -> dict:
    """Parse `raw` file content into the canonical shape, or a fresh `_empty`
    result for anything unparseable/foreign-schema -- forward-migration
    discipline mirroring `state.py`'s tolerant `from_dict` classmethods:
    unrecognized content degrades to "no accumulated total yet" rather than
    raising, since a corrupt/future-schema file is not this task's fault."""
    if not raw.strip():
        return _empty(task_id)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty(task_id)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return _empty(task_id)
    totals = data.get("per_axis_totals") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": data.get("task_id", task_id),
        "per_axis_totals": {axis: int(totals.get(axis, 0) or 0) for axis in AXES},
        "session_ids_contributing": list(data.get("session_ids_contributing") or []),
        "last_updated": data.get("last_updated"),
    }


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(json.dumps(data, indent=2, sort_keys=True))
            tmp.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class _FileLock:
    """Exclusive lock on `path`'s sibling `.lock` file, held for the duration
    of a `with` block. POSIX-only (`fcntl`); degrades to a no-op lock on a
    platform without `fcntl` -- best-effort there, same as the rest of this
    engine's file-based state, which assumes a single POSIX host."""

    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        finally:
            self._fh.close()
        return False


def get(task_id: str, *, root: Path | None = None) -> dict:
    """This task's accumulated cross-session totals, or a zeroed shape if no
    file exists yet -- "no file" and "a freshly `reset` file" are the same
    state by design, so a caller never special-cases the first read."""
    path = _path(task_id, root)
    if not path.exists():
        return _empty(task_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _empty(task_id)
    return _coerce(raw, task_id)


def add(
    task_id: str,
    axis: str,
    count: int,
    *,
    session_id: str | None = None,
    now: str | None = None,
    root: Path | None = None,
) -> dict:
    """Add `count` to `axis`'s running total for `task_id` and persist it,
    returning the updated totals. Guarded by an exclusive lock spanning the
    read-modify-write (see `_FileLock`) so two concurrently-live sessions on
    the same task never drop an increment.

    `count` may be 0 (a no-op fold, e.g. a session that closed having spent no
    rounds on this axis) but never negative -- this is a monotonic
    accumulator, not a settable value; a caller wanting to zero it uses
    `reset()`, the one named, explicit-renegotiation path for that."""
    if axis not in AXES:
        raise ValueError(f"unknown accumulator axis: {axis!r} (expected one of {AXES})")
    if count < 0:
        raise ValueError(f"accumulator counts never decrease: got {count} for axis {axis!r}")
    path = _path(task_id, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), task_id) if path.exists() else _empty(task_id)
        data["per_axis_totals"][axis] += count
        if session_id and session_id not in data["session_ids_contributing"]:
            data["session_ids_contributing"].append(session_id)
        if now is not None:
            data["last_updated"] = now
        _write_atomic(path, data)
        return data


def reset(task_id: str, *, root: Path | None = None) -> dict:
    """Explicit renegotiation: zero this task's accumulator. Never called from
    `cmd_reset` (task-scoped, not session-scoped -- a session reset must not
    silently forgive a task's accumulated cross-session friction); the only
    intended caller is the dedicated `task-reset` subcommand."""
    path = _path(task_id, root)
    data = _empty(task_id)
    with _FileLock(path):
        _write_atomic(path, data)
    return data
