"""Export, merge and round-trip check of telemetry records between machines.

Difficulty removed: each machine keeps its own record, and an analysis that spans machines
needs their rows in one record without duplicating a row, joining two machines' unrelated
tasks, or letting an old copy of a row overwrite a newer one.

An export is a gzip JSON-lines file created 0600: a header (the record's random origin,
taken_at, the version of every enumeration table, the fixture revision of the imported
causes, per-table row counts), then one line per row with the row's own origin. Text
artifacts are exported only with `with_text`. A merge applies each row by its table's merge
key (MERGE_KEYS, the identity table of the record): an ignore-mode row is inserted when its
key is absent; an upsert-mode row also replaces the stored row, but only when it belongs to
the export's own origin, that origin is not the receiving record's, and the export was taken
after the newest export of that origin merged before (merge_state). Afterwards every imported
event row a live row of the record supersedes is deleted, as replay does. SQLite access stays
in store.py; this module never imports sqlite3.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from lib import config_root
from lib.telemetry import store, tables

FORMAT = "telemetry-export/1"
EXPORT_MODE = 0o600

_TASK_KEY = ("task_origin", "task_id", "task_epoch")

# The identity table of the record: each data table's merge key. Tasks come first, so a row
# naming a task key finds its task already merged.
MERGE_KEYS = {
    "tasks": ("origin", "task_id", "epoch"),
    "sessions": ("session_id",),
    "contours": ("contour_id",),
    "events": ("event_id",),
    "stages": _TASK_KEY + ("stage_index",),
    "difficulties": ("difficulty_id",),
    "reviews": ("review_id",),
    "spawns": ("spawn_id",),
    "token_usage": ("message_id",),
    "compactions": ("compaction_id",),
    "context_births": ("birth_id",),
    "judge_usage": ("usage_id",),
    "causes": ("cause_id",),
    "losses": ("session_id", "loss_mode"),
    "standing_surface": ("origin", "core_head"),
    "artifacts": ("sha256",),
}
MERGE_MODE = {
    "tasks": "upsert", "sessions": "upsert", "contours": "upsert", "stages": "upsert",
    "difficulties": "upsert", "reviews": "upsert", "spawns": "upsert", "token_usage": "upsert",
    "losses": "upsert", "standing_surface": "upsert",
    "events": "ignore", "compactions": "ignore", "context_births": "ignore", "judge_usage": "ignore",
    "causes": "ignore", "artifacts": "ignore",
}
LOCAL_ONLY = frozenset({"meta", "schema_migrations", "spool_offsets", "import_offsets", "session_head_marks",
                        "merge_state"})
TEXT_TABLES = frozenset({"artifacts"})
LOCAL_ROWID_COLUMNS = {"artifacts": ("artifact_id",)}

# Column storing the enumeration-table version a value was validated against -> that table.
VERSION_COLUMNS = {
    "role_table_version": "roles",
    "phase_table_version": "phases",
    "loss_mode_table_version": "loss_modes",
    "cause_category_table_version": "categories",
    "mechanism_table_version": "registry",
    "functional_place_table_version": "places",
}


def _session_origin(column: str) -> str:
    return f"(SELECT s.origin FROM sessions AS s WHERE s.session_id = t.{column})"


def _event_origin(column: str) -> str:
    return ("(SELECT s.origin FROM events AS e JOIN sessions AS s ON s.session_id = e.session_id "
            f"WHERE e.event_id = t.{column})")


# The origin a row belongs to (the machine that recorded it), an SQL expression over the row
# aliased t; NULL falls back to the exporting record's origin.
ROW_ORIGIN = {
    "tasks": "t.origin",
    "sessions": "t.origin",
    "causes": "t.origin",
    "standing_surface": "t.origin",
    "stages": "t.task_origin",
    "artifacts": "t.task_origin",
    "events": _session_origin("session_id"),
    "losses": _session_origin("session_id"),
    "compactions": _session_origin("session_id"),
    "context_births": _session_origin("session_id"),
    "judge_usage": _session_origin("session_id"),
    "token_usage": f"COALESCE({_session_origin('session_id')}, t.task_origin)",
    "contours": f"COALESCE({_session_origin('parent_session')}, t.task_origin)",
    "difficulties": f"COALESCE({_event_origin('difficulty_id')}, {_session_origin('session_id')})",
    "reviews": f"COALESCE({_event_origin('review_id')}, {_session_origin('session_id')})",
    "spawns": f"COALESCE({_event_origin('spawn_id')}, {_session_origin('parent_session')})",
}


class Refused(Exception):
    """The export cannot be taken or merged as it is; the message names why."""


def undeclared_tables(conn) -> "list[str]":
    """Tables of the record that are neither merged by a key nor declared machine-local."""
    return [name for name in store.table_names(conn) if name not in MERGE_KEYS and name not in LOCAL_ONLY]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _exported(table: str, row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in LOCAL_ROWID_COLUMNS.get(table, ())}


def export(record, out, with_text: bool = False, taken_at: "str | None" = None) -> dict:
    """Write the record's structured rows (and text artifacts with `with_text`) into the new file
    `out`, created 0600; the record is read quietly (store.read_record_quietly). Returns the header."""
    out = Path(out)
    taken_at = taken_at or _now_iso()
    return store.read_record_quietly(record, lambda conn: _write_export(conn, out, with_text, taken_at),
                                     lambda: out.unlink(missing_ok=True))


def _write_export(conn, out: Path, with_text: bool, taken_at: str) -> dict:
    undeclared = undeclared_tables(conn)
    if undeclared:
        raise Refused(f"table(s) {undeclared} have no merge key and are not declared local-only")
    with store.read_snapshot(conn):
        origin = store.record_origin(conn)
        names = [t for t in MERGE_KEYS if t in store.table_names(conn) and (with_text or t not in TEXT_TABLES)]
        header = {"format": FORMAT, "origin": origin, "taken_at": taken_at,
                  "table_versions": {name: tables.load(name).version for name in tables.TABLE_NAMES},
                  "fixture_sha256": sorted(store.fixture_revisions(conn)), "with_text": with_text,
                  "counts": store.table_counts(conn, names)}
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, EXPORT_MODE)
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps(header, sort_keys=True) + "\n")
            for table in names:
                for row_origin, row in store.export_rows(conn, table, ROW_ORIGIN[table]):
                    line = {"table": table, "origin": row_origin or origin, "row": _exported(table, row)}
                    handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
    return header


def read_export(path) -> "tuple[dict, list[dict]]":
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        header = json.loads(handle.readline())
        if not isinstance(header, dict) or header.get("format") != FORMAT:
            raise Refused(f"{path} is not a {FORMAT} export")
        return header, [json.loads(line) for line in handle if line.strip()]


class _Versions:
    """Refuses a table version the receiving Core's snapshot history does not hold."""

    def __init__(self):
        self.seen = {}

    def require(self, name: str, version) -> None:
        if (name, version) not in self.seen:
            self.seen[(name, version)] = tables.known_version(name, version)
        if not self.seen[(name, version)]:
            raise Refused(f"unknown version {version!r} of table {name!r}")


