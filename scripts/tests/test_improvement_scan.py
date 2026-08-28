"""Tests for improvement-scan.py's shared core: the Finding model, its
registration as a second external producer in self_diagnose_store.py, and the
two resume seams (PriorBoard for backlog, LedgerCursor for telemetry).

The module is loaded by path (its filename carries a dash) via
importlib.util.spec_from_file_location, the same pattern
test_self_diagnose_store.py uses for self-diagnose.py and
hook-turn-end-gate.py.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import self_diagnose_store as sds  # noqa: E402
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


def _cost(measured=False, usd=None, basis=""):
    return scan.CostSignal(usd_per_week=usd, basis=basis, measured=measured)


def _finding(kind="backlog-item", signal="sig-1", next_step="planner", **kw):
    defaults = dict(
        kind=kind,
        signal=signal,
        title="a title",
        functional_ground="a ground",
        evidence=("ev-1",),
        cost_signal=_cost(),
        source_ref="ref-1",
        recommended_next_step=next_step,
    )
    defaults.update(kw)
    return scan.Finding(**defaults)


# --- Finding vocabulary -------------------------------------------------

def test_finding_rejects_a_recommendation_outside_the_closed_vocabulary():
    with pytest.raises(ValueError):
        _finding(next_step="do-it-yourself")


def test_finding_accepts_every_declared_next_step():
    for step in scan.RECOMMENDED_NEXT_STEPS:
        assert _finding(next_step=step).recommended_next_step == step


def test_finding_requires_kind_and_signal():
    with pytest.raises(ValueError):
        _finding(kind="")
    with pytest.raises(ValueError):
        _finding(signal="")


def test_finding_evidence_is_coerced_to_a_tuple():
    f = _finding(evidence=["a", "b"])
    assert f.evidence == ("a", "b")


def test_cost_signal_requires_a_basis_when_measured():
    with pytest.raises(ValueError):
        scan.CostSignal(measured=True, basis="")
    assert scan.CostSignal(measured=True, basis="ledger row count").measured


# --- store registration ---------------------------------------------------

def test_new_kinds_are_registered_in_exactly_the_right_tables():
    assert sds.KIND_BACKLOG_ITEM in sds.EXTERNAL_KINDS
    assert sds.KIND_TELEMETRY_PATTERN in sds.EXTERNAL_KINDS
    assert sds.KIND_BACKLOG_ITEM in sds.ADVISORY_KINDS
    assert sds.KIND_TELEMETRY_PATTERN in sds.ADVISORY_KINDS
    # never actionable: an improvement-scan finding must never block a turn
    assert sds.KIND_BACKLOG_ITEM not in sds.ACTIONABLE_KINDS
    assert sds.KIND_TELEMETRY_PATTERN not in sds.ACTIONABLE_KINDS
    assert sds.KIND_BACKLOG_ITEM in sds.REMEDIATION
    assert sds.KIND_TELEMETRY_PATTERN in sds.REMEDIATION


def test_store_findings_round_trips_through_the_store(tmp_path):
    store = tmp_path / "findings.jsonl"
    findings = [
        _finding(kind=sds.KIND_BACKLOG_ITEM, signal="core-issue-144", title="quota drain"),
        _finding(kind=sds.KIND_TELEMETRY_PATTERN, signal="repeat-respawn", title="respawn loop"),
    ]
    rows = scan.store_findings(findings, store_path=store)
    assert {r["kind"] for r in rows} == {sds.KIND_BACKLOG_ITEM, sds.KIND_TELEMETRY_PATTERN}
    assert {r["source"] for r in rows} == {sds.SOURCE_IMPROVEMENT_SCAN}
    loaded = sds.load_rows(store)
    assert {r["path"] for r in loaded} == {"core-issue-144", "repeat-respawn"}


def test_source_partitioned_resolve_out_leaves_other_producers_alone(tmp_path):
    """An improvement-scan run must never resolve away self-diagnose's or
    policy-scorecard's rows — and vice versa."""
    store = tmp_path / "findings.jsonl"
    sds.upsert_findings(
        [{"kind": "orphan-leaf", "path": "/mem/x.md", "detail": "d"}],
        store, T0, source=sds.SOURCE_SELF_DIAGNOSE,
    )
    sds.upsert_findings(
        [{"kind": sds.KIND_POLICY_FLAG, "path": "spend-rate/7d", "detail": "d"}],
        store, T0, source=sds.SOURCE_POLICY_SCORECARD,
    )

    scan.store_findings([_finding(signal="only-this-run")], store_path=store)
    # a SECOND, empty improvement-scan run resolves out only its own prior row
    scan.store_findings([], store_path=store)

    remaining = sds.load_rows(store, T0)
    assert {r["source"] for r in remaining} == {
        sds.SOURCE_SELF_DIAGNOSE, sds.SOURCE_POLICY_SCORECARD,
    }


def test_detector_vocabulary_partition_still_holds_with_the_two_new_kinds():
    """The pre-existing vocabulary-partition test in test_self_diagnose_store.py
    covers this at the self-diagnose.py level; this asserts the same equality
    holds from this module's perspective too, so a future edit to either file
    cannot desync them without failing here as well."""
    assert sds.ACTIONABLE_KINDS & sds.ADVISORY_KINDS == frozenset()
    assert {sds.KIND_BACKLOG_ITEM, sds.KIND_TELEMETRY_PATTERN} <= sds.ADVISORY_KINDS
    assert {sds.KIND_BACKLOG_ITEM, sds.KIND_TELEMETRY_PATTERN} <= set(sds.REMEDIATION)


