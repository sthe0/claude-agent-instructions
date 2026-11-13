"""The record against the shapes live sources really have: times in every spelling a ledger writes
(and none at all), the two spellings of an act key, the session a report run is charged to, the
judge ledger's token usage, and an import that reads a frozen snapshot of the sources instead of
the growing live files. Every record and source here is the isolated one of the test."""
from __future__ import annotations

import builtins
import io
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentctl import advisor
from lib import config_root, host_llm, judge_ledger
from lib.telemetry import importer, live, recorder, report, snapshot, store, sync, tokens
from telemetry_import_support import at, line, world  # noqa: F401  (world is a fixture)
from telemetry_live_support import events, query, spool_events, start
from test_core_version_report import S, appeared, event as cv_event, put, task, trigger, view
from test_core_version_report import event as record_event
from test_telemetry_tokens import check as check_tokens, judge_event, ledger_call, open_task, usage, write_record

EPOCH_2026 = 1767225600


@pytest.fixture(autouse=True)
def no_fixture_causes(monkeypatch, tmp_path):
    from lib.telemetry import align
    monkeypatch.setattr(align, "FIXTURES", tmp_path / "no-fixtures")


def untimed(session, kind="command", source_key="cli:plan", provenance="imported") -> None:
    put("events", {"event_id": f"{session}-{kind}-{source_key}-untimed", "session_id": session, "kind": kind,
                   "source_key": source_key, "provenance": provenance, "time": "", "fields": "{}"})


def gate_times() -> list:
    return [r[0] for r in query("SELECT time FROM events WHERE kind = 'gate' ORDER BY time")]


# --- times: every live spelling, and none --------------------------------------------------------

def test_importer_reads_live_ts_shapes(world):
    assert importer.moment_ms(EPOCH_2026 * 1000 + 123) == EPOCH_2026 * 1000 + 123
    assert importer.moment_ms(str(EPOCH_2026 * 1000 + 5)) == EPOCH_2026 * 1000 + 5
    assert importer.moment_ms(EPOCH_2026 + 1.5) == EPOCH_2026 * 1000 + 1500
    assert importer.moment_ms(at(2)) == EPOCH_2026 * 1000 + 2000
    assert importer.moment_ms("") is None and importer.moment_ms(None) is None
    world.add(world.gate, {"ts": EPOCH_2026 * 1000 + 123, "session": "R", "gate": "plan", "passed": True},
              {"at": str(EPOCH_2026 + 1), "session": "R", "gate": "plan", "passed": True},
              {"timestamp": at(2), "session": "R", "gate": "plan", "passed": True})
    world.run()
    assert gate_times() == ["2026-01-01T00:00:00.123+00:00", "2026-01-01T00:00:01.000+00:00",
                            "2026-01-01T00:00:02.000+00:00"]


def test_import_fills_empty_time_of_stored_row_once_ts_is_readable(world, monkeypatch):
    world.add(world.gate, {"ts": EPOCH_2026 * 1000 + 7, "session": "R", "gate": "plan", "passed": True})
    with monkeypatch.context() as old_reader:
        old_reader.setattr(importer, "moment_ms", lambda value: None)
        assert world.run()["rows_added"] == 1
    assert gate_times() == [""]
    write_record("DELETE FROM import_offsets WHERE source = ?", str(world.gate))
    assert world.run()["rows_added"] == 0
    assert gate_times() == ["2026-01-01T00:00:00.007+00:00"]
    stored = json.loads(query("SELECT fields FROM events WHERE kind = 'gate'")[0][0])
    assert stored.get("time", "2026-01-01T00:00:00.007+00:00") == "2026-01-01T00:00:00.007+00:00"


def test_sync_run_tolerates_imported_rows_with_empty_time(world, monkeypatch):
    world.live_event("S", "command", "cli:plan")
    world.replay("S")
    untimed("S")
    monkeypatch.setattr(sync, "is_eligible", lambda stem, begun: True)
    result = sync.run()
    assert result[sync.NO_TIME_ROWS] >= 1


def test_latency_marks_skip_rows_with_empty_time(world):
    record_event("S", "command", 10, source_key="cli:plan")
    record_event("S", "command", 20, source_key="cli:plan")
    untimed("S")
    skipped = {}
    conn = store.open_record()
    try:
        end, first, commit = sync.latency_marks(conn, "S", skipped)
    finally:
        conn.close()
    assert end == datetime.fromisoformat(at(20).replace("Z", "+00:00")) + timedelta(seconds=sync.LIVE_TTL_S)
    assert first is None and commit is None
    assert skipped[sync.NO_TIME_ROWS] == 1


def test_check_latency_counts_empty_time_rows(world):
    record_event("S", "command", 10, source_key="cli:plan")
    untimed("S")
    untimed("S", source_key="cli:approve")
    conn = store.open_record()
    try:
        result = report.check_latency(conn, 600, at(0), datetime(2026, 1, 2, tzinfo=timezone.utc))
    finally:
        conn.close()
    assert result[sync.NO_TIME_ROWS] == 2


def test_report_shows_no_ts_rows_beside_shares():
    task("t1")
    untimed("s-t1")
    untimed("s-t1", source_key="cli:approve")
    payload = view()[1]
    assert payload[sync.NO_TIME_ROWS] == 2 and "heads" in payload


