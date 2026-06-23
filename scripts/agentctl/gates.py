"""Gate registry: the hard approval/resolution gates the engine enforces.

A gate is a named GateRecord on the SessionState plus a guardian predicate that
says whether the state is allowed to pass through it. The two gates mirror the
prose hard gates:

  - plan_approval : PLAN_READY -> APPROVED needs explicit user approval. The
    engine cannot infer approval from silence; `armed` once a plan is submitted,
    `passed` only when cli.approve records an explicit approver.
  - resolution    : RESOLUTION -> RESOLVED needs every stage PASSED and an
    explicit user confirmation (measurable: check ran; acceptance: user accepted).

Guardians return a list of human-readable blockers ([] == may pass). cli.py calls
the guardian before flipping `passed`, so an illegal pass is impossible.
"""
from __future__ import annotations

from .state import SessionState, StageStatus


def plan_approval_blockers(state: SessionState) -> list[str]:
    out: list[str] = []
    if not state.plan_path:
        out.append("no plan artifact submitted")
    if not state.plan_verified:
        out.append("plan not verified (structure check failed or not run)")
    return out


def resolution_blockers(state: SessionState) -> list[str]:
    out: list[str] = []
    if not state.stages:
        out.append("no stages defined")
    unpassed = [s.index for s in state.stages if s.status != StageStatus.PASSED.value]
    if unpassed:
        out.append(f"stages not PASSED: {unpassed}")
    return out


# gate name -> guardian predicate
GUARDIANS = {
    "plan_approval": plan_approval_blockers,
    "resolution": resolution_blockers,
}


def blockers(state: SessionState, gate_name: str) -> list[str]:
    guardian = GUARDIANS.get(gate_name)
    if guardian is None:
        return [f"unknown gate {gate_name!r}"]
    return guardian(state)
