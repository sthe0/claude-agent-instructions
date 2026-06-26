"""Tenet: Core changes go through the human approval gate (ADR-0001 governance + constraint 5).

A protected-Core change is only landed via planner -> approval -> developer, and a non-author
never auto-edits Core. A candidate edit that would let an agent auto-apply a Core change
semantically contradicts this propose-not-execute rule — a class-2 conflict.
"""
from ..runner import Tenet

TENET = Tenet(
    name="core-changes-through-human-gate",
    description="Protected-Core changes go through planner -> approval -> developer; the agent "
                "proposes and never auto-edits Core (propose-not-execute, no veto).",
    protected_terms=frozenset({"core", "changes", "human", "approval", "gate", "propose", "auto"}),
    must_affirm=True,
)
