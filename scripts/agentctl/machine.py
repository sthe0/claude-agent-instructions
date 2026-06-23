"""The deterministic state machine: legal node transitions.

machine.py is pure — it never touches the filesystem or spawns a process. It owns
the transition table (which node may follow which, under what event) and the
`transition()` function that validates a requested edge. cli.py composes these
edges with state mutation and the gate registry; the engine, not the LLM, owns
the control flow.

The graph (happy path + carve-outs):

  CLASSIFIED --classify--> ROUTED
  ROUTED --plan--> PLANNING            (substantive)
  ROUTED --execute--> EXECUTING        (small-change carve-out: approval auto-passed)
  ROUTED --(chat)                       terminal
  PLANNING --submit_plan--> PLAN_READY
  PLAN_READY --approve--> APPROVED      (plan-approval gate passes)
  APPROVED --execute--> EXECUTING
  EXECUTING --verify--> VERIFYING       (a stage result recorded)
  VERIFYING --next_stage--> EXECUTING   (more ready stages remain)
  VERIFYING --final--> RESOLUTION       (all stages PASSED)
  RESOLUTION --resolve--> RESOLVED      (resolution gate passes)
  any --block--> BLOCKED ; BLOCKED --unblock--> <prior>
  EXECUTING/VERIFYING --replan_refine--> BLOCKED   (then back to EXECUTING)
  PLAN_READY/APPROVED <--replan_substantive-- (re-arm plan-approval gate)
"""
from __future__ import annotations

from .state import Node

# event -> (from_node, to_node)
TRANSITIONS: dict[str, tuple[str, str]] = {
    "classify": (Node.CLASSIFIED.value, Node.ROUTED.value),
    "plan": (Node.ROUTED.value, Node.PLANNING.value),
    "submit_plan": (Node.PLANNING.value, Node.PLAN_READY.value),
    "approve": (Node.PLAN_READY.value, Node.APPROVED.value),
    "execute_approved": (Node.APPROVED.value, Node.EXECUTING.value),
    "execute_small": (Node.ROUTED.value, Node.EXECUTING.value),
    "verify": (Node.EXECUTING.value, Node.VERIFYING.value),
    "next_stage": (Node.VERIFYING.value, Node.EXECUTING.value),
    "final": (Node.VERIFYING.value, Node.RESOLUTION.value),
    "resolve": (Node.RESOLUTION.value, Node.RESOLVED.value),
}


class TransitionError(Exception):
    """A requested transition is not a legal edge from the current node."""


def legal_targets(node: str) -> list[str]:
    return [to for (frm, to) in TRANSITIONS.values() if frm == node]


def transition(node: str, event: str) -> str:
    """Return the destination node for `event` fired from `node`, or raise."""
    if event not in TRANSITIONS:
        raise TransitionError(f"unknown event {event!r}")
    frm, to = TRANSITIONS[event]
    if node != frm:
        raise TransitionError(
            f"event {event!r} requires node={frm}, but node={node}"
        )
    return to
