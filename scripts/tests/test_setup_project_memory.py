"""Installer test for setup-project-memory.sh's isolated-root placement.

Runs the real script via subprocess with HOME/CLAUDE_AGENT_HOME redirected to
tmp_path trees. Verifies the per-cwd native-memory symlink lands under
CLAUDE_AGENT_HOME/projects/<hash>/memory (never under $HOME/.claude), that a
populated legacy native dir is migrated rather than orphaned, and that an
existing legacy in-place symlink is re-pointed rather than left dangling.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "setup-project-memory.sh"


def _hash(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _run(project_cwd: Path, home: Path, agent_home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "CLAUDE_AGENT_HOME": str(agent_home)}
    return subprocess.run(
        ["bash", str(SCRIPT), str(project_cwd)],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    agent_home = tmp_path / "agent-root"
    project_cwd = tmp_path / "myproject"
    project_cwd.mkdir()
    return home, agent_home, project_cwd


def test_fresh_isolated_placement_touches_nothing_under_home_claude(tree):
    home, agent_home, project_cwd = tree
    result = _run(project_cwd, home, agent_home)
    assert result.returncode == 0, result.stderr

    h = _hash(project_cwd)
    target = agent_home / "projects" / h / "memory"
    agent_memory = project_cwd / ".claude" / "agent-memory"

    assert target.is_symlink()
    assert target.resolve() == agent_memory.resolve()
    assert (agent_memory / "MEMORY.md").exists()
    assert not (home / ".claude").exists()


def test_idempotent(tree):
    home, agent_home, project_cwd = tree
    assert _run(project_cwd, home, agent_home).returncode == 0
    result = _run(project_cwd, home, agent_home)
    assert result.returncode == 0, result.stderr
    assert "already linked" in result.stdout


def test_migrates_populated_legacy_native_dir_into_tree(tree):
    home, agent_home, project_cwd = tree
    h = _hash(project_cwd)
    legacy_target = home / ".claude" / "projects" / h / "memory"
    legacy_target.mkdir(parents=True)
    (legacy_target / "MEMORY.md").write_text("# legacy content\n")
    (legacy_target / "leaf.md").write_text("leaf\n")

    result = _run(project_cwd, home, agent_home)
    assert result.returncode == 0, result.stderr

    agent_memory = project_cwd / ".claude" / "agent-memory"
    assert (agent_memory / "MEMORY.md").read_text() == "# legacy content\n"
    assert (agent_memory / "leaf.md").read_text() == "leaf\n"

    # Isolated target now symlinks to the tree.
    target = agent_home / "projects" / h / "memory"
    assert target.is_symlink()
    assert target.resolve() == agent_memory.resolve()

    # The legacy dir was backed up, not deleted, and no longer holds a bare dir.
    legacy_dir = home / ".claude" / "projects" / h
    backups = list(legacy_dir.glob("memory.premigrate.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / "leaf.md").read_text() == "leaf\n"


def test_repoints_existing_legacy_symlink_instead_of_orphaning(tree):
    home, agent_home, project_cwd = tree
    h = _hash(project_cwd)
    agent_memory = project_cwd / ".claude" / "agent-memory"
    agent_memory.mkdir(parents=True)
    (agent_memory / "MEMORY.md").write_text("# tree content\n")

    legacy_projects_dir = home / ".claude" / "projects" / h
    legacy_projects_dir.mkdir(parents=True)
    legacy_target = legacy_projects_dir / "memory"
    legacy_target.symlink_to(agent_memory)

    result = _run(project_cwd, home, agent_home)
    assert result.returncode == 0, result.stderr

    target = agent_home / "projects" / h / "memory"
    assert target.is_symlink()
    assert target.resolve() == agent_memory.resolve()

    # Legacy symlink kept working, re-pointed at the same tree.
    assert legacy_target.is_symlink()
    assert legacy_target.resolve() == agent_memory.resolve()


def test_refuses_when_both_native_and_tree_hold_content(tree):
    home, agent_home, project_cwd = tree
    h = _hash(project_cwd)
    agent_memory = project_cwd / ".claude" / "agent-memory"
    agent_memory.mkdir(parents=True)
    (agent_memory / "MEMORY.md").write_text("# tree content\n")

    legacy_target = home / ".claude" / "projects" / h / "memory"
    legacy_target.mkdir(parents=True)
    (legacy_target / "MEMORY.md").write_text("# legacy content\n")

    result = _run(project_cwd, home, agent_home)
    assert result.returncode == 1
    assert "refuse" in result.stderr


def test_refuses_on_home_as_project_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    agent_home = tmp_path / "agent-root"
    result = _run(home, home, agent_home)
    assert result.returncode == 1
    assert "refuse" in result.stderr
