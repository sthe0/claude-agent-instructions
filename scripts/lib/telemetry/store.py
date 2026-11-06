"""The telemetry record's single writer: creation, migrations, spool replay, row inserts.

Difficulty removed: many short processes may emit events at once, and a recorder must
neither delay a command nor lose an event. Emitters append to private spools
(emit.py, no lock); this module is the only one that imports sqlite3 or opens the record,
and it applies a spool idempotently: events are inserted with INSERT OR IGNORE keyed by
the event id the emitter assigned, and the byte offset reached is committed in the same
transaction as the inserts, so a retried, concurrent or interrupted replay can only
re-insert the same keys. Exactly-once follows from at-least-once delivery.

Every call runs under one monotonic deadline (DEADLINE_S): the per-spool flock is polled
with LOCK_NB until it, then BEGIN IMMEDIATE runs with busy_timeout set to the time left.
On expiry the call returns and the spool waits for the next trigger (deferral, not loss).
A failure of the store (read-only, corrupt, full disk) is caught at the boundary
(`_guarded`); the spool and its offset stay as they were.

The record is SQLite in WAL mode (sqlite.org/wal.html: one writer, concurrent readers),
created 0600 in a 0700 directory. Schema changes are numbered files in migrations/,
applied in order, never edited; gaps are allowed and each applied number is recorded.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from lib import config_root
from lib.telemetry import emit, tables

DEADLINE_S = 0.25
LOCK_POLL_S = 0.005
PREFIX_LINES = 500
READ_CHUNK = 1 << 20
MAX_LINE_BYTES = 64 << 20
FIRST_LINE_WINDOW = 1 << 20
RECORD_MODE = 0o600
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_FILE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# Enumerated columns of the record: table -> column -> (enumeration table, column storing
# the table version the value was validated against).
ENUM_COLUMNS = {
    "sessions": {"role": ("roles", "role_table_version")},
    "contours": {"role": ("roles", "role_table_version")},
    "spawns": {"role": ("roles", "role_table_version")},
    "token_usage": {"role": ("roles", "role_table_version"), "phase": ("phases", "phase_table_version")},
    "losses": {"loss_mode": ("loss_modes", "loss_mode_table_version")},
}
_INTERNAL_TABLES = frozenset({"meta", "schema_migrations", "spool_offsets", "sqlite_sequence"})

_clock = time.monotonic
_sleep = time.sleep


class _Deferred(Exception):
    """The deadline expired before the lock or the commit; the work waits for the next trigger."""


class _Deadline:
    def __init__(self, seconds: "float | None" = None):
        self.end = _clock() + (DEADLINE_S if seconds is None else seconds)

    def remaining(self) -> float:
        return self.end - _clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def busy_ms(self) -> int:
        if self.expired():
            raise _Deferred()
        return max(1, int(self.remaining() * 1000))


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _guarded(run, on_deferred, on_failed):
    """The fail-open boundary: no store failure reaches the caller."""
    try:
        return run()
    except _Deferred:
        return on_deferred()
    except sqlite3.OperationalError as exc:
        return on_deferred() if _is_busy(exc) else on_failed()
    except Exception:
        return on_failed()


def _set_busy(conn: sqlite3.Connection, ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout={int(ms)}")


def _begin(conn: sqlite3.Connection, deadline: _Deadline) -> None:
    _set_busy(conn, deadline.busy_ms())
    conn.execute("BEGIN IMMEDIATE")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --- record creation, origin, migrations -------------------------------------------

def _probe_fts5() -> bool:
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        probe.close()


def _side_file(record: Path) -> Path:
    return record.with_name(record.name + ".origin")


def _side_file_origin(side: Path) -> "str | None":
    try:
        text = side.read_text(encoding="ascii").strip()
        uuid.UUID(text)
        return text
    except (OSError, ValueError):
        return None


def _write_side_file(side: Path, origin: str) -> None:
    fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, RECORD_MODE)
    try:
        os.write(fd, origin.encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(side, RECORD_MODE)


def _migration_files() -> "list[tuple[int, str, str]]":
    found = []
    for path in MIGRATIONS_DIR.iterdir():
        match = _MIGRATION_FILE.match(path.name)
        if match:
            found.append((int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    return sorted(found)


def _statements(sql: str):
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""


def _create_artifacts_index(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE VIRTUAL TABLE artifacts_fts USING fts5(text, content='artifacts', content_rowid='artifact_id')")
    conn.execute("CREATE TRIGGER artifacts_ai AFTER INSERT ON artifacts BEGIN "
                 "INSERT INTO artifacts_fts(rowid, text) VALUES (new.artifact_id, new.text); END")
    conn.execute("CREATE TRIGGER artifacts_ad AFTER DELETE ON artifacts BEGIN "
                 "INSERT INTO artifacts_fts(artifacts_fts, rowid, text) VALUES ('delete', old.artifact_id, old.text); END")
    conn.execute("CREATE TRIGGER artifacts_au AFTER UPDATE ON artifacts BEGIN "
                 "INSERT INTO artifacts_fts(artifacts_fts, rowid, text) VALUES ('delete', old.artifact_id, old.text); "
                 "INSERT INTO artifacts_fts(rowid, text) VALUES (new.artifact_id, new.text); END")


def _bootstrap(conn: sqlite3.Connection, record: Path, deadline: _Deadline) -> None:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    applied = set()
    if "schema_migrations" in names:
        applied = {row[0] for row in conn.execute("SELECT number FROM schema_migrations")}
    pending = [m for m in _migration_files() if m[0] not in applied]
    settled = "meta" in names and all(
        conn.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone() for key in ("origin", "fts5"))
    if not pending and settled:
        return
    _begin(conn, deadline)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        applied = {row[0] for row in conn.execute("SELECT number FROM schema_migrations")}
        for number, name, sql in _migration_files():
            if number in applied:
                continue
            for statement in _statements(sql):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (number, name, applied_at) VALUES (?, ?, ?)",
                         (number, name, emit.now_iso()))
        if conn.execute("SELECT 1 FROM meta WHERE key = 'origin'").fetchone() is None:
            side = _side_file(record)
            origin = _side_file_origin(side) or str(uuid.uuid4())
            conn.execute("INSERT INTO meta (key, value) VALUES ('origin', ?)", (origin,))
        origin = conn.execute("SELECT value FROM meta WHERE key = 'origin'").fetchone()[0]
        if _side_file_origin(_side_file(record)) != origin:
            _write_side_file(_side_file(record), origin)
        if conn.execute("SELECT 1 FROM meta WHERE key = 'fts5'").fetchone() is None:
            has_fts5 = _probe_fts5()
            if has_fts5:
                _create_artifacts_index(conn)
            conn.execute("INSERT INTO meta (key, value) VALUES ('fts5', ?)", ("1" if has_fts5 else "0",))
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise


def _open(deadline: _Deadline) -> sqlite3.Connection:
    if emit.fault_active("enospc_record"):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    record = config_root.telemetry_db()
    emit.ensure_private_dir(record.parent)
    os.close(os.open(record, os.O_RDWR | os.O_CREAT, RECORD_MODE))
    conn = sqlite3.connect(str(record), timeout=0, isolation_level=None)
    try:
        _set_busy(conn, deadline.busy_ms())
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _bootstrap(conn, record, deadline)
    except BaseException:
        conn.close()
        raise
    return conn


def open_record(deadline_s: "float | None" = None) -> "sqlite3.Connection | None":
    """A connection to the record, created and migrated if needed; None when the record
    is unavailable (locked past the deadline, read-only, corrupt, full disk). Never raises."""
    return _guarded(lambda: _open(_Deadline(deadline_s)), lambda: None, lambda: None)


def fts5_available(conn: sqlite3.Connection) -> bool:
    """True when the artifacts text index is an FTS5 table, False when it is the plain fallback."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'fts5'").fetchone()
    return bool(row) and row[0] == "1"


