"""Atomic sidecar files for the detached enumeration worker's result payload.

The detached enumeration child (cmd_question_enumerate_worker) must NEVER call
store.save() -- FileStateStore.save() is an unlocked, whole-state truncating
write (store.py:49-52), and the child's write racing against whatever
`approve`/`replan` the user runs next would corrupt or silently lose state.
Instead the child writes its result here, keyed by (session_id, plan content
digest) so it can only ever be folded into the bag it was actually computed
against; cmd_approve/cmd_replan read it (non-destructively -- see
read_discarding_superseded) and persist the fold with their own store.save().

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


def read_discarding_superseded(
    session_id: str, content_digest: str, *, root: Path | None = None
) -> dict | None:
    """The payload a background enumeration wrote for THIS EXACT plan content, or
    None if none has landed yet.

    The MATCHING sidecar is LEFT IN PLACE, which is what makes the fold idempotent:
    a `cmd_approve` that folds the payload and then refuses on the very blockers
    the fold just raised persists nothing, so the next `approve` must be able to
    read the same payload again. An unlink-on-read here instead left the session
    with candidates that existed nowhere on disk and no sidecar to re-fold, and
    `_ENUMERATE_NOT_RUN` forever after (there is no launch site on the approve
    path) -- the whole point of detaching, undone.

    Sidecars for OTHER digests ARE removed: a result computed against an abandoned
    plan (a replan that changed the content before the child finished) is dead
    weight, and leaving it would let it resurface if that digest came back around.
    A concurrent worker's `.tmp-*.json` is skipped -- unlinking one mid-write makes
    its `os.replace` raise."""
    r = root if root is not None else DEFAULT_ROOT
    match = sidecar_path(session_id, content_digest, root=r)
    result = None
    if match.exists():
        try:
            result = json.loads(match.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None
    _discard_for_session(session_id, root=r, keep=match.name)
    return result


def discard_all_for_session(session_id: str, *, root: Path | None = None) -> None:
    """Remove every sidecar (any digest) for `session_id` -- session-end cleanup,
    called from cmd_resolve whether or not a sidecar was ever read. The read path
    keeps its own matching sidecar and so calls _discard_for_session directly."""
    _discard_for_session(session_id, root=root)


def _discard_for_session(
    session_id: str, *, root: Path | None = None, keep: str | None = None
) -> None:
    r = root if root is not None else DEFAULT_ROOT
    session_dir = _session_dir(r, session_id)
    if not session_dir.is_dir():
        return
    for f in session_dir.glob("*.json"):
        if f.name.startswith(".tmp-") or f.name == keep:
            continue
        try:
            f.unlink()
        except OSError:
            pass
    try:
        session_dir.rmdir()
    except OSError:
        pass