# --- the session a report run is charged to ------------------------------------------------------

def report_rows(session) -> list:
    return [r for r in spool_events(session) if r["kind"] == "report"]


def test_report_usage_follows_session_env(monkeypatch):
    task("t1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S-env")
    view("--task", "t1")
    assert [r["fields"]["session"] for r in report_rows("S-env")] == ["S-env"]
    assert report_rows("s-t1") == [] and report_rows(report.REPORT_SOURCE) == []


def test_report_usage_reaches_task_session_without_session_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    task("t1")
    view("--task", "t1")
    assert [r["fields"]["view"] for r in report_rows("s-t1")] == ["core-version"]
    assert report_rows(report.REPORT_SOURCE) == []
    assert [e["kind"] for e in events("s-t1", "report")] == ["report"], "the run's own row is replayed"


def test_report_usage_without_session_or_task_stays_under_report_source_and_is_counted(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    task("t1")
    view()
    view()
    rows = report_rows(report.REPORT_SOURCE)
    assert len(rows) == 2 and {r["fields"]["session"] for r in rows} == {report.REPORT_SOURCE}
    assert report_rows("s-t1") == []


# --- one act-key spelling ------------------------------------------------------------------------

def test_act_key_normalization_is_one_function_used_by_recorder_importer_report(world, monkeypatch):
    assert live.act_key("record_result") == "cli:record-result" == live.act_key("record-result")
    assert live.normalized_source_key("cli:record_result") == "cli:record-result"
    assert live.normalized_source_key("gate:effort_fire") == "gate:effort_fire"
    real = live.act_key
    monkeypatch.setattr(live, "act_key", lambda verb: real(verb) + "#one-function")

    start(world.tmp / "engine-root", "S1", "task-a")
    assert "cli:start#one-function" in {e["source_key"] for e in events("S1", "command")}, "recorder"

    world.state("H", [{"ts": at(1), "event": "start"}, {"ts": at(2), "event": "record_result"}])
    world.run()
    assert {e["source_key"] for e in events("H", "command")} == {"cli:start#one-function",
                                                                 "cli:record-result#one-function"}, "importer"

    assert "cli:record-result#one-function" in report.agentctl_command_keys(), "report"
    acts = report._engine_acts(["H"], frozenset({"cli:record-result#one-function"}))
    assert [a[1] for a in acts] == ["cli:record-result#one-function"], "report history"


def acts_task(world, history, live_keys) -> None:
    key = task("t1")
    world.state("s-t1", history, task="t1")
    for seconds, source_key in live_keys:
        record_event(f"s-{key[1]}", "command", seconds, {"duration_s": 1.0}, source_key)


def test_check_events_matches_hyphen_and_underscore_act_keys(world):
    acts_task(world, [{"event": "record_result", "ts": at(10)}, {"event": "record-result", "ts": at(20)}],
              [(10.5, "cli:record-result"), (20.5, "cli:record-result")])
    conn = store.open_record()
    try:
        result = report.check_events(conn, "t1", command_keys=frozenset({"cli:record-result"}))
    finally:
        conn.close()
    assert result["matched"] == 2 and result["missing"] == [] and result["ok"]


def test_stored_rows_of_either_spelling_read_without_rebuild(world):
    acts_task(world, [{"event": "record_result", "ts": at(10)}, {"event": "record-result", "ts": at(20)}],
              [(10.5, "cli:record_result"), (20.5, "cli:record-result")])
    before = {r[0] for r in query("SELECT source_key FROM events WHERE kind = 'command'")}
    conn = store.open_record()
    try:
        result = report.check_events(conn, "t1", command_keys=frozenset({"cli:record-result"}))
    finally:
        conn.close()
    assert result["matched"] == 2 and result["missing"] == []
    assert {r[0] for r in query("SELECT source_key FROM events WHERE kind = 'command'")} == before


# --- judge usage ---------------------------------------------------------------------------------

ENVELOPE = {"type": "result", "result": "VERDICT: pass", "total_cost_usd": 0.01,
            "usage": {"input_tokens": 11, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5,
                      "output_tokens": 7, "service_tier": "standard"}}


def run_judge(monkeypatch, argv, stdout):
    monkeypatch.delenv("AGENTCTL_LIVE_JUDGES", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda a, **kw: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=""))
    return advisor.subprocess_runner(argv, timeout=5)


def last_call(world) -> dict:
    lines = [json.loads(raw) for raw in world.judge.read_text(encoding="utf-8").splitlines()]
    return [obj for obj in lines if obj.get("kind") == "call"][-1]


def structured_argv() -> list:
    return host_llm.build_launch_argv(host_llm.HOST_CLAUDE, "sonnet", structured_output=True)


def test_judge_ledger_call_records_usage_from_response(world, monkeypatch):
    run_judge(monkeypatch, structured_argv(), json.dumps(ENVELOPE))
    call = last_call(world)
    assert call["usage"] == {"input_tokens": 11, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5,
                             "output_tokens": 7}
    assert call["usage_schema"] == judge_ledger.USAGE_SCHEMA and not call.get("usage_reason")
    tokens.collect()
    assert query("SELECT input_tokens, output_tokens, has_usage FROM judge_usage")[0][:] == (11, 7, 1)


