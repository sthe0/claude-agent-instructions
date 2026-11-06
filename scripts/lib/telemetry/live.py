"""The live emission layer every Core emitter writes through.

Difficulty removed: the engine, hooks, spawner and landing script must put their acts into the
record without any of them importing the store, opening the record, or deciding for itself what
an event looks like. This module is the one place that knows the off switch, the registry
source key every event names, the task epoch open in the session, and how a ledger line's
back-reference (path, offset, sha256 of the bytes written) is taken. It imports `emit`,
`config_root` and the standard library only, so a hook's import closure stays off sqlite3.

`AGENTCTL_TELEMETRY=off` makes every function here a no-op that opens neither spool nor record.
The per-session `<session>.epoch` sidecar in the spool directory names the task epoch the
session works under; the wrapper in `cli.main` writes it and every later emit reads it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from lib import config_root
from lib.telemetry import emit

SWITCH_ENV = "AGENTCTL_TELEMETRY"
SYNTHETIC_ENV = "AGENTCTL_TELEMETRY_SYNTHETIC"
EPOCH_SUFFIX = ".epoch"
END_SUFFIX = ".end"
SYNC_SESSION = "telemetry:sync"
LANDING_SESSION = "telemetry:landing"
PRECOMMIT_SESSION = "telemetry:precommit"
LIVE_TTL_S = 1800.0

_recorder_seconds = 0.0
_current_verb: "str | None" = None


def set_verb(verb: "str | None") -> None:
    """The agentctl verb the process is running; the gate events its handlers log name it."""
    global _current_verb
    _current_verb = verb


def current_verb() -> "str | None":
    return _current_verb


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "").strip().lower() != "off"


def spool_dir() -> Path:
    return config_root.telemetry_spool_dir()


def end_marker_path(session_id: str) -> Path:
    return spool_dir() / (session_id + END_SUFFIX)


def epoch_path(session_id: str) -> Path:
    return spool_dir() / (session_id + EPOCH_SUFFIX)


def read_epoch(session_id: str) -> "dict | None":
    """The session's open task epoch ({task_id, epoch, resolved}), or None."""
    try:
        data = json.loads(epoch_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("epoch") else None


def write_epoch(session_id: str, task_id: str, epoch: str, resolved: bool = False) -> None:
    """Replace the sidecar (write to a temp name, then rename, so a reader never sees half of it)."""
    stem = emit.session_file_stem(session_id)
    if stem is None or not enabled():
        return
    try:
        emit.ensure_private_dir(spool_dir())
        path = epoch_path(stem)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"task_id": task_id, "epoch": epoch, "resolved": bool(resolved)}),
                       encoding="utf-8")
        os.chmod(tmp, emit.PRIVATE_FILE_MODE)
        os.replace(tmp, path)
    except OSError:
        pass


def recorder_total() -> float:
    """Seconds this process has spent inside emit_event; the wrapper reads the delta over a command."""
    return _recorder_seconds


def emit_event(session_id, kind: str, source_key: str, fields: "dict | None" = None, backref=None) -> "str | None":
    """Append one live event naming its emitter by `source_key`; the event carries the task
    epoch open in its session at emit time. Never raises; None when off or not appended."""
    global _recorder_seconds
    if not enabled():
        return None
    started = time.perf_counter()
    try:
        body = dict(fields or {})
        body["source_key"] = source_key
        stem = emit.session_file_stem(session_id)
        if stem is not None and "task_epoch" not in body:
            epoch = read_epoch(stem)
            if epoch is not None:
                body["task_epoch"] = epoch["epoch"]
        return emit.emit(session_id, kind, body, backref)
    except Exception:
        return None
    finally:
        _recorder_seconds += time.perf_counter() - started


def emit_landing(event: str, session_id: "str | None", fields: dict) -> "str | None":
    """One landing event (`land:<event>`); a landing run without an engine session writes it
    under the shared landing spool, so its row carries no task."""
    return emit_event(session_id or LANDING_SESSION, "landing", f"land:{event}", fields)


def append_line(path, data: bytes, mode: int = emit.PRIVATE_FILE_MODE) -> "tuple[str, int, str]":
    """Append `data` to a file ledger with one O_APPEND write (as the ledger writers always have)
    and return its back-reference (path, offset, sha256): the offset is the file position after
    the write minus the length written, so it is exact under concurrent appenders. Raises OSError
    as the plain write would; the caller keeps its own fail-open."""
    path = Path(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        end = os.lseek(fd, 0, os.SEEK_CUR)
    finally:
        os.close(fd)
    return str(path), end - len(data), hashlib.sha256(data).hexdigest()


def spool_or_end_marker_exists(session_id: str) -> bool:
    """The file half of 'a session has live events': its spool or its end marker exists."""
    stem = emit.session_file_stem(session_id)
    return stem is not None and (emit.spool_path(stem).exists() or end_marker_path(stem).exists())
