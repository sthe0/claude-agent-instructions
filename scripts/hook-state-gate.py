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

State file: <agent-home>/agentctl/state/<session_id>.json (see scripts/agentctl/store.py;
the session_id is sanitized to alnum/-/_ exactly as FileStateStore does). <agent-home>
resolves via lib/config_root.py (isolated ~/.claude-agent when present, else legacy
~/.claude). A session file left behind by a not-yet-migrated root is still found —
resolve_state_path() checks the legacy root too — so the gate fails CLOSED (keeps
blocking) rather than open during a half-migrated transition.

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
from agentctl import gates as _gates  # noqa: E402
from agentctl.exempt_paths import is_gated_path, is_plan_file  # noqa: E402
from agentctl.state import SessionState as _SessionState  # noqa: E402
from lib import config_root  # noqa: E402

# Current-vs-legacy-root fallback lookup lives in lib/config_root.py (shared
# with the other state-reading hooks); resolve_state_path is a thin alias kept
# here so this hook's own docstring reference to it still resolves.
resolve_state_path = config_root.resolve_agentctl_state_file


# Nodes where production edits are legitimate regardless of weight class: the
# plan-approval gate (or the small-change carve-out) has already been passed.
ALLOW_NODES = {"EXECUTING", "VERIFYING", "RESOLUTION"}

# Nodes that constitute the "planning position": where a plan file is the live
# result-image being authored or refined. A plan write is legitimate only here.
# Changing a plan during execution is a difficulty to be overcome reflexively
# (overcome-difficulty -> replan_substantive re-arms at PLAN_READY), so writes at
# every later node are denied with a pointer back to that path.
PLAN_MUTABLE_NODES = {"CLASSIFIED", "ROUTED", "PLANNING", "PLAN_READY"}

# Nodes where a plan-dir file is treated as the live plan being authored (so is_plan
# routes it through the node-aware plan rule). Superset of PLAN_MUTABLE_NODES with
# DIAGNOSING (#9): the corrected plan is authored there, but ONLY once the difficulty
# record is complete — gate_decision applies that conditional guard, not blanket allow.
PLAN_WRITE_POSITION_NODES = PLAN_MUTABLE_NODES | {"DIAGNOSING"}


def diagnosing_plan_write_ok(path: Path) -> bool:
    """At DIAGNOSING a plan write is authoring the CORRECTED plan — legitimate iff the
    difficulty record is complete (the SAME predicate that unblocks `replan`). Reuse
    gates.difficulty_blockers rather than re-deriving completeness from the raw JSON,
    so the hook and the engine can never diverge on what 'complete' means. Any load
    error -> False (keep blocking): a plan write is released only on a
    positively-confirmed complete record."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = _SessionState.from_dict(data)
    except Exception:
        return False
    return not _gates.difficulty_blockers(state)


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


def gate_decision(weight_class: str | None, node: str, is_plan: bool = False,
                  diagnosing_plan_ok: bool = False) -> tuple[str, str]:
    """Pure weight-aware gate. Returns ("allow"|"deny", reason).

    Plan files get a node-aware rule that overrides the standard one: a plan is the
    result-image of active planning, writable only at a planning-position node.
    This is checked first because EXECUTING is in ALLOW_NODES, yet a plan write at
    EXECUTING must be denied (it is a difficulty to overcome via replan).

    DIAGNOSING is the one execution-side node where authoring a plan IS legitimate
    (#9): it is where the corrected plan is written before `replan`. But only once
    the difficulty record is complete — `diagnosing_plan_ok` (computed by the caller
    from gates.difficulty_blockers) carries that predicate in."""
    if is_plan:
        if node in PLAN_MUTABLE_NODES:
            return "allow", ""
        if node == "DIAGNOSING":
            if diagnosing_plan_ok:
                return "allow", ""
            return "deny", (
                "authoring the corrected plan at DIAGNOSING is allowed only once the "
                "difficulty record is complete (declare -> investigate -> critique); "
                "complete the cycle, then edit the plan"
            )
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

    sp = resolve_state_path(session_id)
    if sp is None:
        return 0

    if not is_gated_path(file_path):
        return 0

    fields = load_gate_fields(sp)
    if fields is None:
        return 0
    weight_class, node, plan_path = fields

    in_plans_dir = is_plan_file(file_path)
    is_tracked = bool(plan_path) and os.path.realpath(file_path) == os.path.realpath(plan_path)
    is_plan = in_plans_dir and (node in PLAN_WRITE_POSITION_NODES or is_tracked)

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

    # #9: at DIAGNOSING a plan write authors the CORRECTED plan — release it only
    # when the difficulty record is complete, reusing the engine's own replan
    # precondition (gates.difficulty_blockers) so the two never diverge.
    diagnosing_plan_ok = node == "DIAGNOSING" and is_plan and diagnosing_plan_write_ok(sp)

    decision, reason = gate_decision(weight_class, node, is_plan=is_plan,
                                     diagnosing_plan_ok=diagnosing_plan_ok)
    if decision == "deny":
        deny_with(node, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