def test_judge_callers_receive_unchanged_response_text(world, monkeypatch):
    assert run_judge(monkeypatch, structured_argv(), json.dumps(ENVELOPE)).stdout == "VERDICT: pass"
    plain = run_judge(monkeypatch, ["claude", "-p", "--model", "sonnet"], "VERDICT: pass\n")
    assert plain.stdout == "VERDICT: pass\n"
    assert last_call(world)["usage_reason"] == host_llm.USAGE_NOT_REQUESTED and not last_call(world).get("usage")
    garbled = run_judge(monkeypatch, structured_argv(), "not json")
    assert garbled.stdout == "not json" and last_call(world)["usage_reason"] == "unparsed"


def judge_line(world, seconds, **extra) -> None:
    world.add(world.judge, {"kind": "call", "judge": "j", "source": "s", "ts": at(seconds), "timed_out": False, **extra})


def test_check_tokens_lists_judge_rows_without_usage_separately(world, capsys):
    open_task("s", "task-j", 0)
    judge_event("s", 2, "j", ledger_call(world, "s", 2, "j"))
    judge_line(world, 3, usage_schema=1, usage_reason="timed_out")
    world.transcript("s", [usage(4, "m1", "s")])
    tokens.collect()
    rc, out = check_tokens(capsys, "--imported-coverage")
    assert rc == 0, out
    assert out["judge"]["without_usage_before_schema"] == 1
    assert out["judge"]["without_usage_by_reason"] == {"timed_out": 1} and out["judge"]["failures"] == []
    rc, out = check_tokens(capsys, "--task", "task-j")
    assert rc == 0 and out["gaps"]["judge_rows_without_usage"] == 2


def test_check_tokens_fails_post_change_judge_line_without_usage(world, capsys):
    judge_line(world, 1, usage_schema=1, usage=None, usage_reason=None)
    tokens.collect()
    rc, out = check_tokens(capsys, "--imported-coverage")
    assert rc == 1 and [f["reason"] for f in out["judge"]["failures"]] == ["judge_line_without_usage"]


def test_check_tokens_fails_judge_call_without_ledger_line(world, capsys):
    open_task("s", "task-j", 0)
    missing = judge_event("s", 5, "j", "no-such-ledger-row")
    world.transcript("s", [usage(6, "m1", "s")])
    tokens.collect()
    rc, out = check_tokens(capsys, "--task", "task-j")
    assert rc == 1 and [g["event_id"] for g in out["gaps"]["missing_judge_call"]] == [missing]


def test_check_tokens_fails_judge_line_with_usage_but_no_row(world, capsys):
    judge_line(world, 1, usage_schema=1, usage={"input_tokens": 4, "output_tokens": 1})
    tokens.collect()
    assert check_tokens(capsys, "--imported-coverage")[0] == 0
    write_record("DELETE FROM judge_usage")
    rc, out = check_tokens(capsys, "--imported-coverage")
    assert rc == 1 and [f["reason"] for f in out["judge"]["failures"]] == ["judge_line_without_row"]


# --- import from a frozen snapshot ---------------------------------------------------------------

def sources(world) -> dict:
    gate = world.add(world.gate, world.gate_line("R", seconds=1), world.gate_line("R", "exec", seconds=2))
    world.add(world.judge, {"ts": at(3), "kind": "call", "source": "R", "judge": "j", "duration": 1.0})
    state = world.state("R", [{"ts": at(1), "event": "start"}, {"ts": at(4), "event": "plan"}])
    transcript = world.transcript("T", [usage(5, "m1", "T"), usage(6, "m2", "T")])
    return {"gate": gate, "state": state, "transcript": transcript}


def take(world) -> Path:
    out = world.tmp / "snap"
    snapshot.take(out)
    return out


def manifest(out: Path) -> list:
    return json.loads((out / snapshot.MANIFEST).read_text(encoding="utf-8"))["files"]


def test_snapshot_cuts_append_only_sources_at_their_size(world, monkeypatch):
    sources(world)
    before = world.gate.read_bytes()
    real = snapshot.shutil.disk_usage

    def grow_then_measure(path):
        world.add(world.gate, world.gate_line("R", "late", seconds=9))
        return real(path)
    monkeypatch.setattr(snapshot.shutil, "disk_usage", grow_then_measure)
    out = take(world)
    entry = next(f for f in manifest(out) if f["live"] == str(world.gate))
    assert entry["size"] == len(before) and (out / entry["copy"]).read_bytes() == before
    assert world.gate.read_bytes() != before


def test_snapshot_manifest_maps_each_file_to_its_live_path(world):
    made = sources(world)
    out = take(world)
    files = {f["live"]: f for f in manifest(out)}
    for kind, path in (("ledger", world.gate), ("ledger", world.judge), ("state", made["state"]),
                       ("transcript", made["transcript"])):
        assert files[str(path)]["kind"] == kind
        assert (out / files[str(path)]["copy"]).read_bytes() == Path(path).read_bytes()
    assert all(not Path(f["copy"]).is_absolute() for f in files.values())


def test_import_from_snapshot_reads_no_live_source(world, monkeypatch):
    sources(world)
    out = take(world)
    live_paths = {f["live"] for f in manifest(out)}
    opened = []
    real_open, real_io_open = builtins.open, io.open

    def guard(real):
        def wrapper(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)
        return wrapper
    monkeypatch.setattr(builtins, "open", guard(real_open))
    monkeypatch.setattr(io, "open", guard(real_io_open))
    report_ = world.run(include_transcripts=True, sources=out)
    monkeypatch.undo()
    assert report_["rows_added"] > 0
    assert sorted(set(opened) & live_paths) == []
    assert any(p.startswith(str(out)) for p in opened)


