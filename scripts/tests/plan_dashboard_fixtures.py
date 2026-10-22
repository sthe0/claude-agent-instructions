"""Fixture roots and the read-only guard for the plan-dashboard tests.

A fixture root is a throwaway agent home laid out as the engine lays it out::

    <home>/agentctl/state/<session>.json
    <home>/agentctl/state/child-sessions/<session>.json
    <home>/agentctl/engine-session-bindings/<harness session>.json
    <home>/projects/<project>/<harness session>.jsonl      (transcripts)
    <home>/spawn-costs.jsonl                               (the spawn ledger)

``read_only_guard`` is how a test proves the data layer writes nothing under such a root:
it refuses every write-shaped call under the root, makes the files read-only on disk, and
checks on exit that no path was added or removed and no byte changed.

What the call-time guard does not intercept: ``os.remove``, ``os.mkdir``, ``os.rmdir`` and
``shutil.rmtree``. A write by those is caught afterwards, by the snapshot of paths and bytes,
and by the read-only mode on disk when the tests do not run as root; as root only the
snapshot catches it.
"""
from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

PROJECT = "-fixture-project"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


@dataclass(frozen=True)
class FixtureRoot:
    state_root: Path
    cost_log: Path
    transcripts_root: Path
    home: Path


def set_mtime(path: Path, ts: dt.datetime) -> None:
    stamp = ts.timestamp()
    os.utime(path, (stamp, stamp))


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def build_state_dir(tmp: Path, sessions, *, bindings=None, now: dt.datetime | None = None) -> Path:
    """Write ``sessions`` (SessionState objects) under ``tmp/home/agentctl/state``.

    ``bindings`` maps a harness session id to the engine session ids it drove. A session
    with a ``parent_link`` also gets a ``child-sessions`` file. With ``now`` given, a
    session's file mtime is its newest history ``ts`` (else ``now``).
    """
    home = Path(tmp) / "home"
    state_root = home / "agentctl" / "state"
    (state_root / "child-sessions").mkdir(parents=True, exist_ok=True)
    for state in sessions:
        path = state_root / f"{state.session_id}.json"
        path.write_text(state.to_json(), encoding="utf-8")
        stamps = [e["ts"] for e in state.history if isinstance(e, dict) and e.get("ts")]
        if now is not None:
            newest = dt.datetime.fromisoformat(max(stamps)) if stamps else now
            set_mtime(path, newest)
        link = getattr(state, "parent_link", None)
        if link:
            child = state_root / "child-sessions" / f"{state.session_id}.json"
            child.write_text(json.dumps({"parent_session": link.get("parent_session")
                                         if isinstance(link, dict) else getattr(link, "parent_session", None)}),
                             encoding="utf-8")
    if bindings:
        bdir = home / "agentctl" / "engine-session-bindings"
        bdir.mkdir(parents=True, exist_ok=True)
        for harness, engines in bindings.items():
            (bdir / f"{harness}.json").write_text(
                json.dumps({"driven": [{"session": e, "ts": "2026-10-01T00:00:00+00:00"} for e in engines]}),
                encoding="utf-8",
            )
    return state_root


def _transcript_line(row: dict) -> str:
    message = {"id": row["message_id"], "model": row.get("model", "claude-sonnet-5-5"), "usage": row["usage"]}
    obj = {"type": "assistant", "message": message}
    if row.get("ts") is not None:
        obj["timestamp"] = row["ts"] if isinstance(row["ts"], str) else _iso(row["ts"])
    return json.dumps(obj)


def write_transcript(transcripts_root: Path, harness_session: str, rows, *, project: str = PROJECT) -> Path:
    path = Path(transcripts_root) / project / f"{harness_session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_transcript_line(r) + "\n" for r in rows), encoding="utf-8")
    return path


