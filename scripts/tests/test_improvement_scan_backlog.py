"""Tests for improvement-scan.py's `backlog` subcommand: the cross-source
collection producer (Phase A) and the deterministic scoring half of the
triage rubric (Phase B).

Loaded by path exactly like test_improvement_scan.py; see that file's
docstring for why (the module's filename carries a dash).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from ast_purity import impure_names  # noqa: E402
from difficulty_channel import DifficultyChannel, DifficultyRecord, Severity, register_channel  # noqa: E402
from difficulty_channel.port import StreamUnsupported  # noqa: E402


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()


def _rec(ts, target, ground, ref, evidence="ev", cost="not estimable: n/a", reporter="agent"):
    return DifficultyRecord(
        ts=ts, layer="core", target=target, functional_ground=ground,
        severity=Severity.MEDIUM, reporter=reporter, evidence=evidence,
        cost_estimate=cost, ref=ref,
    )


def _write_config(tmp_path):
    p = tmp_path / "config.md"
    p.write_text(
        "| Key | Value |\n"
        "|---|---|\n"
        "| `budget-small-usd` | `1.00` |\n"
        "| `budget-medium-usd` | `3.00` |\n"
        "| `budget-large-usd` | `8.00` |\n",
        encoding="utf-8",
    )
    return p


# --- (1) four-bucket diff: content, not date, decides unchanged vs changed --

def test_four_bucket_diff_uses_content_not_date():
    prior = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA,
        generated_at="x",
        items={
            "ref-a": scan.PriorBoardItem(
                classification="narrow", score=1.0, rank=1,
                source_digest=scan.item_digest(
                    scan._backlog_text(_rec("2026-01-01", "T-A", "ground A", "ref-a")), "open"
                ),
            ),
            "ref-b": scan.PriorBoardItem(
                classification="narrow", score=1.0, rank=2,
                source_digest=scan.item_digest(
                    scan._backlog_text(_rec("2026-01-01", "T-B", "ground B", "ref-b")), "open"
                ),
            ),
        },
    )
    # ref-a: date changes, text identical -> unchanged.
    rec_a_new_date = _rec("2026-02-02", "T-A", "ground A", "ref-a")
    # ref-b: text changes, date identical -> changed.
    rec_b_new_text = _rec("2026-01-01", "T-B-edited", "ground B", "ref-b")

    new_items, changed_items, unchanged_refs, closed_refs = scan.diff_backlog(
        [rec_a_new_date, rec_b_new_text], prior
    )

    assert unchanged_refs == ["ref-a"]
    assert [ref for ref, _r in changed_items] == ["ref-b"]
    assert new_items == []
    assert closed_refs == []


def test_four_bucket_diff_new_and_closed():
    prior = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA,
        generated_at="x",
        items={
            "ref-gone": scan.PriorBoardItem(classification="narrow", score=1.0, rank=1, source_digest="whatever"),
        },
    )
    rec_new = _rec("2026-01-01", "T-New", "ground new", "ref-new")
    new_items, changed_items, unchanged_refs, closed_refs = scan.diff_backlog([rec_new], prior)
    assert [ref for ref, _r in new_items] == ["ref-new"]
    assert changed_items == []
    assert unchanged_refs == []
    assert closed_refs == ["ref-gone"]


# --- (2) unchanged item: verbatim carry-forward, no reclassification --------

def test_unchanged_item_carried_forward_without_reclassification():
    prior_item = scan.PriorBoardItem(
        classification="narrow", score=3.5, rank=1, source_digest="deadbeef",
        title="carried title", functional_ground="carried ground", evidence=(),
        recommended_next_step="planner", blocked_by=(),
    )
    prior = scan.PriorBoard(schema=scan.BOARD_SCHEMA, generated_at="x", items={"ref-x": prior_item})

    # classified is empty: ref-x is never looked up in it, so an out-of-vocabulary
    # value there could never even be reached — proof no classifier call happens.
    board, findings, no_urgency = scan.classify_and_score(prior, classified={}, closed_refs=[])

    assert board.items["ref-x"].classification == "narrow"
    assert board.items["ref-x"].score == 3.5
    assert board.items["ref-x"].rank == 1
    assert no_urgency == []
    assert len(findings) == 1
    assert findings[0].source_ref == "ref-x"
    assert findings[0].title == "carried title"


# --- (3) coverage_gaps: StreamUnsupported AND a generic exception -----------

class _UnsupportedBacklogChannel(DifficultyChannel):
    def submit(self, record):
        return "id"

    def pull(self, since=None):
        return []

    def pull_stream(self, stream="report", since=None):
        if stream == "backlog":
            raise StreamUnsupported("no backlog stream here")
        return [_rec("2026-01-01", "T", "ground ok", "ref-ok")]


class _AlwaysFailsChannel(DifficultyChannel):
    def submit(self, record):
        return "id"

    def pull(self, since=None):
        return []

    def pull_stream(self, stream="report", since=None):
        raise RuntimeError("kaboom")


def test_coverage_gaps_distinguish_unsupported_from_failure_and_never_abort():
    register_channel("test-unsupported-backlog", _UnsupportedBacklogChannel)
    register_channel("test-always-fails", _AlwaysFailsChannel)

    records, gaps = scan.collect_records(
        ["test-unsupported-backlog", "test-always-fails", "test-unregistered-channel"]
    )

    assert [r.ref for r in records] == ["ref-ok"]  # the reachable stream still collected

    by_pair = {(g["channel"], g["stream"]): g["reason"] for g in gaps}
    assert by_pair[("test-unsupported-backlog", "backlog")] == "unsupported"
    assert ("test-unsupported-backlog", "report") not in by_pair
    assert by_pair[("test-always-fails", "report")].startswith("collection-failed:")
    assert by_pair[("test-always-fails", "backlog")].startswith("collection-failed:")
    assert by_pair[("test-unregistered-channel", "report")].startswith("collection-failed:")
    assert by_pair[("test-unregistered-channel", "backlog")].startswith("collection-failed:")


# --- (4) scoring arithmetic against a hand-computed table -------------------

def test_scoring_arithmetic_matches_hand_computed_table(tmp_path):
    config_path = _write_config(tmp_path)
    score = scan.score_item("universal", 5, "medium", "clear-direction", config_path=config_path)
    assert score == pytest.approx(8 * 5 / (3.00 / 0.5))


# --- (5) hard partial order overrides a higher score ------------------------

def test_hard_partial_order_overrides_higher_score(tmp_path):
    config_path = _write_config(tmp_path)
    shared_ground = "a shared functional ground both items sit on"
    classified = {
        "ref-high": {
            "breadth": "universal", "cost_to_resolve": "small", "in_flight": "none",
            "recommended_next_step": "planner", "severity": "high",
            "functional_ground": shared_ground, "title": "high score item",
            "blocked_by": ["ref-low"],
        },
        "ref-low": {
            "breadth": "narrow", "cost_to_resolve": "large", "in_flight": "none",
            "recommended_next_step": "planner", "severity": "low",
            "functional_ground": shared_ground, "title": "low score item",
        },
    }
    board, findings, no_urgency = scan.classify_and_score(
        scan._empty_board(), classified, closed_refs=[], config_path=config_path
    )
    assert no_urgency == []
    assert board.items["ref-high"].score > board.items["ref-low"].score
    assert board.items["ref-low"].rank < board.items["ref-high"].rank


# --- (6) no-urgency-signal section -------------------------------------------

def test_no_urgency_signal_section(tmp_path):
    config_path = _write_config(tmp_path)
    classified = {
        "ref-lonely": {
            "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
            "recommended_next_step": "planner", "severity": "low",
            "functional_ground": "a truly unique ground nobody else shares",
            "title": "lonely item",
        },
    }
    board, findings, no_urgency = scan.classify_and_score(
        scan._empty_board(), classified, closed_refs=[], config_path=config_path
    )
    assert no_urgency == ["ref-lonely"]
    assert board.items["ref-lonely"].score is None
    assert board.items["ref-lonely"].rank is None
    assert board.items["ref-lonely"].classification == "no-urgency-signal"
    assert findings == []


# --- (7) out-of-vocabulary classification value rejected --------------------

def test_out_of_vocabulary_classification_is_rejected(tmp_path):
    config_path = _write_config(tmp_path)
    classified = {
        "ref-x": {
            "breadth": "gigantic",  # not in BREADTH_WEIGHTS
            "cost_to_resolve": "small", "in_flight": "none",
            "recommended_next_step": "planner", "severity": "low",
            "functional_ground": "g", "title": "t",
        },
    }
    with pytest.raises(ValueError):
        scan.classify_and_score(scan._empty_board(), classified, closed_refs=[], config_path=config_path)


# --- (8) neither phase writes to any tracker ---------------------------------

def test_backlog_never_shells_out_or_reaches_the_network():
    for fn in (
        scan.collect_records,
        scan._run_backlog_phase_a,
        scan._run_backlog_phase_b,
        scan.classify_and_score,
        scan.diff_backlog,
        scan.build_worklist,
        scan._cmd_backlog,
    ):
        assert impure_names(fn) == set()
