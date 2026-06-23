"""Workflow-level permission check — the engine's read-only seam onto
permissions-cli.py.

A specialist's PERMISSION-REQUEST is checked against the recorded grants before the
manager asks the user: an already-granted action skips the ask entirely. The actual
`grant` (and the user ask) stay cognitive (manager-driven); the engine only checks.
The runner is injectable so tests exercise the branch with zero subprocess.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .dispatch import RunResult, subprocess_runner

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PERMISSIONS_CLI = REPO_ROOT / "scripts" / "permissions-cli.py"

Runner = Callable[[list[str]], RunResult]


def check_permission(action: str, *, runner: Runner | None = None) -> bool:
    """True if `action` is already granted (global file). Shells out to
    permissions-cli.py check, whose exit code 0 means matched."""
    run = runner or subprocess_runner
    result = run(["python3", str(PERMISSIONS_CLI), "check", action])
    return result.returncode == 0
