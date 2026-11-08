"""The sync's write lock stays bounded as the record grows.

Difficulty removed: at live size (5.3M events) a session-end `telemetry.py sync` held the store's
write transaction for minutes, so the importer, which waits at most a few seconds per batch,
deferred until its 300 s budget ran out and exited incomplete. Two things scaled with the record:
materialize() filed every waiting session (the parent of any task-less contour, 905 of 969 live
sessions) inside one store.transact, and inside it _task_key_for re-read a session's task-open
events once per spawn and judge event, quadratic in a session's event count. The session-end
reads also scanned all events (no index on kind or on the fields the latency rule reads);
migration 010 adds those indexes.

The record built here (SESSIONS waiting sessions holding SESSIONS * JUDGES_PER_SESSION task-less
judge contours, plus one session with BIG_EVENTS spawn and judge events) is small enough for the
suite; the tests assert the shape (one session per transaction, at most the cap per call, one
task-open read per session, index searches), not the time. The same record at 240 sessions x 125
contours plus 5000 big-session events, one materialize([BIG]) call on the dev VM on 2026-11-08
(recorded, never asserted, because wall-clock depends on the machine): the unfixed code held one
write transaction for 361 s; the fixed code ran 103 transactions, the longest 4.1 s.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

import pytest

from lib.telemetry import store, sync

SESSIONS = 120
JUDGES_PER_SESSION = 4
BIG = "big-session"
BIG_EVENTS = 1000
BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)
TASK_OPEN_SQL = re.compile(r"kind = 'task' AND json_extract\(fields, '\$\.action'\) = 'open'")


@pytest.fixture(autouse=True)
def _patient_store(monkeypatch):
    """A cold bootstrap on a loaded machine can outlast the production 250 ms."""
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)


def _at(seconds: float) -> str:
    return sync._iso(BASE + timedelta(seconds=seconds))


def _event(event_id: str, session: str, kind: str, seconds: float, fields: str = "{}", child=None) -> dict:
    return {"event_id": event_id, "session_id": session, "kind": kind, "source_key": "test", "provenance": "live",
            "time": _at(seconds), "child_session_id": child, "fields": fields}


def _waiting_session(index: int) -> str:
    return f"w{index:05d}"


def _add_waiting(indices) -> None:
    """Sessions whose events name no task, each the parent of JUDGES_PER_SESSION task-less judge contours."""
    events, contours = [], []
    for index in indices:
        session = _waiting_session(index)
        events.append(_event(f"{session}-s", session, "session", index, '{"depth":0}'))
        events.append(_event(f"{session}-j", session, "judge", index + 0.5))
        contours.extend({"contour_id": f"{session}-c{n}", "parent_session": session, "role": "judge"}
                        for n in range(JUDGES_PER_SESSION))
    assert store.insert_rows("events", events)["inserted"] == len(events)
    assert store.insert_rows("contours", contours)["inserted"] == len(contours)


def _add_big() -> None:
    """One session with BIG_EVENTS spawn and judge events after the event that opens its task."""
    events = [_event("big-open", BIG, "task", 0, '{"action":"open","task_id":"task-big"}')]
    for n in range(BIG_EVENTS):
        kind = "spawn" if n % 2 else "judge"
        events.append(_event(f"big-{n:05d}", BIG, kind, 1 + n, child=f"big-kid-{n}" if kind == "spawn" else None))
    assert store.insert_rows("events", events)["inserted"] == len(events)
    assert store.insert_rows("tasks", [{"origin": store.origin_id(), "task_id": "task-big", "epoch": "big-open",
                                        "status": "open", "synthetic": 0, "opened_at": _at(0)}])["inserted"] == 1


def _replayed(session: str) -> None:
    """A session the sync replayed: live events and a spool offset, no sessions row yet."""
    assert store.insert_rows("events", [_event(f"{session}-s", session, "session", 1, '{"depth":0}')])["inserted"] == 1

    def offset(conn):
        conn.execute("INSERT INTO spool_offsets (spool, generation, byte_offset) VALUES (?, 'g', 1)", (session,))
    assert store.transact(offset)["committed"] is True


def _has_session_row(session: str) -> bool:
    conn = store.open_record()
    try:
        return conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session,)).fetchone() is not None
    finally:
        conn.close()


@pytest.fixture
def record():
    _add_waiting(range(SESSIONS))
    _add_big()


class Transactions:
    """Wraps store.transact and the two per-session filing functions: the sessions each write
    transaction filed."""

    def __init__(self, monkeypatch):
        self.filed: "list[set[str]]" = []
        self._current: "set[str]" = set()
        real_transact = store.transact
        real_rows, real_session = sync._file_task_rows, sync._materialize_session

        def transact(work, deadline_s=None):
            self._current = set()
            self.filed.append(self._current)
            return real_transact(work, deadline_s)

        def file_rows(conn, origin, session):
            self._current.add(session)
            return real_rows(conn, origin, session)

        def materialize_session(conn, origin, session, *rest):
            self._current.add(session)
            return real_session(conn, origin, session, *rest)

        monkeypatch.setattr(store, "transact", transact)
        monkeypatch.setattr(sync, "_file_task_rows", file_rows)
        monkeypatch.setattr(sync, "_materialize_session", materialize_session)

    def sessions(self) -> "set[str]":
        return set().union(*self.filed) if self.filed else set()

    def waiting(self) -> "set[str]":
        return {s for s in self.sessions() if s.startswith("w")}


def test_each_write_transaction_files_at_most_one_session_and_a_call_takes_at_most_the_cap(record, monkeypatch):
    _replayed("replayed-a")
    transactions = Transactions(monkeypatch)

    outcome = sync.materialize([BIG, "replayed-a"])

    assert outcome["committed"] is True
    assert all(len(filed) <= 1 for filed in transactions.filed)
    assert {BIG, "replayed-a"} <= transactions.sessions()
    assert 0 < len(transactions.waiting()) <= sync.WAITING_PER_SYNC


def test_task_opens_of_a_session_are_read_once_per_materialize_session_call(record, monkeypatch):
    statements = []
    real = sync._materialize_session

    def traced(conn, origin, session, *rest):
        if session == BIG:
            conn.set_trace_callback(statements.append)
        try:
            return real(conn, origin, session, *rest)
        finally:
            conn.set_trace_callback(None)

    monkeypatch.setattr(sync, "_materialize_session", traced)
    assert sync.materialize([BIG])["committed"] is True

    assert len([s for s in statements if TASK_OPEN_SQL.search(s)]) == 1
    conn = store.open_record()
    try:
        assert conn.execute("SELECT COUNT(*) FROM contours WHERE parent_session = ? AND task_id = 'task-big'",
                            (BIG,)).fetchone()[0] == BIG_EVENTS
    finally:
        conn.close()


def test_latency_and_task_epoch_reads_search_an_index_not_scan_events(record):
    statements = []
    conn = store.open_record()
    try:
        conn.set_trace_callback(statements.append)
        sync.latency_marks(conn, BIG)
        sync.latency_marks(conn, _waiting_session(0))
        sync.events_of_task(conn, store.origin_id(), "task-big", "big-open")
        conn.set_trace_callback(None)
        reads = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "FROM events" in s
                 and ("kind = " in s or "json_extract(fields, '$.session')" in s or "'$.task_epoch'" in s)]
        assert len(reads) >= 4
        for sql in reads:
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
            on_events = [step for step in plan if re.search(r"\bevents\b", step)]
            assert on_events and all(step.startswith("SEARCH") for step in on_events), (sql, plan)
    finally:
        conn.close()


def _attach(sessions) -> None:
    """Move sessions out of the waiting set: their contours get a task."""
    def work(conn):
        for session in sessions:
            conn.execute("UPDATE contours SET task_id = 'attached' WHERE parent_session = ?", (session,))
    assert store.transact(work)["committed"] is True


def test_rotation_visits_every_waiting_session_within_ceil_n_over_cap_calls(record, monkeypatch):
    left = {_waiting_session(i) for i in range(5, 40, 7)}
    joined = range(SESSIONS, SESSIONS + 30)
    assert SESSIONS > 2 * sync.WAITING_PER_SYNC
    calls = math.ceil((SESSIONS + len(joined)) / sync.WAITING_PER_SYNC)
    visited = set()
    for call in range(calls):
        with monkeypatch.context() as scoped:
            transactions = Transactions(scoped)
            assert sync.materialize([])["committed"] is True
            assert len(transactions.waiting()) <= sync.WAITING_PER_SYNC
            visited |= transactions.waiting()
        if call == 0:
            _attach(left)
            _add_waiting(joined)

    waiting_throughout = {_waiting_session(i) for i in range(SESSIONS)} - left
    assert waiting_throughout <= visited


def test_a_replayed_session_left_unfiled_by_a_failed_call_is_filed_by_the_next(record, monkeypatch):
    _replayed("replayed-a")
    _replayed("replayed-b")
    real = sync._materialize_session

    def fails_on_b(conn, origin, session, *rest):
        if session == "replayed-b":
            raise RuntimeError("lock lost mid-call")
        return real(conn, origin, session, *rest)

    with monkeypatch.context() as scoped:
        scoped.setattr(sync, "_materialize_session", fails_on_b)
        first = sync.materialize(["replayed-a", "replayed-b"])
    assert first["committed"] is False and first["failed_session"] == "replayed-b"
    assert _has_session_row("replayed-a") and not _has_session_row("replayed-b")

    assert sync.materialize([])["committed"] is True
    assert _has_session_row("replayed-b")
