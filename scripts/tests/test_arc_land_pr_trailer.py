"""arc-land-pr.sh: Agent-Session/Agent-Task trailer injection (--dry-run).

arc has no commit-msg hook, so arc-land-pr.sh (the sole scripted arc-commit
path for both the .claude-storage and product arc contexts) is the only
place that can inject the trailer on the arc side. It calls the same Core
helper (scripts/agent_commit_trailer.py) the git commit-msg hook uses, so the
trailer format is byte-identical across all three VCS contexts.

arc-land-pr.sh itself lives in the machine-local arc common tree, not in
Core (Core is the public org-neutral repo; arc specifics stay out of it —
see check-org-neutral.py). This test drives the REAL script by absolute
path via --dry-run (no `arc` state changes, no network), and skips itself
on a machine that does not have the arc common tree checked out.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

ARC_ANCHOR = Path(os.environ.get("ARC_ANCHOR") or os.path.expanduser("~/task-mounts/main"))
# arc-land-pr.sh lives in the machine-local arc common tree, whose location is
# org-specific and MUST NOT be hard-coded into this public Core repo (it would
# leak an org-internal path past check-org-neutral.py). Point ARC_LAND_PR_SH at
# it to exercise this test; it self-skips when the variable is unset or the
# script is absent.
_ARC_LAND_PR_SH = os.environ.get("ARC_LAND_PR_SH")
ARC_LAND_PR_SH = Path(_ARC_LAND_PR_SH) if _ARC_LAND_PR_SH else None

pytestmark = pytest.mark.skipif(
    ARC_LAND_PR_SH is None or not ARC_LAND_PR_SH.is_file() or not ARC_ANCHOR.is_dir(),
    reason="set ARC_LAND_PR_SH to the arc common-tree arc-land-pr.sh to run this test",
)


def _write_state(config_dir: Path, session_id: str, data: dict) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _run_dry_run(env: dict, tmp_path: Path) -> "subprocess.CompletedProcess[str]":
    dummy = tmp_path / "dummy.txt"
    dummy.write_text("x", encoding="utf-8")
    return subprocess.run(
        [str(ARC_LAND_PR_SH), "-m", "test subject", "--dry-run", "--", str(dummy)],
        cwd=str(ARC_ANCHOR),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_dry_run_surfaces_agent_session_trailer_with_session_env(tmp_path):
    config_dir = tmp_path / "config"
    _write_state(config_dir, "ARCSESS", {"tracker_key": "ARC-1", "task_id": "task-x"})
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_SESSION_ID": "ARCSESS",
        "CLAUDE_INSTRUCTIONS_REPO": str(SCRIPTS_DIR.parent),
    }

    result = _run_dry_run(env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Agent-Session: ARCSESS" in result.stdout
    assert "Agent-Task: ARC-1" in result.stdout


def test_dry_run_human_no_session_env_gets_no_trailer(tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"
    }
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(SCRIPTS_DIR.parent)

    result = _run_dry_run(env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Agent-Session" not in result.stdout
