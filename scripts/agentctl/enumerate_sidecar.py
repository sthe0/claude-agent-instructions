"""Atomic sidecar files for the detached enumeration worker's result payload.

The detached enumeration child (cmd_question_enumerate_worker) must NEVER call
store.save() -- FileStateStore.save() is an unlocked, whole-state truncating
write (store.py:49-52), and the child's write racing against whatever
`approve`/`replan` the user runs next would corrupt or silently lose state.
Instead the child writes its result here, keyed by (session_id, plan content
digest) so it can only ever be folded into the bag it was actually computed
against; cmd_approve/cmd_replan read-and-discard it before their own existing
store.save() persists the fold.

Atomic by construction: write to a temp file in the same directory, then
os.replace() over the final path -- a reader never observes a partial write
(os.replace is a single rename syscall on POSIX).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from lib import config_root

from .store import _safe

DEFAULT_ROOT = config_root.agentctl_enumerate_sidecar_dir()


def _session_dir(root: Path, session_id: str) -> Path:
    return root / _safe(session_id)


def sidecar_path(session_id: str, content_digest: str, *, root: Path | None = None) -> Path:
    r = root if root is not None else DEFAULT_ROOT
    return _session_dir(r, session_id) / f"{content_digest}.json"


def write(session_id: str, content_digest: str, payload: dict, *, root: Path | None = None) -> None:
    """Atomically write `payload` (JSON-serializable) to the sidecar keyed by
    (session_id, content_digest). Called ONLY by the detached worker -- never
    by a command that also calls store.save()."""
    target = sidecar_path(session_id, content_digest, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_and_discard(session_id: str, content_digest: str, *, root: Path | None = None) -> dict | None:
    """The payload a background enumeration wrote for THIS EXACT plan content,
    or None if none has landed yet. Any sidecar found for `session_id` is
    removed regardless of whether its digest matches -- a sidecar computed
    against an abandoned digest (a replan that changed the plan before the
    child finished) is dead weight, discarded rather than left to resurface
    against a later plan."""
    r = root if root is not None else DEFAULT_ROOT
    match = sidecar_path(session_id, content_digest, root=r)
    result = None
    if match.exists():
        try:
            result = json.loads(match.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None
    discard_all_for_session(session_id, root=r)
    return result


def discard_all_for_session(session_id: str, *, root: Path | None = None) -> None:
    """Remove every sidecar (any digest) for `session_id`. Called from
    read_and_discard (stale-digest cleanup) and from cmd_resolve (session-end
    cleanup, whether or not a sidecar was ever read)."""
    r = root if root is not None else DEFAULT_ROOT
    session_dir = _session_dir(r, session_id)
    if not session_dir.is_dir():
        return
    for f in session_dir.glob("*.json"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        session_dir.rmdir()
    except OSError:
        pass
