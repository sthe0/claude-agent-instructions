"""improvement-scan phase B clusters backlog items by a judged YES, and parks undecided items as unjudged."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
VERDICTS_ENV = "CLAUDE_SAME_DIFFICULTY_VERDICTS"

_CONFIG = (
    "| Key | Value |\n|---|---|\n"
    "| `budget-small-usd` | `1.00` |\n| `budget-medium-usd` | `3.00` |\n"
    "| `budget-large-usd` | `8.00` |\n"
)

GROUND_A = "authentication token expiry not handled on reconnect"
GROUND_B = "token expiry for authentication unhandled when reconnecting"


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_cluster_judged", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()


class Judge:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, a, b, timeout=None):
        self.calls.append((a, b, timeout))
        return self.answer, ""


def _env(monkeypatch, tmp_path, judge, budget):
    monkeypatch.setattr(importlib.import_module("lib.semantic_join"), "default_judge", judge)
    monkeypatch.setenv(BUDGET_ENV, budget)
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "verdicts.json"))


def _item(title, ground):
    return {
        "title": title, "functional_ground": ground, "severity": "high",
        "source_digest": f"d-{title}", "breadth": "narrow", "cost_to_resolve": "small",
        "in_flight": "none", "recommended_next_step": "planner", "severity_labeled": False,
    }


def _score(tmp_path):
    cfg = tmp_path / "config.md"
    cfg.write_text(_CONFIG, encoding="utf-8")
    classified = {"ref-1": _item("one", GROUND_A), "ref-2": _item("two", GROUND_B)}
    return scan.classify_and_score(scan._empty_board(), classified, [], config_path=cfg)


def test_backlog_cluster_follows_judge(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, Judge(True), "300")
    board, _findings, no_urgency = _score(tmp_path)
    assert no_urgency == []
    assert {r: i.cluster_size for r, i in board.items.items()} == {"ref-1": 2, "ref-2": 2}

    _env(monkeypatch, tmp_path / "other", Judge(False), "300")
    (tmp_path / "other").mkdir()
    board, _findings, no_urgency = _score(tmp_path / "other")
    assert sorted(no_urgency) == ["ref-1", "ref-2"]
    assert {r: i.cluster_size for r, i in board.items.items()} == {"ref-1": 1, "ref-2": 1}


def test_unjudged_backlog_item_marked_unjudged(monkeypatch, tmp_path, capsys):
    judge = Judge(True)
    _env(monkeypatch, tmp_path, judge, "0")
    state = tmp_path / "state" / "board.json"
    monkeypatch.setenv("IMPROVEMENT_SCAN_BOARD_STATE", str(state))
    cls = tmp_path / "classifications.json"
    cls.write_text(json.dumps({
        "items": {"ref-1": _item("one", GROUND_A), "ref-2": _item("two", GROUND_B)},
        "closed_refs": [],
    }), encoding="utf-8")
    args = argparse.Namespace(
        prior=None, classifications=str(cls), worklist=None, out=None,
        store=str(tmp_path / "store.jsonl"), dry_run=False,
    )

    assert scan._run_backlog_phase_b(args) == 0

    items = json.loads(state.read_text(encoding="utf-8"))["items"]
    assert {ref: items[ref].get("unjudged") for ref in items} == {"ref-1": True, "ref-2": True}
    assert {items[ref]["classification"] for ref in items} == {"unjudged"}
    assert {items[ref]["cluster_size"] for ref in items} == {1}
    assert judge.calls == []
    out = capsys.readouterr().out
    assert "cluster judge: judged_calls=0 cached_hits=0 identity_joins=0 unjudged_items=1" in out
