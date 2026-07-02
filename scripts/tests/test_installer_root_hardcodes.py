"""Isolated-root coverage for the two installers not covered elsewhere in this
audit: setup-ccgram.sh's Claude Code detection, and project_entry/registry.sh's
machine-local plugin directory (used by session-isolate.sh's backend lookup).

setup-ccgram.sh performs real installs (uv, ccgram, tmux, launchd/systemd) when
run, so its hook-install detection branch is covered structurally rather than
by subprocess execution — the same style test_config_root.py already uses for
setup-symlinks.sh / doctor.sh / etc.

registry.sh only defines shell functions at source time (no top-level side
effects beyond computing _REGISTRY_DIR), so its plugin-dir resolver is covered
by sourcing it directly and calling _plugin_dir.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SETUP_CCGRAM = SCRIPTS / "setup-ccgram.sh"
REGISTRY = SCRIPTS / "project_entry" / "registry.sh"


# ── setup-ccgram.sh: structural check on the hook-install detection line ──────

def test_ccgram_detection_checks_isolated_root():
    text = SETUP_CCGRAM.read_text()
    assert 'CLAUDE_AGENT_HOME/settings.json' in text, (
        "setup-ccgram.sh's Claude Code detection must check "
        "$CLAUDE_AGENT_HOME/settings.json, not only the legacy $HOME/.claude path"
    )


def test_ccgram_sources_config_root_resolver():
    text = SETUP_CCGRAM.read_text()
    assert "lib/config-root.sh" in text


# ── registry.sh: _plugin_dir resolution order ──────────────────────────────

def _plugin_dir(home: Path, agent_home: "str | None" = None, override: "str | None" = None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_AGENT_HOME", "CLAUDE_PROJECT_PLUGIN_DIR")}
    env["HOME"] = str(home)
    if agent_home is not None:
        env["CLAUDE_AGENT_HOME"] = agent_home
    if override is not None:
        env["CLAUDE_PROJECT_PLUGIN_DIR"] = override
    result = subprocess.run(
        ["bash", "-c", f'source "{REGISTRY}" && _plugin_dir'],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_plugin_dir_explicit_override_wins(tmp_path):
    custom = str(tmp_path / "custom-plugins")
    assert _plugin_dir(tmp_path / "home", override=custom) == custom


def test_plugin_dir_prefers_existing_isolated_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    isolated = tmp_path / "agent-root"
    (isolated / "project-entry-plugins").mkdir(parents=True)
    legacy = home / ".claude" / "project-entry-plugins"
    legacy.mkdir(parents=True)

    assert _plugin_dir(home, agent_home=str(isolated)) == str(isolated / "project-entry-plugins")


def test_plugin_dir_falls_back_to_legacy_when_only_legacy_exists(tmp_path):
    home = tmp_path / "home"
    legacy = home / ".claude" / "project-entry-plugins"
    legacy.mkdir(parents=True)
    isolated = tmp_path / "agent-root"  # not created

    assert _plugin_dir(home, agent_home=str(isolated)) == str(legacy)


def test_plugin_dir_defaults_to_isolated_when_neither_exists(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    isolated = tmp_path / "agent-root"  # not created

    assert _plugin_dir(home, agent_home=str(isolated)) == str(isolated / "project-entry-plugins")
