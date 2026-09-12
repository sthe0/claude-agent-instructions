"""spawn-cursor-specialist.py's --complexity / --model precedence and the
--continue-worktree / --stage-index parity additions with the Claude-side
spawn-specialist.py wrapper (Host-isolated spawn plan, step 8).

All exercised via --dry-run: the wrapper's dry-run path assembles the full
prompt and `agent -p` argv, then exits before touching proc_tree/subprocess,
so these tests never spawn a real child or make a live Cursor API call.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from lib.runtime_models import CURSOR_COMPLEXITY_MODEL

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-cursor-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_cursor_specialist_complexity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_dry(monkeypatch, capsys, tmp_path, extra_argv):
    mod = _load()
    plan = tmp_path / "plan.md"
    plan.write_text("do the thing", encoding="utf-8")
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: developer\n---\n\nspecialization body\n", encoding="utf-8")
    monkeypatch.setattr(mod, "skill_path", lambda kind: skill)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    argv = [
        "spawn-cursor-specialist.py", "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "done", "--criterion-type", "measurable",
        "--workspace", str(tmp_path), "--dry-run",
    ] + extra_argv
    monkeypatch.setattr(sys, "argv", argv)
    rc = mod.main()
    return rc, capsys.readouterr().out


# --- --complexity -> CURSOR_COMPLEXITY_MODEL, --model wins -------------------

@pytest.mark.parametrize("complexity,expected_model", list(CURSOR_COMPLEXITY_MODEL.items()))
def test_complexity_maps_to_cursor_model(monkeypatch, capsys, tmp_path, complexity, expected_model):
    rc, out = _run_dry(monkeypatch, capsys, tmp_path, ["--complexity", complexity])
    assert rc == 0
    if expected_model is None:
        assert "--model" not in out
    else:
        assert f"--model {expected_model}" in out


def test_explicit_model_overrides_complexity(monkeypatch, capsys, tmp_path):
    rc, out = _run_dry(
        monkeypatch, capsys, tmp_path, ["--complexity", "medium", "--model", "gpt-5.3-codex"]
    )
    assert rc == 0
    assert "--model gpt-5.3-codex" in out
    assert f"--model {CURSOR_COMPLEXITY_MODEL['medium']}" not in out


def test_default_model_is_auto_without_complexity_or_model(monkeypatch, capsys, tmp_path):
    rc, out = _run_dry(monkeypatch, capsys, tmp_path, [])
    assert rc == 0
    assert "--model" not in out


def test_unknown_complexity_rejected():
    mod = _load()
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(["--complexity", "extreme"])


# --- --continue-worktree in the assembled prompt (parity with spawn-specialist.py) -

def test_continue_worktree_appears_in_prompt(monkeypatch, capsys, tmp_path):
    rc, out = _run_dry(
        monkeypatch, capsys, tmp_path, ["--continue-worktree", "/repo/.claude/worktrees/demo"]
    )
    assert rc == 0
    assert "Continue the prior stage" in out
    assert "/repo/.claude/worktrees/demo" in out


def test_no_continue_worktree_section_when_unset(monkeypatch, capsys, tmp_path):
    rc, out = _run_dry(monkeypatch, capsys, tmp_path, [])
    assert rc == 0
    assert "Continue the prior stage" not in out


# --- --stage-index accepted and threaded to cost telemetry (parity check) ----

def test_stage_index_accepted_and_defaults_to_none():
    mod = _load()
    args = mod.build_parser().parse_args(["--kind", "developer"])
    assert args.stage_index is None
    args = mod.build_parser().parse_args(["--kind", "developer", "--stage-index", "3"])
    assert args.stage_index == 3
