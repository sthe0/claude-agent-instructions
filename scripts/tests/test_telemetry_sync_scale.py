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

Since PR 9 no session is read inside a write transaction: the tests observe it with a probe in the
patched read, a second connection whose BEGIN IMMEDIATE under the store's real deadline must
succeed while the read runs. A waiting session is passed over while its mark is unchanged; every
sync ends with a sync.end row carrying the longest write transaction; a spool still deferred when
the budget runs out is retried by one detached process per stem. The retry tests inject the clock
and the sleep, so none asserts a wall-clock duration.
"""
from __future__ import annotations

import fcntl
import math
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from lib import config_root
from lib.telemetry import live, recorder, store, sync
from telemetry_live_support import events, no_background_sweep

REAL_DEADLINE_S = store.DEADLINE_S
SESSIONS = 120
JUDGES_PER_SESSION = 4
BIG = "big-session"
BIG_EVENTS = 1000
BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


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

        def file_rows(conn, origin, session, *rest):
            self._current.add(session)
            return real_rows(conn, origin, session, *rest)

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
    """The opens are read with the session's events, outside the write transaction (see
    test_materialize_reads_sessions_outside_write_transaction), so they are counted as calls."""
    reads = []
    real = sync._task_opens

    def counted(conn, session):
        if session == BIG:
            reads.append(session)
        return real(conn, session)

    monkeypatch.setattr(sync, "_task_opens", counted)
    assert sync.materialize([BIG])["committed"] is True

    assert len(reads) == 1
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


def test_rotation_advances_when_a_task_row_arrives_before_every_call(record, monkeypatch):
    """A new task row before each call makes every waiting session due again, so the waiting
    marks pass none over: only the cursor carries the rotation on."""
    calls = math.ceil(SESSIONS / sync.WAITING_PER_SYNC)
    visited = set()
    for call in range(calls):
        assert store.insert_rows("tasks", [{"origin": store.origin_id(), "task_id": f"task-{call}",
                                            "epoch": f"open-{call}", "status": "open", "synthetic": 0,
                                            "opened_at": _at(call)}])["inserted"] == 1
        with monkeypatch.context() as scoped:
            transactions = Transactions(scoped)
            assert sync.materialize([])["committed"] is True
            visited |= transactions.waiting()
    assert {_waiting_session(i) for i in range(SESSIONS)} <= visited


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


# --- PR 9: reads outside the write transaction, waiting marks, sync.end, the deferred retry ------

def _write_lock_free() -> bool:
    """Whether another writer gets the write lock now, under the store's real deadline."""
    probe = sqlite3.connect(str(store.record_path()), timeout=REAL_DEADLINE_S)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def _probe_reads(monkeypatch, during) -> list:
    """Patch the per-session event read so `during(session)` runs inside it; returns what it saw."""
    seen = []
    real = sync._session_events

    def read(conn, session):
        seen.append((session, during(session)))
        return real(conn, session)
    monkeypatch.setattr(sync, "_session_events", read)
    return seen


def test_materialize_reads_sessions_outside_write_transaction(record, monkeypatch):
    _replayed("replayed-a")
    seen = _probe_reads(monkeypatch, lambda session: _write_lock_free())

    assert sync.materialize([BIG, "replayed-a"])["committed"] is True

    assert {BIG, "replayed-a"} <= {session for session, _ in seen}
    assert seen and all(free for _, free in seen), seen


def test_writer_with_short_deadline_commits_while_materialize_files_large_session(record, monkeypatch):
    writes = iter(range(10_000))

    def write(session):
        if session != BIG:
            return None
        n = next(writes)
        return store.insert_rows("events", [_event(f"writer-{n}", "writer", "note", 5 + n)],
                                 deadline_s=REAL_DEADLINE_S)
    seen = _probe_reads(monkeypatch, write)

    assert sync.materialize([BIG])["committed"] is True

    outcomes = [outcome for session, outcome in seen if session == BIG]
    assert outcomes and all(o["inserted"] == 1 and not o["deferred"] for o in outcomes), outcomes


