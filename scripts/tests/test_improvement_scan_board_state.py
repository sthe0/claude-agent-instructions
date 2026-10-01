"""The durable board is a local state file: phase B writes it, phase A / telemetry / report read it by default.

An artifact is a view only — it returned HTTP 451 to the run that had just published it, so it
cannot be the store the next run reads back.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan_board_state", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()


def _state(tmp_path, monkeypatch):
    p = tmp_path / "state" / "board.json"
    monkeypatch.setenv("IMPROVEMENT_SCAN_BOARD_STATE", str(p))
    return p


def _classify(tmp_path, out=None):
    item = {
        "title": "t", "functional_ground": "g", "severity": "high", "source_digest": "d1",
        "breadth": "narrow", "cost_to_resolve": "small", "in_flight": "none",
        "recommended_next_step": "planner", "severity_labeled": True,
    }
    cls = tmp_path / "classifications.json"
    cls.write_text(json.dumps({"items": {"ref-1": item}, "closed_refs": []}), encoding="utf-8")
    args = argparse.Namespace(
        prior=None, classifications=str(cls), worklist=None,
        out=str(out) if out else None, store=str(tmp_path / "store.jsonl"),
    )
    return scan._run_backlog_phase_b(args)


def _phase_a(tmp_path, monkeypatch, prior=None):
    seen = {}
    monkeypatch.setattr(scan, "collect_records", lambda ch: ([], []))
    real_diff = scan.diff_backlog

    def spy(records, board):
        seen["refs"] = set(board.items)
        return real_diff(records, board)

    monkeypatch.setattr(scan, "diff_backlog", spy)
    args = argparse.Namespace(
        prior=prior, channels=["x"], emit_worklist=str(tmp_path / "wl.json")
    )
    return scan._run_backlog_phase_a(args), seen


def test_phase_b_writes_board_state_by_default(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    assert _classify(tmp_path) == 0
    assert "ref-1" in json.loads(state.read_text(encoding="utf-8"))["items"]


def test_phase_b_also_writes_explicit_out(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    out = tmp_path / "view.json"
    assert _classify(tmp_path, out=out) == 0
    assert out.exists() and state.exists()


def test_phase_a_reads_board_state_when_prior_omitted(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    assert _classify(tmp_path) == 0
    rc, seen = _phase_a(tmp_path, monkeypatch)
    assert rc == 0
    assert seen["refs"] == {"ref-1"}


def test_cold_start_without_state_file_is_not_an_error(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path, monkeypatch)
    assert not state.exists()
    rc, seen = _phase_a(tmp_path, monkeypatch)
    assert rc == 0
    assert seen["refs"] == set()
    assert "cold start" in capsys.readouterr().err


def test_explicit_prior_overrides_state_file(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    assert _classify(tmp_path) == 0
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    rc, seen = _phase_a(tmp_path, monkeypatch, prior=str(empty))
    assert rc == 0
    assert seen["refs"] == set()


def test_report_board_defaults_to_state_file(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    parser = scan.build_parser()
    assert parser.parse_args(["report"]).board == str(state)
    assert parser.parse_args(["telemetry", "--grounds", "g"]).board == str(state)
    assert parser.parse_args(["report", "--board", "x.json"]).board == "x.json"
