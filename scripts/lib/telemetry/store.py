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
import itertools
import json
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
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
INTERNAL_TABLES = frozenset({"meta", "schema_migrations", "spool_offsets", "import_offsets", "merge_state",
                             "sqlite_sequence"})
# A merge is a foreground command the user starts and can interrupt, not a command-path call:
# it waits on the write lock with SQLite's longest busy timeout instead of DEADLINE_S.
MERGE_BUSY_MS = 2 ** 31 - 1
# Rows per merge transaction: a merge into the live record releases the write lock between
# chunks, so hook writers on the command-path deadline get it, and the WAL stays bounded.
MERGE_CHUNK_ROWS = 50_000

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


def _open(deadline: _Deadline, record: "Path | None" = None) -> sqlite3.Connection:
    if emit.fault_active("enospc_record"):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    record = record or config_root.telemetry_db()
    emit.ensure_private_dir(record.parent)
    # Create the file owner-only, but never open an existing one: closing any descriptor of
    # the record drops every POSIX lock this process holds on it, a held reader's included.
    try:
        _create_owner_only(record)
    except FileExistsError:
        # Refuse a read-only record before SQLite opens it: SQLite would fall back to a
        # read-only open, and that state outlives the connection for the whole process.
        if not os.access(record, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(record))
    return _connect_writable(record, deadline)


def _connect_writable(record: Path, deadline: _Deadline) -> sqlite3.Connection:
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

def table_columns(conn: sqlite3.Connection, table: str) -> "set[str]":
    """The columns of any table of the record; empty when there is no such table."""
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _table_columns(conn: sqlite3.Connection, table: str) -> "set[str]":
    if table in INTERNAL_TABLES or table.startswith("artifacts_fts"):
        raise ValueError(f"{table!r} is not a data table of the record")
    columns = table_columns(conn, table)
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


def fill_empty_time(conn: sqlite3.Connection, event_id: str, moment: str) -> int:
    """Give a stored event whose time is empty the `moment` a later import read for it; a stored
    time that is already set is never changed. Returns the number of rows filled (0 or 1)."""
    if not moment:
        return 0
    return conn.execute(
        "UPDATE events SET time = ?, fields = CASE WHEN json_extract(fields, '$.time') = '' "
        "THEN json_set(fields, '$.time', ?) ELSE fields END WHERE event_id = ? AND time = ''",
        (moment, moment, event_id)).rowcount


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


# A table whose rows are replaced by a better-ranked row of the same key: key column, rank columns
# compared as one row value (lower wins, text in byte order).
RANKED_TABLES = {
    "token_usage": ("message_id", ("ts_ms", "copy_rank", "session_start_ms", "transcript", "byte_offset")),
}

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_READ_OFFSET_UPSERT = (
    "INSERT INTO import_offsets (source, generation, consumer, byte_offset, last_start, last_sha256, "
    "read_size, read_outcome, carry) VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', ?) "
    "ON CONFLICT (source, generation, consumer) DO UPDATE SET byte_offset = excluded.byte_offset, "
    "last_start = excluded.last_start, last_sha256 = excluded.last_sha256, read_size = excluded.read_size, "
    "read_outcome = 'committed', carry = excluded.carry")

_READ_ATTEMPT_UPSERT = (
    "INSERT INTO import_offsets (source, generation, consumer, byte_offset, last_start, last_sha256, "
    "read_size, read_outcome) VALUES (?, ?, ?, 0, 0, ?, ?, ?) "
    "ON CONFLICT (source, generation, consumer) DO UPDATE SET read_size = excluded.read_size, "
    "read_outcome = excluded.read_outcome")


def _upsert_ranked(conn: sqlite3.Connection, table: str, prepared: dict) -> None:
    key, rank = RANKED_TABLES[table]
    names = list(prepared)
    assignments = ", ".join(f'"{n}" = excluded."{n}"' for n in names if n != key)
    mine = ", ".join(f'excluded."{c}"' for c in rank)
    theirs = ", ".join(f'"{table}"."{c}"' for c in rank)
    conn.execute(
        f'INSERT INTO "{table}" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
        f'VALUES ({", ".join("?" for _ in names)}) ON CONFLICT ("{key}") DO UPDATE SET {assignments} '
        f'WHERE ({mine}) < ({theirs})',
        [prepared[n] for n in names])


def _write_read_offset(conn: sqlite3.Connection, offset: dict) -> None:
    conn.execute(_READ_OFFSET_UPSERT, (
        offset["source"], offset["generation"], offset["consumer"], offset["byte_offset"], offset["last_start"],
        offset["last_sha256"], offset["read_size"], offset.get("carry")))


