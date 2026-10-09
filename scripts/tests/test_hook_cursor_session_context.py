"""Hermetic tests for config + skill catalog injection in hook-cursor-memory-context.py."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK = SCRIPTS_DIR / "hook-cursor-memory-context.py"

sys.path.insert(0, str(SCRIPTS_DIR))


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("hook_cursor_memory_context", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_skill(path: Path, name: str, description: str, body: str = "# Skill body\nSECRET_BODY\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )


@pytest.fixture
def session_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(agent_home))
    return agent_home, project


def test_config_key_value_only_injection(session_tree):
    agent_home, project = session_tree
    config_path = agent_home / "config.md"
    config_path.write_text(
        "\n".join(
            [
                "# Coordination constants",
                "",
                "| Key | Value | Meaning |",
                "|---|---|---|",
                "| `small-change-max-lines` | `20` | Should not appear in injection. |",
                "| `max-recursion-depth` | `5` | Also excluded. |",
            ]
        ),
        encoding="utf-8",
    )

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "small-change-max-lines" in context
    assert "`20`" in context
    assert "max-recursion-depth" in context
    assert "Should not appear" not in context
    assert "Also excluded" not in context
    assert str(config_path.resolve()) in context


def test_missing_config_fail_open(session_tree):
    agent_home, project = session_tree
    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert context == "" or "config.md" not in context.lower()


def test_skill_catalog_without_bodies(session_tree):
    agent_home, project = session_tree
    skill_path = agent_home / "skills" / "overcome-difficulty" / "SKILL.md"
    _write_skill(
        skill_path,
        "overcome-difficulty",
        "TRIGGER on blockers. SKIP when progressing.",
        body="# Body\nSKILL_BODY_MUST_NOT_APPEAR\n",
    )

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "overcome-difficulty" in context
    assert str(skill_path.resolve()) in context
    assert "TRIGGER on blockers" in context
    assert "SKILL_BODY_MUST_NOT_APPEAR" not in context


def test_memory_indexes_still_present(session_tree):
    agent_home, project = session_tree
    project_index = project / ".claude" / "agent-memory" / "MEMORY.md"
    project_index.parent.mkdir(parents=True)
    project_index.write_text("# Project memory\nMEMORY_SENTINEL\n", encoding="utf-8")

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "MEMORY_SENTINEL" in context
    assert "Agent memory indexes" in context


def test_oversized_meaning_cell_not_injected_via_table(session_tree):
    agent_home, project = session_tree
    huge_meaning = "X" * 5000
    config_path = agent_home / "config.md"
    config_path.write_text(
        f"| Key | Value | Meaning |\n|---|---|---|\n| `tiny-key` | `1` | {huge_meaning} |\n",
        encoding="utf-8",
    )

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "tiny-key" in context
    assert huge_meaning not in context


def test_aggregate_bound_trims_skill_catalog(session_tree):
    agent_home, project = session_tree
    config_path = agent_home / "config.md"
    config_path.write_text(
        "| Key | Value | Meaning |\n|---|---|---|\n| `k` | `v` | m |\n",
        encoding="utf-8",
    )
    for index in range(40):
        skill_path = agent_home / "skills" / f"skill-{index:02d}" / "SKILL.md"
        _write_skill(
            skill_path,
            f"skill-{index:02d}",
            "D" * 400,
        )

    mod = _load_hook_module()
    original_cap = mod.MAX_AGGREGATE_BYTES
    mod.MAX_AGGREGATE_BYTES = 4000
    try:
        context = mod.assemble_context([str(project)])
    finally:
        mod.MAX_AGGREGATE_BYTES = original_cap

    assert "Skill catalog" in context
    assert "truncated" in context.lower() or context.count("- **skill-") < 40


def test_valid_json_includes_config_and_skills(session_tree):
    agent_home, project = session_tree
    (agent_home / "config.md").write_text(
        "| Key | Value | Meaning |\n|---|---|---|\n| `small-change-max-lines` | `20` | m |\n",
        encoding="utf-8",
    )
    _write_skill(
        agent_home / "skills" / "planner" / "SKILL.md",
        "planner",
        "TRIGGER for planning.",
    )

    proc = subprocess_run_hook({"workspace_roots": [str(project)]}, agent_home)
    assert proc.returncode == 0
    parsed = json.loads(proc.stdout)
    assert "additional_context" in parsed
    body = parsed["additional_context"]
    assert "small-change-max-lines" in body
    assert "planner" in body


def subprocess_run_hook(payload: dict, agent_home: Path):
    import subprocess

    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={
            "HOME": str(agent_home.parent / "home"),
            "CLAUDE_AGENT_HOME": str(agent_home),
            "PATH": os.environ.get("PATH", ""),
        },
        check=False,
    )