def applied_migrations(conn: sqlite3.Connection) -> "list[int]":
    return [row[0] for row in conn.execute("SELECT number FROM schema_migrations ORDER BY number")]


def _read_origin(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT value FROM meta WHERE key = 'origin'").fetchone()[0]


def origin_id() -> "str | None":
    """This record's origin id (random, created with the record, kept in <record>.origin)."""
    def run():
        conn = _open(_Deadline())
        try:
            return _read_origin(conn)
        finally:
            conn.close()
    return _guarded(run, lambda: None, lambda: None)


def imported_row_id(source_path, offset: int, line_sha256: str, origin: "str | None" = None) -> str:
    """UUIDv5 in the origin namespace over realpath(source_path) in UTF-8, NUL, the decimal
    offset, NUL and the lowercase hex sha256: the one function by which the importer names
    an imported row and replay's supersession names the row a back-reference points to."""
    origin = origin if origin is not None else origin_id()
    if origin is None:
        raise LookupError("the telemetry record is unavailable, so its origin id is too")
    basis = (os.fsencode(os.path.realpath(source_path)) + b"\0" + str(int(offset)).encode("ascii")
             + b"\0" + str(line_sha256).lower().encode("ascii"))
    digest = hashlib.sha1(uuid.UUID(origin).bytes + basis).digest()
    return str(uuid.UUID(bytes=digest[:16], version=5))


# --- row inserts -------------------------------------------------------------------

def _table_columns(conn: sqlite3.Connection, table: str) -> "set[str]":
    if table in _INTERNAL_TABLES or table.startswith("artifacts_fts"):
        raise ValueError(f"{table!r} is not a data table of the record")
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if not columns:
        raise ValueError(f"no table {table!r}")
    return columns


def _prepare_row(table: str, row: dict, columns: "set[str]") -> dict:
    unknown = set(row) - columns
    if unknown:
        raise ValueError(f"{table} has no column(s) {sorted(unknown)}")
    prepared = {}
    for column, value in row.items():
        prepared[column] = json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else value
    for column, (table_name, version_column) in ENUM_COLUMNS.get(table, {}).items():
        if prepared.get(column) is not None:
            prepared[version_column] = tables.validate(table_name, prepared[column])
    return prepared


def _insert_prepared(conn: sqlite3.Connection, table: str, prepared: dict) -> None:
    names = list(prepared)
    conn.execute(
        f'INSERT INTO "{table}" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
        f'VALUES ({", ".join("?" for _ in names)}) ON CONFLICT DO NOTHING',
        [prepared[n] for n in names])


def insert_rows(table: str, rows, deadline_s: "float | None" = None) -> dict:
    """Insert `rows` (dicts of column -> value) into `table` in one transaction under the
    call's deadline, ignoring a row whose key is already present (a NOT NULL or CHECK
    violation is not ignored: it fails the batch). Returns {inserted, deferred, failed},
    summing to the rows given: a row already present counts as inserted, all rows are
    deferred when the deadline expires before the commit and all failed when the
    transaction raises (an enumerated value the table refuses included). Never raises."""
    rows = list(rows)
    outcome = {"inserted": 0, "deferred": 0, "failed": 0}
    if not rows:
        return outcome
    deadline = _Deadline(deadline_s)

    def run():
        conn = _open(deadline)
        try:
            columns = _table_columns(conn, table)
            prepared = [_prepare_row(table, row, columns) for row in rows]
            _begin(conn, deadline)
            try:
                for row in prepared:
                    _insert_prepared(conn, table, row)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()
        outcome["inserted"] = len(rows)
        return outcome

    def deferred():
        outcome["deferred"] = len(rows)
        return outcome

    def failed():
        outcome["failed"] = len(rows)
        return outcome

    return _guarded(run, deferred, failed)


# --- spool replay ------------------------------------------------------------------

def _inode(stat: os.stat_result) -> int:
    return stat.st_ino


def _first_line_sha256(fd: int) -> "str | None":
    """sha256 of the spool's first non-empty complete line (every append begins with a
    newline, so the first line is always empty; the next one carries the first event's
    UUIDv7, unique per spool), or None while there is none."""
    data = os.pread(fd, FIRST_LINE_WINDOW, 0)
    start = 0
    while True:
        end = data.find(b"\n", start)
        if end < 0:
            return None
        line = data[start:end]
        if line:
            return hashlib.sha256(line).hexdigest()
        start = end + 1


def _generation(fd: int) -> "str | None":
    first = _first_line_sha256(fd)
    return None if first is None else f"{_inode(os.fstat(fd))}:{first}"


def _read_prefix(fd: int, offset: int) -> "tuple[list[bytes], int]":
    """Up to PREFIX_LINES complete non-empty lines from `offset`, and the offset after the
    last newline consumed; an unterminated tail is left alone."""
    chunk = READ_CHUNK
    while True:
        data = os.pread(fd, chunk, offset)
        lines = []
        index = consumed = 0
        saw_newline = False
        while len(lines) < PREFIX_LINES:
            end = data.find(b"\n", index)
            if end < 0:
                break
            saw_newline = True
            if end > index:
                lines.append(data[index:end])
            index = consumed = end + 1
        if saw_newline or len(data) < chunk or chunk >= MAX_LINE_BYTES:
            return lines, offset + consumed
        chunk *= 4


def _parse_event(raw: bytes) -> "dict | None":
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    for key in ("event_id", "kind", "time"):
        if not isinstance(event.get(key), str) or not event[key]:
            return None
    if "session_id" in event and not isinstance(event["session_id"], str):
        return None
    backref = event.get("backref")
    if backref is not None and not (
            isinstance(backref, dict) and isinstance(backref.get("path"), str)
            and isinstance(backref.get("offset"), int) and isinstance(backref.get("sha256"), str)):
        return None
    if not isinstance(event.get("fields", {}), dict):
        return None
    return event


def _insert_live_event(conn: sqlite3.Connection, event: dict, spool_session: str, origin: str) -> "tuple[int, int]":
    """Insert one replayed event and apply replay's half of live wins in the same
    transaction. Returns (inserted, superseded)."""
    fields = event.get("fields", {})
    backref = event.get("backref")
    backref_id = None
    if backref is not None:
        backref_id = imported_row_id(backref["path"], backref["offset"], backref["sha256"], origin=origin)
    child_session = fields.get("child_session_id")
    inserted = conn.execute(
        "INSERT OR IGNORE INTO events (event_id, session_id, kind, source_key, provenance, time, "
        "backref_id, child_session_id, fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event["event_id"], event.get("session_id", spool_session), event["kind"], fields.get("source_key"),
         event.get("provenance", "live"), event["time"], backref_id, child_session,
         json.dumps(fields, separators=(",", ":")))).rowcount
    superseded = 0
    if backref_id is not None:
        superseded += conn.execute(
            "DELETE FROM events WHERE event_id = ? AND provenance = 'imported'", (backref_id,)).rowcount
    if event["kind"] == "spawn" and child_session:
        superseded += conn.execute(
            "DELETE FROM events WHERE kind = 'spawn_cost' AND provenance = 'imported' AND child_session_id = ?",
            (child_session,)).rowcount
    return inserted, superseded


