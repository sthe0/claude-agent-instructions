"""Export, merge and round-trip check between machines' telemetry records (lib/telemetry/portability.py).

Every record is built under tmp_path: the conftest points the live record at a per-test path,
and the other machines' records are scratch records beside it."""
from __future__ import annotations

import ast
import gzip
import hashlib
import itertools
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
import tracemalloc
import uuid
from pathlib import Path

import pytest

import telemetry
from lib import config_root
from lib.telemetry import portability, store, tables
from telemetry_import_support import world  # noqa: F401  (fixture)

T1, T2, T3 = "2026-03-01T10:00:00.000000+00:00", "2026-03-02T10:00:00.000000+00:00", "2026-03-03T10:00:00.000000+00:00"


class Rec:
    """A record of one machine: its path, a writable connection and its origin."""

    def __init__(self, path: Path, conn=None):
        self.path = path
        self.conn = conn or store.open_scratch(path)
        self.origin = store.record_origin(self.conn)

    def put(self, table: str, *rows, mode: str = "ignore") -> None:
        store.merge_rows(self.conn, table, rows, portability.MERGE_KEYS[table], mode)

    def set(self, table: str, *rows) -> None:
        self.put(table, *rows, mode="upsert")

    def rows(self, sql: str, *args) -> list:
        return [tuple(r) for r in self.conn.execute(sql, args)]

    def counts(self) -> dict:
        return store.table_counts(self.conn, [t for t in portability.MERGE_KEYS if t in store.table_names(self.conn)])

    def export(self, name: str, taken_at: "str | None" = None, with_text: bool = False) -> Path:
        out = self.path.parent / f"{name}.jsonl.gz"
        portability.export(self.path, out, with_text=with_text, taken_at=taken_at)
        return out

    def merge(self, export: Path) -> dict:
        return portability.merge(self.conn, export)


@pytest.fixture
def records(tmp_path):
    made = []

    def make(name: str) -> Rec:
        rec = Rec(tmp_path / f"{name}.sqlite")
        made.append(rec)
        return rec
    yield make
    for rec in made:
        rec.conn.close()


@pytest.fixture
def live_record():
    """The record at the conftest's per-test live path, built by the store's own creation path."""
    conn = store.open_for_merge()
    rec = Rec(config_root.telemetry_db(), conn)
    yield rec
    conn.close()


def task(origin, task_id="t", epoch="1", **extra) -> dict:
    return {"origin": origin, "task_id": task_id, "epoch": epoch, "status": "open", **extra}


def key(origin, task_id="t", epoch="1") -> dict:
    return {"task_origin": origin, "task_id": task_id, "task_epoch": epoch}


def session(session_id, origin, task_key=None, **extra) -> dict:
    return {"session_id": session_id, "origin": origin, **(task_key or {}), **extra}


def event(event_id, session_id, kind="gate", provenance="live", **extra) -> dict:
    return {"event_id": event_id, "session_id": session_id, "kind": kind, "provenance": provenance,
            "time": "2026-03-01T10:00:00Z", **extra}


def loss(session_id, mode="torn_event", count=1) -> dict:
    return {"session_id": session_id, "loss_mode": mode, "loss_mode_table_version": tables.load("loss_modes").version,
            "count": count}


def populate(rec: Rec, tag: str = "a") -> None:
    """One row in most structured tables, and one text artifact, all of `rec`'s origin."""
    o = rec.origin
    rec.put("tasks", task(o, f"{tag}-task"))
    rec.put("sessions", session(f"{tag}-s", o, key(o, f"{tag}-task"), core_head="h1"))
    rec.put("contours", {"contour_id": f"{tag}-c", "parent_session": f"{tag}-s", **key(o, f"{tag}-task")})
    rec.put("events", event(f"{tag}-e1", f"{tag}-s"), event(f"{tag}-e2", f"{tag}-s", provenance="imported"))
    rec.put("stages", {**key(o, f"{tag}-task"), "stage_index": 1, "status": "open"})
    rec.put("token_usage", {"message_id": f"{tag}-m", "session_id": f"{tag}-s", **key(o, f"{tag}-task"),
                            "input_tokens": 3})
    rec.put("losses", loss(f"{tag}-s"))
    rec.put("artifacts", {"sha256": hashlib.sha256(tag.encode()).hexdigest(), "kind": "plan",
                          **key(o, f"{tag}-task"), "text": f"secret text {tag}"})


