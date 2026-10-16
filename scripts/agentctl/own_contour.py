"""The own contour of a linked child: the one definition of what its channel may do.

Difficulty removed: a child spawned for a parent stage is gated by its own engine
session and moves that session through `scripts/agentctl-own.py`. Three scripts name
that channel -- the wrapper that runs it, the spawn that exports and grants it, the
PreToolUse gate that trusts it -- so the session variable and the verb set are defined
here once, next to `norm_handoff.NORM_HANDOFF_ENV` (the handoff the channel is only
available to), and imported by all three. The child's own plan file path is defined here
too (`own_plan_path`), for the same reason: the spawn grants it, the gate guards it.
"""
from __future__ import annotations

from pathlib import Path

from lib import config_root

# The engine session the channel is pinned to; exported only to a child whose own
# session adopted the norm handoff.
OWN_SESSION_ENV = "AGENTCTL_OWN_SESSION"

# The spine verbs a linked child moves its own session with. `plan` (ROUTED->PLANNING)
# and marker-free `partition` (APPROVED->PARTITIONED, one delivery unit) are the
# transitions machine.py requires on the way to EXECUTING; every user-authority verb
# stays with the parent. The `plan-review-*` walk/history/compose verbs are what the
# topological review driver (`plan-review-topological.py`) runs for the child's plan.
OWN_CONTOUR_VERBS = frozenset({
    "classify", "status", "plan", "next-stage", "submit-plan", "plan-review",
    "plan-review-delta", "approve", "partition", "dispatch", "record-result",
    "code-review", "verify-final", "declare", "investigate", "critique",
    "normalize", "replan", "stage-grants", "plan-grants", "question-raise",
    "question-dispose", "question-research", "question-candidate-dispose",
    "order-raise", "order-dispose",
    "plan-review-walk", "plan-review-pair-history", "plan-review-compose",
})

# `partition` options a child may not set: the delivery-partition decision stays the
# root's, so only the marker-free (single delivery unit) form is available.
PARTITION_REFUSED_FLAGS = frozenset({
    "--m1", "--m2", "--m3", "--m4", "--m3-severe", "--m4-severe", "--unit",
})

# `approve` is available only as `approve --by <this>`.
APPROVE_BY = "agent"


def own_plan_path(session_id: str) -> Path:
    """The one plan file a linked child authors: `own_plans_dir()/<sid>/plan.toml`. It is
    granted to that child alone at spawn; every other session's write to it is refused
    by the state gate (`exempt_paths.own_plan_owner`)."""
    return config_root.own_plans_dir() / config_root.sanitize_session_id(session_id) / "plan.toml"