def _add_loss(conn: sqlite3.Connection, session_id: str, loss_mode: str, count: int) -> None:
    version = tables.validate("loss_modes", loss_mode)
    conn.execute(
        "INSERT INTO losses (session_id, loss_mode, loss_mode_table_version, count) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (session_id, loss_mode) DO UPDATE SET count = count + excluded.count",
        (session_id, loss_mode, version, count))


def _upsert_offset(conn: sqlite3.Connection, spool: str, generation: str, offset: int) -> None:
    conn.execute(
        "INSERT INTO spool_offsets (spool, generation, byte_offset) VALUES (?, ?, ?) "
        "ON CONFLICT (spool) DO UPDATE SET generation = excluded.generation, byte_offset = excluded.byte_offset",
        (spool, generation, offset))


def _apply_prefix(conn, spool, generation, new_offset, lines, origin, deadline) -> "tuple[int, int, int]":
    """One transaction: the prefix's inserts and supersession, its torn lines, the offset.
    Returns (inserted, superseded, torn)."""
    events = []
    torn = 0
    for raw in lines:
        event = _parse_event(raw)
        if event is None:
            torn += 1
        else:
            events.append(event)
    _begin(conn, deadline)
    try:
        inserted = superseded = 0
        for event in events:
            added, removed = _insert_live_event(conn, event, spool, origin)
            inserted += added
            superseded += removed
        if torn:
            _add_loss(conn, spool, "torn_event", torn)
        _upsert_offset(conn, spool, generation, new_offset)
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise
    return inserted, superseded, torn


