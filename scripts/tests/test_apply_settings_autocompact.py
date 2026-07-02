"""Installer test for apply-settings.sh's autocompaction pin + prune.

Runs the real script via subprocess with CLAUDE_SETTINGS redirected to a
tmp_path file so the real ~/.claude/settings.json is never touched. Verifies
the base OWNS the autocompact window (pins it, prunes deprecated keys) while
machine-specific keys survive, and that a second run is idempotent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APPLY = REPO / "scripts" / "apply-settings.sh"


def _run(fixture: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(APPLY)],
        env={**os.environ, "CLAUDE_SETTINGS": str(fixture)},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "autoCompactWindow": 150000,
                "model": "__sentinel__",
                "env": {
                    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
                    "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
                    "SOME_MACHINE_KEY": "keepme",
                },
            }
        ),
        encoding="utf-8",
    )
    yield path
    bak = Path(str(path) + ".bak")
    if bak.exists():
        bak.unlink()


@pytest.fixture(autouse=True)
def _require_tools():
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq required for apply-settings.sh")


def test_prunes_and_pins(fixture):
    result = _run(fixture)
    assert result.returncode == 0, result.stderr

    data = json.loads(fixture.read_text(encoding="utf-8"))
    env = data["env"]
    assert data["autoCompactWindow"] == 210000
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "210000"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" not in env
    # Machine-specific keys survive untouched.
    assert data["model"] == "__sentinel__"
    assert env["SOME_MACHINE_KEY"] == "keepme"


def test_idempotent(fixture):
    assert _run(fixture).returncode == 0
    after_first = json.loads(fixture.read_text(encoding="utf-8"))
    assert _run(fixture).returncode == 0
    after_second = json.loads(fixture.read_text(encoding="utf-8"))
    assert after_first == after_second


def test_default_target_is_isolated_root_not_home_claude(tmp_path):
    """Without CLAUDE_SETTINGS, TARGET must resolve under CLAUDE_AGENT_HOME —
    never fall back to $HOME/.claude/settings.json (the dead root)."""
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq required for apply-settings.sh")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    agent_home = tmp_path / "agent-root"
    env = {**os.environ, "HOME": str(fake_home), "CLAUDE_AGENT_HOME": str(agent_home)}
    env.pop("CLAUDE_SETTINGS", None)

    result = subprocess.run(
        ["bash", str(APPLY)], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert (agent_home / "settings.json").exists()
    assert not (fake_home / ".claude").exists()
