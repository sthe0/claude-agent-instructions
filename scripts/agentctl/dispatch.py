"""Dispatch a stage to a spawned specialist via spawn-specialist.py or
spawn-cursor-specialist.py, selected by the session's bound runtime_host.

This is the engine's one process-spawning seam. It does NOT reimplement the spawn
template, recursion cap, budget resolution, marker validation, or cost logging —
all of that lives in the two wrapper scripts, which this module shells out to
(see spawn_cli_for). The runner is injectable (default = real subprocess) so the
full state-machine cycle can be exercised in tests with a fake runner and zero
`claude -p` / `agent -p` spend.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lib import argv_text
from lib.runtime_models import HOST_CLAUDE, HOST_CURSOR, HOSTS

from .state import CriterionType, Stage

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPAWN_CLI = REPO_ROOT / "scripts" / "spawn-specialist.py"
SPAWN_CLI_CURSOR = REPO_ROOT / "scripts" / "spawn-cursor-specialist.py"

_SPAWN_CLI_BY_HOST = {HOST_CLAUDE: SPAWN_CLI, HOST_CURSOR: SPAWN_CLI_CURSOR}


def spawn_cli_for(host: str) -> Path:
    """The wrapper script `runtime_host=host` dispatches through."""
    try:
        return _SPAWN_CLI_BY_HOST[host]
    except KeyError:
        raise ValueError(f"unknown host {host!r}; must be one of {HOSTS}") from None



# Conservative staging threshold for a value THIS process forwards on to a
# child's argv — well under MAX_ARG_STRLEN (131072) so a caller that reaches
# dispatch_stage directly (a test, or a future in-process driver) rather than
# via OS argv is covered too; OS argv already bounds a CLI-supplied value to
# roughly this process's own received argv size, which the ceiling alone does
# not guarantee for a direct Python call.
_FORWARD_STAGE_THRESHOLD_BYTES = 32768

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
    # Set only by agentctl.advisor.subprocess_runner's own TimeoutExpired
    # branch — the structural discriminator between a judge that timed out
    # (outcome 5) and one that returned fast without a judgment (outcome 7).
    # Never derived by matching subprocess_runner's own stderr literal
    # ("advisor timed out after Ns") — self-referential string matching on a
    # format your own code controls is fragile by construction.
    timed_out: bool = False


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
    constraints: str = "",
    done_criterion: str | None = None,
    runtime_host: str = HOST_CLAUDE,
) -> list[str]:
    kind = stage.spawn_kind()
    if not kind:
        raise ValueError(f"stage {stage.index} is not a spawn stage (executor={stage.actor.executor!r})")
    argv = [
        "python3",
        str(spawn_cli_for(runtime_host)),
        "--kind",
        kind,
        "--plan",
        plan_path,
        "--done-criterion",
        done_criterion if done_criterion is not None else stage.criterion.done_criterion,
        "--criterion-type",
        _CRITERION_FLAG.get(stage.criterion.criterion_type, "measurable"),
        "--budget",
        budget,
        "--complexity",
        complexity,
    ]
    argv.extend(["--stage-index", str(stage.index)])
    argv.append("--plan-brief")
    if continue_worktree:
        argv.extend(["--continue-worktree", continue_worktree])
    if constraints:
        argv.extend(["--constraints", constraints])
    if dry_run:
        argv.append("--dry-run")
    return argv


def _normalize_forward_value(value: str, element: str) -> tuple[str, Path | None]:
    """Prepare one FORWARD-class value for a spawned child's argv.

    The child resolves this value itself (via its own ``read_arg_text``), so
    this process must never read a ``@``-reference's contents and inline them —
    that would relocate the E2BIG defect one hop later rather than remove it.
    Returns ``(value_for_argv, staged_path)``; the caller deletes
    ``staged_path`` (when not ``None``) once the child has exited.

    ``element`` names the failing flag/field (e.g. ``"--constraints"``) and is
    prefixed onto a missing-``@ref`` error so a reader can tell which forwarded
    element failed instead of guessing between ``--constraints`` and the plan's
    ``--done-criterion``.
    """
    if not value:
        return value, None
    kind, payload = argv_text.classify_arg_text(value)
    if kind == "ref":
        resolved = Path(payload).resolve()
        if not argv_text.is_readable_file(resolved):
            raise SystemExit(f"{element}: {argv_text.ref_error(payload)}")
        return f"@{resolved}", None
    # escaped or inline: forward `value` VERBATIM — preserving its own '@@'
    # escaping, if any — as long as it fits the child's argv comfortably. Only
    # an oversized payload is staged, and it is staged UN-escaped (a file's
    # contents carry no further '@' meaning), so read_arg_text on the child's
    # side recovers the same text either way.
    if len(value.encode("utf-8")) <= _FORWARD_STAGE_THRESHOLD_BYTES:
        return value, None
    staged = argv_text.stage_text_to_tempfile(payload)
    return f"@{staged}", staged


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
    constraints: str = "",
    runtime_host: str = HOST_CLAUDE,
) -> RunResult:
    staged: list[Path] = []
    try:
        norm_constraints, staged_constraints = _normalize_forward_value(
            constraints, "--constraints"
        )
        if staged_constraints is not None:
            staged.append(staged_constraints)
        norm_done_criterion, staged_done_criterion = _normalize_forward_value(
            stage.criterion.done_criterion, "--done-criterion (from plan)"
        )
        if staged_done_criterion is not None:
            staged.append(staged_done_criterion)
        argv = build_argv(
            stage, plan_path, budget=budget, complexity=complexity, dry_run=dry_run,
            continue_worktree=continue_worktree, constraints=norm_constraints,
            done_criterion=norm_done_criterion, runtime_host=runtime_host,
        )
        run = runner or subprocess_runner
        # cwd is only threaded to the runner when set, so every pre-existing
        # single-arg fake runner (and the None -> inherit-cwd default) stays
        # byte-identical; a session carrying delivery_worktree/repo_root is the
        # sole case that now requires the runner to accept a `cwd` kwarg.
        if cwd is not None:
            return run(argv, cwd=cwd)
        return run(argv)
    finally:
        # Cleanup runs only after `run()` returns, so a --dry-run preview (which
        # resolves a staged @<tmp> ref synchronously inside that same child call)
        # always sees the file; nothing downstream is left holding a stale path.
        for path in staged:
            path.unlink(missing_ok=True)
