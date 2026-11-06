"""Shared driver for the live-emission tests of the telemetry record (identity, events,
recorder cost, dual write): run agentctl in process, read the record and the spool back."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from pathlib import Path

from agentctl import cli
from lib import config_root
from lib.telemetry import emit, recorder, store

SHA = "cd" * 32
REAL_MAYBE_SWEEP = recorder.maybe_sweep


def no_background_sweep(monkeypatch) -> None:
    """A test's verdict must not depend on a detached `telemetry.py sync --stale` racing it:
    the command-path sweep is off unless the test starts it itself (REAL_MAYBE_SWEEP)."""
    monkeypatch.setattr(recorder, "maybe_sweep", lambda session: False)


def agentctl(state_root, *argv) -> dict:
    """One agentctl verb through `cli.main` (the recording wrapper); returns its directive plus `rc`."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.main(["--state-root", str(state_root), *argv])
    directive = json.loads(out.getvalue())
    directive["rc"] = rc
    return directive


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config_root.telemetry_db()))
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, *args) -> list:
    conn = db()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def events(session: "str | None" = None, kind: "str | None" = None, source_key: "str | None" = None) -> list:
    """Events of the record, oldest first, each a dict with `fields` parsed."""
    sql, args = "SELECT * FROM events WHERE 1 = 1", []
    for column, value in (("session_id", session), ("kind", kind), ("source_key", source_key)):
        if value is not None:
            sql += f" AND {column} = ?"
            args.append(value)
    rows = []
    for row in query(sql + " ORDER BY time, event_id", *args):
        item = dict(row)
        item["fields"] = json.loads(item["fields"] or "{}")
        rows.append(item)
    return rows


def spool_events(session: str) -> list:
    path = emit.spool_path(session)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tasks() -> list:
    return [dict(r) for r in query("SELECT * FROM tasks ORDER BY opened_at, epoch")]


def start(state_root, session: str, task: str = "task-a", *extra) -> dict:
    return agentctl(state_root, "start", "--session", session, "--task", task, *extra)


def reset(state_root, session: str, task: str, *extra) -> dict:
    return agentctl(state_root, "reset", "--session", session, "--task", task, *extra)


def make_resolved(state_root, session: str) -> None:
    """Put the session's engine state at RESOLVED the way a stored state would be."""
    from agentctl.state import Node
    from agentctl.store import FileStateStore
    file_store = FileStateStore(Path(state_root))
    state = file_store.load(session)
    state.node = Node.RESOLVED.value
    state.resolution.passed = True
    file_store.save(state)


def open_epoch(session: str) -> "str | None":
    from lib.telemetry import live
    data = live.read_epoch(session)
    return data["epoch"] if data else None


def record_events_for_fixture(rows: list) -> dict:
    return store.insert_rows("events", rows)
