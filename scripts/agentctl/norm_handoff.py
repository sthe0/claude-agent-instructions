"""Norm handoff: the order a spawned specialist -- a depth n+1 manager -- receives.

Difficulty removed: a spawned child engine knew nothing of the stage that caused its
spawn, so the child's depth, its parent's norm and the chain of ancestor norms were
rebuilt (or lost) from prose and a bare `AGENT_RECURSION_DEPTH`. The spawn wrapper
writes ONE digest-bound record naming the parent stage's norm, its effective grants,
the child's depth and the ancestor-norm chain, and exports it as
`AGENTCTL_NORM_HANDOFF=<path>:<sha256>`; the child's engine verifies and adopts it as
`SessionState.parent_link`.

This module is the only place that knows the record's shape: the writer
(`scripts/spawn-specialist.py`) and the reader (`cli._adopt_norm_handoff`) both go
through it, so they cannot drift.

It also keeps the child-session record `<state_root>/child-sessions/<parent>-<stage>.json`:
the engine session and workdir of the linked child last spawned for a parent stage, which
a re-dispatch of that stage resumes instead of minting a new session.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .stage_norm import StageNorm
from .store import safe_session_id

NORM_HANDOFF_ENV = "AGENTCTL_NORM_HANDOFF"
HANDOFF_SCHEMA = "norm-handoff/v1"
HANDOFF_DIRNAME = "handoffs"
# Beside `handoffs/`, never inside the parent's session state: no engine code that loads
# and saves that state reads this directory, so an engine that predates it cannot drop
# or reject the record.
CHILD_SESSIONS_DIRNAME = "child-sessions"


def lineage_entry(parent_session: str, stage_index: int, norm: StageNorm) -> dict:
    return {
        "session": parent_session,
        "stage_index": stage_index,
        "norm_digest": norm.norm_digest(),
        "title": norm.title,
        "done_criterion": norm.done_criterion,
    }


def build_record(
    *,
    parent_session: str,
    stage_index: int,
    kind: str,
    norm: StageNorm,
    grants: list[dict] | None,
    depth: int,
    parent_lineage: list[dict] | None,
    created_at: str | None = None,
    closure_digest: str | None = None,
) -> dict:
    """The handoff record. `parent_lineage` is the parent session's own
    `parent_link.norm_lineage` (None for a session nobody linked); the record's lineage
    extends it by the parent stage's own entry, so the last entry always names the
    stage this child was spawned for. `closure_digest` is the stage's
    `plan.stage_closure_digest`: the plan content the stage's brief was built from."""
    record = {
        "schema": HANDOFF_SCHEMA,
        "parent_session": parent_session,
        "stage_index": stage_index,
        "kind": kind,
        "norm": norm.to_dict(),
        "norm_digest": norm.norm_digest(),
        "grants": list(grants or []),
        "depth": depth,
        "norm_lineage": [*(parent_lineage or []), lineage_entry(parent_session, stage_index, norm)],
        "created_at": created_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    if closure_digest is not None:
        record["closure_digest"] = closure_digest
    return record


def format_env_value(path: Path | str, sha256: str) -> str:
    return f"{path}:{sha256}"


def write_record(state_root: Path | str, record: dict) -> tuple[Path, str]:
    """Write `record` atomically (temp file + rename) under `<state_root>/handoffs/` and
    return `(absolute path, sha256 of the written bytes)`. The file name carries the
    record's own digest prefix, so two spawns never share a path."""
    payload = (json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    sha = hashlib.sha256(payload).hexdigest()
    directory = Path(state_root) / HANDOFF_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{safe_session_id(record['parent_session'])}-{record['stage_index']}-{sha[:16]}.json"
    final = (directory / name).absolute()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final, sha


def verify_env_value(raw: str) -> tuple[dict | None, str | None]:
    """Verify an `AGENTCTL_NORM_HANDOFF` value. Returns `(link, None)` -- the dict stored
    as `SessionState.parent_link` -- or `(None, reason)`. Never raises: a handoff that
    cannot be trusted is ignored by the caller, not fatal to the session."""
    value = (raw or "").strip()
    path_text, sep, digest = value.rpartition(":")
    if not sep or not path_text or not digest:
        return None, "malformed value: expected <path>:<sha256>"
    try:
        data = Path(path_text).read_bytes()
    except OSError as exc:
        return None, f"cannot read handoff file {path_text!r}: {exc.strerror or exc}"
    if hashlib.sha256(data).hexdigest() != digest:
        return None, "handoff file bytes do not match the recorded sha256"
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"handoff file is not valid JSON: {exc}"
    if not isinstance(record, dict):
        return None, "handoff record is not an object"
    if record.get("schema") != HANDOFF_SCHEMA:
        return None, f"unsupported handoff schema {record.get('schema')!r}"
    try:
        norm = StageNorm.from_dict(record["norm"])
        norm_digest = norm.norm_digest()
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"handoff norm cannot be reconstructed: {exc}"
    if norm_digest != record.get("norm_digest"):
        return None, "handoff norm does not reproduce its norm_digest"
    depth = record.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        return None, f"handoff depth {depth!r} is not an integer >= 1"
    lineage = record.get("norm_lineage")
    if not isinstance(lineage, list) or not lineage or not isinstance(lineage[-1], dict):
        return None, "handoff norm_lineage is not a non-empty list"
    last = lineage[-1]
    parent_session = record.get("parent_session")
    stage_index = record.get("stage_index")
    if not isinstance(parent_session, str) or not parent_session:
        return None, "handoff parent_session is missing"
    if (last.get("session") != parent_session or last.get("stage_index") != stage_index
            or last.get("norm_digest") != norm_digest):
        return None, "handoff norm_lineage does not end in the parent stage it names"
    return {
        "handoff": str(Path(path_text)),
        "sha256": digest,
        "parent_session": parent_session,
        "stage_index": stage_index,
        "kind": record.get("kind"),
        "norm_digest": norm_digest,
        "norm": record["norm"],
        "grants": record.get("grants") or [],
        "depth": depth,
        "norm_lineage": lineage,
    }, None


def child_session_path(state_root: Path | str, parent_session: str, stage_index: int) -> Path:
    return Path(state_root) / CHILD_SESSIONS_DIRNAME / f"{safe_session_id(parent_session)}-{stage_index}.json"


def write_child_session(
    state_root: Path | str, parent_session: str, stage_index: int, *, session: str, workdir: str,
) -> Path:
    """Record the engine session and spawn workdir of the linked child last spawned for
    `(parent_session, stage_index)`, replacing any earlier record (temp file + rename)."""
    final = child_session_path(state_root, parent_session, stage_index)
    final.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"session": session, "workdir": workdir}, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".child-session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def read_child_session(state_root: Path | str, parent_session: str, stage_index: int) -> dict | None:
    """The `{"session", "workdir"}` record of the previous linked child of the stage, or
    None when there is none or it cannot be read -- a fresh child is then spawned."""
    try:
        record = json.loads(child_session_path(state_root, parent_session, stage_index).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    session, workdir = record.get("session"), record.get("workdir")
    if not (isinstance(session, str) and session and isinstance(workdir, str) and workdir):
        return None
    return {"session": session, "workdir": workdir}
