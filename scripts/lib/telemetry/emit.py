"""Spool emitter: append one event to the session's private spool, never block, never raise.

Difficulty removed: hooks run as short processes many times per command, so letting each
open SQLite would put lock waits on the hook path. An event is instead one line appended
to `<config root>/telemetry/spool/<session>.jsonl` with one O_APPEND write (no lock, no
fsync, hook_telemetry's discipline); `store.replay` applies the spool idempotently. The
event id (UUIDv7, RFC 9562) is assigned here, before any delivery attempt, so a retried
or concurrent replay can only re-insert the same key.

Every append is `b"\\n" + JSON + b"\\n"` in one write: the leading newline terminates a
torn predecessor so it is never glued to this event. A failed or short write, or any other
exception, is swallowed and counted as one line in `<session>.drops`; if that marker also
fails the loss is uncounted. This module imports neither sqlite3 nor store.

`AGENTCTL_TELEMETRY_FAULT` is a test-only comma list of fault tokens read here and in
store: `emit_fail` (emission raises internally), `enospc_spool` (the spool append fails
with ENOSPC), `enospc_record` (the store's writes fail with ENOSPC), `enospc_marker` (the
drop marker fails too) and `enospc` (all three ENOSPC tokens).

Stdlib and config_root only.
"""
from __future__ import annotations

import errno
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lib import config_root

FAULT_ENV = "AGENTCTL_TELEMETRY_FAULT"
SPOOL_SUFFIX = ".jsonl"
DROPS_SUFFIX = ".drops"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def fault_active(token: str) -> bool:
    tokens = os.environ.get(FAULT_ENV, "").split(",")
    return token in tokens or (token.startswith("enospc_") and "enospc" in tokens)


def uuid7() -> str:
    """A UUIDv7 (RFC 9562): 48-bit Unix millisecond timestamp, then random bits."""
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ensure_private_dir(path: Path) -> None:
    """Create `path` and any missing ancestors with mode 0700 set explicitly: a hook's
    emit may be the first writer, and the umask must not decide the mode."""
    missing = []
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory)
        except FileExistsError:
            continue
        os.chmod(directory, PRIVATE_DIR_MODE)


def session_file_stem(session_id) -> "str | None":
    """The session id as a spool file stem, or None when it cannot be one."""
    if not isinstance(session_id, str) or not session_id or session_id in (".", ".."):
        return None
    if "/" in session_id or "\0" in session_id:
        return None
    return session_id


def spool_path(session_id: str) -> Path:
    return config_root.telemetry_spool_dir() / (session_id + SPOOL_SUFFIX)


def drops_path(session_id: str) -> Path:
    return config_root.telemetry_spool_dir() / (session_id + DROPS_SUFFIX)


def drop_count(session_id: str) -> int:
    """Number of emissions this session's emitter counted as dropped."""
    try:
        data = drops_path(session_id).read_bytes()
    except OSError:
        return 0
    return data.count(b"\n")


def _append(path: Path, data: bytes) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
    try:
        return os.write(fd, data)
    finally:
        os.close(fd)


def _mark_drop(session_id: str, reason: str) -> None:
    try:
        if fault_active("enospc_marker"):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        ensure_private_dir(config_root.telemetry_spool_dir())
        row = json.dumps({"time": now_iso(), "reason": reason}, separators=(",", ":")) + "\n"
        _append(drops_path(session_id), row.encode("utf-8"))
    except Exception:
        pass


def _backref(backref) -> "dict | None":
    if backref is None:
        return None
    if isinstance(backref, dict):
        path, offset, sha = backref["path"], backref["offset"], backref["sha256"]
    else:
        path, offset, sha = backref
    return {"path": str(path), "offset": int(offset), "sha256": str(sha)}


def emit(session_id, kind, fields, backref=None):
    """Append one event; return its event id, or None when it could not be appended.
    Never raises. `backref` is (source path, byte offset, sha256 of the line) of the file
    ledger line this event mirrors, as a tuple or a dict with those three keys."""
    stem = session_file_stem(session_id)
    if stem is None:
        return None
    try:
        if fault_active("emit_fail"):
            raise RuntimeError("injected emission failure")
        event_id = uuid7()
        event = {
            "event_id": event_id,
            "session_id": stem,
            "kind": kind,
            "time": now_iso(),
            "provenance": "live",
            "backref": _backref(backref),
            "fields": fields if fields is not None else {},
        }
        line = b"\n" + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"
        ensure_private_dir(config_root.telemetry_spool_dir())
        if fault_active("enospc_spool"):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        written = _append(spool_path(stem), line)
        if written != len(line):
            _mark_drop(stem, "short_write")
            return None
        return event_id
    except Exception as exc:
        _mark_drop(stem, type(exc).__name__)
        return None
