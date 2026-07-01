"""Warn-only advisory LLM judge for the semantic cognition residue.

The advisor NEVER sets directive.ok=False, NEVER changes directive.node, and NEVER
blocks a transition. With the advisor returning [] (disabled / errored / stubbed),
control flow is byte-identical to advisor-absent. Fail-open: any exception yields [].
Default-off: only active when AGENTCTL_ADVISOR=1 is set in the environment, OR when
resolve_enabled()'s config-mode + weight-class rule turns it on for a substantive
session (see resolve_enabled).
"""
from __future__ import annotations

import os
import subprocess

from .config import Thresholds
from .dispatch import RunResult

# Cheap model + hard cap: the advisor auto-activates for every substantive session's
# cognition points, so each call must stay bounded in cost and can never hang a
# coordination step.
_ADVISOR_MODEL = "sonnet"
_ADVISOR_TIMEOUT_S = 20

_ADVISOR_MODE_SUBSTANTIVE = "substantive"
_SUBSTANTIVE_WEIGHT_CLASS = "SUBSTANTIVE"

_PROMPTS: dict[str, str] = {
    "weight_classification": (
        "Review this task classification. Flag any concerns about whether the weight class "
        "or route is correct. Return each concern as one line. Return nothing if none.\n{payload}"
    ),
    "plan_completeness": (
        "Review this plan for completeness: do the stages cover the goal? "
        "Flag missing coverage, hand-waving, or omitted prerequisites as one concern per line. "
        "Return nothing if the plan looks complete.\n{payload}"
    ),
    "hypothesis_distinctness": (
        "Review these hypotheses for genuine distinctness in MEANING (not just string difference). "
        "Flag if any two hypotheses describe the same failure mode, or if the declaration does not "
        "capture a real divergence. One concern per line; nothing if all look distinct.\n{payload}"
    ),
    "acceptance_observation": (
        "Review this acceptance observation: does it describe what was actually observed, "
        "or is it vague, generic, or a rephrase of the expected result? "
        "One concern per line; nothing if the observation is concrete and adequate.\n{payload}"
    ),
}


def judge(kind: str, payload: dict, runner, *, enabled: bool | None = None) -> list[str]:
    """Return advisory strings for the given cognition point, or [] if disabled/failed.

    Warn-only: callers MUST NOT branch on the return value for control flow.
    Fail-open: runner=None, non-zero exit, or any exception returns [].
    """
    if enabled is None:
        enabled = os.environ.get("AGENTCTL_ADVISOR") == "1"
    if not enabled or runner is None:
        return []
    try:
        template = _PROMPTS.get(kind)
        if not template:
            return []
        prompt = template.format(payload=payload)
        result = runner(["claude", "-p", "--model", _ADVISOR_MODEL, prompt])
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def resolve_enabled(weight_class: str | None, *, thresholds: Thresholds | None = None) -> bool:
    """Resolve whether the advisor should run for this call.

    AGENTCTL_ADVISOR overrides in both directions ("1" forces on, "0" forces off,
    regardless of config or weight class). Absent the env override, the advisor is
    on only when config.md's advisor-mode == "substantive" AND the session's
    weight_class == SUBSTANTIVE — auto-activation is scoped to substantive work,
    never chat/small-change. A missing/unreadable advisor-mode key resolves to off
    (fail-open, same default-off posture as the rest of this module).
    """
    env = os.environ.get("AGENTCTL_ADVISOR")
    if env == "1":
        return True
    if env == "0":
        return False
    thr = thresholds if thresholds is not None else Thresholds()
    try:
        mode = thr.advisor_mode
    except KeyError:
        return False
    return mode == _ADVISOR_MODE_SUBSTANTIVE and weight_class == _SUBSTANTIVE_WEIGHT_CLASS


def subprocess_runner(argv: list[str], *, timeout: int = _ADVISOR_TIMEOUT_S) -> RunResult:
    """Real `claude -p` runner with a hard timeout. Not judge()'s default (a caller
    that wants a live advisor pass this explicitly) — kept separate so the fail-open
    `runner=None -> []` contract in judge() stays byte-identical to advisor-absent."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return RunResult(1, "", f"advisor timed out after {timeout}s")
