"""Tests for scripts/lib/config-root.sh — the CLAUDE_AGENT_HOME resolver."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RESOLVER = SCRIPTS / "lib" / "config-root.sh"
SETUP_SYMLINKS = SCRIPTS / "setup-symlinks.sh"
CONFIGURE_IDENTITY = SCRIPTS / "configure-identity.sh"
INSTALL_HOOKS = SCRIPTS / "install-reminder-hooks.sh"
DOCTOR = SCRIPTS / "doctor.sh"
ONBOARD = SCRIPTS / "onboard.sh"
INSTALL_CURSOR = SCRIPTS.parent / "cursor" / "scripts" / "install-cursor-links.sh"

_INSTALL_TARGET_PATTERN = re.compile(
    r'\$HOME/\.claude/'
    r'(CLAUDE\.md|config\.md|memory-global|skills|agents|settings\.json|agent-identity\.local)'
)


def _source_and_echo(env_extra=None):
    """Source the resolver and echo CLAUDE_AGENT_HOME; returns CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT_HOME"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", f'source "{RESOLVER}" && printf "%s\\n" "$CLAUDE_AGENT_HOME"'],
        env=env,
        capture_output=True,
        text=True,
    )


def test_default_is_dot_claude_agent(tmp_path):
    r = _source_and_echo({"HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / ".claude-agent")


def test_env_override_respected(tmp_path):
    custom = str(tmp_path / "custom-root")
    r = _source_and_echo({"HOME": str(tmp_path), "CLAUDE_AGENT_HOME": custom})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == custom


def test_resolver_is_idempotent(tmp_path):
    """Sourcing twice keeps the first value (no re-assignment if already set)."""
    custom = str(tmp_path / "my-root")
    r = subprocess.run(
        [
            "bash", "-c",
            f'export CLAUDE_AGENT_HOME="{custom}" && '
            f'source "{RESOLVER}" && source "{RESOLVER}" && '
            f'printf "%s\\n" "$CLAUDE_AGENT_HOME"',
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == custom


# ── agent_legacy_inplace_layout: shared legacy-layout detector ────────────────

def _run_legacy_detect(home: Path, repo: Path, agent_home=None):
    """Source the resolver and call agent_legacy_inplace_layout; return rc."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_AGENT_HOME"}
    env["HOME"] = str(home)
    if agent_home is not None:
        env["CLAUDE_AGENT_HOME"] = str(agent_home)
    return subprocess.run(
        ["bash", "-c",
         f'source "{RESOLVER}" && agent_legacy_inplace_layout "{repo}"'],
        env=env, capture_output=True, text=True,
    ).returncode


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claude-agent-instructions"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# constitution\n")
    return repo


def test_legacy_detect_true_when_inplace_symlink(tmp_path):
    """~/.claude/CLAUDE.md symlinked into the repo → legacy layout present (rc 0)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo = _make_repo(tmp_path)
    (home / ".claude" / "CLAUDE.md").symlink_to(repo / "CLAUDE.md")
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude-agent") == 0


def test_legacy_detect_false_when_clean_isolated(tmp_path):
    """~/.claude exists but holds no repo-pointing symlink → not legacy (rc 1)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n")  # personal, not a repo symlink
    repo = _make_repo(tmp_path)
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude-agent") == 1


def test_legacy_detect_false_when_no_dot_claude(tmp_path):
    """Fresh machine with no ~/.claude at all → not legacy (rc 1)."""
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_repo(tmp_path)
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude-agent") == 1


def test_legacy_detect_false_when_claude_is_the_isolated_root(tmp_path):
    """If ~/.claude IS the configured root, its symlinks are not 'legacy' (rc 1)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo = _make_repo(tmp_path)
    (home / ".claude" / "CLAUDE.md").symlink_to(repo / "CLAUDE.md")
    assert _run_legacy_detect(home, repo, agent_home=home / ".claude") == 1


# ── Structural audit: no hardcoded install-target $HOME/.claude/<name> ────────

def _find_install_targets(path: Path) -> list[str]:
    return _INSTALL_TARGET_PATTERN.findall(path.read_text())


def test_no_hardcoded_install_target_in_setup_symlinks():
    found = _find_install_targets(SETUP_SYMLINKS)
    assert not found, f"Hardcoded install-target refs in setup-symlinks.sh: {found}"


def test_no_hardcoded_install_target_in_configure_identity():
    found = _find_install_targets(CONFIGURE_IDENTITY)
    assert not found, f"Hardcoded install-target refs in configure-identity.sh: {found}"


def test_no_hardcoded_install_target_in_install_reminder_hooks():
    found = _find_install_targets(INSTALL_HOOKS)
    assert not found, f"Hardcoded install-target refs in install-reminder-hooks.sh: {found}"


def test_no_hardcoded_install_target_in_doctor():
    found = _find_install_targets(DOCTOR)
    assert not found, f"Hardcoded install-target refs in doctor.sh: {found}"


def test_no_hardcoded_install_target_in_install_cursor():
    found = _find_install_targets(INSTALL_CURSOR)
    assert not found, f"Hardcoded install-target refs in install-cursor-links.sh: {found}"


# ── Python resolver: scripts/lib/config_root.py (read-time analog) ────────────

import importlib  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
from lib import config_root  # noqa: E402


def _reload_env(monkeypatch, tmp_home, **env):
    """Point HOME at a tmp dir, clear the root env vars, then apply overrides."""
    monkeypatch.setenv("HOME", str(tmp_home))
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_AGENT_HOME", "CLAUDE_AGENT_IDENTITY"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config_root)


def test_py_config_config_dir_wins(monkeypatch, tmp_path):
    custom = tmp_path / "cfg-dir"
    _reload_env(monkeypatch, tmp_path, CLAUDE_CONFIG_DIR=str(custom))
    assert config_root.agent_home() == custom


def test_py_agent_home_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "agent-root"
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(custom))
    assert config_root.agent_home() == custom


def test_py_config_dir_precedes_agent_home(monkeypatch, tmp_path):
    _reload_env(
        monkeypatch,
        tmp_path,
        CLAUDE_CONFIG_DIR=str(tmp_path / "a"),
        CLAUDE_AGENT_HOME=str(tmp_path / "b"),
    )
    assert config_root.agent_home() == tmp_path / "a"


def test_py_isolated_default_when_present(monkeypatch, tmp_path):
    (tmp_path / ".claude-agent").mkdir()
    _reload_env(monkeypatch, tmp_path)
    assert config_root.agent_home() == tmp_path / ".claude-agent"


def test_py_legacy_fallback_when_not_isolated(monkeypatch, tmp_path):
    # No ~/.claude-agent, no env → legacy ~/.claude (pre-migration machine).
    _reload_env(monkeypatch, tmp_path)
    assert config_root.agent_home() == tmp_path / ".claude"


def test_py_skills_dir(monkeypatch, tmp_path):
    custom = tmp_path / "agent-root"
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(custom))
    assert config_root.skills_dir() == custom / "skills"


def test_py_identity_file_default(monkeypatch, tmp_path):
    custom = tmp_path / "agent-root"
    _reload_env(monkeypatch, tmp_path, CLAUDE_AGENT_HOME=str(custom))
    assert config_root.identity_file() == custom / "agent-identity.local"


def test_py_identity_file_override(monkeypatch, tmp_path):
    ident = tmp_path / "elsewhere" / "id.local"
    _reload_env(
        monkeypatch,
        tmp_path,
        CLAUDE_AGENT_HOME=str(tmp_path / "agent-root"),
        CLAUDE_AGENT_IDENTITY=str(ident),
    )
    assert config_root.identity_file() == ident