def insert_rows_with_offset(rows, offset: dict, deadline_s: "float | None" = None, updates=()) -> dict:
    """Commit one read batch of a consumer in one transaction: `rows` as (table, row) pairs
    (a row of a RANKED_TABLES table replaces the stored one when it ranks better, any other row
    already present is ignored), `updates` as (table, key column, key, column, value) filling a
    column still NULL, and the consumer's offset row with read outcome committed. `offset` holds
    source, generation, consumer, byte_offset, last_start, last_sha256, read_size and carry.
    Returns {inserted, deferred, failed, committed}: the first three sum to the rows given as
    store.insert_rows's do, so the offset moves only when the rows are returned inserted.
    Never raises."""
    rows = list(rows)
    outcome = {"inserted": 0, "deferred": 0, "failed": 0, "committed": False}
    deadline = _Deadline(deadline_s)

    def run():
        conn = _open(deadline)
        try:
            prepared = []
            for table, row in rows:
                prepared.append((table, _prepare_row(table, row, _table_columns(conn, table))))
            _begin(conn, deadline)
            try:
                _write_read_offset(conn, offset)
                for table, row in prepared:
                    if table in RANKED_TABLES:
                        _upsert_ranked(conn, table, row)
                    else:
                        _insert_prepared(conn, table, row)
                for table, key_column, key, column, value in updates:
                    _table_columns(conn, table)
                    conn.execute(f'UPDATE "{table}" SET "{column}" = ? WHERE "{key_column}" = ? AND "{column}" IS NULL',
                                 (value, key))
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()
        outcome["inserted"] = len(rows)
        outcome["committed"] = True
        return outcome

    def deferred():
        outcome["deferred"] = len(rows)
        return outcome

    def failed():
        outcome["failed"] = len(rows)
        return outcome

    return _guarded(run, deferred, failed)


def record_read_attempt(source: str, generation: str, consumer: str, read_size: int, read_outcome: str,
                        deadline_s: "float | None" = None) -> bool:
    """Record a consumer's read attempt (its attempted end and how it ended) beside its offset,
    which a first attempt creates at 0. True when committed; never raises."""
    deadline = _Deadline(deadline_s)

    def run():
        conn = _open(deadline)
        try:
            _begin(conn, deadline)
            try:
                conn.execute(_READ_ATTEMPT_UPSERT, (source, generation, consumer, _EMPTY_SHA256, read_size, read_outcome))
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()
        return True

    return _guarded(run, lambda: False, lambda: False)


def transact(work, deadline_s: "float | None" = None) -> dict:
    """Run `work(conn)` in one write transaction under the call's deadline, for the sync's
    updates (a task row's status, a contour's task key) that INSERT OR IGNORE cannot express.
    Returns {committed, deferred, failed, value}: value is what `work` returned when
    committed, a failure inside `work` rolls everything back and is reported as failed.
    Never raises."""
    outcome = {"committed": False, "deferred": False, "failed": False, "value": None}
    deadline = _Deadline(deadline_s)

    def run():
        conn = _open(deadline)
        try:
            _begin(conn, deadline)
            try:
                outcome["value"] = work(conn)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()
        outcome["committed"] = True
        return outcome

    def deferred():
        outcome["deferred"] = True
        return outcome

    def failed():
        outcome["failed"] = True
        return outcome

    return _guarded(run, deferred, failed)


def attach_session_task(conn: sqlite3.Connection, session_id: str, task_key: "tuple[str, str, str]") -> None:
    """Name the task row `task_key` = (origin, task_id, epoch) a session attaches to: the
    importer's call, inside its own transaction, for a session already running when live
    emission began. Writes the key only where the session's key is null, and inserts the
    session row when it has none, so a row the sync wrote before the import gets the key too."""
    task_origin, task_id, task_epoch = task_key
    conn.execute(
        "INSERT INTO sessions (session_id, task_origin, task_id, task_epoch) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (session_id) DO UPDATE SET "
        "task_origin = CASE WHEN task_id IS NULL THEN excluded.task_origin ELSE task_origin END, "
        "task_epoch = CASE WHEN task_id IS NULL THEN excluded.task_epoch ELSE task_epoch END, "
        "task_id = CASE WHEN task_id IS NULL THEN excluded.task_id ELSE task_id END",
        (session_id, task_origin, task_id, task_epoch))


