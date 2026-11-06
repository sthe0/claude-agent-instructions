"""The telemetry record's store: creation, migrations, spool replay, row inserts, fail-open."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

import telemetry
from lib import config_root
from lib.telemetry import emit, store

SHA = "ab" * 32


@pytest.fixture(autouse=True)
def _generous_default_deadline(monkeypatch):
    """A cold bootstrap on a loaded machine can outlast the production 250 ms; a test's
    verdict must not depend on machine load, so deadline tests pass their own `deadline_s`."""
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config_root.telemetry_db()))
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, *args) -> list:
    conn = _db()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _event_ids() -> list:
    return sorted(r["event_id"] for r in _query("SELECT event_id FROM events"))


def _emit_many(session: str, count: int, kind: str = "tick", start: int = 0) -> list:
    ids = [emit.emit(session, kind, {"i": start + i}) for i in range(count)]
    assert None not in ids
    return ids


def _spool_bytes(session: str) -> bytes:
    return emit.spool_path(session).read_bytes()


def _create_record() -> None:
    conn = store.open_record()
    assert conn is not None
    conn.close()


class _FakeTime:
    """A controllable monotonic clock for deadline tests."""

    def __init__(self, monkeypatch):
        self.now = 0.0
        monkeypatch.setattr(store, "_clock", lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _step_per_prefix(monkeypatch, fake: _FakeTime, step: float) -> list:
    sizes = []
    real = store._apply_prefix

    def wrapper(conn, spool, generation, new_offset, lines, origin, deadline):
        sizes.append(len(lines))
        try:
            return real(conn, spool, generation, new_offset, lines, origin, deadline)
        finally:
            fake.advance(step)

    monkeypatch.setattr(store, "_apply_prefix", wrapper)
    return sizes


def _hold_write_lock() -> sqlite3.Connection:
    holder = sqlite3.connect(str(config_root.telemetry_db()), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_record_created_owner_only_under_config_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_TELEMETRY_DB")
    monkeypatch.delenv("AGENTCTL_TELEMETRY_SPOOL")
    home = tmp_path / "agent-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    old_umask = os.umask(0o000)
    try:
        conn = store.open_record()
    finally:
        os.umask(old_umask)
    assert conn is not None
    record = home / "telemetry" / "telemetry.sqlite"
    assert config_root.telemetry_db() == record and record.exists()
    assert oct(record.stat().st_mode & 0o777) == oct(0o600)
    assert oct(record.parent.stat().st_mode & 0o777) == oct(0o700)
    assert oct(store._side_file(record).stat().st_mode & 0o777) == oct(0o600)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_origin_id_created_once_and_kept_in_side_file():
    first = store.origin_id()
    assert first is not None and str(uuid.UUID(first)) == first
    assert store.origin_id() == first
    record = config_root.telemetry_db()
    assert store._side_file(record).read_text().strip() == first
    for suffix in ("", "-wal", "-shm"):
        path = record.with_name(record.name + suffix)
        if path.exists():
            path.unlink()
    assert store.origin_id() == first
    assert _query("SELECT value FROM meta WHERE key = 'origin'")[0][0] == first


def test_replay_is_exactly_once_under_retried_concurrent_and_interrupted_delivery(monkeypatch):
    ids = _emit_many("retry", 50)
    first = store.replay("retry")
    assert (first["inserted"], first["drained"], first["failed"]) == (50, True, False)
    assert store.replay("retry")["inserted"] == 0
    conn = _db()
    conn.execute("DELETE FROM spool_offsets")
    conn.commit()
    conn.close()
    redelivered = store.replay("retry")
    assert redelivered["failed"] is False and redelivered["inserted"] == 0
    assert _event_ids() == sorted(ids)

    interrupted = _emit_many("interrupted", 1100)
    real = store._upsert_offset
    calls = []

    def crash_on_second_prefix(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("process interrupted between prefixes")
        return real(*args)

    monkeypatch.setattr(store, "_upsert_offset", crash_on_second_prefix)
    crashed = store.replay("interrupted")
    assert crashed["failed"] is True and crashed["inserted"] == 500
    monkeypatch.setattr(store, "_upsert_offset", real)
    resumed = store.replay("interrupted")
    assert (resumed["inserted"], resumed["drained"]) == (600, True)
    assert set(interrupted) <= set(_event_ids())

    concurrent = _emit_many("concurrent", 200)
    results = []

    def worker():
        results.append(store.replay("concurrent", deadline_s=10.0))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(results) == 4 and not any(r["failed"] for r in results)
    assert sum(r["inserted"] for r in results) == 200
    rows = _query("SELECT event_id FROM events WHERE session_id = 'concurrent'")
    assert sorted(r[0] for r in rows) == sorted(concurrent)


def test_append_racing_replay_loses_nothing():
    emitted = []
    done = threading.Event()

    def writer():
        for i in range(300):
            emitted.append(emit.emit("race", "tick", {"i": i}))
        done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    while not done.is_set():
        store.replay("race", deadline_s=1.0)
    thread.join(timeout=60)
    final = store.replay("race", deadline_s=5.0)
    assert final["drained"] is True and final["failed"] is False
    assert None not in emitted
    stored = [r[0] for r in _query("SELECT event_id FROM events WHERE session_id = 'race'")]
    assert sorted(stored) == sorted(emitted)
    assert _query("SELECT count FROM losses WHERE session_id = 'race'") == []


def test_torn_line_followed_by_append_loses_only_the_torn_event():
    first = emit.emit("torn", "tick", {"n": 1})
    with open(emit.spool_path("torn"), "ab") as spool:
        spool.write(b'{"event_id": "0190-partial", "kind": "ti')
    second = emit.emit("torn", "tick", {"n": 2})
    result = store.replay("torn")
    assert (result["inserted"], result["torn"]) == (2, 1)
    assert sorted(r[0] for r in _query("SELECT event_id FROM events")) == sorted([first, second])
    assert _query("SELECT count FROM losses WHERE session_id = 'torn' AND loss_mode = 'torn_event'")[0][0] == 1


def test_two_sessions_and_importer_writing_lose_and_duplicate_nothing():
    _create_record()
    emitted = {"alpha": [], "beta": []}
    imported = [f"imported-{n}" for n in range(60)]
    problems = []

    def session(name):
        for i in range(100):
            emitted[name].append(emit.emit(name, "tick", {"i": i}))
            if i % 25 == 24:
                store.replay(name, deadline_s=1.0)
        for _ in range(100):
            if store.replay(name, deadline_s=2.0)["drained"]:
                return
        problems.append(f"{name} never drained")

    def importer():
        for start in range(0, len(imported), 10):
            rows = [{"event_id": event_id, "session_id": "ledger", "kind": "tick", "provenance": "imported",
                     "time": "2026-01-01T00:00:00+00:00"} for event_id in imported[start:start + 10]]
            for _ in range(100):
                outcome = store.insert_rows("events", rows, deadline_s=2.0)
                if outcome["inserted"] == len(rows):
                    break
            else:
                problems.append("importer never inserted a batch")

    threads = [threading.Thread(target=session, args=(n,)) for n in emitted] + [threading.Thread(target=importer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert problems == []
    expected = sorted(emitted["alpha"] + emitted["beta"] + imported)
    assert _event_ids() == expected


def test_lock_wait_and_busy_wait_share_one_deadline_then_deferred(monkeypatch):
    _emit_many("dl", 3)
    _create_record()
    lock_path = config_root.telemetry_spool_dir() / "dl.lock"
    holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    released = threading.Event()

    def release():
        if not released.is_set():
            released.set()
            fcntl.flock(holder, fcntl.LOCK_UN)

    safety = threading.Timer(2.0, release)
    safety.start()
    try:
        with monkeypatch.context() as patch:
            fake = _FakeTime(patch)
            busy = []
            real_set_busy = store._set_busy

            def record_busy(conn, ms):
                busy.append(ms)
                real_set_busy(conn, ms)

            def fake_sleep(seconds):
                fake.advance(seconds)
                if fake.now >= 0.2:
                    release()

            patch.setattr(store, "_sleep", fake_sleep)
            patch.setattr(store, "_set_busy", record_busy)
            shared = store.replay("dl", deadline_s=0.25)
        assert (shared["inserted"], shared["drained"], shared["deferred"]) == (3, True, False)
        assert busy and max(busy) <= 55

        _emit_many("dl2", 2)
        lock_path_2 = config_root.telemetry_spool_dir() / "dl2.lock"
        held = os.open(lock_path_2, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX)
        safety_2 = threading.Timer(2.0, lambda: fcntl.flock(held, fcntl.LOCK_UN))
        safety_2.start()
        try:
            with monkeypatch.context() as patch:
                fake = _FakeTime(patch)
                patch.setattr(store, "_sleep", fake.advance)
                waited = store.replay("dl2", deadline_s=0.25)
            assert (waited["deferred"], waited["drained"], waited["inserted"]) == (True, False, 0)
            assert fake.now >= 0.25
        finally:
            safety_2.cancel()
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)
        assert _query("SELECT count(*) FROM events WHERE session_id = 'dl2'")[0][0] == 0
        assert store.replay("dl2")["inserted"] == 2

        _emit_many("dl3", 2)
        writer = _hold_write_lock()
        try:
            stalled = store.replay("dl3", deadline_s=0.3)
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        assert (stalled["deferred"], stalled["drained"], stalled["inserted"], stalled["failed"]) == (
            True, False, 0, False)
        assert store.replay("dl3")["inserted"] == 2
    finally:
        safety.cancel()
        release()
        os.close(holder)


def test_store_failure_never_raises_and_keeps_the_spool(monkeypatch):
    _emit_many("keep", 5)
    before = _spool_bytes("keep")

    monkeypatch.setenv(emit.FAULT_ENV, "enospc_record")
    assert store.open_record() is None
    assert store.origin_id() is None
    assert store.insert_rows("sessions", [{"session_id": "x"}]) == {"inserted": 0, "deferred": 0, "failed": 1}
    refused = store.replay("keep")
    assert (refused["failed"], refused["inserted"], refused["drained"]) == (True, 0, False)
    assert _spool_bytes("keep") == before
    monkeypatch.delenv(emit.FAULT_ENV)

    _create_record()
    record = config_root.telemetry_db()
    record.write_bytes(b"this is not a sqlite database" * 200)
    for suffix in ("-wal", "-shm"):
        record.with_name(record.name + suffix).unlink(missing_ok=True)
    corrupt = store.replay("keep")
    assert corrupt["failed"] is True and corrupt["inserted"] == 0
    assert _spool_bytes("keep") == before

    record.unlink()
    recovered = store.replay("keep")
    assert (recovered["inserted"], recovered["drained"], recovered["failed"]) == (5, True, False)


def test_emit_failure_never_raises_and_is_counted(monkeypatch):
    monkeypatch.setenv(emit.FAULT_ENV, "emit_fail")
    assert emit.emit("faulty", "tick", {}) is None
    assert emit.drop_count("faulty") == 1
    monkeypatch.setenv(emit.FAULT_ENV, "enospc_spool")
    assert emit.emit("faulty", "tick", {}) is None
    assert emit.drop_count("faulty") == 2
    assert not emit.spool_path("faulty").exists()
    monkeypatch.setenv(emit.FAULT_ENV, "enospc")
    assert emit.emit("faulty", "tick", {}) is None
    assert emit.drop_count("faulty") == 2
    monkeypatch.delenv(emit.FAULT_ENV)
    for bad in (None, "", ".", "..", "a/b", "a\0b", 7):
        assert emit.emit(bad, "tick", {}) is None
    assert emit.emit("faulty", "tick", {}) is not None


def test_fts5_probe_falls_back_to_plain_table(monkeypatch):
    conn = store.open_record()
    assert store.fts5_available(conn) is True
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "artifacts_fts" in names
    conn.close()
    assert store.insert_rows("artifacts", [{"sha256": "s1", "kind": "k", "text": "alpha beta"}])["inserted"] == 1
    assert [r[0] for r in _query("SELECT rowid FROM artifacts_fts WHERE artifacts_fts MATCH 'alpha'")] != []

    record = config_root.telemetry_db()
    for suffix in ("", "-wal", "-shm"):
        record.with_name(record.name + suffix).unlink(missing_ok=True)
    monkeypatch.setattr(store, "_probe_fts5", lambda: False)
    plain = store.open_record()
    assert plain is not None and store.fts5_available(plain) is False
    assert "artifacts_fts" not in {r[0] for r in plain.execute("SELECT name FROM sqlite_master")}
    plain.close()
    assert store.insert_rows("artifacts", [{"sha256": "s2", "kind": "k", "text": "gamma delta"}])["inserted"] == 1
    assert [r[0] for r in _query("SELECT sha256 FROM artifacts WHERE text LIKE '%gamma%'")] == ["s2"]


def test_migration_1_creates_the_r1_tables_and_columns():
    conn = store.open_record()
    assert store.applied_migrations(conn) == [1]
    conn.close()
    expected = {
        "tasks": {"origin", "task_id", "epoch", "status", "weight_class", "synthetic", "opened_at", "ended_at"},
        "sessions": {"session_id", "origin", "parent_session", "role", "role_table_version", "depth", "core_head",
                     "dirty", "harness_version", "task_origin", "task_id", "task_epoch", "started_at"},
        "contours": {"contour_id", "parent_session", "role", "role_table_version", "weight_class", "task_origin",
                     "task_id", "task_epoch"},
        "events": {"event_id", "session_id", "kind", "source_key", "provenance", "time", "backref_id",
                   "child_session_id", "fields"},
        "stages": {"task_origin", "task_id", "task_epoch", "stage_index", "status"},
        "difficulties": {"difficulty_id", "task_origin", "task_id", "task_epoch", "session_id"},
        "reviews": {"review_id", "verdict", "task_id"},
        "spawns": {"spawn_id", "parent_session", "child_session_id", "role", "role_table_version"},
        "token_usage": {"message_id", "session_id", "role", "role_table_version", "phase", "phase_table_version",
                        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"},
        "compactions": {"compaction_id", "session_id"},
        "artifacts": {"artifact_id", "sha256", "kind", "text"},
        "losses": {"session_id", "loss_mode", "loss_mode_table_version", "count"},
        "spool_offsets": {"spool", "generation", "byte_offset"},
    }
    for table, columns in expected.items():
        found = {r["name"] for r in _query(f'PRAGMA table_info("{table}")')}
        assert columns <= found, (table, columns - found)
    assert store.insert_rows("tasks", [{"origin": "o", "task_id": "t", "epoch": "e", "synthetic": 2}])["failed"] == 1
    assert store.insert_rows("tasks", [{"origin": "o", "task_id": "t", "epoch": "e", "synthetic": 1}])["inserted"] == 1


def test_migrations_apply_in_numeric_order_gaps_allowed_and_once(tmp_path, monkeypatch):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_first.sql").write_text(
        "CREATE TABLE a (x INTEGER);\nCREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY, text TEXT NOT NULL);\n")
    (directory / "003_third.sql").write_text("CREATE TABLE c (x INTEGER);\nINSERT INTO c VALUES (1);\n")
    (directory / "notes.txt").write_text("not a migration")
    monkeypatch.setattr(store, "MIGRATIONS_DIR", directory)
    conn = store.open_record()
    assert store.applied_migrations(conn) == [1, 3]
    conn.close()
    (directory / "002_second.sql").write_text("CREATE TABLE b (x INTEGER);\n")
    conn = store.open_record()
    assert store.applied_migrations(conn) == [1, 2, 3]
    assert conn.execute("SELECT count(*) FROM c").fetchone()[0] == 1
    conn.close()
    conn = store.open_record()
    assert store.applied_migrations(conn) == [1, 2, 3]
    conn.close()


def test_newline_terminated_unparsable_line_counted_once_as_torn():
    first = emit.emit("junk", "tick", {})
    with open(emit.spool_path("junk"), "ab") as spool:
        spool.write(b"\nthis is not json\n")
        spool.write(b'\n{"kind": "no-id", "time": "t"}\n')
    last = emit.emit("junk", "tick", {})
    result = store.replay("junk")
    assert (result["inserted"], result["torn"], result["drained"]) == (2, 2, True)
    again = store.replay("junk")
    assert (again["inserted"], again["torn"], again["drained"]) == (0, 0, True)
    assert _query("SELECT count FROM losses WHERE session_id = 'junk' AND loss_mode = 'torn_event'")[0][0] == 2
    assert sorted(r[0] for r in _query("SELECT event_id FROM events")) == sorted([first, last])


def test_spool_generation_change_restarts_at_offset_zero_without_duplicates(monkeypatch):
    monkeypatch.setattr(store, "_inode", lambda stat: 1)
    old = _emit_many("gen", 3)
    assert store.replay("gen")["inserted"] == 3
    emit.spool_path("gen").unlink()
    new = _emit_many("gen", 5, start=100)
    result = store.replay("gen")
    assert (result["inserted"], result["drained"], result["failed"]) == (5, True, False)
    assert _event_ids() == sorted(old + new)
    assert store.replay("gen")["inserted"] == 0
    assert len(_event_ids()) == 8


def test_stored_offset_beyond_size_restarts_at_offset_zero():
    ids = _emit_many("short", 5)
    assert store.replay("short")["inserted"] == 5
    path = emit.spool_path("short")
    parts = path.read_bytes().split(b"\n")
    keep = b"\n".join(parts[:4]) + b"\n"
    os.truncate(path, len(keep))
    late = emit.emit("short", "tick", {"late": True})
    assert path.stat().st_size < _query("SELECT byte_offset FROM spool_offsets WHERE spool = 'short'")[0][0]
    result = store.replay("short")
    assert (result["inserted"], result["drained"]) == (1, True)
    assert _event_ids() == sorted(ids + [late])


def test_prefix_inserts_and_offset_commit_together(monkeypatch):
    _emit_many("atomic", 4)

    def failing(*args):
        raise RuntimeError("offset write failed")

    monkeypatch.setattr(store, "_upsert_offset", failing)
    result = store.replay("atomic")
    assert result["failed"] is True
    assert _event_ids() == []
    assert _query("SELECT count(*) FROM spool_offsets")[0][0] == 0


def test_live_event_replayed_after_import_deletes_the_imported_row():
    ledger = "/ledgers/decisions.jsonl"
    imported_id = store.imported_row_id(ledger, 128, SHA)
    assert store.insert_rows("events", [
        {"event_id": imported_id, "session_id": "live", "kind": "tick", "provenance": "imported", "time": "t"},
        {"event_id": "imported-cost", "session_id": "ledger", "kind": "spawn_cost", "provenance": "imported",
         "time": "t", "child_session_id": "child-7"},
        {"event_id": "bystander", "session_id": "ledger", "kind": "tick", "provenance": "imported", "time": "t"},
    ])["inserted"] == 3
    mirrored = emit.emit("live", "tick", {"n": 1}, backref=(ledger, 128, SHA))
    spawned = emit.emit("live", "spawn", {"child_session_id": "child-7"})
    result = store.replay("live")
    assert (result["inserted"], result["superseded"], result["drained"]) == (2, 2, True)
    assert _event_ids() == sorted(["bystander", mirrored, spawned])
    row = _query("SELECT backref_id FROM events WHERE event_id = ?", mirrored)[0]
    assert row["backref_id"] == imported_id
    assert _query("SELECT child_session_id FROM events WHERE event_id = ?", spawned)[0][0] == "child-7"
    alone = emit.emit("live", "tick", {}, backref=("/nowhere", 1, SHA))
    assert store.replay("live")["superseded"] == 0
    assert alone in _event_ids()


def test_imported_row_id_is_the_one_id_function_of_replay_and_importer(tmp_path):
    origin = store.origin_id()
    real = tmp_path / "ledger.jsonl"
    real.write_text("x\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    expected = str(uuid.uuid5(uuid.UUID(origin), f"{os.path.realpath(real)}\0{42}\0{SHA}"))
    assert store.imported_row_id(real, 42, SHA) == expected
    assert store.imported_row_id(link, 42, SHA) == expected
    assert store.imported_row_id(real, 42, SHA.upper()) == expected
    assert store.imported_row_id(real, 43, SHA) != expected
    assert store.imported_row_id(real, 42, SHA, origin=str(uuid.uuid4())) != expected
    event_id = emit.emit("ids", "tick", {}, backref=(str(link), 42, SHA))
    store.replay("ids")
    assert _query("SELECT backref_id FROM events WHERE event_id = ?", event_id)[0][0] == expected


def test_first_emit_creates_telemetry_and_spool_dirs_with_mode_0700(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_TELEMETRY_DB")
    monkeypatch.delenv("AGENTCTL_TELEMETRY_SPOOL")
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    old_umask = os.umask(0o022)
    try:
        assert emit.emit("first", "tick", {}) is not None
    finally:
        os.umask(old_umask)
    assert oct((home / "telemetry").stat().st_mode & 0o777) == oct(0o700)
    assert oct((home / "telemetry" / "spool").stat().st_mode & 0o777) == oct(0o700)
    assert oct((home / "telemetry" / "spool" / "first.jsonl").stat().st_mode & 0o777) == oct(0o600)


def test_emitted_fields_survive_replay_in_the_fields_column():
    fields = {"a": 1, "nested": {"b": [1, 2, {"c": None}]}, "source_key": "k-9", "text": "naïve ✓"}
    event_id = emit.emit("fields", "tick", fields)
    store.replay("fields")
    row = _query("SELECT fields, source_key, provenance, session_id FROM events WHERE event_id = ?", event_id)[0]
    assert json.loads(row["fields"]) == fields
    assert (row["source_key"], row["provenance"], row["session_id"]) == ("k-9", "live", "fields")


def test_insert_rows_returns_inserted_deferred_failed():
    rows = [{"session_id": "r1", "role": "root"}, {"session_id": "r2", "role": "judge"}]
    assert store.insert_rows("sessions", rows) == {"inserted": 2, "deferred": 0, "failed": 0}
    assert store.insert_rows("sessions", rows) == {"inserted": 2, "deferred": 0, "failed": 0}
    assert store.insert_rows("sessions", []) == {"inserted": 0, "deferred": 0, "failed": 0}

    more = [{"session_id": "r3", "role": "root"}, {"session_id": "r4", "role": "root"}]
    writer = _hold_write_lock()
    try:
        outcome = store.insert_rows("sessions", more, deadline_s=0.2)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert outcome == {"inserted": 0, "deferred": 2, "failed": 0}
    assert _query("SELECT count(*) FROM sessions WHERE session_id IN ('r3', 'r4')")[0][0] == 0

    bad_enum = [{"session_id": "r5", "role": "root"}, {"session_id": "r6", "role": "no-such-role"}]
    assert store.insert_rows("sessions", bad_enum) == {"inserted": 0, "deferred": 0, "failed": 2}
    assert store.insert_rows("sessions", [{"nope": 1}])["failed"] == 1
    assert store.insert_rows("no_such_table", [{"x": 1}])["failed"] == 1
    assert store.insert_rows("spool_offsets", [{"spool": "x"}])["failed"] == 1
    assert _query("SELECT count(*) FROM sessions WHERE session_id IN ('r5', 'r6')")[0][0] == 0


def test_spool_too_large_for_one_deadline_drains_over_successive_calls(monkeypatch):
    ids = _emit_many("big", 1700)
    fake = _FakeTime(monkeypatch)
    sizes = _step_per_prefix(monkeypatch, fake, 0.1)
    first = store.replay("big", deadline_s=0.25)
    assert (first["inserted"], first["deferred"], first["drained"]) == (1500, True, False)
    second = store.replay("big", deadline_s=0.25)
    assert (second["inserted"], second["deferred"], second["drained"]) == (200, False, True)
    assert sizes == [500, 500, 500, 200]
    assert _event_ids() == sorted(ids)


def test_replay_stopped_at_deadline_returns_not_drained(monkeypatch):
    _emit_many("stop", 600)
    fake = _FakeTime(monkeypatch)
    _step_per_prefix(monkeypatch, fake, 0.3)
    stopped = store.replay("stop", deadline_s=0.25)
    assert (stopped["inserted"], stopped["deferred"], stopped["drained"], stopped["failed"]) == (
        500, True, False, False)
    finished = store.replay("stop", deadline_s=0.25)
    assert (finished["inserted"], finished["deferred"], finished["drained"]) == (100, False, True)
    _emit_many("exact", 500)
    exact = store.replay("exact", deadline_s=0.25)
    assert (exact["inserted"], exact["deferred"], exact["drained"]) == (500, False, True)


def test_replay_of_a_missing_or_empty_spool_is_drained():
    assert store.replay("never-written")["drained"] is True
    emit.spool_path("blank").parent.mkdir(parents=True, exist_ok=True)
    emit.spool_path("blank").write_bytes(b"")
    assert store.replay("blank")["drained"] is True
    assert store.replay("a/b")["failed"] is True


def test_sync_retries_with_doubling_backoff_only_within_a_budget(monkeypatch):
    results = iter([
        {"inserted": 1, "deferred": True, "torn": 0, "superseded": 0, "drained": False, "failed": False},
        {"inserted": 2, "deferred": True, "torn": 0, "superseded": 0, "drained": False, "failed": False},
        {"inserted": 3, "deferred": False, "torn": 0, "superseded": 0, "drained": True, "failed": False},
    ])
    calls = []
    monkeypatch.setattr(store, "replay", lambda session: calls.append(session) or next(results))
    waits = []
    clock = [0.0]
    outcome = telemetry.sync("s", 100.0, sleep=lambda s: waits.append(s) or clock.__setitem__(0, clock[0] + s),
                             clock=lambda: clock[0])
    assert waits == [1.0, 2.0] and outcome["inserted"] == 6 and outcome["drained"] is True

    results = iter([{"inserted": 0, "deferred": True, "torn": 0, "superseded": 0, "drained": False,
                     "failed": False}] * 5)
    assert telemetry.sync("s", 0.0, sleep=lambda s: pytest.fail("no wait without a budget"))["deferred"] is True
    assert len(calls) == 4


def test_production_call_deadline_is_250_ms():
    import ast
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    values = [n.value.value for n in tree.body if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) == "DEADLINE_S" for t in n.targets)]
    assert values == [0.25]
