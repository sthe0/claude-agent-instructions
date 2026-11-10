"""Token accounting (lib/telemetry/tokens.py): the collector over synthetic transcripts and judge
ledgers, its offsets and read outcomes, the standing surface and `telemetry.py check-tokens`."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import telemetry as telemetry_cli
from lib import config_root
from lib.telemetry import importer, store, sync, tables, tokens
from telemetry_import_support import at, line, world  # noqa: F401  (world is a fixture)
from telemetry_live_support import query as _query


def query(sql, *args) -> list:
    return [tuple(row) for row in _query(sql, *args)]


def usage(seconds, message_id, session, inp=10, out=5, write=0, read=0, model="claude-test"):
    return {"type": "assistant", "timestamp": at(seconds), "sessionId": session,
            "message": {"id": message_id, "model": model, "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": write, "cache_read_input_tokens": read}}}


def user(seconds, session):
    return {"type": "user", "timestamp": at(seconds), "sessionId": session,
            "message": {"role": "user", "content": "go"}}


def boundary(seconds, session):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": at(seconds), "sessionId": session}


def put(table, *rows):
    assert store.insert_rows(table, rows)["failed"] == 0


def event(session, kind, seconds, fields=None, **columns) -> str:
    event_id = str(uuid.uuid4())
    put("events", {"event_id": event_id, "session_id": session, "kind": kind, "source_key": columns.pop("source_key", kind),
                   "provenance": "live", "time": at(seconds), "fields": json.dumps(fields or {}), **columns})
    return event_id


def open_task(session, task_id, seconds) -> tuple:
    epoch = event(session, "task", seconds, {"action": "open", "task_id": task_id}, source_key="cli:start")
    origin = store.origin_id()
    put("tasks", {"origin": origin, "task_id": task_id, "epoch": epoch, "opened_at": at(seconds)})
    return origin, task_id, epoch


def node(session, seconds, node_after):
    event(session, "command", seconds, {"node_after": node_after}, source_key="cli:status")


def spawn(parent, child, seconds=0):
    event(parent, "spawn", seconds, child_session_id=child)


def ledger_call(world, session, seconds, judge, usage_counts=None) -> str:
    obj = {"kind": "call", "judge": judge, "source": session, "ts": at(seconds), "timed_out": False}
    if usage_counts:
        obj["usage"] = usage_counts
    [(offset, sha, _)] = world.add(world.judge, obj)
    return world.id_of(world.judge, offset, sha)


def judge_event(session, seconds, judge, ledger_row_id) -> str:
    return event(session, "judge", seconds, source_key=f"judge:{judge}", backref_id=ledger_row_id)


def subagent(world, session, name, lines) -> Path:
    path = world.projects / "p" / session / "subagents" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line(obj) for obj in lines))
    return path


def check(capsys, *argv):
    rc = telemetry_cli.main(["check-tokens", *argv])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def write_record(sql, *args):
    conn = sqlite3.connect(config_root.telemetry_db())
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def tokens_offset(path) -> list:
    return query("SELECT byte_offset, read_outcome FROM import_offsets WHERE source = ? AND consumer = 'tokens'",
                 str(path))


def counts_of(*tables_) -> dict:
    return {t: query(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables_}


def defer_call(monkeypatch, number):
    """store.insert_rows_with_offset returns its `number`-th call (1-based, None: every call) deferred."""
    real, calls = store.insert_rows_with_offset, []

    def flaky(rows, offset, deadline_s=None, updates=()):
        calls.append(1)
        if number is None or len(calls) == number:
            rows = list(rows)
            return {"inserted": 0, "deferred": len(rows), "failed": 0, "committed": False}
        return real(rows, offset, deadline_s, updates)
    monkeypatch.setattr(store, "insert_rows_with_offset", flaky)


# --- context births and compactions ---------------------------------------------------------

def test_one_context_birth_event_per_spawn_compaction_and_resume(world):
    spawn("parent", "child")
    world.transcript("child", [user(1, "child"), usage(2, "c1", "child")])
    subagent(world, "parent", "agent-a", [user(3, "parent"), usage(4, "s1", "parent")])
    world.transcript("parent", [user(0, "parent"), usage(1, "p1", "parent", inp=100), boundary(5, "parent"),
                                usage(6, "p2", "parent")])
    world.transcript("resumed", [usage(1, "p1", "parent", inp=100), user(7, "resumed"), usage(8, "r1", "resumed")])
    tokens.collect()
    tokens.collect()
    assert dict(query("SELECT kind, COUNT(*) FROM context_births GROUP BY kind")) == {
        "spawn": 2, "compaction": 1, "resume": 1}
    assert {r[0] for r in query("SELECT session_id FROM context_births WHERE kind = 'spawn'")} == {"child", "parent"}
    assert query("SELECT session_id FROM context_births WHERE kind = 'resume'") == [("resumed",)]


def test_compaction_event_carries_pre_and_post_sizes(world, monkeypatch):
    monkeypatch.setattr(tokens, "BATCH_LINES", 1)
    world.transcript("s", [usage(1, "a", "s", inp=100, write=20, read=30), boundary(2, "s"), user(3, "s"),
                           usage(4, "b", "s", inp=40, read=5)])
    tokens.collect()
    assert query("SELECT pre_tokens, post_tokens FROM compactions") == [(150, 45)]


# --- conservation, attribution, deduplication ---------------------------------------------

def test_tokens_conserved_across_role_phase_cells_with_message_id_dedup(world, capsys):
    open_task("s", "task-c", 0)
    node("s", 1, "PLANNING")
    node("s", 10, "EXECUTING")
    world.transcript("s", [user(0.5, "s"), usage(5, "m1", "s", inp=10, out=3), usage(15, "m2", "s", inp=20, out=4),
                           usage(15, "m2", "s", inp=20, out=4)])
    tokens.collect()
    assert world.count("token_usage") == 2
    rc, out = check(capsys, "--task", "task-c")
    assert rc == 0 and out["conserved"]
    assert (out["actual"]["input_tokens"], out["actual"]["output_tokens"]) == (30, 7)
    assert set(out["cells"]) == {"root/planning", "root/execution"}


def test_each_message_lands_in_its_expected_role_phase_cell(world):
    open_task("s", "task-r", 0)
    node("s", 1, "PLANNING")
    node("s", 10, "EXECUTING")
    spawn("s", "kid", 11)
    world.transcript("s", [usage(5, "m1", "s"), usage(15, "m2", "s")])
    world.transcript("kid", [usage(16, "k1", "kid")])
    subagent(world, "s", "agent-b", [usage(17, "a1", "s")])
    tokens.collect()
    cells = {r[0]: (r[1], r[2]) for r in query("SELECT message_id, role, phase FROM token_usage")}
    assert cells == {"m1": ("root", "planning"), "m2": ("root", "execution"), "k1": ("spawned-child", "other"),
                     "a1": ("agent-tool", "execution")}


def test_resumed_copy_of_a_message_counted_once_per_task(world, capsys):
    key = open_task("a", "task-x", 0)
    put("sessions", {"session_id": "b", "task_origin": key[0], "task_id": key[1], "task_epoch": key[2]})
    world.transcript("a", [usage(5, "m1", "a", inp=10)])
    world.transcript("b", [usage(5, "m1", "a", inp=10), user(20, "b"), usage(21, "m2", "b", inp=7)])
    tokens.collect()
    assert world.count("token_usage") == 2
    assert world.one("SELECT session_id FROM token_usage WHERE message_id = 'm1'") == "a"
    rc, out = check(capsys, "--task", "task-x")
    assert rc == 0 and out["expected"]["input_tokens"] == out["actual"]["input_tokens"] == 17


def test_message_id_unique_across_the_record(world):
    open_task("a", "task-1", 0)
    open_task("b", "task-2", 0)
    world.transcript("a", [usage(5, "m", "a")])
    world.transcript("b", [usage(5, "m", "a"), user(6, "b"), usage(7, "n", "b")])
    tokens.collect()
    assert world.count("token_usage", "message_id = 'm'") == 1
    assert world.count("token_usage") == 2


def test_token_rows_carry_the_message_model(world):
    world.transcript("s", [usage(1, "m", "s", model="claude-opus-test")])
    tokens.collect()
    assert query("SELECT model FROM token_usage") == [("claude-opus-test",)]


def test_token_rows_store_role_and_phase_table_versions_validated(world):
    world.transcript("s", [usage(1, "m", "s")])
    tokens.collect()
    assert query("SELECT role_table_version, phase_table_version FROM token_usage") == [
        (tables.load("roles").version, tables.load("phases").version)]
    offset = {"source": "/nowhere/x.jsonl", "generation": "g", "consumer": tokens.CONSUMER, "byte_offset": 9,
              "last_start": 0, "last_sha256": "0" * 64, "read_size": 9, "carry": None}
    bad = store.insert_rows_with_offset([("token_usage", {"message_id": "x", "role": "nobody", "phase": "other"})],
                                        offset)
    assert bad["failed"] == 1 and not bad["committed"]
    assert tokens_offset("/nowhere/x.jsonl") == []


def test_winning_line_is_earliest_timestamp_then_path_whatever_the_read_order(world):
    for n, (first, second) in enumerate(((["late", "z-orig"], ["early", "a-copy"]),
                                         (["early", "a-copy"], ["late", "z-orig"]))):
        world.transcript(f"late{n}", [usage(10, f"t{n}", f"late{n}")])
        world.transcript(f"early{n}", [usage(5, f"t{n}", f"late{n}"), user(6, f"early{n}")])
        world.transcript(f"z-orig{n}", [usage(5, f"o{n}", f"z-orig{n}")])
        world.transcript(f"a-copy{n}", [usage(5, f"o{n}", f"z-orig{n}"), user(6, f"a-copy{n}")])
        tokens.collect([f"{s}{n}" for s in first])
        tokens.collect([f"{s}{n}" for s in second])
        assert world.one("SELECT session_id FROM token_usage WHERE message_id = ?", f"t{n}") == f"early{n}"
        assert world.one("SELECT session_id FROM token_usage WHERE message_id = ?", f"o{n}") == f"z-orig{n}"
        assert world.one("SELECT copy_rank FROM token_usage WHERE message_id = ?", f"o{n}") == 0


def test_copy_owned_by_another_task_listed_as_gap_not_mismatch(world, capsys):
    open_task("a", "task-1", 0)
    open_task("b", "task-0", 0)
    open_task("b", "task-2", 10)
    world.transcript("a", [usage(5, "m", "a", inp=10)])
    world.transcript("b", [usage(5, "m", "a", inp=10), user(6, "b"), usage(7, "early", "b", inp=3),
                           usage(12, "mine", "b", inp=4)])
    tokens.collect()
    rc, out = check(capsys, "--task", "task-2")
    assert rc == 0, out
    assert [g["message_id"] for g in out["gaps"]["owned_by_another_task"]] == ["m"]
    assert out["actual"]["input_tokens"] == out["expected"]["input_tokens"] == 4


def test_winning_line_without_open_epoch_listed_as_gap(world, capsys):
    open_task("s", "task-g", 10)
    world.transcript("s", [usage(5, "m0", "s", inp=2), usage(15, "m1", "s", inp=3)])
    tokens.collect()
    assert query("SELECT task_id FROM token_usage WHERE message_id = 'm0'") == [(None,)]
    rc, out = check(capsys, "--task", "task-g")
    assert rc == 0
    assert [g["message_id"] for g in out["gaps"]["no_open_epoch"]] == ["m0"]
    assert out["actual"]["input_tokens"] == 3


# --- judge usage ----------------------------------------------------------------------------

def test_judge_ledger_rows_counted_and_missing_judge_call_listed_as_gap(world, capsys):
    open_task("s", "task-j", 0)
    judge_event("s", 5, "plan-review", ledger_call(world, "s", 5, "plan-review", {"input_tokens": 7, "output_tokens": 2}))
    world.transcript("s", [usage(6, "m1", "s", inp=1)])
    tokens.collect()
    rc, out = check(capsys, "--task", "task-j")
    assert rc == 0 and out["gaps"]["missing_judge_call"] == []
    assert out["judge_usage"]["input_tokens"] == 7 and out["actual"]["input_tokens"] == 8
    missing = judge_event("s", 8, "plan-review", "no-such-ledger-row")
    rc, out = check(capsys, "--task", "task-j")
    assert rc == 1
    assert [g["event_id"] for g in out["gaps"]["missing_judge_call"]] == [missing]


def test_expected_judge_calls_are_the_judge_events(world, capsys):
    open_task("s", "task-k", 0)
    judge_event("s", 2, "j-a", ledger_call(world, "s", 2, "j-a", {"input_tokens": 3}))
    judge_event("s", 3, "j-b", ledger_call(world, "s", 3, "j-b"))
    stray = ledger_call(world, "s", 4, "j-c", {"input_tokens": 1})
    tokens.collect()
    rc, out = check(capsys, "--task", "task-k")
    assert rc == 0
    assert out["judge_calls"] == 2
    assert out["gaps"]["unexpected_judge_rows"] == [stray]
    assert out["gaps"]["judge_rows_without_usage"] == 1


# --- the standing surface -------------------------------------------------------------------

def test_standing_surface_utf8_bytes_with_hook_outputs_for_current_head(world, monkeypatch, tmp_path):
    assert tokens.static_surface_bytes() > 0
    monkeypatch.setattr(sync, "_canon_state", lambda: ("head-1", 0))
    static = tmp_path / "surface.md"
    static.write_text("é" * 3, encoding="utf-8")
    monkeypatch.setattr(tokens, "_lint", lambda: SimpleNamespace(
        _surface_files=lambda: [("CLAUDE.md", static)], _iter_skill_descriptions=lambda: [(static, "ж")]))
    put("sessions", {"session_id": "s", "core_head": "head-1", "started_at": at(0)},
        {"session_id": "old", "core_head": "head-0", "started_at": at(0)})
    hook = {"type": "attachment", "timestamp": at(0), "sessionId": "s", "attachment": {
        "type": "hook_success", "hookName": "SessionStart:startup", "hookEvent": "SessionStart", "content": "привет"}}
    world.transcript("s", [hook, usage(1, "m", "s")])
    world.transcript("old", [usage(1, "o", "old")])
    tokens.collect()
    assert query("SELECT origin, core_head, bytes, static_bytes, hook_bytes, session_id FROM standing_surface") == [
        (store.origin_id(), "head-1", 20, 8, 12, "s")]


def test_standing_surface_rows_of_two_origins_at_one_head_coexist(world):
    def row(origin):
        return {"origin": origin, "core_head": "h", "bytes": 1, "static_bytes": 1, "hook_bytes": 0}
    put("standing_surface", row(store.origin_id()), row(str(uuid.uuid4())))
    assert world.count("standing_surface", "core_head = 'h'") == 2


# --- the collector's reads ------------------------------------------------------------------

def test_imported_transcripts_with_usage_get_token_rows(world, capsys):
    world.transcript("s", [usage(1, "m1", "s"), usage(2, "m2", "s")])
    subagent(world, "s", "agent-c", [usage(3, "a1", "s")])
    report = world.run(include_transcripts=True)
    assert report["per_source"]["tokens"]["rows"] >= 3
    assert {r[0] for r in query("SELECT message_id FROM token_usage")} == {"m1", "m2", "a1"}
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 0 and out["covered"] == 2


def test_collector_is_incremental_and_idempotent(world):
    path = world.transcript("s", [usage(1, "m1", "s"), usage(2, "m2", "s")])
    assert tokens.collect()["rows"] == 2
    assert tokens.collect()["rows"] == 0
    world.add(path, usage(3, "m3", "s"))
    assert tokens.collect()["rows"] == 1
    assert world.count("token_usage") == 3
    assert tokens_offset(path) == [(path.stat().st_size, "committed")]


def test_token_offsets_per_file_and_consumer_start_at_zero(world):
    a = world.transcript("a", [usage(1, "m1", "a")])
    b = world.transcript("b", [usage(1, "m2", "b"), usage(2, "m3", "b")])
    world.run(include_transcripts=True)
    for path in (a, b):
        consumers = dict(world.offsets(path))
        assert consumers["tokens"] == path.stat().st_size
        assert "transcript_events" in consumers
    assert world.count("token_usage") == 3
    c = world.transcript("c", [usage(1, "m4", "c")])
    tokens.collect()
    assert tokens_offset(c) == [(c.stat().st_size, "committed")]
    assert world.count("token_usage", "message_id = 'm4'") == 1


def test_partial_session_read_to_last_complete_line(world):
    path = world.transcript("s", [usage(1, "m1", "s")])
    first_end = path.stat().st_size
    tail = line(usage(2, "m2", "s"))
    world.add(path, tail[:-5])
    tokens.collect()
    assert world.count("token_usage") == 1
    assert tokens_offset(path) == [(first_end, "committed")]
    world.add(path, tail[-5:])
    tokens.collect()
    assert world.count("token_usage") == 2


def test_reread_from_zero_under_changed_generation_and_after_rebuild_keeps_birth_compaction_and_judge_usage_counts(world):
    path = world.transcript("s", [user(0, "s"), usage(1, "m1", "s", inp=50), boundary(2, "s"), usage(3, "m2", "s")])
    subagent(world, "s", "agent-d", [usage(4, "a1", "s")])
    ledger_call(world, "s", 1, "plan-review", {"input_tokens": 2})
    tokens.collect()
    kept = ("context_births", "compactions", "judge_usage", "token_usage")
    before = counts_of(*kept)
    assert before == {"context_births": 2, "compactions": 1, "judge_usage": 1, "token_usage": 3}
    path.write_bytes(line({"type": "summary", "summary": "x"}) + path.read_bytes())
    world.judge.write_bytes(line({"kind": "hook_start", "hook": "h"}) + world.judge.read_bytes())
    tokens.collect()
    assert counts_of(*kept) == before
    importer.rebuild(include_transcripts=True)
    assert counts_of(*kept) == before


def test_context_births_written_for_sessions_with_live_events(world):
    node("kid", 1, "EXECUTING")
    spawn("p", "kid")
    world.transcript("kid", [usage(2, "k", "kid")])
    tokens.collect()
    assert world.count("context_births", "kind = 'spawn' AND session_id = 'kid'") == 1


# --- offsets and read outcomes --------------------------------------------------------------

def _token_row(message_id):
    return {"message_id": message_id, "session_id": "s", "role": "root", "phase": "other", "ts_ms": 1,
            "copy_rank": 0, "session_start_ms": 1, "transcript": "/nowhere/s.jsonl", "byte_offset": 0}


def _oversized_first_line(session) -> dict:
    return {"type": "summary", "sessionId": session, "summary": "x" * (importer.FIRST_LINE_MAX + 10)}


def test_oversized_first_line_transcript_is_collected(world, capsys):
    path = world.transcript("s", [_oversized_first_line("s"), usage(1, "m1", "s"), usage(2, "m2", "s")])
    tokens.collect()
    assert {r[0] for r in query("SELECT message_id FROM token_usage")} == {"m1", "m2"}
    assert tokens_offset(path) == [(path.stat().st_size, "committed")]
    with open(path, "rb") as handle:
        prefix = handle.read(importer.FIRST_LINE_MAX)
    [(generation,)] = query("SELECT generation FROM import_offsets WHERE source = ? AND consumer = 'tokens'", str(path))
    assert generation == importer.hashlib.sha256(prefix).hexdigest()
    world.add(path, usage(3, "m3", "s"))
    assert tokens.collect()["rows"] == 1
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 0 and out["covered"] == 1


def test_short_first_line_generation_unchanged(world):
    path = world.transcript("s", [usage(1, "m1", "s"), usage(2, "m2", "s")])
    tokens.collect()
    with open(path, "rb") as handle:
        first = handle.readline()
    [(generation,)] = query("SELECT generation FROM import_offsets WHERE source = ? AND consumer = 'tokens'", str(path))
    assert generation == importer.hashlib.sha256(first).hexdigest()
    assert tokens.collect()["rows"] == 0
    unterminated = world.transcript("u", [])
    unterminated.write_bytes(line(usage(1, "m9", "u"))[:-1])
    tokens.collect()
    assert tokens_offset(unterminated) == [] and world.count("token_usage", "message_id = 'm9'") == 0


def test_tokens_offset_advances_in_the_same_transaction_as_its_rows(world):
    offset = {"source": "/nowhere/s.jsonl", "generation": "g", "consumer": tokens.CONSUMER, "byte_offset": 100,
              "last_start": 50, "last_sha256": "0" * 64, "read_size": 100, "carry": None}
    bad = store.insert_rows_with_offset([("token_usage", _token_row("m1")),
                                         ("context_births", {"birth_id": "b", "kind": "bogus"})], offset)
    assert bad["failed"] == 2 and not bad["committed"]
    assert tokens_offset("/nowhere/s.jsonl") == [] and world.count("token_usage") == 0
    good = store.insert_rows_with_offset([("token_usage", _token_row("m1"))], offset)
    assert good["committed"]
    assert tokens_offset("/nowhere/s.jsonl") == [(100, "committed")] and world.count("token_usage") == 1


def test_tokens_offset_unchanged_after_deferred_batch(world, monkeypatch):
    monkeypatch.setattr(tokens, "BATCH_LINES", 1)
    path = world.transcript("s", [usage(1, "a", "s"), usage(2, "b", "s"), usage(3, "c", "s")])
    defer_call(monkeypatch, 2)
    assert tokens.collect()["deferred"] == 1
    assert tokens_offset(path) == [(len(line(usage(1, "a", "s"))), "deferred")]
    assert {r[0] for r in query("SELECT message_id FROM token_usage")} == {"a"}


def test_imported_coverage_fails_when_tokens_offset_short_of_last_line(world, capsys):
    path = world.transcript("s", [usage(1, "a", "s"), usage(2, "b", "s")])
    tokens.collect()
    assert check(capsys, "--imported-coverage")[0] == 0
    write_record("UPDATE import_offsets SET byte_offset = ? WHERE source = ? AND consumer = 'tokens'",
                 len(line(usage(1, "a", "s"))), str(path))
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 1 and out["uncovered"] == [{"transcript": str(path), "reason": "offset_short_of_last_line"}]


def test_transcript_appended_after_import_still_covered(world, capsys):
    path = world.transcript("s", [usage(1, "a", "s")])
    tokens.collect()
    world.add(path, usage(2, "b", "s"))
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 0
    assert out["gaps"]["grown_since_read"] == [{"transcript": str(path), "bytes": len(line(usage(2, "b", "s")))}]


def test_grown_transcript_whose_tail_batch_deferred_fails_coverage(world, monkeypatch, capsys):
    monkeypatch.setattr(tokens, "BATCH_LINES", 1)
    path = world.transcript("s", [usage(1, "a", "s"), usage(2, "b", "s")])
    defer_call(monkeypatch, 2)
    tokens.collect()
    world.add(path, usage(3, "c", "s"))
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 1 and out["uncovered"] == [{"transcript": str(path), "reason": "last_read_deferred"}]


def test_outcome_write_deferred_after_deferred_batch_fails_coverage(world, monkeypatch, capsys):
    path = world.transcript("s", [usage(1, "a", "s")])
    tokens.collect()
    world.add(path, usage(2, "b", "s"))
    defer_call(monkeypatch, None)
    real_attempt = store.record_read_attempt
    monkeypatch.setattr(store, "record_read_attempt", lambda source, generation, consumer, size, outcome, deadline_s=None:
                        outcome == "pending" and real_attempt(source, generation, consumer, size, outcome, deadline_s))
    assert tokens.collect()["deferred"] == 1
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 1 and out["uncovered"] == [{"transcript": str(path), "reason": "last_read_pending"}]


def test_imported_coverage_green_with_epochless_transcript(world, capsys):
    world.transcript("s", [usage(1, "m", "s")])
    world.run(include_transcripts=True)
    rc, out = check(capsys, "--imported-coverage")
    assert rc == 0 and out["gaps"]["no_open_epoch"] == 1


def test_task_conserved_when_its_transcript_grows_after_last_read(world, capsys):
    open_task("s", "task-grow", 0)
    path = world.transcript("s", [usage(1, "a", "s", inp=2)])
    tokens.collect()
    world.add(path, usage(2, "b", "s", inp=9))
    rc, out = check(capsys, "--task", "task-grow")
    assert rc == 0, out
    assert [g["transcript"] for g in out["gaps"]["grown_since_read"]] == [str(path)]
    assert out["actual"]["input_tokens"] == 2


# --- callers and the command line -----------------------------------------------------------

def test_sync_and_importer_call_the_collector(world, monkeypatch):
    monkeypatch.setattr(sync, "_canon_state", lambda: ("head", 0))
    world.live_event("s", "command", "cli:status", {"node_after": "EXECUTING"})
    world.transcript("s", [usage(1, "m1", "s")])
    sync.run("s")
    assert world.one("SELECT session_id FROM token_usage WHERE message_id = 'm1'") == "s"
    world.transcript("t", [usage(1, "m2", "t")])
    world.run(include_transcripts=True)
    assert world.count("token_usage", "message_id = 'm2'") == 1


def test_collector_runs_on_a_zero_row_sync_commit_after_an_early_drain(world, monkeypatch):
    monkeypatch.setattr(sync, "_canon_state", lambda: ("head", 0))
    world.live_event("s", "command", "cli:status", {"node_after": "EXECUTING"})
    world.replay("s")
    world.transcript("s", [usage(1, "m", "s")])
    result = sync.run("s")
    assert result["committed"] == ["s"]
    assert world.count("token_usage", "session_id = 's'") == 1


def test_import_all_collects_tokens_for_sessions_with_live_events(world):
    world.live_event("live-s", "command", "cli:status", {"node_after": "EXECUTING"})
    world.transcript("live-s", [usage(1, "m", "live-s")])
    world.run(replay_all_spools=True)
    assert world.count("token_usage", "session_id = 'live-s'") == 1


def test_check_tokens_exit_codes(world, capsys):
    open_task("s", "task-e", 0)
    world.transcript("s", [usage(1, "m1", "s", inp=5)])
    tokens.collect()
    assert check(capsys, "--task", "task-e")[0] == 0
    assert check(capsys, "--task", "no-such-task")[0] == 2
    assert check(capsys)[0] == 2
    assert check(capsys, "--task", "task-e", "--require-compaction")[0] == 1
    assert check(capsys, "--task", "task-e", "--require-spawn")[0] == 1
    judge_event("s", 2, "plan-review", "no-such-ledger-row")
    assert check(capsys, "--task", "task-e")[0] == 1
    write_record("DELETE FROM events WHERE kind = 'judge'")
    write_record("UPDATE token_usage SET input_tokens = 99")
    rc, out = check(capsys, "--task", "task-e")
    assert rc == 1 and not out["conserved"]
