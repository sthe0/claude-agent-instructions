"""Escape/spawn wrappers refuse outright when AGENTCTL_RUNTIME_HOST names the
OTHER host (Host-isolated spawn plan, step 9): a runtime_host=claude session
must never reach spawn-cursor-escape.py's `agent -p` spawn, and a
runtime_host=cursor session must never reach spawn-specialist.py's `claude -p`
spawn. The refusal fires before argument validation / any subprocess launch.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spawn_cursor_escape_refuses_under_claude_bound_host(monkeypatch, tmp_path):
    mod = _load("spawn-cursor-escape.py", "spawn_cursor_escape_refusal")
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "claude")
    rc = mod.main(["--smoke", "--workspace", str(tmp_path)])
    assert rc == 5


def test_spawn_cursor_escape_proceeds_when_host_is_cursor_or_unset(monkeypatch, tmp_path):
    mod = _load("spawn-cursor-escape.py", "spawn_cursor_escape_refusal_ok")
    monkeypatch.delenv("AGENTCTL_RUNTIME_HOST", raising=False)
    rc = mod.main(["--smoke", "--dry-run", "--workspace", str(tmp_path)])
    assert rc == 0
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "cursor")
    rc = mod.main(["--smoke", "--dry-run", "--workspace", str(tmp_path)])
    assert rc == 0


def test_spawn_specialist_refuses_under_cursor_bound_host(monkeypatch, tmp_path):
    mod = _load("spawn-specialist.py", "spawn_specialist_refusal")
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "cursor")
    plan = tmp_path / "plan.md"
    plan.write_text("plan body", encoding="utf-8")
    rc = mod.main([
        "--kind", "developer", "--plan", str(plan),
        "--done-criterion", "done", "--criterion-type", "measurable",
        "--complexity", "medium",
    ])
    assert rc == 5


def test_spawn_specialist_proceeds_to_normal_validation_when_host_is_claude_or_unset(monkeypatch, tmp_path):
    mod = _load("spawn-specialist.py", "spawn_specialist_refusal_ok")
    monkeypatch.delenv("AGENTCTL_RUNTIME_HOST", raising=False)
    # An unknown --kind fails LATER (unknown-kind, rc=2), proving the refusal
    # check did not short-circuit rc=5 for a claude-bound/unset host.
    plan = tmp_path / "plan.md"
    plan.write_text("plan body", encoding="utf-8")
    rc = mod.main([
        "--kind", "nonexistent-kind-xyz", "--plan", str(plan),
        "--done-criterion", "done", "--criterion-type", "measurable",
        "--complexity", "medium",
    ])
    assert rc == 2
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST", "claude")
    rc = mod.main([
        "--kind", "nonexistent-kind-xyz", "--plan", str(plan),
        "--done-criterion", "done", "--criterion-type", "measurable",
        "--complexity", "medium",
    ])
    assert rc == 2