def _visited(monkeypatch) -> "set[str]":
    with monkeypatch.context() as scoped:
        transactions = Transactions(scoped)
        assert sync.materialize([])["committed"] is True
        return transactions.waiting()


WAITING_THREE = {_waiting_session(i) for i in range(3)}


def test_materialize_skips_waiting_session_unchanged_since_last_try(monkeypatch):
    _add_waiting(range(3))
    assert _visited(monkeypatch) == WAITING_THREE
    assert _visited(monkeypatch) == set()


def test_materialize_rereads_waiting_session_that_gained_events(monkeypatch):
    _add_waiting(range(3))
    assert _visited(monkeypatch) == WAITING_THREE
    session = _waiting_session(1)
    assert store.insert_rows("events", [_event(f"{session}-late", session, "judge", 9)])["inserted"] == 1
    assert _visited(monkeypatch) == {session}


def test_materialize_rereads_waiting_session_after_task_row_arrives(monkeypatch):
    _add_waiting(range(3))
    assert _visited(monkeypatch) == WAITING_THREE
    assert store.insert_rows("tasks", [{"origin": store.origin_id(), "task_id": "task-new", "epoch": "new-open",
                                        "status": "open", "synthetic": 0, "opened_at": _at(9)}])["inserted"] == 1
    assert _visited(monkeypatch) == WAITING_THREE


def test_materialize_rereads_waiting_session_after_session_key_set_by_update(monkeypatch):
    """An UPDATE adds no rowid anywhere; only the count of keyed sessions rows sees it."""
    _add_waiting(range(3))
    assert _visited(monkeypatch) == WAITING_THREE

    def key(conn):
        conn.execute("UPDATE sessions SET task_id = 'keyed' WHERE session_id = ?", (_waiting_session(0),))
    assert store.transact(key)["committed"] is True
    assert _visited(monkeypatch) >= WAITING_THREE - {_waiting_session(0)}


@pytest.fixture
def live_env(monkeypatch):
    """A sync's verdict must not depend on the caller's session, the real canon checkout, the
    real transcripts or a background sweep."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv(live.SWITCH_ENV, raising=False)
    monkeypatch.delenv(sync.HARNESS_VERSION_ENV, raising=False)
    monkeypatch.setattr(sync, "_canon_state", lambda: ("canon-head", 0))
    monkeypatch.setattr(config_root, "projects_roots", lambda: [])
    no_background_sweep(monkeypatch)
    assert store.transact(lambda conn: None)["committed"] is True


def _emit(session: str) -> None:
    assert live.emit_event(session, "note", "telemetry:test", {}) is not None


@contextmanager
def _held(filename: str):
    """Hold an flock in the spool dir, as a competing process would."""
    fd = os.open(live.spool_dir() / filename, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield fd
    finally:
        os.close(fd)


def _own(kind: str) -> list:
    return events(kind=kind)


def test_sync_end_records_max_lock_hold_including_materialize(live_env, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(store, "_hold_clock", lambda: clock[0])
    monkeypatch.setattr(store, "_hold", {"began": None, "max_ms": 0.0})
    real = sync._materialize_session

    def slow(conn, origin, session, *rest):
        clock[0] += 7.0
        return real(conn, origin, session, *rest)
    monkeypatch.setattr(sync, "_materialize_session", slow)
    _emit("s-held")

    sync.run("s-held")

    [end] = _own(sync.SYNC_END)
    assert end["fields"]["max_lock_hold_ms"] == 7000.0


def test_sync_end_written_when_nothing_committed(live_env):
    result = sync.run()

    assert result["committed"] == [] and result["deferred"] == []
    [start] = _own(sync.TRIGGER_START)
    [end] = _own(sync.SYNC_END)
    assert end["fields"]["trigger_start"] == start["time"]
    assert {k: end["fields"][k] for k in ("kind", "committed", "deferred", "retries")} == {
        "kind": "sweep", "committed": 0, "deferred": 0, "retries": 0}


def test_deferred_spool_retried_after_budget_until_commit(live_env, monkeypatch):
    """The parent's own session stem and another stem: each deferred, each retried to its A mark."""
    for stem in ("s-own", "s-other"):
        _emit(stem)
    monkeypatch.setattr(store, "DEADLINE_S", 0.2)
    with _held("s-own.lock"), _held("s-other.lock"):
        parent = sync.run("s-own", budget_s=0)
    assert sorted(parent["deferred"]) == sorted(parent["retried"]) == ["s-other", "s-own"]
    assert sorted(stem for stem, _, _ in sync.spawned_retries) == ["s-other", "s-own"]
    assert {session for _, _, session in sync.spawned_retries} == {"s-own"}
    begun = sync._iso(sync.spawned_retries[0][1])

    for stem, parent_begun, session in sync.spawned_retries:
        outcome = sync.run_retry(stem, sync._iso(parent_begun), session)
        assert outcome["outcome"] == "replayed" and outcome["committed"] is True, outcome

    commits = [e["fields"] for e in _own(sync.SYNC_COMMIT)]
    assert sorted(c["session"] for c in commits) == ["s-other", "s-own"]
    assert all(c["retry"] is True for c in commits)
    assert len(_own(sync.TRIGGER_START)) == 1
    ends = [e["fields"] for e in _own(sync.SYNC_END)]
    assert sorted(e["kind"] for e in ends) == ["retry", "retry", "session_end"]
    assert {e["trigger_start"] for e in ends} == {begun}


