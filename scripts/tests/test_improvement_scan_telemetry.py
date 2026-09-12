"""Tests for improvement-scan.py's `telemetry` subcommand: the incremental
ledger-analysis producer (Stage 4) — its deterministic detector table, the
LedgerCursor-gated scan pass, the evidence bundle it hands to the model, and
the grounds-intake/dedup/store pass that turns model-supplied functional
grounds into stored Findings.

Loaded by path exactly like test_improvement_scan.py; see that file's
docstring for why (the module's filename carries a dash).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from ast_purity import impure_names  # noqa: E402


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _write_config(tmp_path):
    p = tmp_path / "config.md"
    p.write_text(
        "| Key | Value |\n"
        "|---|---|\n"
        "| `budget-small-usd` | `1.00` |\n"
        "| `budget-medium-usd` | `3.00` |\n"
        "| `budget-large-usd` | `8.00` |\n"
        "| `effort-replan-absolute` | `3` |\n",
        encoding="utf-8",
    )
    return p


# --- (1) LedgerCursor gating, integrated through scan_telemetry -------------

def test_cursor_gating_processes_once_then_skips_unchanged_sessions():
    rows = scan._load_ledger_rows(FIXTURES / "telemetry_ledger.jsonl")
    cursor = scan.LedgerCursor()

    items_first, due_first = scan.scan_telemetry(rows, [], cursor)
    assert {sid for sid, _mtime in due_first} == {"sess-cost", "sess-replan", "sess-clean"}
    assert {i["detector"] for i in items_first} == {"cost-concentration", "replan-pressure"}

    for session_id, mtime in due_first:
        cursor.mark(session_id, mtime)

    items_second, due_second = scan.scan_telemetry(rows, [], cursor)
    assert due_second == []
    assert items_second == []


# --- (2) detector threshold boundaries --------------------------------------

@pytest.mark.parametrize(
    "detector, row_under, row_at",
    [
        (
            scan._detect_cost_concentration,
            {"cost_usd": 23.99},
            {"cost_usd": 24.0},
        ),
        (
            scan._detect_replan_pressure,
            {"effectiveness": {"replans": 2}},
            {"effectiveness": {"replans": 3}},
        ),
        (
            scan._detect_delegation_misses,
            {"missed_delegation_clusters": 0},
            {"missed_delegation_clusters": 1},
        ),
        (
            scan._detect_attention_burn,
            {"attention": {"corrections": 1}},
            {"attention": {"corrections": 2}},
        ),
    ],
)
def test_detector_threshold_boundaries(tmp_path, detector, row_under, row_at):
    config_path = _write_config(tmp_path)
    base = {"session_id": "s", "project": "p", "date": "d"}
    assert detector({**base, **row_under}, [], config_path=config_path) is None
    fired = detector({**base, **row_at}, [], config_path=config_path)
    assert fired is not None
    assert "detector" in fired
    assert "description" in fired


def test_run_detectors_stamps_session_project_and_date_onto_every_firing_item(tmp_path):
    config_path = _write_config(tmp_path)
    row = {"session_id": "s", "project": "p", "date": "d", "cost_usd": 24.0}
    items = scan.run_detectors(row, [], config_path=config_path)
    assert len(items) == 1
    assert items[0]["session_id"] == "s"
    assert items[0]["project"] == "p"
    assert items[0]["date"] == "d"


def test_spawn_process_failure_threshold_boundary():
    row = {"session_id": "s", "project": "p", "date": "d"}
    assert scan._detect_spawn_process_failures(row, [], config_path=scan.CONFIG_PATH) is None
    assert scan._detect_spawn_process_failures(
        row, [{"malformed": True}], config_path=scan.CONFIG_PATH
    ) is not None
    assert scan._detect_spawn_process_failures(
        row, [{"malformed": False, "exit_code": 1}], config_path=scan.CONFIG_PATH
    ) is not None
    assert scan._detect_spawn_process_failures(
        row, [{"malformed": False, "exit_code": 0}], config_path=scan.CONFIG_PATH
    ) is None


# --- (3) a failed ledger-refresh subprocess is reported, never swallowed ----

def test_degraded_refresh_is_reported_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan.shell, "refresh_policy_ledger", lambda days, ledger_path=None: (False, "boom")
    )
    spawn_ledger = tmp_path / "spawn.jsonl"
    spawn_ledger.write_text("", encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    args = argparse.Namespace(
        ledger=str(FIXTURES / "telemetry_ledger.jsonl"),
        spawn_ledger=str(spawn_ledger),
        cursor=str(tmp_path / "cursor.json"),
        emit_evidence=str(evidence_path),
        days=7,
        dry_run=False,
    )
    rc = scan._run_telemetry_scan(args)
    assert rc == 1
    bundle = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert bundle["degraded_refresh"] is True
    assert bundle["degraded_reason"] == "boom"
    # a degraded run still scans whatever ledger is on disk — it does not skip evidence
    assert bundle["items"]


def test_healthy_refresh_advances_the_cursor_so_a_second_run_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan.shell, "refresh_policy_ledger", lambda days, ledger_path=None: (True, "")
    )
    spawn_ledger = tmp_path / "spawn.jsonl"
    spawn_ledger.write_text("", encoding="utf-8")
    cursor_path = tmp_path / "cursor.json"
    args = argparse.Namespace(
        ledger=str(FIXTURES / "telemetry_ledger.jsonl"),
        spawn_ledger=str(spawn_ledger),
        cursor=str(cursor_path),
        emit_evidence=str(tmp_path / "evidence1.json"),
        days=7,
        dry_run=False,
    )
    assert scan._run_telemetry_scan(args) == 0
    first_bundle = json.loads((tmp_path / "evidence1.json").read_text(encoding="utf-8"))
    assert first_bundle["sessions_scanned"] == 3

    args.emit_evidence = str(tmp_path / "evidence2.json")
    assert scan._run_telemetry_scan(args) == 0
    second_bundle = json.loads((tmp_path / "evidence2.json").read_text(encoding="utf-8"))
    assert second_bundle["sessions_scanned"] == 0
    assert second_bundle["items"] == []


# --- (4) evidence-bundle shape ------------------------------------------------

def test_evidence_bundle_shape():
    bundle = scan.build_evidence_bundle(
        [{"detector": "cost-concentration"}],
        days=7, sessions_scanned=2,
        degraded_refresh=False, degraded_reason=None, now=T0,
    )
    assert bundle["schema"] == scan.EVIDENCE_SCHEMA
    assert bundle["days"] == 7
    assert bundle["sessions_scanned"] == 2
    assert bundle["degraded_refresh"] is False
    assert bundle["degraded_reason"] is None
    assert bundle["items"] == [{"detector": "cost-concentration"}]
    assert bundle["generated_at"] == T0.isoformat()


# --- (5) a dedup match is recorded, never silently dropped ------------------

def test_dedup_match_against_experience_is_recorded_not_dropped(monkeypatch):
    monkeypatch.setattr(
        scan.shell, "search_experience",
        lambda keywords, scope="global": (True, True, "matched: some-leaf.md"),
    )
    grounds = [{
        "detector": "attention-burn",
        "functional_ground": "a recurring ground already tracked elsewhere",
        "title": "t",
    }]
    findings, dedup_log = scan.build_findings_from_grounds(grounds)
    assert findings == []
    assert len(dedup_log) == 1
    assert dedup_log[0]["outcome"] == "dedup-match"
    assert "some-leaf" in dedup_log[0]["detail"]


def test_dedup_match_against_backlog_board_skips_the_subprocess_search(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scan.shell, "search_experience",
        lambda keywords, scope="global": calls.append(1) or (True, False, "no analogous"),
    )
    board = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA, generated_at="x",
        items={"ref-a": scan.PriorBoardItem(
            classification="narrow", score=1.0, rank=1, source_digest="d",
            functional_ground="already on the backlog board",
        )},
    )
    grounds = [{"detector": "attention-burn", "functional_ground": "already on the backlog board", "title": "t"}]
    findings, dedup_log = scan.build_findings_from_grounds(grounds, board=board)
    assert findings == []
    assert dedup_log[0]["outcome"] == "board-match"
    assert calls == []  # the cheap board check pre-empted the subprocess search


def test_search_subprocess_failure_is_recorded_and_still_stores_the_finding(monkeypatch):
    monkeypatch.setattr(
        scan.shell, "search_experience",
        lambda keywords, scope="global": (False, False, "record-experience search exited 1"),
    )
    grounds = [{"detector": "attention-burn", "functional_ground": "ground text", "title": "t"}]
    findings, dedup_log = scan.build_findings_from_grounds(grounds)
    assert len(findings) == 1  # a broken search must not silently suppress the finding
    assert dedup_log[0]["outcome"] == "search-failed"


# --- (6) store key is detector+ground derived, never session-derived -------

def test_store_key_is_detector_ground_derived_and_accumulates_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan.shell, "search_experience",
        lambda keywords, scope="global": (True, False, "no analogous experience leaf found"),
    )
    store = tmp_path / "findings.jsonl"
    shared_ground = "opus inherited by default for a delegatable haiku-shaped task"

    findings_a, _ = scan.build_findings_from_grounds([{
        "detector": "delegation-misses", "functional_ground": shared_ground,
        "title": "t", "evidence_refs": ["sess-a"],
    }])
    rows_a = scan.store_findings(findings_a, store_path=store)
    row_a = next(r for r in rows_a if r["kind"] == scan.sds.KIND_TELEMETRY_PATTERN)
    assert row_a["times_surfaced"] == 1

    findings_b, _ = scan.build_findings_from_grounds([{
        "detector": "delegation-misses", "functional_ground": shared_ground,
        "title": "t", "evidence_refs": ["sess-b"],
    }])
    rows_b = scan.store_findings(findings_b, store_path=store)
    row_b = next(r for r in rows_b if r["kind"] == scan.sds.KIND_TELEMETRY_PATTERN)

    assert row_b["path"] == row_a["path"]  # same detector+ground -> same store key
    assert row_b["times_surfaced"] == 2  # accumulates rather than minting a new row


def test_store_key_differs_for_different_ground_text():
    a = scan._ground_signal("attention-burn", "ground text A")
    b = scan._ground_signal("attention-burn", "ground text B")
    c = scan._ground_signal("replan-pressure", "ground text A")
    assert a != b
    assert a != c


# --- (7) never files, never dispatches, no network --------------------------

def test_telemetry_functions_never_shell_out_or_reach_the_network():
    for fn in (
        scan._detect_cost_concentration,
        scan._detect_replan_pressure,
        scan._detect_delegation_misses,
        scan._detect_spawn_process_failures,
        scan._detect_attention_burn,
        scan.run_detectors,
        scan._load_ledger_rows,
        scan._group_spawn_rows_by_session,
        scan.scan_telemetry,
        scan.build_evidence_bundle,
        scan._ground_signal,
        scan._board_ground_match,
        scan.build_findings_from_grounds,
        scan._run_telemetry_scan,
        scan._run_telemetry_grounds,
        scan._cmd_telemetry,
    ):
        assert impure_names(fn) == set()


def test_shell_module_reaches_only_subprocess_and_only_the_two_named_scripts():
    import improvement_scan_shell as shell_mod

    assert impure_names(shell_mod) <= {"subprocess"}
    assert shell_mod.POLICY_SCORECARD.name == "policy-scorecard.py"
    assert shell_mod.RECORD_EXPERIENCE.name == "record-experience.py"
