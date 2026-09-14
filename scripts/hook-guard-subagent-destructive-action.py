#!/usr/bin/env python3
"""PreToolUse hook: deny a subagent (spawned/forked) issuing a destructive filesystem
command on its own authority.

Difficulty removed: a forked subagent briefed for read-only investigation instead posed
and answered its own AskUserQuestion, then ran `rm -rf` against 24 VCS mount-registry
entries and ~54GB of their store directories -- entirely outside the agentctl
plan-approval spine, discovered only afterward (2026-09-14 incident). CLAUDE.md's own
"Executing actions with care" rule already says a destructive/hard-to-reverse action
needs confirmation; the incident showed a subagent's own self-answered AskUserQuestion
inside its own tool loop is not that confirmation -- only the root session's own gate
counts. Prose alone did not stop this; this hook mechanizes the rule part.

The gate is decidable from two observable inputs on the PreToolUse payload, no semantic
judgment required: (1) is this call issued by a subagent, not the root session -- the
payload carries `agent_id` (equivalently `agent_type`) only for a subagent call, never
for a root-issued call (empirically verified 2026-09-15, see
plans/subagent-destructive-guard-payload-diff.md for the captured evidence); (2) is the
command a destructive filesystem delete -- reusing hook-guard-destructive-rm.py's own
recursive-rm detector (`_rm_recursive_targets`) rather than re-deriving it, per the
self-improvement policy's "extend the existing mechanism" tie-breaker.

Deliberately does NOT touch hook-guard-destructive-rm.py's PROTECTED_PATHS or add any
~/.arc path to it -- that narrower, path-specific extension is measure C, which the user
explicitly did not approve for the 2026-09-14 incident. This hook keys ONLY on
subagent-origin + destructive-command, regardless of target path: a subagent must never
self-authorize a recursive delete of ANYTHING outside its own scratch/worktree, not only
of the agent's own critical dirs.

Narrow by design: a root-issued recursive rm is untouched (still governed only by
hook-guard-destructive-rm.py's critical-path check); a subagent's non-destructive Bash
calls are untouched. Always exits 0 -- a hook crash must never wedge the workflow; any
unexpected error falls through to allow.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout, matching
hook-guard-destructive-rm.py's own contract:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_rm_target_detector():
    """Import `_rm_recursive_targets` from the sibling hook-guard-destructive-rm.py by
    file path, so this hook reuses that detector instead of duplicating its shlex/regex
    logic -- reuse, not a copy, of the existing destructive-rm gate."""
    sibling = Path(__file__).resolve().parent / "hook-guard-destructive-rm.py"
    spec = importlib.util.spec_from_file_location("hook_guard_destructive_rm", sibling)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module._rm_recursive_targets


def _is_subagent_call(payload: dict) -> bool:
    # Empirically verified discriminator (2026-09-15 capture-diff): a subagent-issued
    # PreToolUse payload carries `agent_id` (and `agent_type`); a root-issued one does
    # not carry either. `session_id` is unusable here -- a fork shares the parent's.
    return bool(payload.get("agent_id")) or bool(payload.get("agent_type"))


def decide(payload: dict, rm_recursive_targets) -> str | None:
    if payload.get("tool_name", "") != "Bash":
        return None
    if not _is_subagent_call(payload):
        return None  # root-issued call: not this hook's concern

    tool_input = payload.get("tool_input") or {}
    command = (tool_input.get("command") or "").strip()
    if not command:
        return None

    targets = rm_recursive_targets(command)
    if not targets:
        return None

    agent_id = payload.get("agent_id") or "?"
    agent_type = payload.get("agent_type") or "?"
    return (
        f"Refusing a recursive rm issued by a subagent (agent_id={agent_id!r}, "
        f"agent_type={agent_type!r}) against {targets!r}. A subagent must never "
        "self-authorize a destructive/irreversible filesystem action, even having "
        "answered 'yes' to its own AskUserQuestion inside its own tool loop -- only the "
        "root session's own coordination-spine gate counts. If this deletion is "
        "actually warranted, stop and hand the finding back to the root session's "
        "terminal report; the root will confirm with the user and run it itself."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        rm_recursive_targets = _load_rm_target_detector()
        reason = decide(payload, rm_recursive_targets)
    except Exception:
        return 0  # never wedge the workflow on an unexpected error

    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))

    return 0


if __name__ == "__main__":
    sys.exit(main())
