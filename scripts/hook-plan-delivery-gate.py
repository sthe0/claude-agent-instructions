#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny a same-turn plan-approval ask.

Difficulty removed: "PLAN-READY: is a hard gate" (CLAUDE.md) requires the user to
have actually SEEN the plan before being asked to approve it — but pre-tool-call
text in a turn may never render (CLAUDE.md § "Approved plan" definition). Three
live failures (2026-07-01..02) had the coordinator author a plan, call
`agentctl submit-plan`, and immediately fire the plan-approval AskUserQuestion in
the SAME turn: the plan text never rendered, so the click-question arrived with
nothing behind it ("Я не вижу плана").

The primary observable is the session transcript itself: was the plan submitted
(plan_submitted_ts) before the start of the LATEST turn (transcript_turns.
latest_turn_start)? A turn boundary is either a real user prompt or a
`queued_command` attachment entry — the latter is how a background task-
notification (the timer-split's `sleep 2` completion) opens the next turn
WITHOUT firing a UserPromptSubmit, so hook-engine-start.py's `last_user_prompt_ts`
never advances for it. A gate keyed on that stale timestamp alone would deny the
correct timer-split sequence (plan delivered as the turn's final text, `sleep 2`
started, next turn opens with the ask) as if it were still the submitting turn.
The transcript observable fixes that: it sees the queued_command boundary
directly. `last_user_prompt_ts` remains a fallback for when the transcript is
unavailable (unreadable file, no boundary found — e.g. compaction dropped it) —
in that degraded case the original same-turn check applies, which means the
original false-deny can reappear; that is an accepted residual risk, not a
fail-open hole (see runtime_check_plan_delivery_gate.py for the fallback tests).

The `time.time()` epoch of plan_submitted_ts and the transcript's ISO timestamps
are different clocks, compared with a bare `<`. That is acceptable by design:
the timer split enforces a >=2s gap between plan submission and the next turn's
boundary, well above any plausible clock skew between the two sources, so no
epsilon is applied.

Fails open (allow) whenever every observable is missing: no live session, wrong
node, or (no transcript boundary AND no timestamp pair). Always exits 0 — a hook
crash must never wedge the workflow.

DENY uses the same PreToolUse permissionDecision JSON contract as hook-state-gate.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402
from lib.transcript_turns import latest_turn_start  # noqa: E402

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
    node: str,
    plan_submitted_ts: float | None,
    last_user_prompt_ts: float | None,
    turn_start_ts: float | None = None,
) -> tuple[str, str]:
    """Pure decision. Returns ("allow"|"deny", reason).

    turn_start_ts (from the transcript) is the primary observable when present:
    the plan must have been submitted strictly before the latest turn started.
    When it's unavailable, fall back to the legacy last_user_prompt_ts
    comparison; when both are unavailable, fail open."""
    if node != GATED_NODE:
        return "allow", ""
    if plan_submitted_ts is None:
        # no submission timestamp to compare against -> cannot establish "same turn"
        return "allow", ""
    same_turn_reason = (
        "the plan was submitted this same turn — it cannot have rendered to the "
        "user yet (pre-tool-call text may never render); deliver the plan as this "
        "turn's FINAL text message and ask for approval next turn"
    )
    if turn_start_ts is not None:
        if plan_submitted_ts < turn_start_ts:
            return "allow", ""
        return "deny", same_turn_reason
    if last_user_prompt_ts is None:
        return "allow", ""
    if plan_submitted_ts >= last_user_prompt_ts:
        return "deny", same_turn_reason
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

    turn_start_ts = None
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        turn_start_ts = latest_turn_start(Path(transcript_path))

    decision, reason = gate_decision(node, plan_ts, prompt_ts, turn_start_ts)
    if decision == "deny":
        deny_with(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
