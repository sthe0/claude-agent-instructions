"""Substrate adequacy (lib/telemetry/substrate.py, `improvement-scan.py substrate`): difficulty
mass per functional place, read from the telemetry record through report.tasks() and
report.difficulties(). Every fixture is rows inserted straight into the isolated record."""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib import config_root
from lib.telemetry import report, store, substrate, tables
from telemetry_import_support import BASE, at

SCRIPTS_DIR = Path(__file__).resolve().parents[1]


def _load_scan():
    spec = importlib.util.spec_from_file_location("improvement_scan_substrate", SCRIPTS_DIR / "improvement-scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load_scan()

NOW = (BASE + timedelta(days=1)).isoformat()


# --- record fixtures ----------------------------------------------------------------------

def put(table, *rows):
    outcome = store.insert_rows(table, rows, deadline_s=60)
    assert outcome["failed"] == 0 and outcome["deferred"] == 0


def event(session, kind, seconds, fields, source_key) -> str:
    event_id = str(uuid.uuid4())
    put("events", {"event_id": event_id, "session_id": session, "kind": kind, "source_key": source_key,
                   "provenance": "live", "time": at(seconds), "fields": json.dumps(fields)})
    return event_id


def task(task_id, status="resolved", synthetic=0) -> tuple:
    session = f"s-{task_id}"
    put("sessions", {"session_id": session, "core_head": "h1", "started_at": at(0)})
    epoch = event(session, "task", 0, {"action": "open", "task_id": task_id, "synthetic": bool(synthetic)},
                  "cli:start")
    origin = store.origin_id()
    put("tasks", {"origin": origin, "task_id": task_id, "epoch": epoch, "opened_at": at(0),
                  "weight_class": "small", "synthetic": synthetic, "status": status})
    conn = store.open_record()
    conn.execute("UPDATE sessions SET task_origin = ?, task_id = ?, task_epoch = ? WHERE session_id = ?",
                 (origin, task_id, epoch, session))
    conn.commit()
    conn.close()
    return origin, task_id, epoch


def command(key, seconds, name, before, after):
    event(f"s-{key[1]}", "command", seconds, {"command": name, "outcome": "ok", "task_epoch": key[2],
                                              "node_before": before, "node_after": after}, f"cli:{name}")


def difficulty(key, start_s, minutes=None, exit_kind="replan", route="failed_stage"):
    """One DIAGNOSING interval from `start_s`, closed `minutes` later by `exit_kind` (open when None)."""
    if route == "effort_fire":
        event(f"s-{key[1]}", "gate", start_s - 1, {"outcome": "fire", "task_epoch": key[2]}, "gate:effort_fire")
    command(key, start_s, "record-result", "PLAN_READY" if route == "review_cycle" else "EXECUTING", "DIAGNOSING")
    if minutes is not None:
        command(key, start_s + minutes * 60, exit_kind, "DIAGNOSING", "PLAN_READY")


def cause(key, index, mechanism=None, place=None, machinery=True):
    if machinery and place is None:
        place = tables.load("registry").members[mechanism].attrs["place"]
    put("causes", {"cause_id": str(uuid.uuid4()), "session": f"s-{key[1]}", "difficulty_index": index,
                   "cause_category": "other", "cause_category_table_version": 1,
                   "machinery_induced": int(machinery), "mechanism": mechanism if machinery else "none",
                   "mechanism_table_version": 1, "functional_place": place,
                   "functional_place_table_version": 1, "origin": store.origin_id()})


def unaligned(place, mechanism="unknown", machinery=True, place_basis="category_default"):
    """An imported cause no difficulty was aligned to: interval unknown."""
    put("causes", {"cause_id": f"fixture-{uuid.uuid4()}", "session": "fixture", "difficulty_index": None,
                   "cause_category": "other", "cause_category_table_version": 1,
                   "machinery_induced": int(machinery), "mechanism": mechanism if machinery else "none",
                   "mechanism_table_version": 1, "functional_place": place,
                   "functional_place_table_version": 1, "origin": store.origin_id(), "imported": 1,
                   "provenance": "fixture:test", "mechanism_basis": "category_default",
                   "place_basis": place_basis, "interval_unknown": 1})


def one(task_id, minutes, mechanism, start_s=100, status="resolved", **kwargs) -> tuple:
    """A task holding one difficulty of `minutes` caused at `mechanism`."""
    key = task(task_id, status=status)
    difficulty(key, start_s, minutes, **kwargs)
    cause(key, 0, mechanism)
    return key


def run(*argv, now=NOW) -> "tuple[int, dict | None]":
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = scan.main(["substrate", "--format", "json", "--now", now, *argv])
    text = out.getvalue()
    return status, json.loads(text) if text.strip() else None


def masses(payload) -> dict:
    return {r["place"]: r["mass"] for r in payload["ranking"]}


def library(registry, now=NOW) -> dict:
    conn = store.open_readonly()
    try:
        return substrate.load(conn, datetime.fromisoformat(now), 7, registry)
    finally:
        conn.close()


def lib_masses(result) -> dict:
    return {r["place"]: r["mass"] for r in substrate.ranking(result)}


def entry(place, part_of=(), kind="elementary"):
    return {"kind": kind, "part_of": list(part_of), "place": place}


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the scan at a config.md of the test's own rows (abandon window 7 days)."""
    def write(**rows):
        rows.setdefault("telemetry-abandon-window-days", 7)
        path = tmp_path / "config.md"
        path.write_text("| Key | Value | Meaning |\n|---|---|---|\n" + "".join(
            f"| `{k}` | `{v}` | x |\n" for k, v in rows.items()), encoding="utf-8")
        monkeypatch.setattr(scan, "CONFIG_PATH", path)
        return path
    return write


# --- findings and threshold -----------------------------------------------------------------

def test_finding_for_every_place_at_or_above_threshold_and_none_below():
    one("a", 90, "cli:approve")
    one("b", 20, "cli:block")
    one("c", 5, "cli:children")
    status, payload = run("--threshold", "2")
    assert status == 0
    assert masses(payload) == {"plan_approval": 3, "execution": 2, "dispatch": 1}
    assert [f["place"] for f in payload["findings"]] == ["plan_approval", "execution"]


def test_threshold_above_max_prints_none():
    one("a", 90, "cli:approve")
    one("b", 20, "cli:block")
    status, payload = run("--threshold-above-max")
    assert status == 0 and payload["threshold"] == 4 and payload["findings"] == []


def test_absent_threshold_key_exits_2(config, capsys):
    config()
    one("a", 90, "cli:approve")
    assert scan.main(["substrate", "--now", NOW]) == 2
    assert "substrate-mass-threshold" in capsys.readouterr().err


def test_threshold_is_read_from_the_config_next_to_the_script(config, tmp_path, monkeypatch):
    assert scan.CONFIG_PATH == Path(scan.__file__).resolve().parents[1] / "config.md"
    home = tmp_path / "agent-home"
    home.mkdir()
    (home / "config.md").write_text("| `substrate-mass-threshold` | `99` | x |\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    config(**{"substrate-mass-threshold": 2})
    one("a", 20, "cli:approve")
    status, payload = run()
    assert status == 0 and payload["threshold"] == 2 and [f["place"] for f in payload["findings"]] == ["plan_approval"]


def test_check_min_findings_and_expect_threshold(capsys):
    one("a", 20, "cli:approve")
    assert run("--threshold", "2", "--check")[0] == 0
    capsys.readouterr()
    assert run("--threshold", "2", "--check", "--min-findings", "2")[0] == 1
    assert "--min-findings 2" in capsys.readouterr().err
    assert run("--threshold", "2", "--check", "--expect-threshold", "2")[0] == 0
    assert run("--threshold", "2", "--check", "--expect-threshold", "3")[0] == 1
    assert "--expect-threshold 3" in capsys.readouterr().err


def test_finding_attaches_tasks_minutes_spend_replans():
    key = task("a")
    difficulty(key, 100, 20)
    difficulty(key, 3000, 30)
    cause(key, 0, "cli:approve")
    cause(key, 1, "cli:approve")
    put("token_usage", {"message_id": "m1", "session_id": "s-a", "role": "root", "phase": "other",
                        "input_tokens": 100000, "output_tokens": 0, "cache_read_tokens": 0,
                        "cache_write_tokens": 0, "ts": at(1), "task_origin": key[0], "task_id": key[1],
                        "task_epoch": key[2], "model": "claude-opus-x"})
    found = run("--threshold", "1")[1]["findings"][0]
    conn = store.open_readonly()
    expected = next(t for t in report.tasks(conn) if t["task"] == key)
    conn.close()
    assert found["difficulties"] == 2 and found["tasks"] == ["/".join(key)]
    assert found["replans"] == expected["replans"] == 2
    assert found["minutes"] == pytest.approx(expected["minutes"]) and found["minutes"] > 50
    assert found["spend_usd"] == pytest.approx(expected["spend_usd"]) and found["spend_usd"] > 0


def test_unknown_mechanism_count_shown():
    unaligned("execution")
    unaligned("execution", place_basis="observed")
    key = task("a")
    difficulty(key, 100, 20)
    cause(key, 0, "cli:block")
    found = run("--threshold", "1")[1]["findings"][0]
    assert found["place"] == "execution" and found["difficulties"] == 3
    assert found["unknown_mechanism"] == 2 and found["interval_unknown"] == 2
    assert found["category_default_place"] == 1


# --- severity -------------------------------------------------------------------------------

def test_band_boundaries_9m59s_10m00s_60m00s():
    one("a", 9 + 59 / 60, "cli:approve")
    one("b", 10, "cli:block")
    one("c", 59 + 59 / 60, "cli:children")
    one("d", 60, "cli:accept-protected-grant")
    assert masses(run("--threshold", "1")[1]) == {
        "plan_approval": 1, "execution": 2, "dispatch": 2, "permission_control": 3}


def test_never_closed_interval_in_ended_task_is_severity_3():
    one("a", None, "cli:approve")
    status, payload = run("--threshold", "1")
    assert masses(payload) == {"plan_approval": 3} and payload["counts"]["cases"]["never_closed"] == 1


def test_open_interval_of_running_task_contributes_no_mass():
    one("a", None, "cli:approve", status="open")
    payload = run("--threshold", "1")[1]
    assert payload["ranking"] == [] and payload["counts"]["cases"]["open_running"] == 1


def test_silent_task_past_abandon_window_counts_as_ended():
    one("a", None, "cli:approve", status="open")
    assert run("--threshold", "1", now=(BASE + timedelta(days=6)).isoformat())[1]["ranking"] == []
    late = run("--threshold", "1", now=(BASE + timedelta(days=8)).isoformat())[1]
    assert masses(late) == {"plan_approval": 3}


def test_fixture_cause_without_interval_is_severity_2_interval_unknown_and_counted():
    unaligned("execution")
    payload = run("--threshold", "1")[1]
    assert masses(payload) == {"execution": 2}
    assert payload["counts"]["cases"]["interval_unknown"] == 1
    assert payload["findings"][0]["interval_unknown"] == 1


def test_unaligned_imported_rows_each_count():
    unaligned("execution")
    unaligned("execution")
    found = run("--threshold", "1")[1]["findings"][0]
    assert found["mass"] == 4 and found["interval_unknown"] == 2 and found["difficulties"] == 2
    assert found["tasks"] == [] and found["minutes"] == 0 and found["spend_usd"] == 0 and found["replans"] == 0


# --- intervals ------------------------------------------------------------------------------

def test_each_diagnosing_entry_route_opens_interval():
    for name, route in (("a", "failed_stage"), ("b", "effort_fire"), ("c", "review_cycle")):
        one(name, 20, {"a": "cli:approve", "b": "cli:block", "c": "cli:children"}[name], route=route)
    conn = store.open_readonly()
    routes = sorted(d["entry_route"] for d in report.difficulties(conn))
    conn.close()
    assert routes == ["effort_fire", "failed_stage", "review_cycle"]
    assert masses(run("--threshold", "1")[1]) == {"plan_approval": 2, "execution": 2, "dispatch": 2}


def test_each_exit_kind_closes_interval():
    for name, exit_kind in (("a", "replan"), ("b", "unblock"), ("c", "reset")):
        one(name, 20, {"a": "cli:approve", "b": "cli:block", "c": "cli:children"}[name], exit_kind=exit_kind)
    payload = run("--threshold", "1")[1]
    assert masses(payload) == {"plan_approval": 2, "execution": 2, "dispatch": 2}
    assert payload["counts"]["cases"]["closed"] == 3


def test_synthetic_tasks_excluded_from_mass():
    key = task("synth", synthetic=1)
    difficulty(key, 100, 20)
    cause(key, 0, "cli:approve")
    one("real", 20, "cli:block")
    assert masses(run("--threshold", "1")[1]) == {"execution": 2}


# --- attribution and roll-up ----------------------------------------------------------------

def test_difficulty_counts_at_most_specific_place_only():
    registry = {"e1": entry("p1", ["c"]), "e2": entry("p2", ["c"]), "c": entry("pc", kind="composite")}
    one("a", 20, "cli:approve")
    with_mechanism("a", "e1")
    assert lib_masses(library(registry)) == {"p1": 2, "pc": 2}


def with_mechanism(task_id, mechanism):
    conn = store.open_record()
    conn.execute("UPDATE causes SET mechanism = ? WHERE session = ?", (mechanism, f"s-{task_id}"))
    conn.commit()
    conn.close()


def test_mass_rolls_up_part_of_closure():
    registry = {"e": entry("pe", ["c1"]), "c1": entry("p1", ["c2"], "composite"), "c2": entry("p2", kind="composite")}
    one("a", 20, "cli:approve")
    with_mechanism("a", "e")
    assert lib_masses(library(registry)) == {"pe": 2, "p1": 2, "p2": 2}


def test_diamond_part_of_counts_each_difficulty_once():
    registry = {"e": entry("pe", ["l", "r"]), "l": entry("pl", ["t"], "composite"),
                "r": entry("pr", ["t"], "composite"), "t": entry("pt", kind="composite")}
    one("a", 20, "cli:approve")
    with_mechanism("a", "e")
    assert lib_masses(library(registry)) == {"pe": 2, "pl": 2, "pr": 2, "pt": 2}


def test_shared_place_counts_each_difficulty_once():
    registry = {"e": entry("pe", ["c1"]), "c1": entry("q", ["c2"], "composite"), "c2": entry("q", kind="composite")}
    one("a", 20, "cli:approve")
    with_mechanism("a", "e")
    assert lib_masses(library(registry)) == {"pe": 2, "q": 2}


def test_part_and_composite_sharing_a_place_count_once():
    one("a", 20, "cli:plan-review")
    payload = run("--threshold", "1")[1]
    assert masses(payload) == {"plan_review": 2}
    assert payload["ranking"][0]["composite"] is True


def test_ranking_lists_each_place_once():
    one("a", 20, "cli:plan-review")
    one("b", 20, "cli:stage-review")
    one("c", 90, "gate:plan_review")
    places = [r["place"] for r in run("--threshold", "1")[1]["ranking"]]
    assert places == ["plan_review"]


def test_none_place_excluded_from_mass_ranking_and_findings():
    key = task("a")
    difficulty(key, 100, 90)
    cause(key, 0, place="none", machinery=False)
    unaligned("none", machinery=False)
    one("b", 20, "cli:block")
    payload = run("--threshold", "0")[1]
    assert masses(payload) == {"execution": 2}
    assert [f["place"] for f in payload["findings"]] == ["execution"] and payload["ties"] == []
    assert payload["counts"]["unplaced"] == 2 and payload["counts"]["uncaused"] == 0
    assert payload["counts"]["difficulties"] == 3
    calibration = run("--calibrate", "--places", "1")[1]["calibration"]
    assert [r["place"] for r in calibration["rows"]] == ["execution"] and calibration["total_mass"] == 2


# --- calibration ----------------------------------------------------------------------------

def test_calibrate_reports_mass_of_the_nth_place_ranking_and_ties():
    one("a", 90, "cli:approve")
    key = task("a2")
    difficulty(key, 100, 90)
    difficulty(key, 9000, 90)
    cause(key, 0, "cli:approve")
    cause(key, 1, "cli:block")
    one("b", 20, "cli:block")
    one("c", 20, "cli:children")
    one("d", 20, "cli:accept-protected-grant")
    status, payload = run("--calibrate", "--places", "3")
    calibration = payload["calibration"]
    assert status == 0
    assert [(r["place"], r["mass"]) for r in payload["ranking"]] == [
        ("plan_approval", 6), ("execution", 5), ("dispatch", 2), ("permission_control", 2)]
    assert calibration["nth_place"] == "dispatch" and calibration["nth_mass"] == 2
    assert calibration["ties_at_nth"] == ["permission_control"]


def test_calibrate_skips_places_part_of_related_to_a_higher_ranked_one():
    registry = {"e": entry("pe", ["c"]), "c": entry("pc", kind="composite"), "x": entry("px")}
    one("a", 90, "cli:approve")
    with_mechanism("a", "e")
    one("b", 20, "cli:approve")
    with_mechanism("b", "x")
    calibration = substrate.calibrate(library(registry), 2)
    assert [(r["place"], r["status"], r["nested_with"]) for r in calibration["rows"]] == [
        ("pc", "counted", []), ("pe", "skipped", ["pc"]), ("px", "counted", [])]
    assert calibration["nth_place"] == "px" and calibration["nth_mass"] == 2


def test_calibrate_prints_total_counted_and_machinery_induced_shares():
    one("a", 90, "cli:approve")
    one("b", 20, "cli:block")
    key = task("c")
    difficulty(key, 100, 5)
    cause(key, 0, place="dispatch", machinery=False)
    calibration = run("--calibrate", "--places", "1")[1]["calibration"]
    assert calibration["total_mass"] == 6
    assert calibration["counted_share"] == pytest.approx(3 / 6)
    assert calibration["machinery_induced_share"] == pytest.approx(5 / 6)


def test_calibrate_runs_without_the_threshold_key(config):
    config()
    one("a", 20, "cli:approve")
    status, payload = run("--calibrate", "--places", "1")
    assert status == 0 and payload["calibration"]["nth_place"] == "plan_approval"


# --- the report -----------------------------------------------------------------------------

def test_report_includes_substrate_section(config, tmp_path, capsys):
    config(**{"substrate-mass-threshold": 2})
    one("a", 20, "cli:approve")
    board = str(tmp_path / "no-board.json")
    assert scan.main(["report", "--format", "json", "--board", board]) == 0
    section = json.loads(capsys.readouterr().out)["substrate"]
    assert section["status"] == "ok" and section["threshold"] == 2
    assert [f["place"] for f in section["findings"]] == ["plan_approval"]
    assert scan.main(["report", "--board", board]) == 0
    assert "## Substrate adequacy" in capsys.readouterr().out


def test_report_substrate_section_without_a_record_names_why(tmp_path, capsys):
    assert scan.main(["report", "--format", "json", "--board", str(tmp_path / "no-board.json")]) == 0
    assert json.loads(capsys.readouterr().out)["substrate"]["status"] == "no telemetry record"
    assert run()[0] == 2
    assert not config_root.telemetry_db().exists()


def _record_state() -> tuple:
    """The record's bytes and mtime, and its WAL's bytes; a read-only open of a WAL database
    may create an empty WAL file, which holds no write."""
    record = config_root.telemetry_db()
    wal = record.with_name(record.name + "-wal")
    return record.read_bytes(), record.stat().st_mtime_ns, wal.read_bytes() if wal.exists() else b""


def test_substrate_leaves_a_populated_record_unchanged(config):
    config(**{"substrate-mass-threshold": 2})
    one("a", 20, "cli:approve")
    before = _record_state()
    assert run()[0] == 0
    assert _record_state() == before


def test_report_survives_a_substrate_fault(config, tmp_path, monkeypatch, capsys):
    config(**{"substrate-mass-threshold": 2})
    one("a", 20, "cli:approve")
    monkeypatch.setattr(substrate, "load_severity", lambda: {"bands": []})
    with pytest.raises(ValueError):
        run()
    capsys.readouterr()
    assert scan.main(["report", "--format", "json", "--board", str(tmp_path / "no-board.json")]) == 0
    status = json.loads(capsys.readouterr().out)["substrate"]["status"]
    assert status.startswith("error: ValueError") and "no severity band" in status


def test_report_no_substrate_skips_the_section(config, tmp_path, monkeypatch, capsys):
    config(**{"substrate-mass-threshold": 2})
    one("a", 20, "cli:approve")

    def scanned(*_args, **_kwargs):
        raise AssertionError("--no-substrate still scanned the record")
    monkeypatch.setattr(substrate, "load", scanned)
    board = str(tmp_path / "no-board.json")
    assert scan.main(["report", "--format", "json", "--no-substrate", "--board", board]) == 0
    assert "substrate" not in json.loads(capsys.readouterr().out)
    assert scan.main(["report", "--no-substrate", "--board", board]) == 0
    assert "## Substrate adequacy" not in capsys.readouterr().out


def test_check_with_calibrate_exits_2(config, capsys):
    config(**{"substrate-mass-threshold": 2})
    one("a", 20, "cli:approve")
    assert run("--calibrate", "--check") == (2, None)
    assert "--check" in capsys.readouterr().err


def test_two_tasks_of_one_session_each_count_their_difficulty():
    session, origin, keys = "s-shared", store.origin_id(), []
    put("sessions", {"session_id": session, "core_head": "h1", "started_at": at(0)})
    for n, task_id in enumerate(("first", "second")):
        epoch = event(session, "task", n * 3000, {"action": "open", "task_id": task_id}, "cli:start")
        put("tasks", {"origin": origin, "task_id": task_id, "epoch": epoch, "opened_at": at(n * 3000),
                      "weight_class": "small", "synthetic": 0, "status": "resolved"})
        keys.append((origin, task_id, epoch))
    for n, key in enumerate(keys):
        for seconds, name, before, after in ((n * 3000 + 100, "record-result", "EXECUTING", "DIAGNOSING"),
                                             (n * 3000 + 1300, "replan", "DIAGNOSING", "PLAN_READY")):
            event(session, "command", seconds, {"command": name, "outcome": "ok", "task_epoch": key[2],
                                                "node_before": before, "node_after": after}, f"cli:{name}")
    put("causes", {"cause_id": str(uuid.uuid4()), "session": session, "difficulty_index": 0,
                   "cause_category": "other", "cause_category_table_version": 1, "machinery_induced": 1,
                   "mechanism": "cli:approve", "mechanism_table_version": 1,
                   "functional_place": "plan_approval", "functional_place_table_version": 1,
                   "origin": origin})
    conn = store.open_readonly()
    found = sorted((d["task"][1], d["difficulty"]) for d in report.difficulties(conn))
    conn.close()
    assert found == [("first", "s-shared#0"), ("second", "s-shared#0")]
    assert masses(run("--threshold", "1")[1]) == {"plan_approval": 4}


def test_band_mass_reads_the_severity_table():
    severity = substrate.load_severity()
    members = tables.load("severity").members
    assert severity["never_closed"] == members["never_closed"].attrs["mass"]
    assert severity["interval_unknown"] == members["interval_unknown"].attrs["mass"]
    assert substrate.band_mass(0, severity) == members["band_1"].attrs["mass"]


def test_now_is_timezone_aware():
    assert datetime.fromisoformat(NOW).tzinfo == timezone.utc
