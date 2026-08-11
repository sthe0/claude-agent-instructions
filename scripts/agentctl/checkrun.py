"""Predsubmit observation of stage verify_command runs (task judge-import-blindness,
stage C.2).

At `submit-plan` for a substantive plan, every stage's verify_command is run once
in its declared venue so a command that is malformed, missing required arguments,
or dependent on unset ambient environment surfaces before plan approval rather
than at first dispatch. This mechanizes the RUN, not the source: a static lint
over a verify_command's target script cannot structurally tell a legitimate
`os.environ.get(...)` read from one that silently swallows a missing project
root — the two have identical syntactic shape, and only differ in what the call
actually prints when exercised. Classifying that difference by regex/AST would be
the meaning-via-syntax antipattern this module exists to avoid
(memory-global/leaves/regex-not-for-semantic-classification.md); running the
command and reporting its own output sidesteps the classification entirely.

Three structurally-decidable labels — never a judgment on whether the redness is
legitimate, which stays a human/thinker call:
  - NOT_JUDGED  — the declared venue does not exist on disk yet, the command
                  exceeded its timeout, or running it raised (including the
                  process failing to launch at all). The command is not run
                  at all in the venue-missing case; in the other two cases it
                  may have partially run.
  - GREEN_AT_SUBMIT — rc already equals expected_exit before any work happened.
                  Either the stage's work is already done (so rc reflects that)
                  or the control cannot go red on the mutation it exists to
                  catch; this module sees only rc, never session history, so it
                  cannot tell which — the label states the fact and leaves the
                  reading to the reader. Contrast plan.py's reachability
                  blocker, which proves a control CAN go green; the label that
                  evidences a control CAN go red is RED, not this one.
  - RED         — anything else. Carries venue, resolved cwd, rc, expected_exit,
                  and the head of combined stdout+stderr, because the
                  discriminating information is whatever the checked command
                  itself printed — not a classifier this module writes.

Scope boundaries:
  - Only stage `verify_command` entries are observed. `[[final_check]]` entries
    are not (a final_check is typically an aggregate command — a full test
    suite, a verify-all script — whose per-submit cost would turn the
    plan-approval gate into a full test run).
  - A `verify_kind = "landed"` criterion carries no free-text verify_command by
    construction (plan.py enforces this at parse time) and is skipped.
  - An `acceptance_review` criterion has no verify_command semantics and is
    skipped.
  - This module never executes a stage's `[[final_check]]`, never mutates plan
    or session state, and never blocks submit — its only output is advisory
    text lines.
  - A verify_command with side effects WILL actually execute: this module
    trusts the verify_command contract (check, don't mutate), the same trust
    every other consumer of Criterion.verify_command already extends.
  - submit-plan's wall-clock grows by up to `n * timeout_s` for n observed
    stages.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .dispatch import RunResult, Runner
from .state import CheckKind, CriterionType, Stage

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_HEAD_CHARS = 4000

NOT_JUDGED = "not-judged"
GREEN_AT_SUBMIT = "green-at-submit"
RED = "red"


@dataclass
class CheckObservation:
    stage_index: int
    stage_title: str
    declared_venue: str
    command: str
    expected_exit: int
    label: str
    resolved_cwd: str | None = None
    returncode: int | None = None
    head: str = field(default="")
    reason: str = field(default="")


def _default_runner(timeout_s: float) -> Runner:
    """A Runner that kills the whole process GROUP on timeout, not just the
    direct child. `subprocess.run(timeout=...)` only reaches the process it
    spawned directly; a verify_command that forks further (a shell pipeline, a
    script that launches its own subprocess) would otherwise leave grandchildren
    running past the declared timeout, still writing into a pipe this runner has
    already given up reading.

    `errors="replace"` on the text-mode pipes means a verify_command's output
    containing bytes that are not valid in the local encoding decodes to U+FFFD
    instead of raising UnicodeDecodeError — the observation stays useful (the
    replacement characters are still visible in `head`) rather than being lost
    to a crash. This does not, by itself, launch the process: `Popen(...)` can
    still raise OSError (missing interpreter, exec permission, ...). That is
    deliberately NOT caught here — the caller-level guarantee in
    `observe_stage_checks` that no exception escapes covers every Runner
    (including one injected by a caller/test that raises directly), so a local
    catch here would just duplicate it for one exception type."""

    def run(argv: list[str]) -> RunResult:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            return RunResult(proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            return RunResult(-1, stdout, stderr, timed_out=True)

    return run


def observe_stage_checks(
    stages: list[Stage],
    resolve_venue: Callable[[str], str | None],
    runner: Runner | None = None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    head_chars: int = DEFAULT_HEAD_CHARS,
) -> list[CheckObservation]:
    """Run each eligible stage's verify_command once in its declared venue and
    report what actually happened. Never blocks — the caller (cmd_submit_plan)
    attaches format_observations() output to the warn-only advisories channel
    and nothing else consumes the return value.

    The no-exception-escapes guarantee is enforced HERE, once, around the call
    to `run(...)` — not by each Runner implementation. Any exception the Runner
    raises (a failed process launch, an injected test double that throws, or
    anything else) is caught and turned into a NOT_JUDGED observation naming
    the exception's class and text, rather than propagating out of this
    function and past whatever called it (submit-plan, in production).

    Stated precisely, because an overclaimed guarantee is worse than a narrow
    one: the guarantee covers the `runner` seam only. The other injected seam,
    `resolve_venue`, is called outside that guard, so a resolver that raises
    WOULD propagate. That is not a live crash path — production passes
    `SessionState.resolve_check_venue`, which only reads attributes and
    compares strings — but it is an uncovered seam, not a covered one, and
    widening the guard to the whole loop body is recorded as a residual rather
    than silently assumed here.

    `resolve_venue` is injected as a plain callable (in production,
    `SessionState.resolve_check_venue`) rather than requiring a SessionState, so
    this module has no dependency on the state layer and is testable standalone.
    """
    run = runner or _default_runner(timeout_s)
    observations: list[CheckObservation] = []
    for stage in stages or []:
        crit = stage.criterion
        if crit.criterion_type != CriterionType.MEASURABLE.value:
            continue
        if crit.verify_kind == CheckKind.LANDED.value:
            continue
        if not crit.verify_command:
            continue

        cwd = resolve_venue(crit.verify_venue)
        obs = CheckObservation(
            stage_index=stage.index,
            stage_title=stage.title,
            declared_venue=crit.verify_venue,
            command=crit.verify_command,
            expected_exit=crit.expected_exit,
            label=NOT_JUDGED,
            resolved_cwd=cwd,
        )
        if not cwd or not Path(cwd).is_dir():
            obs.reason = "declared venue does not exist on disk yet"
            observations.append(obs)
            continue

        try:
            result = run(["bash", "-c", f"cd {shlex.quote(cwd)} && {crit.verify_command}"])
        except Exception as exc:  # noqa: BLE001 - see docstring: the never-raises guarantee lives here
            obs.reason = f"running the command raised {type(exc).__name__}: {str(exc)[:200]}"
            observations.append(obs)
            continue
        combined = (result.stdout or "") + (result.stderr or "")
        obs.head = combined[:head_chars]
        if result.timed_out:
            obs.reason = f"exceeded {timeout_s:g}s timeout"
            observations.append(obs)
            continue

        obs.returncode = result.returncode
        obs.label = GREEN_AT_SUBMIT if result.returncode == crit.expected_exit else RED
        observations.append(obs)
    return observations


def format_observations(observations: list[CheckObservation]) -> list[str]:
    """Render each observation as one advisory line. `not-judged` lines carry
    only the reason (no command ran); `green-at-submit` and `red` lines carry
    venue, resolved cwd, rc/expected_exit, and the head of combined
    stdout+stderr — the differentiating content is whatever the checked command
    itself printed, not a judgment this function makes."""
    lines: list[str] = []
    for obs in observations:
        where = f"stage {obs.stage_index} ({obs.stage_title!r})"
        if obs.label == NOT_JUDGED:
            lines.append(
                f"{where} verify_command not-judged at submit "
                f"(venue={obs.declared_venue!r}): {obs.reason}"
            )
        elif obs.label == GREEN_AT_SUBMIT:
            lines.append(
                f"{where} verify_command is green-at-submit: rc={obs.returncode} "
                f"already equals expected_exit={obs.expected_exit} in "
                f"{obs.resolved_cwd!r} before this run did any work — either the "
                f"stage's work is already done (rc reflects that) or the control "
                f"cannot go red on the mutation it exists to catch; this module "
                f"sees only rc, not session history, so it cannot tell which"
            )
        else:
            lines.append(
                f"{where} verify_command is red at submit: venue={obs.declared_venue!r} "
                f"cwd={obs.resolved_cwd!r} rc={obs.returncode} "
                f"expected_exit={obs.expected_exit} head={obs.head!r}"
            )
    return lines
