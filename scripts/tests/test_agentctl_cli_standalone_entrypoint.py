"""Runtime smoke coverage for `agentctl-cli.py`, the standalone shim stage 2
of the spawn-permission-grant-model plan adds so a review-directive or spawn
brief can name `python3 <abs path>/agentctl-cli.py <verb> ...` from any cwd,
without `cd scripts && python3 -m agentctl <verb>` first. Existing coverage
(test_stage_grants.py) only references the file's *name* inside permission
rule strings (`Bash(agentctl-cli.py:*)`) -- nothing actually runs it. This
file spawns it as a real subprocess (the same way a review-directive or a
developer spawn would) and checks its argparse-produced `--help` output,
which is the one thing about the shim genuinely worth checking at runtime:
that it actually delegates to `agentctl.cli.main()` and doesn't error out
before argparse gets to print anything.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "agentctl-cli.py"


def test_standalone_entrypoint_help_prints_usage():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_standalone_entrypoint_works_from_a_different_cwd(tmp_path):
    """The shim's whole point is cwd-independence -- unlike `python3 -m
    agentctl`, which requires `cd scripts` first."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