def test_second_import_of_one_snapshot_adds_no_row_after_sources_grow(world):
    made = sources(world)
    out = take(world)
    assert world.run(include_transcripts=True, sources=out)["rows_added"] > 0
    world.add(world.gate, world.gate_line("R", "late", seconds=9))
    world.add(made["transcript"], usage(10, "m3", "T"))
    assert world.run(include_transcripts=True, sources=out)["rows_added"] == 0


def test_import_run_event_not_in_second_run(world):
    sources(world)
    out = take(world)
    first = world.run(include_transcripts=True, sources=out)
    second = world.run(include_transcripts=True, sources=out)
    assert first["rows_added"] > 0 and second["rows_added"] == 0
    runs = [r[0] for r in query("SELECT json_extract(fields, '$.rows_added') FROM events WHERE kind = 'import.run' "
                                "ORDER BY time, event_id")]
    assert runs == [first["rows_added"], 0]


def test_snapshot_import_after_live_import_of_same_bytes_adds_no_row(world):
    sources(world)
    assert world.run(include_transcripts=True)["rows_added"] > 0
    out = take(world)
    assert world.run(include_transcripts=True, sources=out)["rows_added"] == 0


def test_snapshot_rows_carry_live_path_backreferences(world):
    sources(world)
    out = take(world)
    world.run(include_transcripts=True, sources=out)
    paths = {r[0] for r in query("SELECT json_extract(fields, '$.backref.path') FROM events "
                                 "WHERE provenance = 'imported' AND json_extract(fields, '$.backref.path') IS NOT NULL")}
    assert paths and paths <= {str(world.gate), str(world.judge), str(world.decision)}
    assert not [p for p in paths if p.startswith(str(out))]


def counter_values(obj, name: str) -> list:
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == name] + [x for v in obj.values() for x in counter_values(v, name)]
    if isinstance(obj, list):
        return [x for v in obj for x in counter_values(v, name)]
    return []


def test_snapshot_cut_shorter_than_offset_is_not_truncation(world):
    sources(world)
    out = take(world)
    world.add(world.gate, world.gate_line("R", "late", seconds=9))
    assert world.run(include_transcripts=True)["rows_added"] > 0
    result = world.run(include_transcripts=True, sources=out)
    assert result["rows_added"] == 0
    assert sum(counter_values(result, "truncated_bytes")) == 0 and sum(counter_values(result, "rereads")) == 0


def test_import_sources_reads_no_live_spool(world, monkeypatch):
    sources(world)
    out = take(world)
    touched = []
    monkeypatch.setattr(importer, "_replay_ended_spools", lambda *a, **k: touched.append("replay"))
    monkeypatch.setattr(live, "spool_or_end_marker_exists", lambda session: touched.append(session) or False)
    world.run(include_transcripts=True, sources=out)
    assert touched == []
    world.run(include_transcripts=True)
    assert "replay" in touched


def test_snapshot_take_fails_on_oserror(world, monkeypatch, tmp_path):
    import telemetry as telemetry_cli
    sources(world)
    real = snapshot._copy_prefix

    def fail_gate(source, target, size):
        if Path(source) == world.gate:
            raise OSError("unreadable")
        return real(source, target, size)
    monkeypatch.setattr(snapshot, "_copy_prefix", fail_gate)
    result = snapshot.take(tmp_path / "snap-a")
    assert [s["live"] for s in result["skipped"]] == [str(world.gate)]
    assert telemetry_cli.main(["snapshot-sources", "--out", str(tmp_path / "snap-b")]) == 1


# --- judge losses named per host id ------------------------------------------------------------

def test_judge_losses_named_per_host_id(world, monkeypatch):
    run_judge(monkeypatch, ["agent", "-p", "--model", "m"], "VERDICT: pass")
    assert last_call(world)["usage_reason"] == f"usage_unavailable:{host_llm.HOST_CURSOR}"
    run_judge(monkeypatch, ["claude", "-p"], "VERDICT: pass")
    assert last_call(world)["usage_reason"] == host_llm.USAGE_NOT_REQUESTED
    lines = world.judge.read_text(encoding="utf-8")
    result = run_judge(monkeypatch, ["git", "-C", "/x", "rev-parse", "HEAD"], "abc\n")
    assert result.stdout == "abc\n" and world.judge.read_text(encoding="utf-8") == lines


def test_claude_no_usage_and_unparsed_lines_counted(world, capsys):
    for reason in ("no_usage", "unparsed", "no_usage", host_llm.USAGE_NOT_REQUESTED, "usage_unavailable:cursor"):
        judge_line(world, 1, usage_schema=1, usage_reason=reason)
    tokens.collect()
    rc, out = check_tokens(capsys, "--imported-coverage")
    assert out["judge"]["without_usage_by_reason"] == {"no_usage": 2, "unparsed": 1, host_llm.USAGE_NOT_REQUESTED: 1,
                                                       "usage_unavailable:cursor": 1}
    assert rc == 1 and [f["reason"] for f in out["judge"]["failures"]] == ["judge_line_usage_not_requested"]