def _acquire_lock(spool: str, deadline: _Deadline) -> int:
    fd = os.open(config_root.telemetry_spool_dir() / (spool + ".lock"), os.O_RDWR | os.O_CREAT, RECORD_MODE)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if deadline.expired():
                    raise _Deferred()
                _sleep(LOCK_POLL_S)
    except BaseException:
        os.close(fd)
        raise


def _drain(conn: sqlite3.Connection, fd: int, spool: str, result: dict, deadline: _Deadline) -> None:
    origin = _read_origin(conn)
    while True:
        generation = _generation(fd)
        if generation is None:
            result["drained"] = True
            return
        stored = conn.execute("SELECT generation, byte_offset FROM spool_offsets WHERE spool = ?", (spool,)).fetchone()
        size = os.fstat(fd).st_size
        offset = stored[1] if stored and stored[0] == generation and stored[1] <= size else 0
        lines, new_offset = _read_prefix(fd, offset)
        if not lines:
            result["drained"] = True
            return
        if deadline.expired():
            result["deferred"] = True
            return
        inserted, superseded, torn = _apply_prefix(conn, spool, generation, new_offset, lines, origin, deadline)
        result["inserted"] += inserted
        result["superseded"] += superseded
        result["torn"] += torn


def replay(session_id, deadline_s: "float | None" = None) -> dict:
    """Apply the session's spool to the record: complete lines from the stored byte offset
    under the per-spool flock, INSERT OR IGNORE by event id, one bounded prefix (at most
    PREFIX_LINES lines) per transaction with the offset advanced in the same transaction,
    prefix after prefix until the spool is drained or the deadline expires.

    Returns {inserted, deferred, torn, superseded, drained, failed}: deferred is true when
    the deadline expired with complete lines still past the offset (or before the lock or
    the commit could be taken), drained only when no complete line remains past the offset
    at return, failed when a transaction raised (prefixes committed before stay committed).
    Never raises. A retrying caller backs off 1 s, doubling to at most 30 s, within its budget."""
    result = {"inserted": 0, "deferred": False, "torn": 0, "superseded": 0, "drained": False, "failed": False}
    spool = emit.session_file_stem(session_id)
    if spool is None:
        result["failed"] = True
        return result
    deadline = _Deadline(deadline_s)

    def run():
        try:
            fd = os.open(emit.spool_path(spool), os.O_RDONLY)
        except FileNotFoundError:
            result["drained"] = True
            return result
        try:
            lock = _acquire_lock(spool, deadline)
            try:
                conn = _open(deadline)
                try:
                    _drain(conn, fd, spool, result, deadline)
                finally:
                    conn.close()
            finally:
                os.close(lock)
        finally:
            os.close(fd)
        return result

    def deferred():
        result["deferred"] = True
        result["drained"] = False
        return result

    def failed():
        result["failed"] = True
        result["drained"] = False
        return result

    return _guarded(run, deferred, failed)
