#!/usr/bin/env python3
"""PreToolUse hook: make the agentctl plan-approval gate non-skippable.

When a coordination session is being driven by the agentctl engine, production
edits are legal only in the EXECUTING node (the plan-approval gate, PLAN_READY ->
APPROVED, has been passed). This hook reads the durable session state written by
agentctl's FileStateStore and DENIES Edit/Write on production files whenever the
session is in any node other than EXECUTING.

It is the enforcement twin of hook-prewrite-plan-check.py: that hook nudges when
no agentctl session exists (prose fallback); this one hard-blocks when one does.
The two run in parallel — if there is no agentctl state file for the session this
hook exits 0 (allow) and the prose-fallback nudge still applies.

State file: ~/.claude/agentctl/state/<session_id>.json (see scripts/agentctl/store.py;
the session_id is sanitized to alnum/-/_ exactly as FileStateStore does).

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}

Always exits 0 — a hook crash must never wedge the workflow. Any unexpected error,
missing/corrupt state, or non-production path falls through to allow.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl.exempt_paths import is_gated_path, is_plan_file  # noqa: E402

STATE_ROOT = Path.home() / ".claude" / "agentctl" / "state"


def _safe(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def state_path(session_id: str) -> Path:
    return STATE_ROOT / f"{_safe(session_id)}.json"


# Nodes where production edits are legitimate regardless of weight class: the
# plan-approval gate (or the small-change carve-out) has already been passed.
ALLOW_NODES = {"EXECUTING", "VERIFYING", "RESOLUTION"}

# Nodes that constitute the "planning position": where a plan file is the live
# result-image being authored or refined. A plan write is legitimate only here.
# Changing a plan during execution is a difficulty to be overcome reflexively
# (overcome-difficulty -> replan_substantive re-arms at PLAN_READY), so writes at
# every later node are denied with a pointer back to that path.
PLAN_MUTABLE_NODES = {"CLASSIFIED", "ROUTED", "PLANNING", "PLAN_READY"}


def load_gate_fields(path: Path) -> tuple[str | None, str, str | None] | None:
    """Return (weight_class, node, plan_path) from the state file. weight_class and
    plan_path may be None. Corrupt/unreadable state or a missing/non-string node ->
    None, so main() falls through to allow (unchanged safety)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    node = data.get("node")
    if not isinstance(node, str):
        return None
    weight = data.get("weight_class")
    weight = weight if isinstance(weight, str) else None
    plan_path = data.get("plan_path")
    return weight, node, plan_path


def gate_decision(weight_class: str | None, node: str, is_plan: bool = False) -> tuple[str, str]:
    """Pure weight-aware gate. Returns ("allow"|"deny", reason).

    Plan files get a node-aware rule that overrides the standard one: a plan is the
    result-image of active planning, writable only at a planning-position node.
    This is checked first because EXECUTING is in ALLOW_NODES, yet a plan write at
    EXECUTING must be denied (it is a difficulty to overcome via replan)."""
    if is_plan:
        if node in PLAN_MUTABLE_NODES:
            return "allow", ""
        return "deny", (
            "a plan is the result-image of active planning; changing it now is a "
            "difficulty — step back via `agentctl replan` (re-arms at PLAN_READY) "
            "or overcome-difficulty, then edit the plan"
        )
    if node in ALLOW_NODES:
        return "allow", ""
    # Closed/blocked task: a prod edit here means the agent is acting on a stale session.
    if node in ("RESOLVED", "BLOCKED"):
        return "deny", f"task {node.lower()}; run `agentctl reset` for a new task before editing"
    if weight_class == "CHAT":
        # chat is terminal at ROUTED and never does production edits
        return "allow", ""
    if weight_class is None:
        return "deny", "unclassified: run `agentctl classify` before editing production code"
    if weight_class == "SMALL_CHANGE":
        return "deny", "small change: run `agentctl next-stage` to enter EXECUTING before editing"
    # SUBSTANTIVE (or unknown) before EXECUTING: plan-approval gate not passed yet.
    return "deny", f"plan-approval gate not passed (node={node}); approve the plan first"


def recursion_depth() -> int:
    """AGENT_RECURSION_DEPTH (0 for a top-level/root session). A spawned
    specialist runs at depth >= 1. Non-int/unset -> 0."""
    try:
        return int(os.environ.get("AGENT_RECURSION_DEPTH", "0"))
    except (TypeError, ValueError):
        return 0


def deny_with(node: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"agentctl session is in node={node} — {reason}."
            ),
        }
    }))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    session_id = payload.get("session_id") or ""

    sp = state_path(session_id)
    if not sp.exists():
        return 0

    if not is_gated_path(file_path):
        return 0

    fields = load_gate_fields(sp)
    if fields is None:
        return 0
    weight_class, node, plan_path = fields

    in_plans_dir = is_plan_file(file_path)
    is_tracked = bool(plan_path) and os.path.realpath(file_path) == os.path.realpath(plan_path)
    is_plan = in_plans_dir and (node in PLAN_MUTABLE_NODES or is_tracked)

    # A spawned specialist (AGENT_RECURSION_DEPTH >= 1) is an EXECUTOR of an
    # already-approved stage, not a coordinator: its production-edit authority is
    # inherited from the parent coordinator that passed the plan-approval gate
    # before spawning it (the parent only spawns per an approved stage and verifies
    # the output before record-result). Its own engine auto-starts at UNCLASSIFIED,
    # which would otherwise deny every production write and force the child to fight
    # the gate. So allow a depth>=1 session to edit production CODE.
    #
    # NARROW BY DESIGN: this bypass excludes plan files. is_plan keeps flowing
    # through gate_decision's node-aware plan rule, whose PLAN_MUTABLE_NODES does
    # not include the child's UNCLASSIFIED node -> a spawned executor can never
    # alter an approved plan. Plan integrity is the guarantee; only code editing is
    # unblocked. (A spawned *planner* legitimately reaches a PLAN_MUTABLE node and
    # is governed by the same rule, unchanged.)
    if not is_plan and recursion_depth() >= 1:
        return 0

    decision, reason = gate_decision(weight_class, node, is_plan=is_plan)
    if decision == "deny":
        deny_with(node, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