def test_grounding_judge_usage_recorded_with_structured_output(world, monkeypatch):
    from agentctl import grounding_judge
    run_judge(monkeypatch, structured_argv(), json.dumps(ENVELOPE))
    assert grounding_judge._ask("grounding", "prompt", advisor.subprocess_runner, lambda text: text) == "VERDICT: pass"
    call = last_call(world)
    assert call["judge"] == "grounding" and call["usage"]["output_tokens"] == 7 and not call.get("usage_reason")


def test_real_judge_writes_ledger_line(world, monkeypatch):
    from mandate_cycle import driver
    monkeypatch.setattr(driver.subprocess, "run",
                        lambda a, **kw: subprocess.CompletedProcess(a, 0, stdout=json.dumps(ENVELOPE), stderr=""))
    assert driver.real_judge("prompt") == ("VERDICT: pass", 0.01)
    call = last_call(world)
    assert call["judge"] == driver.MANDATE_JUDGE and call["usage"]["input_tokens"] == 11


def test_judge_usage_report_reads_new_format_lines(world, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("judge_usage_report", Path(advisor.__file__).parents[1]
                                                  / "judge-usage-report.py")
    usage_report = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, usage_report)
    spec.loader.exec_module(usage_report)
    judge_ledger.begin_attributed_call("j")
    run_judge(monkeypatch, structured_argv(), json.dumps(ENVELOPE))
    judge_ledger.begin_attributed_call("j")
    run_judge(monkeypatch, ["claude", "-p"], "VERDICT: pass")
    read = judge_ledger.read_ledger()
    result = usage_report.tally(read, world.judge)
    assert read.dropped_lines == 0 and result.record_count == len(read.records) >= 4
    calls = [r for r in read.records if r.get("kind") == "call"]
    assert [c.get("usage_schema") for c in calls] == [judge_ledger.USAGE_SCHEMA] * 2
    assert result.complaints == [] and result.record_count == len(read.records)


# --- check-events: a session with no live event ------------------------------------------------

def test_check_events_fails_session_with_zero_live_events(world):
    acts_task(world, [{"event": "record_result", "ts": at(10)}], [])
    conn = store.open_record()
    try:
        conn.execute("UPDATE events SET provenance = 'imported' WHERE session_id = 's-t1'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM events WHERE session_id = 's-t1'").fetchone()[0] > 0
        result = report.check_events(conn, "t1", command_keys=frozenset({"cli:record-result"}))
    finally:
        conn.close()
    assert not result["ok"] and [a["act"] for a in result["missing"]] == ["cli:record-result"]
    assert result["lost_before_first_live"] == 0


@pytest.mark.parametrize("window_from, named", [(20, True), (5, False)])
def test_check_events_names_zero_live_session_acts_before_the_emitter(world, window_from, named):
    acts_task(world, [{"event": "record_result", "ts": at(10)}, {"event": "record_result", "ts": at(40)}], [])
    conn = store.open_record()
    try:
        conn.execute("UPDATE events SET provenance = 'imported' WHERE session_id = 's-t1'")
        conn.commit()
        result = report.check_events(conn, "t1", {"from": at(window_from), "to": at(30)},
                                     command_keys=frozenset({"cli:record-result"}))
    finally:
        conn.close()
    assert [a["time_ms"] for a in result["missing"]] == [importer.moment_ms(at(40))]
    assert [a["time_ms"] for a in result["lost_before_emitter"]] == ([importer.moment_ms(at(10))] if named else [])
    assert [a["time_ms"] for a in result["excused"]] == ([] if named else [importer.moment_ms(at(10))])


# --- check-events: a named known loss -----------------------------------------------------------

LOST_AT = 40


def known_loss(seconds=LOST_AT, **override) -> dict:
    return {"session": "s-t1", "act": "cli:record-result", "ts": at(seconds), "cause": "killed after the save",
            **override}


def check_with_known_losses(world, tmp_path, capsys, entries, *bound, extra_acts=()):
    """check-events over a task whose act at LOST_AT (and each of `extra_acts`) has no live row,
    with `entries` as the known-loss file; returns the exit code and the printed result."""
    import telemetry as telemetry_cli
    history = [{"event": "record_result", "ts": at(s)} for s in (10, LOST_AT, *extra_acts)]
    acts_task(world, history, [(10.5, "cli:record-result")])
    path = tmp_path / "known-losses.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    capsys.readouterr()
    rc = telemetry_cli.main(["check-events", "--task", "t1", "--known-losses", str(path), *bound])
    out = capsys.readouterr().out
    return rc, json.loads(out) if rc in (0, 1) else None


@pytest.mark.parametrize("bound_kind", ["until", "landing"])
def test_check_events_accepts_named_known_loss(world, tmp_path, capsys, bound_kind):
    landing = tmp_path / "landing.json"
    landing.write_text(json.dumps({"landed_at": at(100)}), encoding="utf-8")
    bound = (["--known-losses-until", at(100)] if bound_kind == "until"
             else ["--known-losses-until-landing", str(landing)])
    rc, result = check_with_known_losses(world, tmp_path, capsys, [known_loss(LOST_AT + 1.5)], *bound)
    assert rc == 0, result
    assert result["missing"] == []
    assert [(a["time_ms"], a["loss"]) for a in result["known_losses"]] == [
        (importer.moment_ms(at(LOST_AT)), "known_loss:killed after the save")]


