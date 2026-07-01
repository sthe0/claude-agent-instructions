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
