#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge the agent to start / re-arm / consult the agentctl engine.

Companion to hook-state-gate.py (the PreToolUse enforcement twin). The gate hard-blocks
production edits when a live session is parked before EXECUTING; this hook fires on every
user prompt and injects a one-line steer derived purely from the durable session state:

  - No state file       -> engine idle; if the request is substantive / will touch production
                           code, start + classify (the gate bites on the first unclassified edit).
  - Closed prior task    -> (node==RESOLVED, or a CHAT session terminal at ROUTED) re-arm line:
                           if THIS prompt is a new task, `agentctl reset`; else ignore.
  - Live session         -> a status line (task / node / weight) plus a cheap node-derived
                           next-step hint, so the coordinator stays on the deterministic spine.

It NEVER creates or mutates state — it only reads. Corrupt/unreadable state behaves like
"no state" (emit the start line). Always exits 0; a hook crash must never wedge the workflow.

UserPromptSubmit stdin JSON carries session_id / cwd / prompt; stdout becomes additional
turn context (mirrors hook-resolution-reminder.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "agentctl" / "state"

# Nodes from which a fresh prompt may legitimately re-arm the engine for a NEW task.
_CLOSED_NODES = ("RESOLVED",)

# node -> cheap next-step hint for a live session.
_NEXT_HINTS = {
    "CLASSIFIED": "run `agentctl classify`",
    "PLAN_READY": "get explicit user approval, then `agentctl approve --by <user>`",
    "APPROVED": "`agentctl next-stage` to enter EXECUTING",
    "DECOMPOSED": "`agentctl next-stage` to enter EXECUTING",
    "EXECUTING": "production edits allowed; `agentctl record-result` after the stage",
}


def _safe(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def _state_path(session_id: str) -> Path:
    return STATE_ROOT / f"{_safe(session_id)}.json"


def _load_state(session_id: str) -> dict | None:
    """Return the parsed state dict, or None when missing / corrupt / unreadable."""
    if not session_id:
        return None
    try:
        data = json.loads(_state_path(session_id).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _next_hint(node: str) -> str:
    if node == "ROUTED":
        # ROUTED is shared by CHAT (terminal) and SMALL_CHANGE (advances to EXECUTING);
        # the live-session branch only reaches here for SMALL_CHANGE (CHAT@ROUTED is closed).
        return "`agentctl next-stage` to enter EXECUTING"
    return _NEXT_HINTS.get(node, "")


def build_message(session_id: str) -> str:
    data = _load_state(session_id)
    if data is None:
        return (
            f"[engine-start] No agentctl session for this prompt — the coordination engine "
            f"is idle. If this request is substantive or will touch production code, run "
            f"`agentctl start --session {session_id} --if-absent --task <slug> --goal '<goal>'` "
            f"then `agentctl classify ...` to arm the spine. Production edits are gated until "
            f"you classify."
        )

    node = data.get("node")
    weight = data.get("weight_class")
    task = data.get("task_id")

    closed = node in _CLOSED_NODES or (weight == "CHAT" and node == "ROUTED")
    if closed:
        return (
            f"[engine-start] Prior task '{task}' is closed (node={node}). If THIS prompt is a "
            f"NEW task, re-arm the engine: `agentctl reset --session {session_id} --task <slug> "
            f"--goal '<goal>'`. Otherwise ignore this line."
        )

    hint = _next_hint(node or "")
    line = f"[engine-start] Live session: task={task} node={node} weight={weight}."
    if hint:
        line += f" Next: {hint}."
    return line


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    session_id = payload.get("session_id") or ""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        # still nudge on an empty prompt? No — nothing to steer; stay silent.
        return 0

    print(build_message(session_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