@pytest.mark.parametrize("entry", [
    {k: v for k, v in known_loss().items() if k != "cause"}, known_loss(cause=""), known_loss(cause="  "),
    known_loss(session="s-other"), known_loss(act="cli:replan"), known_loss(LOST_AT + 5)],
    ids=["no-cause", "empty-cause", "blank-cause", "other-session", "other-act", "ts-outside-slack"])
def test_known_loss_requires_cause_and_matching_history_act(world, tmp_path, capsys, entry):
    rc, _ = check_with_known_losses(world, tmp_path, capsys, [entry], "--known-losses-until", at(100))
    assert rc == 2


@pytest.mark.parametrize("bound", [LOST_AT, LOST_AT - 10])
def test_known_loss_refused_at_or_after_bound(world, tmp_path, capsys, bound):
    rc, _ = check_with_known_losses(world, tmp_path, capsys, [known_loss()], "--known-losses-until", at(bound))
    assert rc == 2


def test_known_loss_entry_excuses_one_act(world, tmp_path, capsys):
    rc, result = check_with_known_losses(world, tmp_path, capsys, [known_loss()], "--known-losses-until", at(100),
                                         extra_acts=(LOST_AT + 0.5,))
    assert rc == 1
    assert len(result["known_losses"]) == 1 and len(result["missing"]) == 1


@pytest.mark.parametrize("missing", ["known-losses", "landing"])
def test_known_losses_missing_file_refused(world, tmp_path, missing):
    import telemetry as telemetry_cli
    acts_task(world, [{"event": "record_result", "ts": at(10)}], [(10.5, "cli:record-result")])
    known = tmp_path / "known-losses.json"
    landing = tmp_path / "landing.json"
    if missing == "landing":
        known.write_text(json.dumps({"entries": [known_loss(10)]}), encoding="utf-8")
    else:
        landing.write_text(json.dumps({"landed_at": at(100)}), encoding="utf-8")
    argv = ["check-events", "--task", "t1", "--known-losses", str(known), "--known-losses-until-landing", str(landing)]
    assert telemetry_cli.main(argv) == 2


@pytest.mark.parametrize("given", ["losses-only", "bound-only"])
def test_known_losses_without_bound_refused(world, tmp_path, given):
    import telemetry as telemetry_cli
    acts_task(world, [{"event": "record_result", "ts": at(10)}], [(10.5, "cli:record-result")])
    known = tmp_path / "known-losses.json"
    known.write_text(json.dumps({"entries": [known_loss(10)]}), encoding="utf-8")
    argv = (["--known-losses", str(known)] if given == "losses-only" else ["--known-losses-until", at(100)])
    assert telemetry_cli.main(["check-events", "--task", "t1", *argv]) == 2


# --- check-latency: named losses, each proven from the record -----------------------------------

def late_session(session, seconds, provenance="live", earliest=None) -> None:
    if earliest is not None:
        cv_event(session, "command", earliest, {"command": "plan"}, "cli:plan", provenance=provenance)
    cv_event(session, "command", seconds, {"command": "status"}, "cli:status", provenance=provenance)
    trigger(seconds + 10, "session_end", session)
    appeared(seconds + 10 + S + 100, session)


def on_time_session(session, seconds, provenance="live") -> None:
    cv_event(session, "command", seconds, {"command": "status"}, "cli:status", provenance=provenance)
    trigger(seconds + 10, "session_end", session)
    appeared(seconds + 20, session)


def moment(seconds):
    return sync.parse_time(at(seconds))


def lat(landed_s=-10_000, outage=None, entries=None, now_s=100_000) -> dict:
    conn = store.open_readonly()
    try:
        return report.check_latency(conn, S, at(landed_s), moment(now_s),
                                    (moment(outage[0]), moment(outage[1])) if outage else None, entries)
    finally:
        conn.close()


def refused(**kwargs) -> bool:
    try:
        lat(**kwargs)
    except report.LatencyInputError:
        return True
    return False


def statuses(result) -> dict:
    return {r["session"]: r["status"] for r in result["sessions"]}


def spool_of(session) -> None:
    from lib.telemetry import emit
    path = emit.spool_path(emit.session_file_stem(session))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


CAUSE = "pre_fix_cause_x"


def test_sync_outage_window_edges(world):
    for session, seconds in (("s-before", -500), ("s-from", 0), ("s-in", 1000), ("s-until", 2000)):
        late_session(session, seconds)
    result = lat(outage=(10, 2010))
    assert statuses(result) == {"s-before": "late", "s-from": report.SYNC_OUTAGE, "s-in": report.SYNC_OUTAGE,
                                "s-until": "late"}
    assert result["losses"] == {report.SYNC_OUTAGE: 2} and result["late"] == ["s-before", "s-until"]


def landing_event(seconds, head="abc") -> None:
    cv_event("telemetry:landing", "landing", seconds, {"tip": head, "pushed_head": head}, report.LANDED_SOURCE_KEY)


def test_sync_outage_missing_landing_refused(world, tmp_path):
    import telemetry as telemetry_cli
    on_time_session("s-ok", 0)
    landing = tmp_path / "landing.json"
    landing.write_text(json.dumps({"head": "abc", "landed_at": at(500)}), encoding="utf-8")
    argv = ["check-latency", "--max-seconds", str(S), "--since-landing", str(landing),
            "--sync-outage-from", at(100), "--sync-outage-until-landing", str(landing)]
    assert telemetry_cli.main(argv) == 2
    landing_event(450)
    assert telemetry_cli.main(argv) != 2


