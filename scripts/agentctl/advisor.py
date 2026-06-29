"""Warn-only advisory LLM judge for the semantic cognition residue.

The advisor NEVER sets directive.ok=False, NEVER changes directive.node, and NEVER
blocks a transition. With the advisor returning [] (disabled / errored / stubbed),
control flow is byte-identical to advisor-absent. Fail-open: any exception yields [].
Default-off: only active when AGENTCTL_ADVISOR=1 is set in the environment.
"""
from __future__ import annotations

import os

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
        result = runner(["claude", "-p", prompt])
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []
