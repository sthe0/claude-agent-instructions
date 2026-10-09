"""The `claude-recover` function in claude-launchers.sh delegates to claude-recover.py."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
LAUNCHERS = SCRIPTS / "claude-launchers.sh"


def _bash(tmp_path, snippet):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    return subprocess.run(
        ["bash", "-c", f'source "{LAUNCHERS}" && {snippet}'],
        env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )


def test_function_is_defined(tmp_path):
    res = _bash(tmp_path, "type -t claude-recover")
    assert res.stdout.strip().splitlines()[-1] == "function", res.stderr


def test_help_mentions_dry_run(tmp_path):
    res = _bash(tmp_path, "claude-recover --help")
    assert res.returncode == 0, res.stderr
    assert "--dry-run" in res.stdout


def test_subcommand_help_passes_through(tmp_path):
    res = _bash(tmp_path, "claude-recover snapshot --help")
    assert res.returncode == 0, res.stderr
    assert "--force" in res.stdout
