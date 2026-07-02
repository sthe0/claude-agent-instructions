"""session-start-digest.sh: auto-memory mirror fallback root resolution.

For a project with no in-tree .claude/agent-memory, the digest probes the
auto-memory mirror under <config root>/projects/<sanitized-root>/memory —
read-time resolution (override -> isolated -> legacy), with the legacy
~/.claude/projects/ still honored on a not-yet-migrated machine.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
DIGEST = SCRIPTS / "session-start-digest.sh"


def _run_digest(home: Path, project: Path) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR")}
    env["HOME"] = str(home)
    r = subprocess.run(
        ["bash", str(DIGEST), str(project)],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


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