def export_lines(path: Path) -> "tuple[dict, list]":
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    return lines[0], lines[1:]


def rewrite_export(path: Path, header_change=None, row_change=None) -> None:
    header, lines = export_lines(path)
    if header_change:
        header_change(header)
    if row_change:
        for line in lines:
            row_change(line)
    path.unlink()
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for obj in [header, *lines]:
            handle.write(json.dumps(obj) + "\n")


def file_state(root: Path, skip: "Path | None" = None) -> dict:
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and (skip is None or skip not in path.parents):
            state[str(path)] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return state


def digest(path: Path) -> "tuple[int, str]":
    return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()


# --- export ------------------------------------------------------------------------

def test_export_without_text_flag_has_no_text_rows(records, live_record, tmp_path):
    a = records("a")
    populate(a)
    header, lines = export_lines(a.export("plain"))
    assert "artifacts" not in header["counts"] and not header["with_text"]
    assert not [line for line in lines if line["table"] == "artifacts"]
    assert not [line for line in lines if "secret text" in json.dumps(line)]
    header, lines = export_lines(a.export("text", with_text=True))
    texts = [line["row"] for line in lines if line["table"] == "artifacts"]
    assert header["counts"]["artifacts"] == 1 and texts and "artifact_id" not in texts[0]
    populate(live_record)
    live_record.conn.close()
    result = portability.check_roundtrip(tmp_path / "D", config_root.telemetry_db())
    assert result["ok"] and result["text_rows"] == 0


def test_export_file_mode_0600(records):
    a = records("a")
    populate(a)
    previous = os.umask(0o022)
    try:
        out = a.export("mode")
    finally:
        os.umask(previous)
    assert out.stat().st_mode & 0o777 == 0o600


def test_origin_is_random_not_hostname(records):
    a, b = records("a"), records("b")
    host = socket.gethostname()
    for origin in (a.origin, b.origin):
        assert uuid.UUID(origin).version == 4
        assert host not in origin
        assert origin not in {str(uuid.uuid5(ns, host)) for ns in (uuid.NAMESPACE_DNS, uuid.NAMESPACE_URL)}
    assert a.origin != b.origin
    header, _ = export_lines(a.export("origin"))
    assert header["origin"] == a.origin


def test_export_leaves_live_record_unchanged(live_record, tmp_path):
    populate(live_record)
    live_record.conn.close()
    record = config_root.telemetry_db()
    before = file_state(record.parent)
    portability.export(record, tmp_path / "out.jsonl.gz")
    assert file_state(record.parent) == before


# --- merge keys --------------------------------------------------------------------

def test_merge_keys_equal_the_identity_table():
    assert portability.MERGE_KEYS == {
        "tasks": ("origin", "task_id", "epoch"),
        "sessions": ("session_id",), "contours": ("contour_id",), "events": ("event_id",),
        "stages": ("task_origin", "task_id", "task_epoch", "stage_index"),
        "difficulties": ("difficulty_id",), "reviews": ("review_id",), "spawns": ("spawn_id",),
        "token_usage": ("message_id",), "compactions": ("compaction_id",), "context_births": ("birth_id",),
        "judge_usage": ("usage_id",), "causes": ("cause_id",), "losses": ("session_id", "loss_mode"),
        "standing_surface": ("origin", "core_head"), "artifacts": ("sha256",)}
    upsert = {"tasks", "sessions", "contours", "stages", "reviews", "spawns", "difficulties", "token_usage",
              "losses", "standing_surface"}
    assert portability.MERGE_MODE == {t: "upsert" if t in upsert else "ignore" for t in portability.MERGE_KEYS}