def open_readonly() -> "sqlite3.Connection | None":
    """A read-only connection to an existing record, None when there is none or it cannot be
    read. Never creates the record, its directory or its migrations."""
    record = config_root.telemetry_db()
    if not record.exists():
        return None
    conn = None
    try:
        conn = open_record_readonly(record, timeout_s=DEADLINE_S)
        conn.execute("SELECT 1 FROM events LIMIT 1")
        return conn
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        return None


def open_record_readonly(path, timeout_s: float = 5.0, immutable: bool = False) -> sqlite3.Connection:
    """A `file:<path>?mode=ro` URI connection: SQLite refuses every write through it. Raises
    sqlite3.Error when the file is not a readable record. `immutable` also tells SQLite the file
    cannot change, so it takes no lock and creates no -shm or -wal file beside it."""
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return sqlite3.connect("file:" + urllib.parse.quote(str(path)) + query, uri=True,
                           timeout=timeout_s, isolation_level=None)


def record_exists() -> bool:
    return config_root.telemetry_db().exists()


def move_record_aside(stamp: str) -> bool:
    """Rename the record and its WAL files to `<name>.corrupt-<stamp>[-wal|-shm]`; False when
    there was no record. The origin side file stays, so a rebuilt record keeps its origin."""
    record = config_root.telemetry_db()
    if not record.exists():
        return False
    aside = f"{record.name}.corrupt-{stamp}"
    for suffix in ("", "-wal", "-shm"):
        part = record.with_name(record.name + suffix)
        if part.exists():
            os.replace(part, record.with_name(aside + suffix))
    return True


# --- export, merge and round-trip (lib/telemetry/portability.py) -------------------
# Rows cross records verbatim: a merge neither re-validates an enumerated value nor restamps
# its table version, so these entry points bypass _prepare_row.

MERGE_MODES = ("ignore", "upsert", "delete")


def _unbounded() -> _Deadline:
    return _Deadline(MERGE_BUSY_MS / 1000)


def _create_owner_only(path: Path) -> None:
    os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, RECORD_MODE))


def open_for_merge(record: "Path | None" = None) -> sqlite3.Connection:
    """A writable connection to the record (created and migrated if needed) that waits on the
    write lock without the command-path deadline. Raises on failure: merge is a foreground command."""
    return _open(_unbounded(), Path(record) if record else None)


def open_scratch(path) -> sqlite3.Connection:
    """A new record at `path` (0600, migrated, with its own random origin); the file must not exist
    and its directory is left as it is."""
    record = Path(path)
    _create_owner_only(record)
    return _connect_writable(record, _unbounded())


def backup(src_conn: sqlite3.Connection, dest_path) -> None:
    """Copy the record behind `src_conn` into the new file `dest_path` (0600) with the SQLite backup API."""
    dest = Path(dest_path)
    _create_owner_only(dest)
    target = sqlite3.connect(str(dest), isolation_level=None)
    try:
        src_conn.backup(target)
    finally:
        target.close()


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """Hold one read transaction for the body, so every read inside it sees one state of the record."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


def _file_identity(path: Path) -> "tuple[int, int, int]":
    stat = os.stat(path)
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _read_with(record: Path, read, immutable: bool):
    conn = open_record_readonly(record, immutable=immutable)
    try:
        return read(conn)
    finally:
        conn.close()


def read_record_quietly(record, read, discard):
    """Return `read(conn)` over a read-only connection to `record`. A read-only open of a WAL
    record with no -wal file (no connection has it open) would create its -wal and -shm, so such
    a record is read as immutable, which creates no file beside it; the result is kept only when
    the read succeeded and the file is unchanged afterwards. Otherwise (the file changed, or the
    immutable read hit an sqlite3.DatabaseError, which a file changed under it can cause)
    `discard()` undoes the read's output and the record is read again through a plain read-only
    open, as it is while a connection has it open; that open may leave -wal and -shm beside the
    record, as any reader of a WAL record does."""
    record = Path(record)
    wal = record.with_name(record.name + "-wal")
    if not wal.exists():
        before = _file_identity(record)
        try:
            result = _read_with(record, read, immutable=True)
        except sqlite3.DatabaseError:
            pass
        else:
            if _file_identity(record) == before and not wal.exists():
                return result
        discard()
    return _read_with(record, read, immutable=False)


def backup_record(record, dest_path) -> None:
    """Copy the record into the new file `dest_path` with the backup API, read quietly."""
    dest = Path(dest_path)
    read_record_quietly(record, lambda conn: backup(conn, dest), dest.unlink)


def table_names(conn: sqlite3.Connection) -> "list[str]":
    """The record's tables from sqlite_master, without sqlite_ tables, virtual (FTS) tables and their shadows."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    virtual = {name for name, sql in rows if (sql or "").upper().startswith("CREATE VIRTUAL TABLE")}
    return sorted(name for name, _ in rows
                  if not name.startswith("sqlite_") and name not in virtual
                  and not any(name.startswith(v + "_") for v in virtual))