def test_retry_after_budget_stops_when_another_sync_replayed(live_env, monkeypatch):
    _emit("s-done")
    _emit("s-stuck")
    sync.run()
    _emit("s-stuck")
    monkeypatch.setattr(store, "DEADLINE_S", 0.01)
    begun = sync._now()
    now, slept = [begun], []
    monkeypatch.setattr(sync, "_now", lambda: now[0])

    def sleep(seconds):
        slept.append(seconds)
        now[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(sync, "_sleep", sleep)

    with _held("s-done.lock"):
        done = sync.run_retry("s-done", sync._iso(begun))
    assert done["outcome"] == "replayed_elsewhere" and done["attempts"] == 1 and slept == []

    with _held("s-stuck.lock"):
        stuck = sync.run_retry("s-stuck", sync._iso(begun))
    assert stuck["outcome"] == "ceiling" and stuck["committed"] is False
    assert max(slept) == sync.BACKOFF_MAX_S and sum(slept) <= sync.RETRY_CEILING_S
    retry_ends = [e["fields"] for e in _own(sync.SYNC_END) if e["fields"]["kind"] == "retry"]
    assert [e["attempts"] for e in retry_ends] == [1, stuck["attempts"]]


def test_deferred_spool_retried_by_one_sync_only(live_env):
    _emit("s-held")
    before = len(events())

    with _held("s-held" + sync.RETRY_LOCK_SUFFIX):
        outcome = sync.run_retry("s-held", sync._iso(sync._now()))

    assert outcome == {"kind": "retry", "stem": "s-held", "outcome": "held_elsewhere"}
    assert len(events()) == before


def test_sweep_retry_does_not_hold_sweep_lock(live_env, monkeypatch):
    """A sweep holds SWEEP_LOCK on an inheritable descriptor (recorder passes it to the sweep);
    the retry child it starts must not inherit it, or every later sweep is refused for the retry's life."""
    started = []
    real_popen = subprocess.Popen

    def popen(args, **kwargs):
        started.append(kwargs)
        process = real_popen(args, **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(sync.subprocess, "Popen", popen)
    live.spool_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(live.spool_dir() / recorder.SWEEP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(fd, True)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        sync._popen_detached([sys.executable, "-c", "import time; time.sleep(60)"])
    finally:
        os.close(fd)
    kwargs, process = started
    try:
        assert kwargs["close_fds"] is True and not kwargs.get("pass_fds")
        with _held(recorder.SWEEP_LOCK):
            pass
    finally:
        process.kill()
        process.wait()