def test_every_table_has_a_merge_key_or_is_local_only(records):
    a = records("a")
    names = set(store.table_names(a.conn))
    assert store.applied_migrations(a.conn) == [m[0] for m in store._migration_files()]
    keyed, local = set(portability.MERGE_KEYS), set(portability.LOCAL_ONLY)
    assert not keyed & local
    assert names <= keyed | local, sorted(names - keyed - local)
    assert keyed <= names and set(portability.MERGE_MODE) == keyed and set(portability.ROW_ORIGIN) == keyed
    assert set(portability.MERGE_MODE.values()) <= {"ignore", "upsert"}
    version_columns = {c for t in names for c in store.table_columns(a.conn, t) if c.endswith("_table_version")}
    assert version_columns == set(portability.VERSION_COLUMNS)
    a.conn.execute("CREATE TABLE added_later (x TEXT)")
    with pytest.raises(portability.Refused, match="added_later"):
        a.export("undeclared")


# --- merge -------------------------------------------------------------------------

def test_import_into_second_record_same_structured_counts(live_record, tmp_path):
    populate(live_record, "a")
    populate(live_record, "b")
    expected = {t: n for t, n in live_record.counts().items() if t not in portability.TEXT_TABLES}
    live_record.conn.close()
    result = portability.check_roundtrip(tmp_path / "D", config_root.telemetry_db())
    assert result["ok"] and not result["mismatches"]
    assert {t: n for t, n in result["first_merge_added"].items()} == {t: n for t, n in expected.items() if n}
    second = sqlite3.connect(tmp_path / "D" / "second.sqlite")
    try:
        assert store.table_counts(second, expected) == expected
    finally:
        second.close()


def test_reimport_adds_no_row(records):
    a, b = records("a"), records("b")
    populate(a)
    export = a.export("e", T1, with_text=True)
    first = b.merge(export)
    counts = b.counts()
    assert first["added"] and b.merge(export)["added"] == {} and b.counts() == counts
    assert a.merge(export)["added"] == {}


def test_merge_keeps_unrelated_same_slug_tasks_of_two_origins_apart(records):
    a, b = records("a"), records("b")
    a.put("tasks", task(a.origin, "slug", "1"), task(a.origin, "slug", "2"))
    b.put("tasks", task(b.origin, "slug", "1"))
    b.merge(a.export("a", T1))
    assert sorted(b.rows("SELECT origin, epoch FROM tasks WHERE task_id = 'slug'")) == sorted(
        [(a.origin, "1"), (a.origin, "2"), (b.origin, "1")])
    a.merge(b.export("b", T1))
    assert len(a.rows("SELECT 1 FROM tasks WHERE task_id = 'slug'")) == 3


def test_import_refuses_unknown_table_version(records):
    a, b = records("a"), records("b")
    populate(a)
    header_bad = a.export("header", T1)
    rewrite_export(header_bad, header_change=lambda h: h["table_versions"].update(loss_modes=999))
    row_bad = a.export("row", T2)
    rewrite_export(row_bad, row_change=lambda line: line["row"].update(role_table_version=998)
                   if line["table"] == "sessions" else None)
    before = b.counts()
    with pytest.raises(portability.Refused, match="999.*loss_modes"):
        b.merge(header_bad)
    with pytest.raises(portability.Refused, match="998.*roles"):
        b.merge(row_bad)
    assert b.counts() == before
    assert telemetry.main(["merge", str(header_bad)]) == 2


def test_import_accepts_an_older_version_in_the_snapshot_history(records):
    current = tables.load("loss_modes").version
    older = min(int(v) for v in tables.snapshot()["loss_modes"])
    assert older < current
    a, b = records("a"), records("b")
    a.put("sessions", session("s", a.origin))
    a.put("losses", {**loss("s"), "loss_mode_table_version": older})
    export = a.export("old", T1)
    rewrite_export(export, header_change=lambda h: h["table_versions"].update(loss_modes=older))
    b.merge(export)
    assert b.rows("SELECT loss_mode_table_version FROM losses") == [(older,)]


