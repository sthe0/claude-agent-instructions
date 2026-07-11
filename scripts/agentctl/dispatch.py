"""Dispatch a stage to a spawned specialist via spawn-specialist.py.

This is the engine's one process-spawning seam. It does NOT reimplement the spawn
template, recursion cap, budget resolution, marker validation, or cost logging —
all of that lives in spawn-specialist.py, which this module shells out to. The
runner is injectable (default = real subprocess) so the full state-machine cycle
can be exercised in tests with a fake runner and zero `claude -p` spend.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .state import CriterionType, Stage

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPAWN_CLI = REPO_ROOT / "scripts" / "spawn-specialist.py"

# Source of truth: spawn-specialist.py RETURN_MARKERS / MARKER_RE. Mirrored here
# (the engine routes the marker spawn-specialist already parsed onto stdout); a
# drift-guard test asserts the two tuples stay identical.
# REVIEW is not in the explicit if-chain below (cmd_dispatch): it is a recognised
# marker with no dedicated route, so it falls to the same _park_blocked path as
# an unrecognised marker — that is the intended handling, not a gap.
RETURN_MARKERS = (
    "COMPLETED",
    "PLAN-READY",
    "INCOMPLETE",
    "CLARIFY",
    "REPLAN",
    "PERMISSION-REQUEST",
    "ESCALATE",
    "REVIEW",
)
MARKER_RE = re.compile(rf"^({'|'.join(RETURN_MARKERS)}):")


def parse_marker(stdout: str) -> tuple[str | None, str]:
    """Scan a spawn's stdout for the first recognised return marker, tolerating a
    preamble (e.g. a summary the specialist printed before the marker line).
    Return the marker and the body after its colon. A `MALFORMED:` wrapper line
    maps to marker "MALFORMED"; if no line carries a marker, map to (None, "")."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = MARKER_RE.match(line)
        if m:
            return m.group(1), line[m.end():].strip()
        if line.startswith("MALFORMED:"):
            return "MALFORMED", line[len("MALFORMED:"):].strip()
    return None, ""

# A runner takes an argv list and returns (returncode, stdout, stderr).
Runner = Callable[[list[str]], "RunResult"]


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def subprocess_runner(argv: list[str]) -> RunResult:
    proc = subprocess.run(argv, capture_output=True, text=True)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


_CRITERION_FLAG = {
    CriterionType.MEASURABLE.value: "measurable",
    CriterionType.ACCEPTANCE_REVIEW.value: "acceptance-review",
}


def build_argv(
    stage: Stage,
    plan_path: str,
    *,
    budget: str = "medium",
    complexity: str = "medium",
    dry_run: bool = False,
) -> list[str]:
    kind = stage.spawn_kind()
    if not kind:
        raise ValueError(f"stage {stage.index} is not a spawn stage (executor={stage.actor.executor!r})")
    argv = [
        "python3",
        str(SPAWN_CLI),
        "--kind",
        kind,
        "--plan",
        plan_path,
        "--done-criterion",
        stage.criterion.done_criterion,
        "--criterion-type",
        _CRITERION_FLAG.get(stage.criterion.criterion_type, "measurable"),
        "--budget",
        budget,
        "--complexity",
        complexity,
    ]
    argv.extend(["--stage-index", str(stage.index)])
    if dry_run:
        argv.append("--dry-run")
    return argv


def dispatch_stage(
    stage: Stage,
    plan_path: str,
    *,
    runner: Runner | None = None,
    budget: str = "medium",
    complexity: str = "medium",
    dry_run: bool = False,
) -> RunResult:
    argv = build_argv(stage, plan_path, budget=budget, complexity=complexity, dry_run=dry_run)
    run = runner or subprocess_runner
    return run(argv)
