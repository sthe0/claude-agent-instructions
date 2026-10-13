"""Cross-session per-task effort accumulator.

Difficulty this removes: `round_release.py` bounds a review/re-run loop WITHIN
a session, and `effort.py`'s REPLANS scale bounds a session's own replan count
against `effort-replan-absolute` -- but both budgets reset to zero on a fresh
session. A task stuck across a session restart (crash, `/clear`, a new
terminal) gets a brand-new budget each time, so two sessions on the SAME
task_id can each independently pay up to the `effort-replan-absolute` threshold before
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

Schema (``schema_version=1``; adding an AXIS is deliberately not a version event,
see ``READABLE_SCHEMA_VERSIONS``), one JSON file per `task_id` under
``config_root.agentctl_task_accumulator_dir() / "<sha256(task_id)>.json"`` --
honors an ``$AGENTCTL_TASK_ACCUMULATOR_DIR`` override (mirroring
`edit_ledger.py`'s ``$AGENTCTL_EDIT_LEDGER``), which is what lets the test
suite redirect every `cli.py` call site to a per-test tmp dir instead of the
real cross-machine accumulator directory::

    {
      "schema_version": 1,
      "task_id": "<original id, for humans skimming the directory>",
      "per_axis_totals": {
        "replan_count": 0,
        "plan_review_rounds": 0,
        "plan_enumerate_rounds": 0,
        "code_review_rounds": 0,
        "resolved_reentry": 0,
        "review_rounds": 0,
        "replans_since_user_decision": 0,
        "review_rounds_since_user_decision": 0
      },
      "review_rounds_last_sha256": "<plan digest the last review round was counted for>",
      "session_ids_contributing": ["18fb6860", "2442a5ac"],
      "agent_backstop_crossings": [{"event": "agent_backstop_crossed", "session": "...", "ts": 0.0}],
      "last_updated": "<caller-supplied timestamp, e.g. an ISO8601 string>"
    }

`agent_backstop_crossings` is the task's own record of every since-user backstop the agent
crossed (`record_crossing`): the counters those crossings are measured on are per task, so the
disclosure owed at verify-final and at resolve has to be per task too -- a session's history
alone would drop a crossing made by an earlier session of the same task. `reset()` carries it
over and no user decision clears it, because the disclosure is owed for every crossing of the
task until the task resolves. Resolve then clears exactly the entries it disclosed
(`discard_crossings`), so a reopened task, or a later task that shares the id, starts without
the crossings the user has already accepted.

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

#: The axes this accumulator tracks. `replan_count` is the one consumed by
#: `effort.divergence` (the REPLANS scale) and `resolved_reentry` the one consumed
#: by `gates.resolved_reentry_blockers`; the remaining three are recorded for the
#: same cross-session visibility but are not wired into any live gate --
#: `plan_review_rounds`/`code_review_rounds`/`plan_enumerate_rounds`'s own
#: round-release valves stay session-local (`round_release.py`, `gates.py`),
#: unchanged by this module.
#:
#: `resolved_reentry` counts how many times a task that had reached RESOLVED was
#: re-opened by `cmd_reset`. It lives HERE and not on `SessionState` for the reason
#: this whole module exists, in its sharpest form: `cmd_reset` builds a brand-new
#: SessionState, so a counter kept there would be zeroed by the very act it is
#: supposed to count -- an unbounded reopen loop that resets its own budget each lap.
#:
#: `review_rounds` is the plan-review valve's own never-reset spend: one per reviewed
#: plan version or thinker spawn of the task, counted by `add(count_if_digest_differs=)`
#: / `add(force_digest=)` below. Unlike `plan_review_rounds` (folded in only at
#: approve/replan, from a session counter those commands zero) it is incremented at
#: record time, so the review loop cannot reset the budget that is meant to end it.
#:
#: `replans_since_user_decision` / `review_rounds_since_user_decision` are the two
#: backstop axes of the agent self-decision rule (`cli._agent_self_decision_refusal`):
#: they advance by exactly what `replan_count` / `review_rounds` advance by, but
#: `reset()` leaves them alone -- an agent's own `--renegotiation-decision continue`
#: zeroes the ceiling counters, so only a decision the USER took
#: (`clear_since_user_decision`) may zero these, which is what lets them stop an
#: agent that keeps re-deciding for itself.
SINCE_USER_AXES = ("replans_since_user_decision", "review_rounds_since_user_decision")

AXES = (
    "replan_count", "plan_review_rounds", "plan_enumerate_rounds", "code_review_rounds",
    "resolved_reentry", "review_rounds",
) + SINCE_USER_AXES

SCHEMA_VERSION = 1

#: Schema versions `_coerce` can read, and the seam a genuine BREAKING change would
#: use. Adding an axis is not one: `_coerce` is name-keyed and zero-fills every axis
#: (`totals.get(axis, 0)`), so a file written before `resolved_reentry` existed arrives
#: with that axis at 0 and its accumulated `replan_count` intact, and a version event
#: would buy nothing. It would COST something, though, and the cost is why the version
#: stays at 1: this repo is routinely worked from several concurrent worktrees at
#: different commits, and code that does not yet know the newer number reads a file
#: stamped with it as foreign -- i.e. as `_empty()` -- silently zeroing the very
#: cross-session totals this module exists to preserve. `2` stays READABLE because a
#: build during development wrote it; nothing writes it now.
#:
#: Staying at 1 has its own smaller residual, disclosed for the same reason as the
#: `task_id`-bypass in README honest limit 7: `_coerce` keys `per_axis_totals` on
#: THIS module's own `AXES` tuple (`{axis: totals.get(axis, 0) for axis in AXES}`),
#: not on whatever keys the file happens to hold. An older-commit worktree whose
#: `AXES` predates a newly-added axis still reads schema_version 1 as its own, so
#: its next `add()` writes a `per_axis_totals` with that axis silently absent --
#: dropping just the new axis's count, not the whole file (the same holds for the
#: `agent_backstop_crossings` list, which such a worktree also rebuilds without).
#: Smaller blast radius than a version bump (which would zero every axis), and
#: still the reason a genuinely new axis is worth landing everywhere before any
#: worktree starts relying on it.
READABLE_SCHEMA_VERSIONS = (1, 2)


def _hash_task_id(task_id: str) -> str:
    return hashlib.sha256((task_id or "").encode("utf-8")).hexdigest()


def _root(root: Path | None) -> Path:
    """Resolved lazily per call, never cached at import time, so an
    `$AGENTCTL_TASK_ACCUMULATOR_DIR` override set by a test AFTER this module
    is imported still takes effect -- the same discipline `edit_ledger.py`'s
    `_ledger_path()` and `judge_ledger.py` already use for their own
    env-overridable paths."""
    if root is not None:
        return Path(root)
    override = os.environ.get("AGENTCTL_TASK_ACCUMULATOR_DIR")
    if override:
        return Path(override).expanduser()
    return config_root.agentctl_task_accumulator_dir()


def _path(task_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"{_hash_task_id(task_id)}.json"


def _empty(task_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "per_axis_totals": {axis: 0 for axis in AXES},
        "review_rounds_last_sha256": "",
        "session_ids_contributing": [],
        "agent_backstop_crossings": [],
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
    if not isinstance(data, dict) or data.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
        return _empty(task_id)
    totals = data.get("per_axis_totals") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": data.get("task_id", task_id),
        "per_axis_totals": {axis: int(totals.get(axis, 0) or 0) for axis in AXES},
        "review_rounds_last_sha256": str(data.get("review_rounds_last_sha256") or ""),
        "session_ids_contributing": list(data.get("session_ids_contributing") or []),
        "agent_backstop_crossings": [
            dict(c) for c in data.get("agent_backstop_crossings") or [] if isinstance(c, dict)],
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
    now: float | str | None = None,
    root: Path | None = None,
    count_if_digest_differs: str | None = None,
    force_digest: str | None = None,
    mirror_axis: str | None = None,
) -> dict:
    """Add `count` to `axis`'s running total for `task_id` and persist it,
    returning the updated totals. Guarded by an exclusive lock spanning the
    read-modify-write (see `_FileLock`) so two concurrently-live sessions on
    the same task never drop an increment.

    `count` may be 0 (a no-op fold, e.g. a session that closed having spent no
    rounds on this axis) but never negative -- this is a monotonic
    accumulator, not a settable value; a caller wanting to zero it uses
    `reset()`, the one named, explicit-renegotiation path for that.

    The two digest parameters make `add` the one place a plan version becomes a
    counted review round (`review_rounds` axis only): both store the plan digest
    in the task's `review_rounds_last_sha256`, and the comparison with the stored
    digest and the increment are one critical section -- a caller never reads the
    digest and then decides, so two callers holding a stale read still produce one
    increment. `count_if_digest_differs=<sha>` adds `count` only when the stored
    digest differs from `<sha>` (a pair record on an already counted version adds
    nothing); `force_digest=<sha>` always adds `count` (a thinker spawn is a round
    in itself) and records `<sha>` so a pairwise run right after it on the same
    version does not count again.

    `mirror_axis` adds, in the same critical section, exactly what `axis` received --
    after a digest-suppressed count is already 0 -- to a second axis, so a since-user
    twin of a total can never drift from it under concurrent sessions."""
    if axis not in AXES:
        raise ValueError(f"unknown accumulator axis: {axis!r} (expected one of {AXES})")
    if mirror_axis is not None and mirror_axis not in AXES:
        raise ValueError(f"unknown accumulator axis: {mirror_axis!r} (expected one of {AXES})")
    if count < 0:
        raise ValueError(f"accumulator counts never decrease: got {count} for axis {axis!r}")
    if count_if_digest_differs is not None and force_digest is not None:
        raise ValueError("count_if_digest_differs and force_digest are mutually exclusive")
    digest = count_if_digest_differs if count_if_digest_differs is not None else force_digest
    if digest is not None and axis != "review_rounds":
        raise ValueError("a plan digest is only tracked for the 'review_rounds' axis")
    path = _path(task_id, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), task_id) if path.exists() else _empty(task_id)
        if count_if_digest_differs is not None and data["review_rounds_last_sha256"] == digest:
            count = 0
        data["per_axis_totals"][axis] += count
        if mirror_axis is not None:
            data["per_axis_totals"][mirror_axis] += count
        if digest is not None:
            data["review_rounds_last_sha256"] = digest
        if session_id and session_id not in data["session_ids_contributing"]:
            data["session_ids_contributing"].append(session_id)
        if now is not None:
            data["last_updated"] = now
        _write_atomic(path, data)
        return data


def reset(task_id: str, *, root: Path | None = None) -> dict:
    """Explicit renegotiation: zero this task's accumulator. Never called from
    `cmd_reset` (task-scoped, not session-scoped -- a session reset must not
    silently forgive a task's accumulated cross-session friction). Two
    deliberate callers, both an explicit renegotiation regardless of which
    command records it: the dedicated `task-reset` subcommand, and
    `cmd_replan`'s `--renegotiation-decision continue|rescope` path (the
    customer decision that clears the diagnosing_replan round-release
    ceiling, GitHub #177) -- the latter also being the fix for GitHub #201's
    unbounded REPLANS-scale re-fire, since effort.py's effective_deltas()
    reads this same accumulator field against a static ceiling and never
    resets it itself.

    The `SINCE_USER_AXES` survive: an agent's own continue decision is one of the
    callers, and zeroing the since-user counters there would let that decision
    reset the very backstop that bounds it. So does the list of recorded backstop
    crossings, which the same decision is itself one of the writers of."""
    path = _path(task_id, root)
    with _FileLock(path):
        previous = _coerce(path.read_text(encoding="utf-8"), task_id) if path.exists() else _empty(task_id)
        data = _empty(task_id)
        for axis in SINCE_USER_AXES:
            data["per_axis_totals"][axis] = previous["per_axis_totals"][axis]
        data["agent_backstop_crossings"] = previous["agent_backstop_crossings"]
        _write_atomic(path, data)
    return data


def crossing_key(crossing: dict) -> tuple | None:
    """What identifies one recorded crossing -- the session that made it and when -- or None
    for an entry that carries neither (it then never matches another one)."""
    session, ts = crossing.get("session"), crossing.get("ts")
    return None if session is None and ts is None else (session, ts)


def record_crossing(
    task_id: str, crossing: dict, *, session_id: str | None = None,
    now: float | str | None = None, root: Path | None = None,
) -> dict:
    """Append `crossing` to the task's record of agent backstop crossings, once: an entry
    with the same `crossing_key` already on record is left as it is. Held under the same lock
    as every other read-modify-write here, so two live sessions never drop one another's."""
    path = _path(task_id, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), task_id) if path.exists() else _empty(task_id)
        key = crossing_key(crossing)
        if key is None or all(crossing_key(c) != key for c in data["agent_backstop_crossings"]):
            data["agent_backstop_crossings"].append(dict(crossing))
        if session_id and session_id not in data["session_ids_contributing"]:
            data["session_ids_contributing"].append(session_id)
        if now is not None:
            data["last_updated"] = now
        _write_atomic(path, data)
        return data


