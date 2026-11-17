"""The shared live store against rows a writer should not have put there: judge-ledger lines whose
Claude call never asked for usage, a test run's rows, another layer's hook rows, the pinned manual
scale measurement, and the fix bound (`outage_end_from_landing`) that separates a named loss of the
pre-fix writer from a failure of the fixed one, read through the writer marker every production
writer stamps."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from agentctl import advisor, procedure
from lib import config_root, host_llm, judge_ledger
from lib.telemetry import emit, live, report, store, sync, tokens
from telemetry_import_support import BASE, at, world  # noqa: F401  (world is a fixture)
from telemetry_live_support import query, spool_events
from test_core_version_report import event, task, view
from test_telemetry_live_shape import ENVELOPE, judge_line, last_call
from test_telemetry_tokens import check as check_tokens

LANDED = 100
HEAD = "hL"
NOT_REQUESTED = host_llm.USAGE_NOT_REQUESTED


def landing(tmp_path, landed_at=None, seconds=LANDED, name="landing.json") -> str:
    """A land:landed event of HEAD at `seconds` and a landing record claiming `landed_at`."""
    event("s-land", "landing", seconds, {"tip": HEAD}, "land:landed")
    path = tmp_path / name
    path.write_text(json.dumps({"head": HEAD, "landed_at": landed_at or at(seconds)}), encoding="utf-8")
    return str(path)


def landing_file(tmp_path, landed_at, name="claim.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps({"head": HEAD, "landed_at": landed_at}), encoding="utf-8")
    return str(path)


def not_requested(world, seconds, invocation_id, marked=False) -> None:
    extra = {judge_ledger.WRITER_MARKER: 1} if marked else {}
    judge_line(world, seconds, usage_schema=1, usage_reason=NOT_REQUESTED, invocation_id=invocation_id, **extra)


def coverage(capsys, bound_file, *argv):
    tokens.collect()
    return check_tokens(capsys, "--imported-coverage", "--not-requested-until-landing", bound_file, "--json", *argv)


def row(source_key, seconds, marked=False, **fields) -> str:
    if marked:
        fields[judge_ledger.WRITER_MARKER] = 1
    return event(f"s-{source_key}", "decision", seconds, fields, source_key)


def check_version(bound_file=None):
    argv = ["--check", "--json"]
    if bound_file:
        argv += ["--test-leak-until-landing", bound_file]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = report.main(argv)
    return status, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


def classes(payload) -> list:
    return sorted((f["class"], f.get("source_key") or f.get("invocation_id")) for f in payload["failures"])


def under(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents


# --- (a) judge-ledger lines that never asked for usage -------------------------------------------

def test_traced_writer_of_not_requested_lines_writes_none_now(world, monkeypatch):
    monkeypatch.delenv("AGENTCTL_LIVE_JUDGES", raising=False)
    monkeypatch.setattr(subprocess, "run",
                        lambda a, **kw: subprocess.CompletedProcess(a, 0, stdout=json.dumps(ENVELOPE), stderr=""))
    procedure.judge_collapse("method text", "procedure text", advisor.subprocess_runner)
    call = last_call(world)
    assert call["judge"] == "procedure_collapse"
    assert isinstance(call.get("usage"), dict) and call.get("usage_reason") != NOT_REQUESTED


def test_test_run_writes_no_live_judge_line(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_JUDGE_LEDGER", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    target = judge_ledger.ledger_path()
    assert under(target, tmp_path), "the guard is tested against a ledger under tmp, never the live one"
    assert judge_ledger._write("call", judge="j") is None
    assert not target.exists()


def test_not_requested_line_before_fix_bound_is_listed_not_failed(world, tmp_path, capsys):
    bound = landing(tmp_path)
    not_requested(world, LANDED - 50, "inv-before")
    rc, out = coverage(capsys, bound)
    assert rc == 0 and out["failures"] == []
    assert [w["invocation_id"] for w in out["excused"][tokens.NOT_REQUESTED_BEFORE_FIX]] == ["inv-before"]


def test_not_requested_line_at_or_after_fix_bound_fails(world, tmp_path, capsys):
    bound = landing(tmp_path)
    not_requested(world, LANDED, "inv-at", marked=True)
    not_requested(world, LANDED + 5, "inv-after", marked=True)
    rc, out = coverage(capsys, bound)
    assert rc == 1
    assert classes(out) == [("judge_line_usage_not_requested", "inv-after"),
                            ("judge_line_usage_not_requested", "inv-at")]


def test_not_requested_bound_excuses_no_other_usage_reason(world, tmp_path, capsys):
    bound = landing(tmp_path)
    judge_line(world, LANDED - 50, usage_schema=1, invocation_id="inv-bare")
    judge_line(world, LANDED - 40, usage_schema=1, usage_reason="no_usage", invocation_id="inv-no-usage")
    rc, out = coverage(capsys, bound)
    assert rc == 1 and classes(out) == [("judge_line_without_usage", "inv-bare")]
    excused = out["excused"]
    assert excused[tokens.NOT_REQUESTED_BEFORE_FIX] == [] and excused[tokens.PRE_FIX_WRITER_NOT_REQUESTED] == []


def test_pinned_not_requested_lines_are_counted_apart(world, tmp_path, capsys):
    bound = landing(tmp_path)
    not_requested(world, LANDED + 10, "inv-pinned")
    not_requested(world, LANDED + 20, "inv-other")
    known = tmp_path / "known.jsonl"
    known.write_text(json.dumps({"invocation_id": "inv-pinned"}) + "\n", encoding="utf-8")
    rc, out = coverage(capsys, bound, "--not-requested-known", str(known))
    assert rc == 0 and out["excused"][tokens.PINNED_NOT_REQUESTED] == 1
    assert len(out["excused"][tokens.PRE_FIX_WRITER_NOT_REQUESTED]) == 2


@pytest.mark.parametrize("claim", ["missing", "malformed", "naive"])
def test_bound_landing_file_missing_malformed_or_naive_exits_2(world, tmp_path, capsys, claim):
    landing(tmp_path)
    path = tmp_path / "bad.json"
    if claim == "malformed":
        path.write_text("{not json", encoding="utf-8")
    elif claim == "naive":
        path.write_text(json.dumps({"head": HEAD, "landed_at": "2026-01-01T00:01:40"}), encoding="utf-8")
    not_requested(world, LANDED - 1, "inv")
    assert coverage(capsys, str(path))[0] == 2
    assert check_version(str(path))[0] == 2


@pytest.mark.parametrize("offset_s", [-1, report.LANDED_AT_MAX_LAG_S + 1])
def test_bound_landed_at_outside_land_event_window_exits_2(world, tmp_path, capsys, offset_s):
    landing(tmp_path)
    claim = landing_file(tmp_path, at(LANDED + offset_s))
    assert coverage(capsys, claim)[0] == 2
    assert check_version(claim)[0] == 2


def test_bound_landing_file_with_plus_three_offset_and_whole_seconds(tmp_path):
    landing(tmp_path, seconds=LANDED + 0.4)
    moment = (BASE + timedelta(seconds=LANDED)).astimezone(sync.timezone(timedelta(hours=3)))
    claim = landing_file(tmp_path, moment.isoformat(timespec="seconds"))
    assert claim and "+03:00" in Path(claim).read_text(encoding="utf-8")
    rc, payload = check_version(claim)
    assert rc == 0 and payload["failures"] == []


def test_whole_second_rule_only_for_fractionless_landed_at(tmp_path):
    landing(tmp_path, seconds=LANDED + 0.4)
    whole = landing_file(tmp_path, "2026-01-01T00:01:40+00:00", "whole.json")
    fraction = landing_file(tmp_path, "2026-01-01T00:01:40.200+00:00", "fraction.json")
    assert check_version(whole)[0] == 0
    assert check_version(fraction)[0] == 2


# --- (b) unresolved live keys -------------------------------------------------------------------

def test_foreign_layer_hook_key_resolves_as_foreign_hook():
    row("hook:arc:x", 10, mechanism="arc:x", layer="arc", test=False)
    rc, payload = check_version()
    assert rc == 0 and payload["failures"] == [] and payload["foreign_hook"] == {"arc": {"x": 1}}


def test_hook_key_without_explicit_layer_stays_unresolved():
    row("hook:arc:x", 10, mechanism="arc:x", test=False)
    rc, payload = check_version()
    assert rc == 1 and classes(payload) == [("unresolved", "hook:arc:x")] and payload["foreign_hook"] == {}


def test_unknown_hook_key_of_core_layer_stays_unresolved():
    row("hook:core:zz-unknown", 10, mechanism="core:zz-unknown", layer="core", test=False)
    rc, payload = check_version()
    assert rc == 1 and classes(payload) == [("unresolved", "hook:core:zz-unknown")]


def test_foreign_hook_report_lists_names_per_layer():
    row("hook:arc:x", 10, mechanism="arc:x", layer="arc", test=False)
    row("hook:arc:x", 11, mechanism="arc:x", layer="arc", test=False)
    row("hook:arc:y", 12, mechanism="arc:y", layer="arc", test=False)
    row("hook:ext:z", 13, mechanism="ext:z", layer="ext", test=False)
    rc, payload = check_version()
    assert rc == 0 and payload["foreign_hook"] == {"arc": {"x": 2, "y": 1}, "ext": {"z": 1}}


def test_test_run_without_isolation_writes_no_live_row(tmp_path, monkeypatch):
    for name in (*live.ISOLATION_ENV, "AGENTCTL_JUDGE_LEDGER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    spool = config_root.telemetry_spool_dir()
    assert under(spool, tmp_path), "the guard is tested against a spool under tmp, never the live one"
    assert live.emit_event("S-leak", "decision", "hook:core:x", {"mechanism": "core:x"}) is None
    assert not spool.exists() or list(spool.iterdir()) == []


def test_leaked_test_row_before_bound_is_named_loss(tmp_path):
    bound = landing(tmp_path)
    row("hook:core:t-leak", LANDED - 10, mechanism="core:t-leak", layer="core", test=True)
    rc, payload = check_version(bound)
    assert rc == 0 and payload["failures"] == []
    assert payload["excused"][f"{report.TEST_LEAK_PREFIX}hook:core:t-leak"] == {"hook:core:t-leak": 1}


def test_leaked_test_row_at_or_after_bound_fails(tmp_path):
    bound = landing(tmp_path)
    row("hook:core:t-fixed", LANDED, marked=True, mechanism="core:t-fixed", layer="core", test=True)
    row("hook:core:t-old", LANDED + 5, mechanism="core:t-old", layer="core", test=True)
    rc, payload = check_version(bound)
    assert rc == 1 and classes(payload) == [("test_run_marked", "hook:core:t-fixed")]
    assert payload["excused"][report.PRE_FIX_WRITER_TEST_RUN] == {"hook:core:t-old": 1}
    assert f"{report.TEST_LEAK_PREFIX}hook:core:t-old" not in payload["excused"]


def test_test_leak_bound_excuses_no_non_test_key(tmp_path):
    bound = landing(tmp_path)
    row("cli:zz-unknown", LANDED - 10)
    rc, payload = check_version(bound)
    assert rc == 1 and classes(payload) == [("unresolved", "cli:zz-unknown")]


def test_test_leak_class_is_structural_not_by_key_name(tmp_path):
    bound = landing(tmp_path)
    row("cli:zz-plain", LANDED - 10, test=True)
    row("cli:test-like", LANDED - 10, test=False)
    rc, payload = check_version(bound)
    assert classes(payload) == [("unresolved", "cli:test-like")]
    assert payload["excused"][f"{report.TEST_LEAK_PREFIX}cli:zz-plain"] == {"cli:zz-plain": 1}


def test_test_run_class_precedes_foreign_hook(tmp_path):
    bound = landing(tmp_path)
    row("hook:arc:w", LANDED - 10, mechanism="arc:w", layer="arc", test=True)
    rc, payload = check_version(bound)
    assert payload["foreign_hook"] == {}
    assert payload["excused"][f"{report.TEST_LEAK_PREFIX}hook:arc:w"] == {"hook:arc:w": 1}


# --- the pinned manual scale measurement --------------------------------------------------------

def _window_seconds(index: int) -> float:
    return (sync.parse_time(report.SCALE_MEASUREMENT_WINDOW[index]) - BASE).total_seconds()


def scale_row(seconds) -> None:
    event("s-scale", "decision", seconds, {"n": 1}, report.SCALE_MEASUREMENT_KEY)


def test_scale_measurement_window_edges_hold_and_a_row_past_them_fails():
    scale_row(_window_seconds(0))
    scale_row(_window_seconds(1))
    rc, payload = check_version()
    assert rc == 0 and payload["excused"][report.SCALE_MEASUREMENT_LOSS] == 2
    scale_row(_window_seconds(1) + 1)
    rc, payload = check_version()
    assert rc == 1 and classes(payload) == [("outside_pinned_window", report.SCALE_MEASUREMENT_KEY)]


def test_scale_measurement_before_window_fails():
    scale_row(_window_seconds(0) - 1)
    rc, payload = check_version()
    assert rc == 1 and classes(payload) == [("outside_pinned_window", report.SCALE_MEASUREMENT_KEY)]


def test_scale_measurement_count_above_pinned_fails(monkeypatch):
    monkeypatch.setattr(report, "SCALE_MEASUREMENT_ROWS", 2)
    scale_row(_window_seconds(0))
    scale_row(_window_seconds(0) + 1)
    assert check_version()[0] == 0
    scale_row(_window_seconds(0) + 2)
    rc, payload = check_version()
    assert rc == 1 and classes(payload) == [("above_pinned_count", report.SCALE_MEASUREMENT_KEY)]


# --- (c) the session a report run is charged to ---------------------------------------------------

def test_report_usage_under_task_goes_to_task_opener_with_session_env_set(monkeypatch):
    task("t1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S-env")
    view("--task", "t1")
    assert [r["kind"] for r in spool_events("s-t1")].count("report") == 1
    assert [r for r in spool_events("S-env") if r["kind"] == "report"] == []


# --- the JSON contract and the writer marker ----------------------------------------------------

def test_json_failure_test_run_field_is_per_row(tmp_path):
    bound = landing(tmp_path)
    row("cli:zz-unknown", LANDED - 10)
    row("hook:core:t-fixed", LANDED + 1, marked=True, mechanism="core:t-fixed", layer="core", test=True)
    rc, payload = check_version(bound)
    assert rc == 1
    assert {f["source_key"]: f["test_run"] for f in payload["failures"]} == {"cli:zz-unknown": False,
                                                                             "hook:core:t-fixed": True}
    for failure in payload["failures"]:
        assert {"class", "epoch", "test_run", "writer_attribute", "source_key"} <= set(failure)


def test_row_after_bound_without_writer_marker_is_version_loss(world, tmp_path, capsys):
    bound = landing(tmp_path)
    not_requested(world, LANDED + 5, "inv-old-writer")
    rc, out = coverage(capsys, bound)
    assert rc == 0 and [w["invocation_id"] for w in out["excused"][tokens.PRE_FIX_WRITER_NOT_REQUESTED]] == [
        "inv-old-writer"]
    assert out["excused"][tokens.NOT_REQUESTED_BEFORE_FIX] == []
    row("hook:core:t-old", LANDED + 5, mechanism="core:t-old", layer="core", test=True)
    rc, payload = check_version(bound)
    assert rc == 0 and payload["excused"][report.PRE_FIX_WRITER_TEST_RUN] == {"hook:core:t-old": 1}


def test_row_after_bound_with_writer_marker_fails(world, tmp_path, capsys):
    bound = landing(tmp_path)
    not_requested(world, LANDED + 5, "inv-new-writer", marked=True)
    rc, out = coverage(capsys, bound)
    assert rc == 1 and [f["writer_attribute"] for f in out["failures"]] == [True]
    row("hook:core:t-new", LANDED + 5, marked=True, mechanism="core:t-new", layer="core", test=True)
    rc, payload = check_version(bound)
    assert rc == 1 and [f["writer_attribute"] for f in payload["failures"]] == [True]


STAMPING_WRITERS = {"lib/telemetry/emit.py", "lib/judge_ledger.py"}
NON_RECORD_APPENDERS = {"lib/telemetry/live.py", "lib/hook_telemetry.py", "lib/memory_retrieval_ledger.py",
                        "agentctl/cli_common.py"}


def test_every_live_writer_stamps_writer_marker(world, monkeypatch):
    scripts = Path(__file__).resolve().parents[1]
    appenders = {str(p.relative_to(scripts)) for p in scripts.rglob("*.py")
                 if "tests" not in p.parts and "O_APPEND" in p.read_text(encoding="utf-8", errors="replace")}
    assert appenders == STAMPING_WRITERS | NON_RECORD_APPENDERS, "a new appending writer must be classified"

    emit.emit("S-mark", "decision", {"source_key": "hook:core:x"})
    assert [e["fields"].get(judge_ledger.WRITER_MARKER) for e in spool_events("S-mark")] == [1]

    assert judge_ledger._write("call", judge="j") is not None
    assert json.loads(world.judge.read_text(encoding="utf-8").splitlines()[-1])[judge_ledger.WRITER_MARKER] == 1

    sync._keep_in_reserved_spool({"event_id": emit.uuid7(), "kind": "sync", "time": at(1),
                                  "fields": {"source_key": sync.SOURCE_KEY}})
    assert [e["fields"].get(judge_ledger.WRITER_MARKER) for e in spool_events(sync.SYNC_SESSION)] == [1]


def test_replayed_unmarked_spool_line_stays_unmarked(world):
    emit.ensure_private_dir(live.spool_dir())
    spooled = {"event_id": emit.uuid7(), "session_id": "S-replay", "kind": "decision", "time": at(1),
               "provenance": "live", "backref": None, "fields": {"source_key": "hook:core:x"}}
    live.append_line(emit.spool_path("S-replay"), b"\n" + json.dumps(spooled).encode("utf-8") + b"\n")
    world.replay("S-replay")
    [(fields,)] = query("SELECT fields FROM events WHERE event_id = ?", spooled["event_id"])
    assert judge_ledger.WRITER_MARKER not in json.loads(fields)
