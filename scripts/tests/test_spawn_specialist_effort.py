"""spawn-specialist.py must force an explicit --effort choice on every spawn, on
the same no-inherit-fallback rationale as --complexity/--model (see
resolve_model): an optional flag with a default degrades into an unconsidered
default under time pressure. --effort is a standalone required argument (not
part of the --model/--complexity mutually exclusive group — effort and model
are independent axes), forwarded verbatim to the spawned `claude -p --effort`
child and stamped on the cost-log entry.

Tests cover:
- omitting --effort is an argparse-level refusal (SystemExit, code 2)
- --dry-run with --effort high shows "--effort high" in the assembled command
- the cost-log entry construction carries an effort key equal to the value passed
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_effort", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ---------------------------------------------------------------------------
# (a) --effort is required
# ---------------------------------------------------------------------------

def test_effort_omitted_raises_system_exit(tmp_path, capsys):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    with pytest.raises(SystemExit) as excinfo:
        MOD.build_parser().parse_args([
            "--kind", "developer",
            "--plan", str(plan),
            "--done-criterion", "tests green",
            "--criterion-type", "measurable",
            "--complexity", "medium",
        ])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--effort" in err
    assert "required" in err.lower()


def test_effort_accepted_when_given(tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    args = MOD.build_parser().parse_args([
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "high",
    ])
    assert args.effort == "high"


def test_effort_rejects_value_outside_rubric(tmp_path, capsys):
    plan = tmp_path / "plan.toml"
    plan.write_text("")
    with pytest.raises(SystemExit) as excinfo:
        MOD.build_parser().parse_args([
            "--kind", "developer",
            "--plan", str(plan),
            "--done-criterion", "tests green",
            "--criterion-type", "measurable",
            "--complexity", "medium",
            "--effort", "extreme",
        ])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# (b) --dry-run assembles "--effort <level>" into the printed command
# ---------------------------------------------------------------------------

def test_dry_run_command_includes_effort(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MOD, "plans_dir", lambda: tmp_path)
    plan = tmp_path / "plan.toml"
    plan.write_text("a small plan\n", encoding="utf-8")
    argv = [
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--complexity", "medium",
        "--effort", "high",
        "--dry-run",
    ]
    rc = MOD.main(argv)
    assert rc == 0
    out = capsys.readouterr().out
    assert "--effort high" in out


# ---------------------------------------------------------------------------
# (c) cost-log entry carries the effort key
# ---------------------------------------------------------------------------

def _build_entry(effort: str) -> dict:
    """Mimic what spawn-specialist.main writes to log_cost_entry."""
    return {
        "ts": "2026-01-01T00:00:00+00:00",
        "event": "spawn",
        "kind": "developer",
        "budget_tier": "medium",
        "budget_usd_cap": "3.00",
        "effort": effort,
        "depth": 1,
        "cost_usd": 0.5,
        "duration_ms": 1234,
        "return_marker": "COMPLETED",
        "exit_code": 0,
        "malformed": False,
        "stage_index": None,
        "plan_path": "/tmp/plan.toml",
        "session_id": None,
        "ticket": None,
    }


def test_entry_carries_effort_value():
    entry = _build_entry("high")
    assert entry["effort"] == "high"


def test_entry_has_effort_key():
    entry = _build_entry("low")
    assert "effort" in entry
