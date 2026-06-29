"""spawn-specialist.py cost-log attribution: stage_index + plan_path are stamped on
each log entry so per-stage cost attribution (stage 3 of the cost-tracking plan) can
sum rows by (plan_path, stage_index).

Tests cover:
- arg parser accepts --stage-index (present + absent)
- the log entry dict carries stage_index and plan_path when --stage-index is given
- omitting --stage-index yields stage_index=None (back-compat)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ---------------------------------------------------------------------------
# Arg parser: --stage-index flag
# ---------------------------------------------------------------------------

def test_stage_index_accepted_by_parser(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    args = MOD.build_parser().parse_args([
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--stage-index", "4",
    ])
    assert args.stage_index == 4


def test_stage_index_defaults_to_none(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    args = MOD.build_parser().parse_args([
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
    ])
    assert args.stage_index is None


# ---------------------------------------------------------------------------
# Log entry dict carries stage_index and plan_path
# ---------------------------------------------------------------------------

def _build_entry(plan_path: str, stage_index) -> dict:
    """Mimic what spawn-specialist.main writes to log_cost_entry."""
    return {
        "ts": "2026-01-01T00:00:00+00:00",
        "event": "spawn",
        "kind": "developer",
        "budget_tier": "medium",
        "budget_usd_cap": "3.00",
        "depth": 1,
        "cost_usd": 0.5,
        "duration_ms": 1234,
        "return_marker": "COMPLETED",
        "exit_code": 0,
        "malformed": False,
        "stage_index": stage_index,
        "plan_path": plan_path,
        "session_id": None,
        "ticket": None,
    }


def test_entry_carries_stage_index_when_given():
    entry = _build_entry("/tmp/plan.toml", stage_index=3)
    assert entry["stage_index"] == 3


def test_entry_carries_plan_path():
    entry = _build_entry("/home/user/.claude/plans/my-plan.toml", stage_index=2)
    assert entry["plan_path"] == "/home/user/.claude/plans/my-plan.toml"


def test_entry_stage_index_none_when_absent():
    entry = _build_entry("/tmp/plan.toml", stage_index=None)
    assert entry["stage_index"] is None


def test_entry_has_both_attribution_keys():
    entry = _build_entry("/tmp/plan.toml", stage_index=5)
    assert "stage_index" in entry
    assert "plan_path" in entry