def _validate(conn, header: dict, lines: "list[dict]") -> None:
    versions = _Versions()
    for name, version in header["table_versions"].items():
        versions.require(name, version)
    for line in lines:
        table = line["table"]
        if table not in MERGE_KEYS:
            raise Refused(f"table {table!r} has no merge key here")
        for column, name in VERSION_COLUMNS.items():
            if line["row"].get(column) is not None:
                versions.require(name, line["row"][column])
    theirs = set(header["fixture_sha256"])
    ours = store.fixture_revisions(conn)
    if len(theirs) > 1 or (theirs and ours and theirs != ours):
        raise Refused(f"fixture revision {sorted(theirs)} differs from this record's {sorted(ours)}")


def merge(conn, path) -> dict:
    """Merge the export at `path` into the record behind the writable `conn`. Returns the
    origin, whether the export was fresh (its own rows replace stored ones), the rows added
    per table and the imported rows superseded. Raises Refused before writing anything."""
    header, lines = read_export(path)
    _validate(conn, header, lines)
    source = header["origin"]
    watermark = store.merge_watermark(conn, source)
    fresh = source != store.record_origin(conn) and (
        watermark is None or _instant(header["taken_at"]) > _instant(watermark))
    pending: "dict[tuple[str, str], list[dict]]" = {}
    for line in lines:
        table = line["table"]
        upsert = fresh and MERGE_MODE[table] == "upsert" and line["origin"] == source
        pending.setdefault((table, "upsert" if upsert else "ignore"), []).append(line["row"])
    names = [t for t in MERGE_KEYS if t in store.table_names(conn)]
    before = store.table_counts(conn, names)
    for table in MERGE_KEYS:
        for mode in ("ignore", "upsert"):
            batch = pending.get((table, mode))
            if batch:
                store.merge_rows(conn, table, batch, MERGE_KEYS[table], mode)
    superseded = store.superseded_imported_event_ids(conn)
    if superseded:
        store.merge_rows(conn, "events", [{"event_id": i} for i in superseded], MERGE_KEYS["events"], "delete")
    if fresh:
        store.set_merge_watermark(conn, source, header["taken_at"])
    after = store.table_counts(conn, names)
    return {"origin": source, "fresh": fresh, "superseded": len(superseded),
            "added": {t: after[t] - before[t] for t in names if after[t] != before[t]}}


def check_roundtrip(workdir, record=None) -> dict:
    """Copy the record (opened read-only) into `workdir` with the backup API, export the copy,
    merge the export twice into a new record under `workdir`, and compare the row counts per
    table. ok is false on a count mismatch, a row the second merge added, or a text row in the
    export. Writes nothing outside `workdir`."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    store.backup_record(Path(record or config_root.telemetry_db()), workdir / "copy.sqlite")
    export_path = workdir / "export.jsonl.gz"
    header = export(workdir / "copy.sqlite", export_path)
    second = store.open_scratch(workdir / "second.sqlite")
    try:
        first = merge(second, export_path)
        again = merge(second, export_path)
        counts = store.table_counts(second, header["counts"])
    finally:
        second.close()
    expected = header["counts"]
    mismatches = {t: {"exported": expected[t], "merged": counts[t]} for t in expected if counts[t] != expected[t]}
    _, lines = read_export(export_path)
    text_rows = sum(1 for line in lines if line["table"] in TEXT_TABLES)
    return {"ok": not mismatches and not again["added"] and not text_rows, "mismatches": mismatches,
            "first_merge_added": first["added"], "second_merge_added": again["added"], "text_rows": text_rows,
            "export": str(export_path)}
