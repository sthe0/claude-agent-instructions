"""Machine-local project-registry root resolution (projects.py / projects.sh).

The machine-local registry moved from the hardcoded ~/.claude/projects.d to
<config root>/projects.d with a legacy read fallback for not-yet-migrated
machines. Both seams must agree: python's _default_roots and shell's
_projects_local_dir resolve override -> isolated -> legacy the same way.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from project_entry.projects import _default_roots

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECTS_SH = SCRIPTS / "project_entry" / "projects.sh"


# ── python: _default_roots machine-local resolution ───────────────────────────

def _roots(tmp_path: Path, **env: str) -> "list[str]":
    ev = {"HOME": str(tmp_path), **env}
    return _default_roots(lambda k: ev.get(k))


def test_py_explicit_roots_win(tmp_path):
    got = _roots(tmp_path, CLAUDE_PROJECT_ROOTS=f"/a{os.pathsep}/b")
    assert got == ["/a", "/b"]


def test_py_local_dir_override_wins(tmp_path):
    (tmp_path / ".claude-agent").mkdir()
    got = _roots(tmp_path, CLAUDE_PROJECTS_LOCAL_DIR="/custom/reg")
    assert got == ["/custom/reg"]


def test_py_config_dir_resolves_local_root(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "projects.d").mkdir(parents=True)
    got = _roots(tmp_path, CLAUDE_CONFIG_DIR=str(cfg))
    assert got == [str(cfg / "projects.d")]


def test_py_isolated_root_when_present(tmp_path):
    (tmp_path / ".claude-agent" / "projects.d").mkdir(parents=True)
    assert _roots(tmp_path) == [str(tmp_path / ".claude-agent" / "projects.d")]


def test_py_legacy_root_when_no_isolated(tmp_path):
    assert _roots(tmp_path) == [str(tmp_path / ".claude" / "projects.d")]


def test_py_legacy_fallback_for_unmigrated_registry(tmp_path):
    """Isolated root exists but its projects.d does not; the legacy one does —
    the not-yet-migrated registry must still be found."""
    (tmp_path / ".claude-agent").mkdir()
    (tmp_path / ".claude" / "projects.d").mkdir(parents=True)
    assert _roots(tmp_path) == [str(tmp_path / ".claude" / "projects.d")]


def test_py_current_root_wins_over_legacy_when_both_exist(tmp_path):
    (tmp_path / ".claude-agent" / "projects.d").mkdir(parents=True)
    (tmp_path / ".claude" / "projects.d").mkdir(parents=True)
    assert _roots(tmp_path) == [str(tmp_path / ".claude-agent" / "projects.d")]


def test_py_shared_root_ordered_first(tmp_path):
    (tmp_path / ".claude-agent" / "projects.d").mkdir(parents=True)
    got = _roots(tmp_path, CLAUDE_PROJECTS_DIR="/shared/projects")
    assert got == ["/shared/projects",
                   str(tmp_path / ".claude-agent" / "projects.d")]


# ── shell: _projects_local_dir mirrors the python resolution ──────────────────

def _sh_local_dir(tmp_path: Path, **env: str) -> str:
    ev = {k: v for k, v in os.environ.items()
          if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_CONFIG_DIR",
                       "CLAUDE_PROJECTS_LOCAL_DIR")}
    ev["HOME"] = str(tmp_path)
    ev.update(env)
    r = subprocess.run(
        ["bash", "-c", f'source "{PROJECTS_SH}" && _projects_local_dir'],
        env=ev, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_sh_local_dir_override_wins(tmp_path):
    got = _sh_local_dir(tmp_path, CLAUDE_PROJECTS_LOCAL_DIR="/custom/reg")
    assert got == "/custom/reg"


def test_sh_isolated_root_when_present(tmp_path):
    (tmp_path / ".claude-agent" / "projects.d").mkdir(parents=True)
    assert _sh_local_dir(tmp_path) == str(tmp_path / ".claude-agent" / "projects.d")


def test_sh_legacy_fallback_for_unmigrated_registry(tmp_path):
    (tmp_path / ".claude-agent").mkdir()
    (tmp_path / ".claude" / "projects.d").mkdir(parents=True)
    assert _sh_local_dir(tmp_path) == str(tmp_path / ".claude" / "projects.d")


def test_sh_current_root_path_when_neither_exists(tmp_path):
    """Nothing on disk: report the read root's projects.d (register creates it)."""
    (tmp_path / ".claude-agent").mkdir()
    assert _sh_local_dir(tmp_path) == str(tmp_path / ".claude-agent" / "projects.d")