def build_fixture_root(tmp, sessions, *, ledger_rows=(), transcripts=None, bindings=None,
                       now: dt.datetime) -> FixtureRoot:
    """A complete fixture root; every written file's mtime is set relative to ``now``.

    ``transcripts`` maps a harness session id to a list of usage rows, each a dict with
    ``message_id``, ``ts`` (ISO string or datetime), ``usage`` and optionally ``model``.
    """
    tmp = Path(tmp)
    state_root = build_state_dir(tmp, sessions, bindings=bindings, now=now)
    home = tmp / "home"
    cost_log = home / "spawn-costs.jsonl"
    if ledger_rows:
        cost_log.write_text("".join(json.dumps(r) + "\n" for r in ledger_rows), encoding="utf-8")
        set_mtime(cost_log, now)
    transcripts_root = home / "projects"
    for harness, rows in (transcripts or {}).items():
        path = write_transcript(transcripts_root, harness, rows)
        set_mtime(path, now)
    return FixtureRoot(state_root=state_root, cost_log=cost_log, transcripts_root=transcripts_root, home=home)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            out[os.path.join(base, name)] = None
        for name in files:
            full = os.path.join(base, name)
            with _REAL_OPEN(full, "rb") as fh:
                out[full] = fh.read()
    return out


_REAL_OPEN = builtins.open
_REAL_OS_OPEN = os.open
_REAL_FLOCK = fcntl.flock
_REAL_LOCKF = fcntl.lockf
_REAL_RENAME = os.rename
_REAL_REPLACE = os.replace
_REAL_UNLINK = os.unlink


class WriteAttempt(AssertionError):
    """A call that would write, lock or move a file under the guarded root."""


@contextlib.contextmanager
def read_only_guard(root: Path) -> Iterator[None]:
    root = Path(root).resolve()
    prefix = str(root) + os.sep

    def under(path) -> bool:
        if isinstance(path, int):
            return False
        try:
            text = os.path.abspath(os.fsdecode(os.fspath(path)))
        except TypeError:
            return False
        return text == str(root) or text.startswith(prefix)

    def refuse(what: str, path) -> None:
        raise WriteAttempt(f"read_only_guard: {what} {path!r} under {root}")

    def guarded_open(file, mode="r", *args, **kwargs):
        if under(file) and any(c in mode for c in "wax+"):
            refuse(f"open(mode={mode!r})", file)
        return _REAL_OPEN(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if under(path) and flags & WRITE_FLAGS:
            refuse(f"os.open(flags={flags:#o})", path)
        return _REAL_OS_OPEN(path, flags, *args, **kwargs)

    def guarded_lock(*args, **kwargs):
        raise WriteAttempt("read_only_guard: a file lock was requested")

    def guarded_move(real, label):
        def wrapper(src, dst, *args, **kwargs):
            if under(src) or under(dst):
                refuse(label, (src, dst))
            return real(src, dst, *args, **kwargs)
        return wrapper

    def guarded_unlink(path, *args, **kwargs):
        if under(path):
            refuse("os.unlink", path)
        return _REAL_UNLINK(path, *args, **kwargs)

    before = _snapshot(root)
    modes: dict[str, int] = {}
    chmod = os.geteuid() != 0
    if chmod:
        for path in before:
            modes[path] = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(path, 0o555 if before[path] is None else 0o444)
        os.chmod(root, 0o555)
        modes[str(root)] = 0o755
    else:
        warnings.warn("read_only_guard: running as root, so the chmod part of the guard is skipped "
                      "(root ignores file modes); the call interception still applies")

    saved = (builtins.open, io.open, os.open, fcntl.flock, fcntl.lockf, os.rename, os.replace, os.unlink)
    builtins.open = io.open = guarded_open
    os.open = guarded_os_open
    fcntl.flock = fcntl.lockf = guarded_lock
    os.rename = guarded_move(_REAL_RENAME, "os.rename")
    os.replace = guarded_move(_REAL_REPLACE, "os.replace")
    os.unlink = guarded_unlink
    try:
        yield
    finally:
        builtins.open, io.open, os.open, fcntl.flock, fcntl.lockf, os.rename, os.replace, os.unlink = saved
        for path, mode in modes.items():
            os.chmod(path, mode)
    after = _snapshot(root)
    assert sorted(after) == sorted(before), "read_only_guard: the set of paths under the root changed"
    changed = [p for p in before if before[p] != after[p]]
    assert not changed, f"read_only_guard: file bytes changed: {changed[:3]}"
