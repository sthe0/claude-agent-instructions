#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny a same-turn plan-approval ask.

Difficulty removed: "PLAN-READY: is a hard gate" (CLAUDE.md) requires the user to
have actually SEEN the plan before being asked to approve it — but pre-tool-call
text in a turn may never render (CLAUDE.md § "Approved plan" definition). Three
live failures (2026-07-01..02) had the coordinator author a plan, call
`agentctl submit-plan`, and immediately fire the plan-approval AskUserQuestion in
the SAME turn: the plan text never rendered, so the click-question arrived with
nothing behind it ("Я не вижу плана").

The machine-decidable proxy for "the user has had a turn to read the plan" is
timestamp ordering: hook-engine-start.py stamps `last_user_prompt_ts` on every
UserPromptSubmit; cmd_submit_plan stamps `plan_submitted_ts` when the plan is
submitted. If the session is at node PLAN_READY and the plan was submitted AT OR
AFTER the start of the current turn (plan_submitted_ts >= last_user_prompt_ts),
the plan was authored this same turn — the user has not yet had a turn to read
it, so an AskUserQuestion right now is denied. Once a new prompt arrives (a later
turn), last_user_prompt_ts advances past plan_submitted_ts and the ask is allowed.

Fails open (allow) whenever the observable is missing: no live session, wrong
node, or either timestamp absent (legacy state, or a plan submitted before this
gate existed). Always exits 0 — a hook crash must never wedge the workflow.

DENY uses the same PreToolUse permissionDecision JSON contract as hook-state-gate.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402

resolve_state_path = config_root.resolve_agentctl_state_file

# The only node this gate concerns itself with: the plan-approval hard gate.
GATED_NODE = "PLAN_READY"


def load_gate_fields(path: Path) -> tuple[str | None, float | None, float | None] | None:
    """Return (node, plan_submitted_ts, last_user_prompt_ts). None on unreadable/
    corrupt state or a missing/non-string node, so main() falls through to allow."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    node = data.get("node")
    if not isinstance(node, str):
        return None
    plan_ts = data.get("plan_submitted_ts")
    plan_ts = plan_ts if isinstance(plan_ts, (int, float)) else None
    prompt_ts = data.get("last_user_prompt_ts")
    prompt_ts = prompt_ts if isinstance(prompt_ts, (int, float)) else None
    return node, plan_ts, prompt_ts


def gate_decision(
    node: str, plan_submitted_ts: float | None, last_user_prompt_ts: float | None
) -> tuple[str, str]:
    """Pure decision. Returns ("allow"|"deny", reason)."""
    if node != GATED_NODE:
        return "allow", ""
    if plan_submitted_ts is None or last_user_prompt_ts is None:
        # no observable to compare -> cannot establish "same turn", fail open
        return "allow", ""
    if plan_submitted_ts >= last_user_prompt_ts:
        return "deny", (
            "the plan was submitted this same turn — it cannot have rendered to the "
            "user yet (pre-tool-call text may never render); deliver the plan as this "
            "turn's FINAL text message and ask for approval next turn"
        )
    return "allow", ""


def deny_with(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "AskUserQuestion":
        return 0

    session_id = payload.get("session_id") or ""
    sp = resolve_state_path(session_id)
    if sp is None:
        return 0

    fields = load_gate_fields(sp)
    if fields is None:
        return 0
    node, plan_ts, prompt_ts = fields

    decision, reason = gate_decision(node, plan_ts, prompt_ts)
    if decision == "deny":
        deny_with(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