def test_sync_outage_landed_at_bound_to_landing_event(world):
    landing_event(1000)

    def end(landed_s, head="abc"):
        conn = store.open_readonly()
        try:
            return report.outage_end_from_landing(conn, {"head": head, "landed_at": at(landed_s)})
        finally:
            conn.close()
    for landed_s, head in ((990, "abc"), (1000 + report.LANDED_AT_MAX_LAG_S + 1, "abc"), (1100, "zzz")):
        with pytest.raises(report.LatencyInputError):
            end(landed_s, head)
    assert end(1300) == moment(1300)
    appeared(1300, "s-x")
    appeared(1200, "s-y")
    assert end(1300) == moment(1300)
    with pytest.raises(report.LatencyInputError):
        end(990)


def test_pre_fix_late_refuses_entry_outside_pre_outage_span(world):
    late_session("s-real", 5000)
    late_session("s-imp", 5000, provenance="imported")
    for session in ("s-real", "s-imp"):
        assert refused(outage=(3000, 4000), entries=[{"session": session, "cause": CAUSE}]), session
    result = lat(outage=(6000, 7000), entries=[{"session": "s-real", "cause": CAUSE},
                                               {"session": "s-imp", "cause": CAUSE}])
    assert statuses(result) == {"s-real": f"{report.PRE_FIX_LATE}:{CAUSE}", "s-imp": f"{report.IMPORT_ONLY}:{CAUSE}"}


def test_pre_fix_late_refuses_session_not_late(world):
    on_time_session("s-real", 0)
    on_time_session("s-imp", 0, provenance="imported")
    for session in ("s-real", "s-imp"):
        assert refused(entries=[{"session": session, "cause": CAUSE}]), session


def test_pre_fix_late_refuses_bad_cause(world):
    late_session("s1", 0)
    for entry in ({"session": "s1", "cause": "deferred"}, {"session": "s1"}, {"cause": CAUSE}, "s1"):
        assert refused(entries=[entry]), entry
    result = lat(entries=[{"session": "s1", "cause": CAUSE}])
    assert result["ok"] and result["losses"] == {f"{report.PRE_FIX_LATE}:{CAUSE}": 1}


def test_pre_fix_late_refuses_non_real_session_with_live_row(world):
    late_session("import:x", 0, provenance="imported")
    cv_event("import:x", "command", 1, {"command": "plan"}, "cli:plan")
    late_session("s-spooled", 0, provenance="imported")
    spool_of("s-spooled")
    for session in ("import:x", "s-spooled"):
        assert refused(entries=[{"session": session, "cause": CAUSE}]), session


def test_latency_exclusion_not_by_name_prefix(world):
    for session in ("test-1", "canary-1", "fake-1"):
        late_session(session, 0)
    result = lat()
    assert result["late"] == ["canary-1", "fake-1", "test-1"]
    assert [e["session"] for e in result["excluded"]] == [sync.SYNC_SESSION]


def test_latency_exclusion_requires_proof_from_rows(world):
    task("fake", synthetic=1, seconds=0)
    cv_event("s-own", "import.run", 0, {}, importer.SELF_SOURCE_KEY)
    late_session("s-work", 0)
    result = lat()
    assert {e["session"]: e["proof"] for e in result["excluded"]} == {
        "s-fake": report.SYNTHETIC_TASK, "s-own": report.ONLY_RECORD_OWN_ROWS,
        sync.SYNC_SESSION: report.ONLY_RECORD_OWN_ROWS}
    assert result["late"] == ["s-work"] and result["excluded_count"] == 3


def test_import_only_loss_refuses_session_with_live_row(world):
    late_session("s1", 5000, provenance="imported", earliest=-500)
    cv_event("s1", "command", 5001, {"command": "plan"}, "cli:plan")
    assert lat(landed_s=0)["late"] == ["s1"]


def test_import_only_loss_refuses_session_with_spool(world):
    late_session("s1", 5000, provenance="imported", earliest=-500)
    spool_of("s1")
    assert lat(landed_s=0)["late"] == ["s1"]


def test_import_only_pre_emitter_bound_by_pr1_landing(world):
    late_session("s-pre", 5000, provenance="imported", earliest=-500)
    late_session("s-post", 5000, provenance="imported", earliest=100)
    late_session("s-blank", 5000, provenance="imported")
    untimed("s-blank")
    untimed("s-untimed")
    result = lat(landed_s=0)
    assert statuses(result) == {"s-pre": f"{report.IMPORT_ONLY}:{report.PRE_EMITTER}", "s-post": "late",
                                "s-blank": "late"}
    assert result["losses"] == {f"{report.IMPORT_ONLY}:{report.PRE_EMITTER}": 1}


def test_import_only_after_pr1_without_entry_judged_late(world):
    late_session("s1", 5000, provenance="imported", earliest=100)
    result = lat(landed_s=0)
    assert not result["ok"] and result["late"] == ["s1"] and result["losses"] == {}


