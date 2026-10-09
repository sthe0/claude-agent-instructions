"""Hermetic tests for hook-cursor-memory-context.py."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK = SCRIPTS_DIR / "hook-cursor-memory-context.py"
REGISTRY = SCRIPTS_DIR / "hooks" / "desired.json"

sys.path.insert(0, str(SCRIPTS_DIR))

from lib import hook_registry  # noqa: E402


def _hash(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("hook_cursor_memory_context", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook(payload: dict, env: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def memory_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(agent_home))
    return home, agent_home, project


def test_global_only(memory_tree):
    _home, agent_home, project = memory_tree
    global_index = agent_home / "memory-global" / "MEMORY.md"
    global_index.parent.mkdir(parents=True)
    global_index.write_text("# Global index\n- [leaf](leaves/foo.md)\n", encoding="utf-8")

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "Global index" in context
    assert str(global_index) in context
    assert "leaves/foo.md" in context
    assert "Leaf files and skill bodies are not embedded" in context


def test_project_and_global(memory_tree):
    _home, agent_home, project = memory_tree
    global_index = agent_home / "memory-global" / "MEMORY.md"
    global_index.parent.mkdir(parents=True)
    global_index.write_text("# Global\n", encoding="utf-8")

    project_index = project / ".claude" / "agent-memory" / "MEMORY.md"
    project_index.parent.mkdir(parents=True)
    project_index.write_text("# Project sentinel\nPROJECT_SENTINEL\n", encoding="utf-8")

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "# Global" in context
    assert "PROJECT_SENTINEL" in context
    assert context.count("Source:") >= 2


def test_distinct_personal_memory(memory_tree):
    _home, agent_home, project = memory_tree
    personal_index = agent_home / "projects" / _hash(project) / "memory" / "MEMORY.md"
    personal_index.parent.mkdir(parents=True)
    personal_index.write_text("# Personal sentinel\nPERSONAL_SENTINEL\n", encoding="utf-8")

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "PERSONAL_SENTINEL" in context
    assert "### personal memory index" in context


def test_symlink_deduplication(memory_tree):
    _home, agent_home, project = memory_tree
    project_index = project / ".claude" / "agent-memory" / "MEMORY.md"
    project_index.parent.mkdir(parents=True)
    project_index.write_text("# Shared index\nSHARED_ONCE\n", encoding="utf-8")

    personal_dir = agent_home / "projects" / _hash(project) / "memory"
    personal_dir.parent.mkdir(parents=True, exist_ok=True)
    personal_dir.symlink_to(project_index.parent)

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert context.count("SHARED_ONCE") == 1


def test_missing_optional_indexes_still_valid_json(memory_tree, monkeypatch):
    _home, agent_home, project = memory_tree
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    proc = _run_hook(
        {"workspace_roots": [str(project)], "session_id": "sess-1"},
        {"HOME": str(_home), "PATH": os.environ.get("PATH", "")},
    )
    assert proc.returncode == 0
    parsed = json.loads(proc.stdout)
    assert isinstance(parsed, dict)
    assert "additional_context" not in parsed or parsed.get("additional_context", "") == ""


def test_boundary_skips_oversized_index(memory_tree):
    _home, agent_home, project = memory_tree
    global_index = agent_home / "memory-global" / "MEMORY.md"
    global_index.parent.mkdir(parents=True)
    global_index.write_text("\n".join(f"line {index}" for index in range(250)), encoding="utf-8")

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "skipped" in context
    assert "250 lines" in context
    assert "line 0" not in context


def test_no_leaf_body_expansion(memory_tree):
    _home, agent_home, project = memory_tree
    project_index = project / ".claude" / "agent-memory" / "MEMORY.md"
    leaf = project_index.parent / "leaves" / "secret.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("LEAF_BODY_SHOULD_NOT_APPEAR\n", encoding="utf-8")
    project_index.write_text("# Index\n- [secret](leaves/secret.md)\n", encoding="utf-8")

    mod = _load_hook_module()
    context = mod.assemble_context([str(project)])
    assert "LEAF_BODY_SHOULD_NOT_APPEAR" not in context
    assert "leaves/secret.md" in context


def test_valid_json_output(memory_tree):
    _home, agent_home, project = memory_tree
    project_index = project / ".claude" / "agent-memory" / "MEMORY.md"
    project_index.parent.mkdir(parents=True)
    project_index.write_text("# JSON sentinel\nJSON_OK\n", encoding="utf-8")

    proc = _run_hook(
        {"workspace_roots": [str(project)], "session_id": "sess-json"},
        {
            "HOME": str(_home),
            "CLAUDE_AGENT_HOME": str(agent_home),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    assert proc.returncode == 0
    parsed = json.loads(proc.stdout)
    assert "additional_context" in parsed
    assert "JSON_OK" in parsed["additional_context"]


def test_claude_installer_excludes_cursor_only_row():
    rows = hook_registry.claude_desired_tuples()
    commands = [command for _event, _matcher, command, _timeout in rows]
    assert "hook-cursor-memory-context.py" not in " ".join(commands)

    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    memory_rows = [row for row in data["hooks"] if row.get("id") == "cursor-memory-context"]
    assert len(memory_rows) == 1
    assert memory_rows[0].get("claude_skip_reason")
    assert not memory_rows[0].get("claude_event")


def test_direct_install_expectations_include_memory_hook():
    rows = hook_registry.cursor_direct_install_expectations()
    markers = {row["managed_marker"] for row in rows}
    assert "hook-cursor-memory-context.py" in markers
