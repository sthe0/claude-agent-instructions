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
the session_id is sanitized to alnum/-/_ exactly as FileStateStore does), where session_id
is the payload's, or the engine session lib/engine_session_binding.py records that harness
session as driving. A production edit is also denied while another engine session that harness
session drove is SUBSTANTIVE and still before plan approval (pending_substantive_session).
<agent-home> resolves via lib/config_root.py (isolated ~/.claude-agent when present, else
legacy ~/.claude). A session file left behind by a not-yet-migrated root is still found —
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
from agentctl.exempt_paths import (  # noqa: E402
    is_gated_path, is_own_plan_file, is_plan_file, own_plan_owner,
)
from agentctl.norm_handoff import NORM_HANDOFF_ENV  # noqa: E402
from agentctl.own_contour import OWN_SESSION_ENV  # noqa: E402
from agentctl.state import SessionState as _SessionState  # noqa: E402
from lib import config_root, engine_session_binding  # noqa: E402

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


def load_gate_fields(path: Path) -> tuple[str | None, str, str | None, bool] | None:
    """Return (weight_class, node, plan_path, linked) from the state file. weight_class
    and plan_path may be None; `linked` is whether the session holds a norm handoff
    (`parent_link`). Corrupt/unreadable state or a missing/non-string node -> None, so
    main() falls through to allow unless the env carries an own-contour signal."""
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
    return weight, node, plan_path, isinstance(data.get("parent_link"), dict)


def foreign_plan_owner(file_path: str, own_state: Path) -> tuple[Path, str | None, str] | None:
    """(state_path, weight_class, node) of a live (node != RESOLVED) session other
    than the writer whose plan_path is file_path, else None. Unreadable or malformed
    state files are skipped: the ownership lookup fails open, never the write."""
    target = os.path.realpath(file_path)
    dirs = [config_root.agentctl_state_dir(), config_root.agentctl_legacy_state_dir()]
    for d in dict.fromkeys(dirs):
        try:
            entries = sorted(d.glob("*.json"))
        except OSError:
            continue
        for entry in entries:
            # the writer's own session may have a stale copy in the other state dir
            if entry.name == own_state.name:
                continue
            fields = load_gate_fields(entry)
            if fields is None:
                continue
            weight, node, plan_path, _linked = fields
            if node == "RESOLVED" or not isinstance(plan_path, str) or not plan_path:
                continue
            if os.path.realpath(plan_path) == target:
                return entry, weight, node
    return None


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


def contour_signal_denial(session_id: str, linked: bool) -> str | None:
    """Why a session that carries an own-contour signal in its env may not edit, else
    None. The signals are `AGENTCTL_OWN_SESSION` and the norm handoff; each is exported
    only to a child whose own engine session adopted that handoff (`parent_link`), so
    one without the other means the child's contour is not the one the spawn built, and
    the depth bypass is not available to it either."""
    own = os.environ.get(OWN_SESSION_ENV, "").strip()
    handoff = os.environ.get(NORM_HANDOFF_ENV, "").strip()
    if (own or handoff) and not linked:
        named = OWN_SESSION_ENV if own else NORM_HANDOFF_ENV
        return (
            f"{named} is set but this session holds no norm handoff (parent_link), so its "
            "own contour is unverified; a linked child's engine adopts the handoff at "
            "start — report this to your parent instead of editing"
        )
    if own and config_root.sanitize_session_id(own) != config_root.sanitize_session_id(session_id):
        return (
            f"{OWN_SESSION_ENV} names a session other than this one; the channel "
            "scripts/agentctl-own.py is pinned to a session this hook does not gate"
        )
    return None


def linked_decision(weight_class: str | None, node: str) -> tuple[str, str]:
    """The gate for a linked child's production code: it is an executor of the stage
    its own plan was approved for, so only the execution nodes release an edit — CHAT
    included, which the root gate lets through before EXECUTING. The reason names the
    channel the child moves its own node with."""
    if node in ALLOW_NODES:
        return "allow", ""
    _, reason = gate_decision(weight_class, node)
    reason = reason or f"production edits are released only at {', '.join(sorted(ALLOW_NODES))}"
    return "deny", (
        f"{reason}; as a linked child drive your own session with "
        "`python3 <scripts>/agentctl-own.py <verb>` (classify, plan, submit-plan, "
        "approve --by agent, next-stage …)"
    )


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


def has_own_contour_signal() -> bool:
    return bool(os.environ.get(OWN_SESSION_ENV, "").strip() or os.environ.get(NORM_HANDOFF_ENV, "").strip())


def driven_engine_session(harness_session: str) -> str:
    """The engine session whose node decides an edit from `harness_session`: the one the
    cli last bound to it (lib/engine_session_binding.py), else the harness id itself.
    A lookup failure falls back to the harness id, as for an unbound session. A linked
    child (an own-contour signal in the env) is never redirected: its contour is pinned to
    its own session, which is the harness id, exactly as before the binding existed."""
    if has_own_contour_signal():
        return harness_session
    try:
        return engine_session_binding.resolve(harness_session) or harness_session
    except Exception:
        return harness_session


# Nodes where a substantive session still awaits (or re-authors) its plan approval.
PENDING_PLAN_NODES = PLAN_WRITE_POSITION_NODES