def table_counts(conn: sqlite3.Connection, names) -> "dict[str, int]":
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def record_origin(conn: sqlite3.Connection) -> str:
    return _read_origin(conn)


def export_rows(conn: sqlite3.Connection, table: str, origin_sql: str):
    """Yield (row origin, row dict) for every row of `table` in rowid order; `origin_sql` is an
    expression over the row aliased `t`."""
    cursor = conn.execute(f'SELECT {origin_sql} AS "__origin", t.* FROM "{table}" AS t ORDER BY t.rowid')
    names = [d[0] for d in cursor.description][1:]
    for values in cursor:
        yield values[0], dict(zip(names, values[1:]))


def fixture_revisions(conn: sqlite3.Connection) -> "set[str]":
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT fixture_sha256 FROM causes WHERE fixture_sha256 IS NOT NULL")}


def merge_watermark(conn: sqlite3.Connection, origin: str) -> "str | None":
    row = conn.execute("SELECT taken_at FROM merge_state WHERE origin = ?", (origin,)).fetchone()
    return row[0] if row else None


def set_merge_watermark(conn: sqlite3.Connection, origin: str, taken_at: str) -> None:
    _merge_transaction(conn, lambda: conn.execute(
        "INSERT INTO merge_state (origin, taken_at) VALUES (?, ?) "
        "ON CONFLICT (origin) DO UPDATE SET taken_at = excluded.taken_at", (origin, taken_at)))


# Imported event rows a live row of the record supersedes: one whose id a live row
# back-references, and an imported spawn-cost row whose child session a live spawn event names.
_SUPERSEDED_IMPORTED_EVENTS = (
    "SELECT event_id FROM events WHERE provenance = 'imported' AND ("
    "event_id IN (SELECT backref_id FROM events WHERE provenance <> 'imported' AND backref_id IS NOT NULL)"
    " OR (kind = 'spawn_cost' AND child_session_id IN (SELECT child_session_id FROM events "
    "WHERE kind = 'spawn' AND provenance <> 'imported' AND child_session_id IS NOT NULL)))")


def delete_superseded_imported_events(conn: sqlite3.Connection) -> int:
    """Delete every imported event row a live row supersedes, MERGE_CHUNK_ROWS per transaction.
    Each chunk of ids is selected outside the write transaction, since the select scans the
    events table and the write lock must stay short for hook writers. Superseding only grows
    (live rows are never deleted), so an id selected before the transaction stays superseded.
    Returns the rows deleted."""
    deleted = 0
    while True:
        ids = [row[0] for row in conn.execute(f"{_SUPERSEDED_IMPORTED_EVENTS} LIMIT {MERGE_CHUNK_ROWS}")]
        if not ids:
            return deleted
        deleted += _merge_transaction(conn, lambda: conn.executemany(
            "DELETE FROM events WHERE provenance = 'imported' AND event_id = ?", [(i,) for i in ids]).rowcount)


def _merge_transaction(conn: sqlite3.Connection, work):
    _set_busy(conn, MERGE_BUSY_MS)
    conn.execute("BEGIN IMMEDIATE")
    try:
        value = work()
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise
    return value


def _merge_row(conn, table: str, row: dict, columns: "set[str]", keys: "list[str]", mode: str,
               owner) -> int:
    unknown = set(row) - columns
    if unknown:
        raise ValueError(f"{table} has no column(s) {sorted(unknown)}")
    values = {c: json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else v for c, v in row.items()}
    match = " AND ".join(f'"{k}" IS ?' for k in keys)
    key_values = [values.get(k) for k in keys]
    if mode == "delete":
        return conn.execute(f'DELETE FROM "{table}" WHERE {match}', key_values).rowcount
    names = ", ".join(f'"{n}"' for n in values)
    marks = ", ".join("?" for _ in values)
    if mode == "ignore":
        return conn.execute(
            f'INSERT INTO "{table}" ({names}) SELECT {marks} '
            f'WHERE NOT EXISTS (SELECT 1 FROM "{table}" WHERE {match}) ON CONFLICT DO NOTHING',
            [*values.values(), *key_values]).rowcount
    updates = ", ".join(f'"{n}" = excluded."{n}"' for n in values if n not in keys)
    target = ", ".join(f'"{k}"' for k in keys)
    if not updates:
        action, guard = "NOTHING", []
    elif owner is None:
        action, guard = f"UPDATE SET {updates}", []
    else:
        action, guard = f"UPDATE SET {updates} WHERE ({owner[0]}) IS ?", [owner[1]]
    return conn.execute(
        f'INSERT INTO "{table}" AS t ({names}) VALUES ({marks}) ON CONFLICT ({target}) DO {action}',
        [*values.values(), *guard]).rowcount