def test_merge_drops_an_imported_row_a_live_row_back_references(records):
    for live_first in (True, False):
        for kind in ("gate", "spawn"):
            holder, liver, target = records(f"h-{kind}-{live_first}"), records(f"l-{kind}-{live_first}"), \
                records(f"t-{kind}-{live_first}")
            imported_id = str(uuid.uuid4())
            holder.put("sessions", session("old", holder.origin))
            liver.put("sessions", session("new", liver.origin))
            if kind == "gate":
                holder.put("events", event(imported_id, "old", provenance="imported"))
                liver.put("events", event("live-1", "new", backref_id=imported_id))
            else:
                holder.put("events", event(imported_id, "old", kind="spawn_cost", provenance="imported",
                                           child_session_id="child"))
                liver.put("events", event("live-1", "new", kind="spawn", child_session_id="child"))
            exports = [liver.export(f"live-{kind}-{live_first}", T1),
                       holder.export(f"imported-{kind}-{live_first}", T1)]
            for export in exports if live_first else exports[::-1]:
                target.merge(export)
            assert target.rows("SELECT event_id FROM events ORDER BY event_id") == [("live-1",)], (kind, live_first)


def test_merge_two_exports_of_one_machine_taken_at_different_times(records):
    a = records("a")
    o = a.origin
    a.put("tasks", task(o, "t1"), task(o, "t2"))
    a.put("sessions", session("s", o, key(o, "t1"), core_head="h1"))
    a.put("contours", {"contour_id": "c", "parent_session": "s", **key(o, "t1")})
    a.put("token_usage", {"message_id": "m", "session_id": "s", **key(o, "t1"), "input_tokens": 1})
    a.put("losses", loss("s", count=1), loss("s", "partial_session_before_first_live_event", 1))
    earlier = a.export("earlier", T1)
    a.set("sessions", session("s", o, key(o, "t2"), core_head="h2"))
    a.set("contours", {"contour_id": "c", "parent_session": "s", **key(o, "t2")})
    a.set("token_usage", {"message_id": "m", "session_id": "s", **key(o, "t2"), "input_tokens": 1})
    a.set("losses", loss("s", count=4), loss("s", "partial_session_before_first_live_event", 0))
    later = a.export("later", T2)
    for name, order in (("later-first", (later, earlier)), ("earlier-first", (earlier, later))):
        b = records(name)
        for export in order:
            b.merge(export)
        assert b.rows("SELECT core_head, task_id FROM sessions") == [("h2", "t2")], name
        assert b.rows("SELECT task_id FROM contours") == [("t2",)], name
        assert b.rows("SELECT task_id FROM token_usage") == [("t2",)], name
        assert sorted(b.rows("SELECT loss_mode, count FROM losses")) == [
            ("partial_session_before_first_live_event", 0), ("torn_event", 4)], name
        assert store.merge_watermark(b.conn, o) == T2


def test_merge_three_origins_keeps_the_newest_copy_of_each_origins_rows(records):
    x_seed, y, z = records("x-seed"), records("y"), records("z")
    z.put("sessions", session("zs", z.origin))
    z.put("losses", loss("zs", count=1))
    z1 = z.export("z1", T1)
    y.merge(z1)
    z.set("losses", loss("zs", count=5))
    z2 = z.export("z2", T2)
    y.put("sessions", session("ys", y.origin))
    y.put("losses", loss("ys", count=7))
    y1 = y.export("y1", T3)
    x_seed.conn.close()
    for index, order in enumerate(itertools.permutations((z1, z2, y1))):
        x = records(f"x{index}")
        for export in order:
            x.merge(export)
        assert sorted(x.rows("SELECT session_id, count FROM losses")) == [("ys", 7), ("zs", 5)], order