def pending_substantive_session(harness_session: str, read_state: Path) -> tuple[str, str] | None:
    """(session, node) of a substantive engine session `harness_session` drove that still
    awaits plan approval, other than the one the gate reads (`read_state`), else None. Without it a
    coordinator whose plan awaits approval could release production edits by driving a
    side session onto an execution node. Only sessions bound by a state-changing verb
    count (the harness id itself when it drove its own session). An unreadable binding or
    state file is skipped: the lookup fails open, never the edit."""
    if has_own_contour_signal():
        return None
    try:
        driven = engine_session_binding.driven_sessions(harness_session)
    except Exception:
        return None
    for session in driven:
        if session == read_state.stem:
            continue
        path = resolve_state_path(session)
        fields = load_gate_fields(path) if path is not None else None
        if fields is None:
            continue
        weight, node, _plan_path, _linked = fields
        if weight == "SUBSTANTIVE" and node in PENDING_PLAN_NODES:
            return session, node
    return None


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

    # A linked child's own plan (`own_plans_dir()/<sid>/plan.toml`) belongs to that child
    # alone. Decided before every early return below: a scratch-rooted alias or a writer
    # with no engine state must not slip past it.
    writer = config_root.sanitize_session_id(session_id)
    own_plan = None
    plan_owner = own_plan_owner(file_path)
    if plan_owner is not None:
        own_plan = is_own_plan_file(file_path)
        if config_root.sanitize_session_id(plan_owner) != writer or own_plan != plan_owner:
            deny_with("(own plan)", (
                f"foreign-own-plan: {file_path} is the own plan of session {plan_owner}; "
                "only that session writes it, through its canonical path, while it plans"
            ))
            return 0

    sp = resolve_state_path(driven_engine_session(session_id))
    if sp is None:
        # No engine state: allow, unless the env says this is a linked child — then the
        # missing state is a contour that never started, not a session nobody drives.
        if is_gated_path(file_path):
            denial = contour_signal_denial(session_id, linked=False)
            if denial is not None:
                deny_with("(no engine state)", denial)
        return 0

    if own_plan is None and not is_gated_path(file_path):
        return 0

    fields = load_gate_fields(sp)
    if fields is None:
        # State that cannot be read says nothing about a link: the contour signals
        # alone decide whether this is a child whose contour is not verifiable.
        denial = contour_signal_denial(session_id, linked=False)
        if denial is not None:
            deny_with("(unreadable engine state)", denial)
        return 0
    weight_class, node, plan_path, linked = fields

    denial = contour_signal_denial(session_id, linked)
    if denial is not None:
        deny_with(node, denial)
        return 0

    # An own plan counts as a plan at every node; gate_decision then freezes it past planning.
    in_plans_dir = is_plan_file(file_path)
    is_tracked = bool(plan_path) and os.path.realpath(file_path) == os.path.realpath(plan_path)
    is_plan = in_plans_dir and (
        node in PLAN_WRITE_POSITION_NODES or is_tracked or own_plan is not None
    )

    # A plan file owned by another live session is decided by THAT session's node:
    # the writer's node only says what the writer may do with its own work. Checked
    # before the depth bypass because a spawned child starts at CLASSIFIED, a
    # plan-mutable node, and must not thereby rewrite its parent's approved plan.
    # An owner at PLAN_READY allows the write on purpose: a child may refine its
    # parent's plan awaiting approval, and approval binds a digest, so drift shows.
    if in_plans_dir:
        owner = foreign_plan_owner(file_path, sp)
        if owner is not None:
            owner_path, owner_weight, owner_node = owner
            owner_ok = owner_node == "DIAGNOSING" and diagnosing_plan_write_ok(owner_path)
            decision, reason = gate_decision(owner_weight, owner_node, is_plan=True,
                                             diagnosing_plan_ok=owner_ok)
            if decision == "deny":
                deny_with(owner_node, (
                    f"this plan belongs to another live session ({owner_path.stem}, "
                    f"node={owner_node}); {reason}. If that session is abandoned, "
                    "release the plan with `agentctl reset` or resolve in that session"
                ))
            return 0

    # A spawned specialist (AGENT_RECURSION_DEPTH >= 1) is an EXECUTOR of an
    # already-approved stage, not a coordinator: its production-edit authority is
    # inherited from the parent coordinator that passed the plan-approval gate.
    # Its own engine auto-starts at CLASSIFIED, which would otherwise deny every
    # production write, so a depth>=1 session may edit production CODE.
    #
    # Plan files stay gated: is_plan flows through gate_decision's node-aware rule
    # (the child's own plan only at its planning-position nodes), and a plan owned
    # by another live session is governed by the owner check above.
    #
    # A LINKED child (its engine holds the parent stage's norm handoff) has a contour
    # of its own, so it never takes this bypass: its code edits are released by its own
    # node, which it moves through scripts/agentctl-own.py.
    if linked and not is_plan:
        decision, reason = linked_decision(weight_class, node)
        if decision == "deny":
            deny_with(node, reason)
        return 0
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

    if not in_plans_dir:
        pending = pending_substantive_session(session_id, sp)
        if pending is not None:
            pending_session, pending_node = pending
            deny_with(pending_node, (
                f"engine session {pending_session} (substantive) is driven by this session "
                "and has not passed plan approval; approve it, or resolve/reset it, before "
                "editing production code through another session"
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