def merge_rows(conn: sqlite3.Connection, table: str, rows, key_columns, mode: str, owner=None) -> int:
    """Apply the iterable `rows` to a data table by its merge key, MERGE_CHUNK_ROWS rows per
    transaction, waiting on the write lock without the command-path deadline: `ignore` inserts a
    row whose key is absent, `upsert` also replaces the stored row's other columns, `delete`
    removes the row with the key. With `owner` = (origin SQL over the stored row aliased `t`,
    origin), an upsert replaces only a stored row of that origin. Rows are stored verbatim.
    Returns the rows changed; raises on failure, keeping the chunks already committed."""
    if mode not in MERGE_MODES:
        raise ValueError(f"unknown merge mode {mode!r}")
    columns = _table_columns(conn, table)
    keys = list(key_columns)
    rows = iter(rows)
    changed = 0
    while True:
        chunk = list(itertools.islice(rows, MERGE_CHUNK_ROWS))
        if not chunk:
            return changed
        changed += _merge_transaction(
            conn, lambda: sum(_merge_row(conn, table, row, columns, keys, mode, owner) for row in chunk))


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


def _is_str(value) -> bool:
    """A str SQLite can bind: a lone surrogate from a JSON escape cannot be encoded."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_text(value) -> bool:
    return _is_str(value) and bool(value)


def _is_time(value) -> bool:
    """A time the sync will later parse (ISO 8601, convertible to UTC)."""
    if not _is_text(value):
        return False
    try:
        datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return False
    return True


def _valid_backref(backref) -> bool:
    return (isinstance(backref, dict) and _is_text(backref.get("path")) and "\0" not in backref["path"]
            and isinstance(backref.get("offset"), int) and not isinstance(backref["offset"], bool)
            and backref["offset"] >= 0 and _is_str(backref.get("sha256")))


def _valid_event(event) -> bool:
    """Every field a replay binds into a row has the type and form that insert accepts, so a
    parsed line that passes cannot fail the transaction that carries it."""
    if not isinstance(event, dict) or not all(_is_text(event.get(k)) for k in ("event_id", "kind")):
        return False
    if not _is_time(event.get("time")):
        return False
    if "session_id" in event and not _is_text(event["session_id"]):
        return False
    if "provenance" in event and not _is_text(event["provenance"]):
        return False
    if event.get("backref") is not None and not _valid_backref(event["backref"]):
        return False
    fields = event.get("fields", {})
    if not isinstance(fields, dict):
        return False
    return all(fields.get(k) is None or _is_str(fields[k]) for k in ("source_key", "child_session_id"))


def _parse_event(raw: bytes) -> "dict | None":
    """The event a spool line holds, or None when the line is not one a replay can file."""
    try:
        event = json.loads(raw)
        return event if _valid_event(event) else None
    except (ValueError, RecursionError):
        return None


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


def add_loss(conn: sqlite3.Connection, session_id: str, loss_mode: str, count: int) -> None:
    """Add `count` to the session's loss of one enumerated mode, inside the caller's transaction."""
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
            add_loss(conn, spool, "torn_event", torn)
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


@contextmanager
def spool_lock(spool: str, deadline_s: "float | None" = None):
    """Hold the spool's replay lock (the one `replay` takes) for the body; yields True, or False
    when the lock was not obtained before the deadline or could not be opened. Whoever deletes a
    spool does so under it, so no replay reads the file mid-removal. The lock file itself is
    never removed: a second process could hold a lock on the unlinked inode while a third
    locks a fresh one."""
    try:
        fd = _acquire_lock(spool, _Deadline(deadline_s))
    except (_Deferred, OSError):
        yield False
        return
    try:
        yield True
    finally:
        os.close(fd)


def spool_state(spool: str) -> "tuple[int, str | None] | None":
    """(size, generation) of the spool file now at the spool's path, read through one descriptor;
    None when there is no file. The generation is None while the file holds no complete line."""
    try:
        fd = os.open(emit.spool_path(spool), os.O_RDONLY)
    except OSError:
        return None
    try:
        return os.fstat(fd).st_size, _generation(fd)
    finally:
        os.close(fd)


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
