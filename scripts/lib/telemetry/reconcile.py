"""`telemetry.py check-reconcile`: a scratch import of a snapshot against the live record.

Difficulty removed: an import into a scratch record proved only that the importer is idempotent
on its own output, never that the live record holds what an import of the same bytes yields. This
check opens the live record read-only and compares the two per source kind of the manifest.

- ledger (consumer `events`): every complete line of a copy whose scratch row exists and that lies
  below the live importer's `events` offset for the same file and generation must have its row in
  the live record under the id the live origin gives it (store.imported_row_id). A line at or past
  that offset is `beyond_live_offset`; a file whose generation the live record does not know while
  it knows another is `generation_changed`. A line the live import skipped because a live row
  back-references it, or a spawn cost of a live child, is `held_by_live_row`. Lines whose live row
  exists while the scratch one does not are `live_not_in_scratch`.
- transcript: every scratch token row from below the live `tokens` offset of its transcript must
  have its message id live; live token rows of a manifest transcript below that offset and the
  copy's size that scratch lacks are `live_not_in_scratch`.
- state: every scratch history row of a state file must be live under its live-origin id, except
  the history of a session live in the live record (a live-provenance row, a spool or an end
  marker), `live_session_history`; live history rows of the session that the current file no
  longer yields are `superseded_engine_state`.

The other consumers and tables (contours, sessions, causes, losses, judge_usage) are listed as
not_reconciled. A manifest kind with zero checked rows fails unless it is in OFFSET_FREE_KINDS,
printed by every run. Exit 0 reconciled, 1 a missing row or an unchecked kind, 2 unreadable input.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lib import config_root
from lib.telemetry import importer, live, snapshot, store, tokens

OFFSET_FREE_KINDS: "tuple[str, ...]" = ()
NOT_RECONCILED = ("contours", "sessions", "causes", "losses", "judge_usage")
BATCH = 500


class ReconcileInputError(Exception):
    pass


def _origin(conn) -> str:
    return store._read_origin(conn)


def _present(conn, table: str, column: str, ids: list) -> set:
    found = set()
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        marks = ",".join("?" * len(chunk))
        found.update(r[0] for r in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IN ({marks})", chunk))
    return found


def _generation(path: Path) -> "str | None":
    with open(path, "rb") as handle:
        first = importer._first_line(handle)
    return None if first is None else hashlib.sha256(first).hexdigest()


def _live_offset(live_conn, source: str, generation: str, consumer: str) -> "tuple[int, bool]":
    """(the live importer offset, whether the live record knows another generation of the file)."""
    row = live_conn.execute("SELECT byte_offset FROM import_offsets WHERE source = ? AND generation = ? "
                            "AND consumer = ?", (source, generation, consumer)).fetchone()
    if row is not None:
        return row[0], False
    other = live_conn.execute("SELECT 1 FROM import_offsets WHERE source = ? AND consumer = ? LIMIT 1",
                              (source, consumer)).fetchone()
    return 0, other is not None


def _complete_lines(path: Path):
    position = 0
    with open(path, "rb") as handle:
        for raw in iter(handle.readline, b""):
            if not raw.endswith(b"\n"):
                return
            yield position, hashlib.sha256(raw).hexdigest()
            position += len(raw)


class _Kind:
    def __init__(self) -> None:
        self.checked = 0
        self.counted = {}
        self.missing = 0
        self.first_missing = None

    def miss(self, row: dict) -> None:
        self.missing += 1
        if self.first_missing is None:
            self.first_missing = row

    def count(self, name: str, n: int = 1) -> None:
        if n:
            self.counted[name] = self.counted.get(name, 0) + n

    def report(self) -> dict:
        return {"checked": self.checked, "counted": dict(sorted(self.counted.items())),
                "missing": self.missing, "first_missing": self.first_missing}


class _Records:
    def __init__(self, scratch, live_conn) -> None:
        self.scratch, self.live = scratch, live_conn
        self.scratch_origin, self.live_origin = _origin(scratch), _origin(live_conn)
        self._live_sessions = None

    def session_is_live(self, session: str) -> bool:
        if self._live_sessions is None:
            self._live_sessions = {r[0] for r in self.live.execute(
                "SELECT DISTINCT session_id FROM events WHERE provenance = 'live'")}
        return session in self._live_sessions or live.spool_or_end_marker_exists(session)


def _held_by_live_row(records: _Records, live_id: str, scratch_id: str) -> bool:
    if records.live.execute("SELECT 1 FROM events WHERE backref_id = ? LIMIT 1", (live_id,)).fetchone():
        return True
    row = records.scratch.execute("SELECT kind, child_session_id FROM events WHERE event_id = ?",
                                  (scratch_id,)).fetchone()
    return row is not None and row[0] == "spawn_cost" and row[1] is not None and records.session_is_live(row[1])


def _reconcile_ledger(records: _Records, frozen: snapshot.Snapshot, kind: _Kind) -> None:
    for unit in importer._file_units():
        copy = frozen.copy_of(unit.path)
        if copy is None or unit.consumer != "events":
            continue
        generation = _generation(copy)
        if generation is None:
            continue
        offset, other_generation = _live_offset(records.live, unit.path, generation, unit.consumer)
        for chunk in _batches(_complete_lines(copy)):
            _reconcile_lines(records, unit.path, chunk, offset, other_generation, kind)


def _batches(items):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def _reconcile_lines(records: _Records, source: str, chunk: list, offset: int, other_generation: bool,
                     kind: _Kind) -> None:
    lines = [(position, store.imported_row_id(source, position, sha, records.scratch_origin),
              store.imported_row_id(source, position, sha, records.live_origin)) for position, sha in chunk]
    in_scratch = _present(records.scratch, "events", "event_id", [s for _, s, _ in lines])
    if other_generation:
        kind.count("generation_changed", sum(1 for _, s, _ in lines if s in in_scratch))
        return
    below = [line for line in lines if line[0] < offset]
    in_live = _present(records.live, "events", "event_id", [l for _, _, l in below])
    kind.count("beyond_live_offset", sum(1 for p, s, _ in lines if p >= offset and s in in_scratch))
    for position, scratch_id, live_id in below:
        if scratch_id not in in_scratch:
            kind.count("live_not_in_scratch", int(live_id in in_live))
            continue
        kind.checked += 1
        if live_id in in_live:
            continue
        if _held_by_live_row(records, live_id, scratch_id):
            kind.count("held_by_live_row")
        else:
            kind.miss({"source": source, "offset": position, "live_id": live_id})


def _reconcile_transcripts(records: _Records, frozen: snapshot.Snapshot, kind: _Kind) -> None:
    for path in frozen.live_paths(snapshot.TRANSCRIPT):
        copy = frozen.copy_of(path)
        generation = _generation(copy)
        if generation is None:
            continue
        source = str(path)
        scratch_rows = records.scratch.execute(
            "SELECT message_id, byte_offset FROM token_usage WHERE transcript = ?", (source,)).fetchall()
        offset, other_generation = _live_offset(records.live, source, generation, tokens.CONSUMER)
        if other_generation:
            kind.count("generation_changed", len(scratch_rows))
            continue
        below = [m for m, at in scratch_rows if at < offset]
        kind.count("beyond_live_offset", len(scratch_rows) - len(below))
        in_live = _present(records.live, "token_usage", "message_id", below)
        kind.checked += len(below)
        for message_id in below:
            if message_id not in in_live:
                kind.miss({"transcript": source, "message_id": message_id})
        cut = min(offset, copy.stat().st_size)
        live_ids = [r[0] for r in records.live.execute(
            "SELECT message_id FROM token_usage WHERE transcript = ? AND byte_offset < ?", (source, cut))]
        in_scratch = _present(records.scratch, "token_usage", "message_id", live_ids)
        kind.count("live_not_in_scratch", sum(1 for m in live_ids if m not in in_scratch))


def _reconcile_state(records: _Records, frozen: snapshot.Snapshot, kind: _Kind) -> None:
    for path in frozen.live_paths(snapshot.STATE):
        try:
            data = json.loads(frozen.copy_of(path).read_text(encoding="utf-8"))
            state = importer._project_state(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        session = state["session_id"]
        entries = [(store.imported_row_id(path, index, digest, records.scratch_origin),
                    store.imported_row_id(path, index, digest, records.live_origin))
                   for index, digest in enumerate(state["entry_digests"]) if digest is not None]
        in_scratch = _present(records.scratch, "events", "event_id", [s for s, _ in entries])
        current = [l for _, l in entries]
        in_live = _present(records.live, "events", "event_id", current)
        live_session = records.session_is_live(session)
        for scratch_id, live_id in entries:
            if scratch_id not in in_scratch:
                continue
            if live_id in in_live:
                kind.checked += 1
            elif live_session:
                kind.count("live_session_history")
            else:
                kind.checked += 1
                kind.miss({"state": str(path), "session": session, "live_id": live_id})
        history_rows = {r[0] for r in records.live.execute(
            "SELECT event_id FROM events WHERE session_id = ? AND provenance = 'imported' AND kind = 'command'",
            (session,))}
        kind.count("superseded_engine_state", len(history_rows - set(current)))


def check(scratch_path, sources_dir) -> "tuple[int, dict]":
    """(exit code, report) of reconciling the scratch record built from `sources_dir`."""
    try:
        frozen = snapshot.Snapshot(sources_dir)
        scratch = store.open_record_readonly(scratch_path)
        live_conn = store.open_record_readonly(config_root.telemetry_db())
        records = _Records(scratch, live_conn)
    except (OSError, ValueError, KeyError, TypeError, store.sqlite3.Error) as err:
        raise ReconcileInputError(str(err)) from err
    try:
        kinds = {name: _Kind() for name in sorted({e["kind"] for e in frozen.files.values()})}
        readers = {snapshot.LEDGER: _reconcile_ledger, snapshot.TRANSCRIPT: _reconcile_transcripts,
                   snapshot.STATE: _reconcile_state}
        for name, kind in kinds.items():
            readers[name](records, frozen, kind)
    finally:
        scratch.close()
        live_conn.close()
    unchecked = [name for name, kind in kinds.items() if kind.checked == 0 and name not in OFFSET_FREE_KINDS]
    failed = unchecked or any(kind.missing for kind in kinds.values())
    report = {"kinds": {name: kind.report() for name, kind in kinds.items()},
              "offset_free_kinds": list(OFFSET_FREE_KINDS), "unchecked_kinds": unchecked,
              "not_reconciled": list(NOT_RECONCILED), "live_offset_read": "import_offsets.byte_offset of the "
              "live record per (live source path, copy generation, consumer events | tokens)",
              "reconciled": not failed}
    return (1 if failed else 0), report