# --- PriorBoard: content-digest-based unchanged detection ------------------

def test_item_digest_is_stable_for_the_same_text_and_status():
    a = scan.item_digest("Nothing reads the transcripts", "open")
    b = scan.item_digest("Nothing reads the transcripts", "open")
    assert a == b


def test_item_digest_differs_on_status_or_text_change():
    base = scan.item_digest("text", "open")
    assert scan.item_digest("text", "closed") != base
    assert scan.item_digest("different text", "open") != base


def test_prior_board_round_trips(tmp_path):
    path = tmp_path / "board.json"
    board = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA,
        generated_at=T0.isoformat(),
        items={
            "core-144": scan.PriorBoardItem(
                classification="telemetry", score=8.0, rank=1,
                source_digest=scan.item_digest("quota drain", "open"),
            )
        },
    )
    scan.write_board(board, path)
    loaded = scan.load_prior_board(path)
    assert loaded.items["core-144"].score == 8.0
    assert loaded.items["core-144"].rank == 1


def test_prior_board_unchanged_detection_ignores_the_run_date(tmp_path):
    """Same text and status, later date -> unchanged: identity is content-
    derived, not date-derived."""
    path = tmp_path / "board.json"
    board = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA,
        generated_at=T0.isoformat(),
        items={
            "core-144": scan.PriorBoardItem(
                classification=None, score=None, rank=None,
                source_digest=scan.item_digest("quota drain narrative", "open"),
            )
        },
    )
    scan.write_board(board, path)

    later = datetime(2026, 8, 1, tzinfo=timezone.utc)
    reloaded = scan.load_prior_board(path)
    assert reloaded.is_unchanged("core-144", "quota drain narrative", "open")
    assert not reloaded.is_unchanged("core-144", "quota drain narrative", "closed")
    assert not reloaded.is_unchanged("core-144", "a rewritten narrative", "open")
    assert not reloaded.is_unchanged("never-seen", "quota drain narrative", "open")
    del later  # only the absence of a date parameter matters here


def test_prior_board_load_fails_open_on_missing_or_corrupt(tmp_path):
    missing = scan.load_prior_board(tmp_path / "nope.json")
    assert missing.items == {}

    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert scan.load_prior_board(corrupt).items == {}


def test_prior_board_load_rejects_a_future_schema(tmp_path):
    path = tmp_path / "board.json"
    path.write_text('{"schema": 999, "generated_at": "x", "items": {}}', encoding="utf-8")
    assert scan.load_prior_board(path).items == {}


# --- LedgerCursor: mtime-gated resume ---------------------------------------

def test_ledger_cursor_is_due_for_a_new_session():
    cur = scan.LedgerCursor()
    assert cur.is_due("sess-a", 100.0)


def test_ledger_cursor_skips_an_unchanged_mtime():
    cur = scan.LedgerCursor()
    cur.mark("sess-a", 100.0)
    assert not cur.is_due("sess-a", 100.0)


def test_ledger_cursor_is_due_again_when_mtime_grows():
    cur = scan.LedgerCursor()
    cur.mark("sess-a", 100.0)
    assert cur.is_due("sess-a", 150.0)


def test_ledger_cursor_round_trips_through_disk(tmp_path):
    path = tmp_path / "cursor.json"
    cur = scan.LedgerCursor()
    cur.mark("sess-a", 100.0)
    cur.mark("sess-b", 200.0)
    cur.save(path)

    reloaded = scan.LedgerCursor.load(path)
    assert not reloaded.is_due("sess-a", 100.0)
    assert not reloaded.is_due("sess-b", 200.0)
    assert reloaded.is_due("sess-a", 101.0)
    assert reloaded.is_due("sess-c", 1.0)


def test_ledger_cursor_load_fails_open_on_missing_or_corrupt(tmp_path):
    missing = scan.LedgerCursor.load(tmp_path / "nope.json")
    assert missing.sessions == {}

    corrupt = tmp_path / "bad.json"
    corrupt.write_text("not json", encoding="utf-8")
    assert scan.LedgerCursor.load(corrupt).sessions == {}


# --- CLI skeleton ------------------------------------------------------------

def test_help_exits_zero_and_lists_the_three_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        scan.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("backlog", "telemetry", "report"):
        assert sub in out


@pytest.mark.parametrize("sub", ["telemetry", "report"])
def test_each_stubbed_subcommand_exits_2_and_names_itself(sub, capsys):
    assert scan.main([sub]) == 2
    err = capsys.readouterr().err
    assert "not implemented in this stage" in err


def test_backlog_with_no_mode_flags_exits_2_and_names_itself(capsys):
    """`backlog` is no longer a stage-2 stub (stage 3 implements it), so it gets
    its own usage-error message rather than the generic "not implemented" one."""
    assert scan.main(["backlog"]) == 2
    err = capsys.readouterr().err
    assert "backlog" in err
    assert "--emit-worklist" in err


def test_no_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        scan.main([])
    assert exc.value.code != 0


# --- purity: never files, never dispatches, no network ----------------------

def test_module_never_shells_out_or_reaches_the_network():
    """The hard invariant this stage's Expected result image requires: an
    improvement-scan run never files a difficulty, never dispatches a
    specialist, and performs no network I/O — all of which would require one
    of the transport-capable roots ast_purity.py already names."""
    assert impure_names(scan) == set()
