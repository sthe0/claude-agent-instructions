"""session-start-digest.sh: auto-memory mirror fallback root resolution, plus
the machine-local VCS-section plugin.

For a project with no in-tree .claude/agent-memory, the digest probes the
auto-memory mirror under <config root>/projects/<sanitized-root>/memory —
read-time resolution (override -> isolated -> legacy), with the legacy
~/.claude/projects/ still honored on a not-yet-migrated machine.

git is the only VCS Core knows; another VCS attaches as an executable at
<config root>/session-digest-vcs.local ($CLAUDE_SESSION_DIGEST_VCS override).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
DIGEST = SCRIPTS / "session-start-digest.sh"


def _run_digest(home: Path, project: Path, vcs_plugin: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR")}
    env["HOME"] = str(home)
    env["CLAUDE_SESSION_DIGEST_VCS"] = str(vcs_plugin) if vcs_plugin else str(home / "absent")
    r = subprocess.run(
        ["bash", str(DIGEST), str(project)],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _write_plugin(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "session-digest-vcs.local"
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _mirror_for(root_dir: Path, project: Path) -> Path:
    san = str(project).replace("/", "-")
    return root_dir / "projects" / san / "memory"


def test_digest_finds_mirror_under_isolated_root(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    mem = _mirror_for(home / ".claude-agent", project)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# index\n")
    out = _run_digest(home, project)
    assert "agent memory (top-level)" in out
    assert "MEMORY.md" in out


def test_digest_falls_back_to_legacy_mirror(tmp_path):
    """Isolated root exists but holds no mirror; the legacy ~/.claude one does."""
    home = tmp_path / "home"
    (home / ".claude-agent").mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    mem = _mirror_for(home / ".claude", project)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# index\n")
    out = _run_digest(home, project)
    assert "agent memory (top-level)" in out
    assert "MEMORY.md" in out


def test_digest_no_memory_section_when_no_mirror(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    out = _run_digest(home, project)
    assert "agent memory (top-level)" not in out


# --- machine-local VCS-section plugin ----------------------------------------

def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def test_plugin_output_replaces_the_git_section(tmp_path):
    """A deployment on another VCS gets its own section and Core's git block
    stays silent — the priority the hardcoded VCS branch used to have."""
    project = _git_project(tmp_path)
    plugin = _write_plugin(tmp_path, '#!/bin/sh\necho "--- othervcs ---"\necho "branch: trunk"\n')
    out = _run_digest(tmp_path / "home", project, vcs_plugin=plugin)
    assert "--- othervcs ---" in out and "branch: trunk" in out
    assert "--- git ---" not in out


def test_plugin_is_passed_the_project_root(tmp_path):
    project = _git_project(tmp_path)
    plugin = _write_plugin(tmp_path, '#!/bin/sh\necho "root=$1"\n')
    assert f"root={project}" in _run_digest(tmp_path / "home", project, vcs_plugin=plugin)


def test_nonzero_plugin_falls_back_to_git(tmp_path):
    """Fail-open: a plugin that errors must not cost the digest its VCS section."""
    project = _git_project(tmp_path)
    plugin = _write_plugin(tmp_path, '#!/bin/sh\necho "half output"\nexit 3\n')
    out = _run_digest(tmp_path / "home", project, vcs_plugin=plugin)
    assert "--- git ---" in out
    assert "half output" not in out


def test_no_plugin_uses_git(tmp_path):
    project = _git_project(tmp_path)
    out = _run_digest(tmp_path / "home", project)
    assert "--- git ---" in out and "branch:" in out
