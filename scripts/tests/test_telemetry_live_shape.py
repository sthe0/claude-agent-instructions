"""The record against the shapes live sources really have: times in every spelling a ledger writes
(and none at all), the two spellings of an act key, the session a report run is charged to, the
judge ledger's token usage, and an import that reads a frozen snapshot of the sources instead of
the growing live files. Every record and source here is the isolated one of the test."""
from __future__ import annotations

import builtins
import io
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentctl import advisor
from lib import config_root, host_llm, judge_ledger
from lib.telemetry import importer, live, recorder, report, snapshot, store, sync, tokens
from telemetry_import_support import at, line, world  # noqa: F401  (world is a fixture)
from telemetry_live_support import events, query, spool_events, start
from test_core_version_report import event as record_event, put, task, view
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
    assert payload["no_ts_rows"] == 2 and "heads" in payload


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
    assert last_call(world)["usage_reason"] == "usage_unavailable:claude" and not last_call(world).get("usage")
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