def discard_crossings(
    task_id: str, crossings: list[dict], *, now: float | str | None = None,
    root: Path | None = None,
) -> dict:
    """Remove from the task's record exactly the entries whose `crossing_key` matches one of
    `crossings` -- what a resolve has just disclosed. Matched under the lock against the file
    as it is now, so an entry another session recorded since the caller took its snapshot
    survives. Writes nothing when nothing matches (nor creates the task's record; the lock
    still creates the directory and its `.lock` file). An entry with neither `session` nor
    `ts` (`crossing_key` None) is never discarded: `record_crossing` stamps both, so only a
    hand-edited or foreign file can hold one."""
    keys = {crossing_key(c) for c in crossings} - {None}
    path = _path(task_id, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), task_id) if path.exists() else _empty(task_id)
        kept = [c for c in data["agent_backstop_crossings"] if crossing_key(c) not in keys]
        if len(kept) != len(data["agent_backstop_crossings"]):
            data["agent_backstop_crossings"] = kept
            if now is not None:
                data["last_updated"] = now
            _write_atomic(path, data)
        return data


def clear_since_user_decision(
    task_id: str, *, session_id: str | None = None, now: float | str | None = None,
    root: Path | None = None,
) -> dict:
    """Zero only the `SINCE_USER_AXES`: the user took a decision (approval,
    fire-acknowledgement, renegotiation, review override, task-reset), so the
    agent's autonomous-decision backstops start counting again from here. Leaves
    every other axis, the review digest and the recorded backstop crossings as they
    are: a user decision restarts the counters, it does not forgive a crossing."""
    path = _path(task_id, root)
    with _FileLock(path):
        data = _coerce(path.read_text(encoding="utf-8"), task_id) if path.exists() else _empty(task_id)
        for axis in SINCE_USER_AXES:
            data["per_axis_totals"][axis] = 0
        if session_id and session_id not in data["session_ids_contributing"]:
            data["session_ids_contributing"].append(session_id)
        if now is not None:
            data["last_updated"] = now
        _write_atomic(path, data)
        return data
