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
    """Read a spawn's stdout for its return marker.

    ONE ordered scan of the non-blank lines: the first line carrying either a
    known ``^MARKER:`` or a ``MALFORMED:`` prefix wins, and both tests share the
    single loop body so the winner is the first in DOCUMENT order. Keeping them
    in one pass is load-bearing — two sequential passes (all lines for a marker,
    then all lines for MALFORMED) would let a stray ``COMPLETED:`` line inside a
    MALFORMED envelope's preserved original out-rank the envelope itself, a
    fail-open mis-route.

    ``lib.planner_plan_check.check_planner_return`` — which ``spawn-specialist.py``
    already ran on this text before it reached our stdout — canonicalises a
    passing result onto its FIRST non-blank line (``lib.planner_plan_check.canonicalize``)
    whenever the second-pass extractor ran; that pass alone can recover a marker
    under markdown emphasis, e.g. ``**COMPLETED:**``, that this regex would miss.
    So for canonical input the ordered scan returns the canonical marker on its
    first iteration, with no special first-line branch needed, and the same scan
    still tolerates a preamble in legacy / kill-switch output.

    Return the marker and the body after its colon. The canonical marker line is
    BARE, so that body is ``""`` for every canonicalised marker — the digest
    lives on its own ``Digest:`` line, deliberately off the line this parse
    feeds to ``cmd_dispatch``'s deterministic consumers (the permission gate
    among them). A ``MALFORMED:`` line maps to marker "MALFORMED"; if no line
    carries a marker, map to (None, "")."""
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


def subprocess_runner(argv: list[str], cwd: str | None = None) -> RunResult:
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
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
    continue_worktree: str | None = None,
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
    if continue_worktree:
        argv.extend(["--continue-worktree", continue_worktree])
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
    continue_worktree: str | None = None,
    cwd: str | None = None,
) -> RunResult:
    argv = build_argv(
        stage, plan_path, budget=budget, complexity=complexity, dry_run=dry_run,
        continue_worktree=continue_worktree,
    )
    run = runner or subprocess_runner
    # cwd is only threaded to the runner when set, so every pre-existing
    # single-arg fake runner (and the None -> inherit-cwd default) stays
    # byte-identical; a session carrying delivery_worktree/repo_root is the
    # sole case that now requires the runner to accept a `cwd` kwarg.
    if cwd is not None:
        return run(argv, cwd=cwd)
    return run(argv)
