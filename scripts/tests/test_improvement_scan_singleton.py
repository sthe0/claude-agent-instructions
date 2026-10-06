"""Severity-labeled singletons are scored; unlabeled ones stay no-urgency-signal.

The GitHub adapter defaults an unlabeled issue to MEDIUM, so a parsed record must keep
whether its severity was stated (`severity_labeled`) for the scorer to tell the cases apart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import difficulty_channel as dc  # noqa: E402
from difficulty_channel.adapters import github  # noqa: E402


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_singleton", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

_CONFIG = (
    "| Key | Value |\n|---|---|\n"
    "| `budget-small-usd` | `1.00` |\n| `budget-medium-usd` | `3.00` |\n"
    "| `budget-large-usd` | `8.00` |\n"
)


def _classified(**over):
    c = {
        "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
        "recommended_next_step": "planner", "severity": "high",
        "functional_ground": "a unique ground nobody else shares", "title": "lone item",
    }
    c.update(over)
    return c


def _score(classified, tmp_path):
    cfg = tmp_path / "config.md"
    cfg.write_text(_CONFIG, encoding="utf-8")
    return scan.classify_and_score(scan._empty_board(), classified, [], config_path=cfg)


def test_labeled_singleton_is_scored(tmp_path):
    board, findings, no_urgency = _score(
        {"ref-1": _classified(severity_labeled=True)}, tmp_path
    )
    assert no_urgency == []
    item = board.items["ref-1"]
    assert item.score is not None and item.rank == 1
    assert item.severity_labeled is True
    assert item.cluster_size == 1
    assert [f.source_ref for f in findings] == ["ref-1"]


def test_unlabeled_singleton_has_no_urgency_signal(tmp_path):
    board, findings, no_urgency = _score({"ref-1": _classified()}, tmp_path)
    assert no_urgency == ["ref-1"]
    assert board.items["ref-1"].score is None
    assert board.items["ref-1"].cluster_size == 1
    assert findings == []


def test_clustered_item_score_unchanged_by_label(tmp_path):
    shared = {"functional_ground": "one shared ground", "severity": "medium"}
    a, _f, _n = _score(
        {"a": _classified(**shared), "b": _classified(**shared)}, tmp_path
    )
    b, _f, _n = _score(
        {"a": _classified(severity_labeled=True, **shared), "b": _classified(**shared)},
        tmp_path,
    )
    assert a.items["a"].score == b.items["a"].score
    assert a.items["a"].cluster_size == 2


def _rec(ref, labeled):
    return dc.DifficultyRecord(
        ts="2026-01-01", layer="core", target="T", functional_ground="g " + ref,
        severity=dc.Severity.HIGH, reporter="r", ref=ref, severity_labeled=labeled,
    )


def _prior_item(rec, **over):
    kw = dict(
        classification="no-urgency-signal", score=None, rank=None,
        source_digest=scan.item_digest(scan._backlog_text(rec), "open"),
    )
    kw.update(over)
    return scan.PriorBoardItem(**kw)


def test_prior_unscored_labeled_item_is_rescored():
    labeled, unlabeled = _rec("ref-l", True), _rec("ref-u", False)
    scored = _rec("ref-s", True)
    prior = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA, generated_at="x",
        items={
            "ref-l": _prior_item(labeled),
            "ref-u": _prior_item(unlabeled),
            "ref-s": _prior_item(scored, classification="narrow", score=2.0, rank=1),
        },
    )
    records = [labeled, unlabeled, scored]
    new, changed, unchanged, closed = scan.diff_backlog(records, prior)
    assert sorted(unchanged) == ["ref-l", "ref-s", "ref-u"]

    rescore = scan.rescore_candidates(records, prior)
    assert [ref for ref, _r in rescore] == ["ref-l"]

    wl = scan.build_worklist(new, changed, [], closed, rescore_items=rescore)
    assert [(i["item_ref"], i["bucket"]) for i in wl["items"]] == [("ref-l", "rescore")]
    assert wl["items"][0]["severity_labeled"] is True



def test_prior_unjudged_item_is_rescored_once_labeled():
    rec = _rec("ref-j", True)
    prior = scan.PriorBoard(
        schema=scan.BOARD_SCHEMA, generated_at="x",
        items={"ref-j": _prior_item(rec, classification="unjudged")},
    )
    assert [ref for ref, _r in scan.rescore_candidates([rec], prior)] == ["ref-j"]

def test_worklist_items_carry_severity_labeled():
    wl = scan.build_worklist([("ref-n", _rec("ref-n", True))], [], [], [])
    assert wl["items"][0]["severity_labeled"] is True


def test_board_round_trips_severity_labeled_and_cluster_size(tmp_path):
    board, _f, _n = _score({"ref-1": _classified(severity_labeled=True)}, tmp_path)
    path = tmp_path / "board.json"
    scan.write_board(board, path)
    loaded = scan.load_prior_board(path).items["ref-1"]
    assert loaded.severity_labeled is True and loaded.cluster_size == 1


def test_adapter_sets_severity_labeled_only_on_a_severity_label():
    labeled = github._issue_to_record(
        {"title": "[core] x", "labels": [{"name": "severity:high"}], "body": ""}
    )
    bare = github._issue_to_record({"title": "[core] x", "labels": [], "body": ""})
    assert labeled.severity is dc.Severity.HIGH and labeled.severity_labeled is True
    assert bare.severity is dc.Severity.MEDIUM and bare.severity_labeled is False
