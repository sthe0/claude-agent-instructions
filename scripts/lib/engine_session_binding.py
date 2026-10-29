"""Which engine session a harness session drives.

The harness conversation id (``CLAUDE_CODE_SESSION_ID``, the ``session_id`` in every hook
payload) may drive a different engine session through ``agentctl --session E``. The
production-edit gate only receives the payload, so the cli records here, whenever a verb
saves the state of the session it ran on, that the harness session drives that engine
session; the gate then reads its decision from the engine session it was bound to.

One JSON file per harness session under ``<agentctl dir>/engine-session-bindings``,
holding the bound engine sessions in drive order (the last entry is the most recent).
Recency is the order of the list, not a clock reading, so two drives within one clock
tick still order correctly; each entry's ``ts`` is diagnostic only and is never read back.
A binding whose state file no longer exists is skipped on
read, never trusted.

The same records answer the reverse question, which conversation transcript belongs to an
engine session (``transcript_for_session``): the delivery proof and the writer gate read the
transcript of the conversation that ran the session.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path

from lib import config_root

BINDINGS_DIRNAME = "engine-session-bindings"


def _bindings_dir() -> Path:
    return config_root.agentctl_dir() / BINDINGS_DIRNAME


def _binding_path(harness_session: str) -> Path:
    return _bindings_dir() / f"{config_root.sanitize_session_id(harness_session)}.json"


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("driven") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("session"), str)]


def record_drive(harness_session: str, engine_session: str, now: dt.datetime) -> None:
    """Bind ``engine_session`` to ``harness_session`` as its most recently driven one."""
    if not harness_session or not engine_session:
        return
    engine = config_root.sanitize_session_id(engine_session)
    path = _binding_path(harness_session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = [e for e in _read(path) if e["session"] != engine]
        entries.append({"session": engine, "ts": now.isoformat(timespec="seconds")})
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps({"driven": entries}), encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def driven_sessions(harness_session: str) -> list[str]:
    """Every engine session bound to ``harness_session`` whose state file exists, most
    recently driven first."""
    if not harness_session:
        return []
    entries = _read(_binding_path(harness_session))
    return [
        e["session"] for e in reversed(entries)
        if config_root.resolve_agentctl_state_file(e["session"]) is not None
    ]


def harness_sessions_driving(engine_session: str) -> list[str]:
    """Every harness session recorded as having driven ``engine_session`` (unordered)."""
    if not engine_session:
        return []
    engine = config_root.sanitize_session_id(engine_session)
    return [p.stem for p in binding_paths() if any(e["session"] == engine for e in _read(p))]


def binding_paths() -> list[Path]:
    """Every binding file, sorted by name: the files share one directory, so this is path order
    without comparing thousands of path objects."""
    try:
        return sorted(_bindings_dir().glob("*.json"), key=lambda path: path.name)
    except OSError:
        return []


def harness_index() -> dict[str, list[str]]:
    """``harness_sessions_driving`` for every engine session at once, from one pass over the
    binding files: a reader of many tasks scans the store once rather than once per task."""
    index: dict[str, list[str]] = {}
    for path in binding_paths():
        for engine in dict.fromkeys(e["session"] for e in _read(path)):
            index.setdefault(engine, []).append(path.stem)
    return index


def _is_linked_child(session: str) -> bool:
    path = config_root.resolve_agentctl_state_file(session)
    if path is None:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("parent_link"), dict)


def transcript_for_session(session: str) -> Path | None:
    """The newest transcript of the conversation that ran engine ``session``, or None.

    A conversation writes its transcript under its harness id, which is the engine session
    id only when the coordinator never passed ``--session``. The candidates are therefore
    ``session`` itself and every harness session bound to it, so continuing an old session
    from a new conversation works. A linked child's harness id equals its engine id by
    construction, and its parent drives it with verbs of its own (approve, resolve) from a
    transcript that may be newer, so a linked child looks only at its own transcript; the
    parent pointer in its state is never followed. Derived, never supplied: the gated actor
    must not choose which transcript is checked."""
    if not session:
        return None
    found: list[Path] = []
    names = [session] if _is_linked_child(session) else [session, *harness_sessions_driving(session)]
    for name in dict.fromkeys(names):
        found.extend(config_root.iter_transcripts(f"*/{name}.jsonl"))
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def resolve(harness_session: str) -> str | None:
    """The most recently driven engine session of ``harness_session`` that still has a
    state file, or None when nothing is bound (the caller then uses the harness id)."""
    driven = driven_sessions(harness_session)
    return driven[0] if driven else None