def test_merge_of_own_older_export_never_replaces_own_rows(records):
    a = records("a")
    a.put("sessions", session("s", a.origin, core_head="h1"))
    a.put("losses", loss("s", count=1))
    older = a.export("older", T1)
    a.set("sessions", session("s", a.origin, core_head="h2"))
    a.set("losses", loss("s", count=4))
    result = a.merge(older)
    assert not result["fresh"] and result["added"] == {}
    assert a.rows("SELECT core_head FROM sessions") == [("h2",)]
    assert a.rows("SELECT count FROM losses") == [(4,)]


def test_merge_of_own_older_export_restores_no_row_its_replay_deleted(world, tmp_path):
    conn = store.open_for_merge()
    rec = Rec(config_root.telemetry_db(), conn)
    source = tmp_path / "ledger.jsonl"
    sha = hashlib.sha256(b"line").hexdigest()
    imported_id = store.imported_row_id(str(source), 0, sha, rec.origin)
    rec.put("sessions", session("s", rec.origin))
    rec.put("events", event(imported_id, "s", provenance="imported"))
    older = rec.export("older", T1)
    world.live_event("s", "gate", "gate:plan", backref={"path": str(source), "offset": 0, "sha256": sha})
    assert world.replay("s")["superseded"] == 1
    assert not rec.rows("SELECT 1 FROM events WHERE event_id = ?", imported_id)
    rec.merge(older)
    assert not rec.rows("SELECT 1 FROM events WHERE event_id = ?", imported_id)
    conn.close()


def test_merged_session_attaches_to_its_own_task_across_origins(records):
    a, b = records("a"), records("b")
    b.put("tasks", task(b.origin, "b-task"))
    a.put("tasks", task(a.origin, "a-task"))
    mine = key(a.origin, "a-task")
    rows = {
        "sessions": session("s", a.origin, mine),
        "contours": {"contour_id": "c", "parent_session": "s", **mine},
        "stages": {**mine, "stage_index": 1},
        "difficulties": {"difficulty_id": "d", "session_id": "s", **mine},
        "reviews": {"review_id": "r", "session_id": "s", **mine},
        "token_usage": {"message_id": "m", "session_id": "s", **mine},
        "context_births": {"birth_id": "cb", "session_id": "s", "kind": "spawn", **mine},
        "judge_usage": {"usage_id": "j", "session_id": "s", "has_usage": 0, **mine},
        "artifacts": {"sha256": "0" * 64, "text": "x", **mine},
    }
    with_task_key = {t for t in store.table_names(a.conn) if "task_origin" in store.table_columns(a.conn, t)}
    assert set(rows) == with_task_key
    for table, row in rows.items():
        a.put(table, row)
    b.merge(a.export("a", T1, with_text=True))
    for table in rows:
        assert b.rows(f"SELECT task_origin, task_id, task_epoch FROM {table}") == [(a.origin, "a-task", "1")], table