def test_no_time_rows_counted_once_under_one_name(world):
    for session in ("s1", "s2"):
        cv_event(session, "command", 0, {"command": "status"}, "cli:status")
    trigger(sync.LIVE_TTL_S + 10)
    untimed("s1")
    untimed(sync.SYNC_SESSION, kind=sync.TRIGGER_START, source_key=sync.SOURCE_KEY)
    result = lat()
    assert {r["session"] for r in result["sessions"]} == {"s1", "s2"}
    assert result[sync.NO_TIME_ROWS] == 2 and "no_ts_rows" not in result


def test_source_with_every_line_without_time_is_counted(world):
    world.add(world.gate, {**world.gate_line("R"), "ts": ""}, {**world.gate_line("R", "exec"), "ts": None})
    world.run()
    assert query("SELECT COUNT(*) FROM events WHERE session_id = 'R' AND time = ''")[0][0] == 2
    late_session("s1", 0)
    assert lat()[sync.NO_TIME_ROWS] == 2


# --- class A: an isolation test's subprocess writes no live engine state ------------------------

def test_isolation_tests_write_no_live_engine_state(monkeypatch, tmp_path):
    import uuid
    from test_decision_log_isolation import CLI, isolated_home_env
    live_root = tmp_path / "live-root"
    live_root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(live_root))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(live_root))
    nonce = f"canary-{uuid.uuid4().hex}"
    done = subprocess.run([sys.executable, str(CLI), "start", "--session", nonce, "--task", "t", "--goal", "g",
                           "--done-criterion", "d", "--criterion-type", "measurable"],
                          env=isolated_home_env(tmp_path / "home"), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    leaked = [p for p in live_root.rglob("*") if p.is_file() and nonce in p.read_text(errors="replace") + p.name]
    assert leaked == []
    assert [p for p in (tmp_path / "home").rglob("*") if nonce in p.name]


# --- the command row of a verb interrupted after its state save ---------------------------------

INTERRUPTED_SESSION = "s-int"


def _open_writer_when_reader_waits(fifo: Path, proc) -> int:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"the CLI never reached the sync point (rc={proc.poll()})")


def interrupt_start_at(tmp_path, sync_env: str):
    """Run `start` in a subprocess, deliver SIGTERM while it waits at the sync point `sync_env`,
    and release the point only after the process ended, so the signal is the only way past it."""
    from test_decision_log_isolation import CLI, isolated_home_env
    fifo = tmp_path / "sync.fifo"
    os.mkfifo(fifo)
    env = {**isolated_home_env(tmp_path / "home"), sync_env: str(fifo)}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    proc = subprocess.Popen(
        [sys.executable, str(CLI), "--state-root", str(tmp_path / "state"), "start",
         "--session", INTERRUPTED_SESSION, "--task", "t", "--goal", "g", "--done-criterion", "d",
         "--criterion-type", "measurable"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    writer = _open_writer_when_reader_waits(fifo, proc)
    try:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        os.close(writer)
    return proc.returncode, out, err


def command_rows(session: str) -> list:
    store.open_record().close()
    spooled = [r["fields"] for r in spool_events(session) if r["kind"] == "command"]
    return spooled + [r["fields"] for r in events(session, kind="command")]


def test_command_row_written_when_interrupted_after_state_save(tmp_path):
    rc, _, err = interrupt_start_at(tmp_path, recorder.SYNC_AFTER_SAVE_ENV)
    rows = command_rows(INTERRUPTED_SESSION)
    assert rc == 143, err
    assert (tmp_path / "state" / f"{INTERRUPTED_SESSION}.json").exists()
    assert [(r["command"], r["outcome"], r["exit"]) for r in rows] == [("start", "interrupted", 143)], rows


def test_interrupt_inside_finish_after_row_writes_no_second_row(tmp_path):
    rc, _, err = interrupt_start_at(tmp_path, recorder.SYNC_AFTER_ROW_ENV)
    rows = command_rows(INTERRUPTED_SESSION)
    assert rc == 143, err
    assert [(r["command"], r["outcome"]) for r in rows] == [("start", "ok")], rows


def test_interrupt_not_swallowed_as_error_directive(tmp_path):
    rc, out, err = interrupt_start_at(tmp_path, recorder.SYNC_AFTER_SAVE_ENV)
    assert rc not in (0, 1), err
    assert '"error"' not in out
    assert [r["outcome"] for r in command_rows(INTERRUPTED_SESSION)] == ["interrupted"]


def test_interrupt_before_state_save_writes_no_row(tmp_path):
    rc, _, err = interrupt_start_at(tmp_path, recorder.SYNC_BEFORE_SAVE_ENV)
    assert rc == 143, err
    assert not (tmp_path / "state" / f"{INTERRUPTED_SESSION}.json").exists()
    assert command_rows(INTERRUPTED_SESSION) == []


# --- class C: a deferred sync keeps retrying within the bound -----------------------------------

def test_deferred_sync_retried_within_latency_bound(world, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sync, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    real_replay = store.replay

    def locked_for_300_s(*args, **kwargs):
        if clock[0] < 300:
            return {"inserted": 0, "deferred": 1, "drained": False, "failed": 0}
        return real_replay(*args, **kwargs)
    monkeypatch.setattr(store, "replay", locked_for_300_s)
    session = "s-locked"
    world.live_event(session, "command", "cli:status")
    result = sync.run(session)
    assert session in result["committed"], result