def test_merge_insert_runs_past_the_command_path_deadline(records, monkeypatch):
    monkeypatch.setattr(store, "DEADLINE_S", 0.25)
    a, b = records("a"), records("b")
    populate(a)
    export = a.export("e", T1)
    held, done = threading.Event(), threading.Event()

    def hold():
        other = sqlite3.connect(b.path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(0.5)
        other.execute("COMMIT")
        other.close()
        done.set()
    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    started = time.monotonic()
    try:
        b.merge(export)
    finally:
        thread.join()
    assert time.monotonic() - started >= 0.4 and done.is_set()
    assert {t: n for t, n in b.counts().items() if n} == {t: n for t, n in a.counts().items()
                                                          if n and t not in portability.TEXT_TABLES}


# --- fixture causes ----------------------------------------------------------------

def _import_fixture_into(world, monkeypatch, path: Path) -> None:
    monkeypatch.setenv("AGENTCTL_TELEMETRY_DB", str(path))
    world.run()


def test_merge_of_two_origins_keeps_235_fixture_causes(world, monkeypatch, tmp_path):
    first, second = tmp_path / "one" / "telemetry.sqlite", tmp_path / "two" / "telemetry.sqlite"
    _import_fixture_into(world, monkeypatch, first)
    _import_fixture_into(world, monkeypatch, second)
    export = tmp_path / "one.jsonl.gz"
    portability.export(first, export, taken_at=T1)
    conn = store.open_for_merge(second)
    try:
        assert store.record_origin(conn) != export_lines(export)[0]["origin"]
        portability.merge(conn, export)
        assert conn.execute("SELECT COUNT(*) FROM causes WHERE fixture_sha256 IS NOT NULL").fetchone()[0] == 235
    finally:
        conn.close()


def test_merge_refuses_an_export_with_another_fixture_revision(world, monkeypatch, tmp_path, records):
    first = tmp_path / "one" / "telemetry.sqlite"
    _import_fixture_into(world, monkeypatch, first)
    export = tmp_path / "one.jsonl.gz"
    portability.export(first, export, taken_at=T1)
    b = records("b")
    b.put("causes", {"cause_id": "other", "session": "s", "cause_category": "x", "cause_category_table_version": 1,
                     "machinery_induced": 0, "mechanism": "x", "mechanism_table_version": 1,
                     "functional_place": "x", "functional_place_table_version": 1, "origin": b.origin,
                     "imported": 1, "fixture_sha256": "f" * 64, "fixture_row": 0})
    with pytest.raises(portability.Refused, match="fixture revision"):
        b.merge(export)
    assert b.rows("SELECT COUNT(*) FROM causes") == [(1,)]


# --- check-roundtrip ---------------------------------------------------------------

def test_check_roundtrip_fails_on_count_mismatch(live_record, tmp_path, monkeypatch):
    populate(live_record)
    live_record.conn.close()
    real = store.merge_rows

    def lossy(conn, table, rows, key_columns, mode, **kw):
        return real(conn, table, [] if table == "sessions" and mode != "delete" else rows, key_columns, mode, **kw)
    monkeypatch.setattr(store, "merge_rows", lossy)
    result = portability.check_roundtrip(tmp_path / "D", config_root.telemetry_db())
    assert not result["ok"] and result["mismatches"] == {"sessions": {"exported": 1, "merged": 0}}
    assert telemetry.main(["check-roundtrip", "--workdir", str(tmp_path / "D2")]) == 1


def test_check_roundtrip_writes_only_into_workdir(live_record, tmp_path):
    populate(live_record)
    live_record.conn.close()
    workdir = tmp_path / "D"
    before = file_state(tmp_path)
    assert telemetry.main(["check-roundtrip", "--workdir", str(workdir)]) == 0
    assert file_state(tmp_path, skip=workdir) == before
    assert {p.name for p in workdir.iterdir()} >= {"copy.sqlite", "export.jsonl.gz", "second.sqlite"}


def test_check_roundtrip_opens_the_live_record_read_only(live_record, tmp_path, monkeypatch):
    populate(live_record)
    live_record.conn.close()
    record = config_root.telemetry_db()
    before = digest(record)
    refused = []
    real = store.backup

    def probing(src_conn, dest_path):
        try:
            src_conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")
        except sqlite3.OperationalError as err:
            refused.append("readonly" in str(err))
        return real(src_conn, dest_path)
    monkeypatch.setattr(store, "backup", probing)
    assert portability.check_roundtrip(tmp_path / "D", config_root.telemetry_db())["ok"]
    assert refused == [True]
    assert digest(record) == before


def test_check_roundtrip_refuses_without_free_disk(live_record, tmp_path, monkeypatch):
    populate(live_record)
    live_record.conn.close()
    real = shutil.disk_usage
    monkeypatch.setattr(portability.shutil, "disk_usage", lambda path: real(path)._replace(free=1))
    with pytest.raises(portability.Refused, match="bytes free"):
        portability.check_roundtrip(tmp_path / "D", config_root.telemetry_db())
    assert not (tmp_path / "D" / "copy.sqlite").exists()
    assert telemetry.main(["check-roundtrip", "--workdir", str(tmp_path / "D2")]) == 2


# --- streaming and validation before any write ---------------------------------------

def _events_record(records, name: str, n: int) -> Rec:
    rec = records(name)
    rec.put("sessions", session("s", rec.origin))
    store.merge_rows(rec.conn, "events", (event(f"{name}-{i:07d}", "s", source_key="k" * 200) for i in range(n)),
                     portability.MERGE_KEYS["events"], "ignore")
    return rec


def _merge_peak_bytes(target: Rec, export: Path) -> int:
    tracemalloc.start()
    try:
        target.merge(export)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_merge_memory_stays_bounded_as_the_export_grows(records, monkeypatch):
    monkeypatch.setattr(store, "MERGE_CHUNK_ROWS", 100)
    small, large = _events_record(records, "small", 1_000), _events_record(records, "large", 8_000)
    small_peak = _merge_peak_bytes(records("small-target"), small.export("small", T1))
    large_target = records("large-target")
    large_peak = _merge_peak_bytes(large_target, large.export("large", T1))
    assert large_target.counts()["events"] == 8_000
    assert large_peak < small_peak * 1.5 + 256 * 1024, (small_peak, large_peak)
    assert large_peak < 4 * 1024 * 1024, large_peak


def test_merge_commits_in_chunks(records, monkeypatch):
    monkeypatch.setattr(store, "MERGE_CHUNK_ROWS", 2)
    a = records("a")
    transactions = []
    real = store._merge_transaction

    def counting(conn, work):
        transactions.append(1)
        return real(conn, work)
    monkeypatch.setattr(store, "_merge_transaction", counting)
    assert store.merge_rows(a.conn, "tasks", (task(a.origin, f"t{i}") for i in range(5)),
                            portability.MERGE_KEYS["tasks"], "ignore") == 5
    assert len(transactions) == 3


def test_merge_refuses_a_truncated_export(records):
    a, b = records("a"), records("b")
    populate(a)
    short = a.export("short", T1)
    header, lines = export_lines(short)
    short.unlink()
    with gzip.open(short, "wt", encoding="utf-8") as handle:
        for obj in [header, *lines[:-1]]:
            handle.write(json.dumps(obj) + "\n")
    cut = a.export("cut", T2)
    cut.write_bytes(cut.read_bytes()[:-20])
    before = b.counts()
    with pytest.raises(portability.Refused, match="truncated or altered"):
        b.merge(short)
    with pytest.raises(portability.Refused, match="truncated or corrupt"):
        b.merge(cut)
    assert b.counts() == before


def test_merge_refuses_an_unknown_column_before_writing(records):
    a, b = records("a"), records("b")
    populate(a)
    newer = a.export("newer", T1)
    rewrite_export(newer, row_change=lambda line: line["row"].update(future_col=1)
                   if line["table"] == "sessions" else None)
    before = b.counts()
    with pytest.raises(portability.Refused, match="future_col"):
        b.merge(newer)
    assert b.counts() == before


def test_merge_refuses_a_malformed_header(records):
    a, b = records("a"), records("b")
    populate(a)
    for field in ("table_versions", "fixture_sha256", "origin", "counts"):
        bad = a.export(f"no-{field}", T1)
        rewrite_export(bad, header_change=lambda h, f=field: h.pop(f))
        with pytest.raises(portability.Refused, match=field):
            b.merge(bad)
    bad = a.export("bad-time", T1)
    rewrite_export(bad, header_change=lambda h: h.update(taken_at="never"))
    with pytest.raises(portability.Refused, match="no time"):
        b.merge(bad)
    assert b.counts() == {t: 0 for t in b.counts()}


# --- row origins --------------------------------------------------------------------

def test_unresolved_row_round_trip_leaves_its_owner_row_unchanged(records):
    a, b = records("a"), records("b")
    a.put("token_usage", {"message_id": "m", "session_id": "no-session-row", "input_tokens": 3})
    first = a.export("a1", T1)
    assert [line["origin"] for line in export_lines(first)[1] if line["table"] == "token_usage"] == [None]
    b.merge(first)
    a.set("token_usage", {"message_id": "m", "session_id": "no-session-row", "input_tokens": 9})
    a.merge(b.export("b1", T2))
    assert a.rows("SELECT input_tokens FROM token_usage") == [(9,)]


def test_unresolved_row_merges_insert_or_ignore_where_the_receiver_resolves_it(records):
    x, y = records("x"), records("y")
    y.put("token_usage", {"message_id": "m", "session_id": "ys", "input_tokens": 9})
    x.put("sessions", session("ys", y.origin))
    x.put("token_usage", {"message_id": "m", "session_id": "ys", "input_tokens": 3})
    assert x.merge(y.export("y1", T1))["fresh"]
    assert x.rows("SELECT input_tokens FROM token_usage") == [(3,)]


def test_upsert_never_overwrites_a_stored_row_of_another_origin(records):
    a, b = records("a"), records("b")
    b.put("sessions", session("s", b.origin, core_head="hb"))
    a.put("sessions", session("s", a.origin, core_head="ha"))
    assert b.merge(a.export("a", T1))["fresh"]
    assert b.rows("SELECT origin, core_head FROM sessions") == [(b.origin, "hb")]


# --- quiet read of the record ----------------------------------------------------------

def _probe_reads(monkeypatch) -> list:
    modes = []
    real = store._read_with

    def probing(record, read, immutable):
        modes.append(immutable)
        return real(record, read, immutable)
    monkeypatch.setattr(store, "_read_with", probing)
    return modes


def test_read_quietly_reads_plainly_while_a_wal_exists(records, monkeypatch):
    a = records("a")
    populate(a)
    assert a.path.with_name(a.path.name + "-wal").exists()
    modes, discards = _probe_reads(monkeypatch), []
    count = store.read_record_quietly(a.path, lambda conn: store.table_counts(conn, ["tasks"]),
                                      lambda: discards.append(1))
    assert count == {"tasks": 1} and modes == [False] and not discards


def test_read_quietly_discards_a_read_when_the_record_changes(records, monkeypatch):
    a = records("a")
    populate(a)
    a.conn.close()
    assert not a.path.with_name(a.path.name + "-wal").exists()
    modes, discards, reads = _probe_reads(monkeypatch), [], []

    def read(conn):
        reads.append(1)
        if len(reads) == 1:
            writer = store.open_for_merge(a.path)
            store.merge_rows(writer, "tasks", [task(a.origin, "added")], portability.MERGE_KEYS["tasks"], "ignore")
            writer.close()
        return store.table_counts(conn, ["tasks"])
    assert store.read_record_quietly(a.path, read, lambda: discards.append(1)) == {"tasks": 2}
    assert modes == [True, False] and discards == [1]


def test_read_quietly_retries_after_an_immutable_read_error(records, monkeypatch):
    a = records("a")
    populate(a)
    a.conn.close()
    modes, discards = _probe_reads(monkeypatch), []

    def read(conn):
        if len(modes) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return store.table_counts(conn, ["tasks"])
    assert store.read_record_quietly(a.path, read, lambda: discards.append(1)) == {"tasks": 1}
    assert modes == [True, False] and discards == [1]


def test_export_removes_a_partial_file_on_error(records, monkeypatch):
    a = records("a")
    populate(a)
    real = store.export_rows

    def failing(conn, table, origin_sql):
        yield from real(conn, table, origin_sql)
        raise RuntimeError("disk gone")
    monkeypatch.setattr(store, "export_rows", failing)
    out = a.path.parent / "partial.jsonl.gz"
    with pytest.raises(RuntimeError, match="disk gone"):
        portability.export(a.path, out)
    assert not out.exists()
    out.write_bytes(b"kept")
    with pytest.raises(FileExistsError):
        portability.export(a.path, out)
    assert out.read_bytes() == b"kept"


def test_portability_opens_the_record_only_through_store():
    source = Path(portability.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "sqlite3" not in imported
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "sqlite3"]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "connect"]
