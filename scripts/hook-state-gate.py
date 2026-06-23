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
import re
import sys
from pathlib import Path

PRODUCTION_FILE_RE = re.compile(
    r"\.(py|sh|yaml|yml|json|ts|tsx|js|jsx|go|rs|cpp|c|h|java|kt|rb|tf|toml|cfg|conf|ini)$",
    re.IGNORECASE,
)

STATE_ROOT = Path.home() / ".claude" / "agentctl" / "state"

# Same skip set as hook-prewrite-plan-check.py: the instructions repo and
# scratch/config trees are never gated (editing them is meta-work, not the task's
# production code).
SKIP_SEGMENTS = ("claude-agent-instructions", "/tmp/", "/.claude/", "/memory/")


def _safe(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def state_path(session_id: str) -> Path:
    return STATE_ROOT / f"{_safe(session_id)}.json"


def load_node(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    node = data.get("node")
    return node if isinstance(node, str) else None


def deny(node: str) -> None:
    reason = (
        f"agentctl session is in node={node}, not EXECUTING — the plan-approval "
        "gate has not been passed. Production edits are allowed only after the plan "
        "is approved (run `agentctl approve --by <user>` once the user has explicitly "
        "approved the plan, which moves the session to APPROVED -> EXECUTING)."
    )
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

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    session_id = payload.get("session_id") or ""

    sp = state_path(session_id)
    if not sp.exists():
        return 0

    node = load_node(sp)
    if node is None or node == "EXECUTING":
        return 0

    if not PRODUCTION_FILE_RE.search(file_path):
        return 0

    if any(seg in file_path for seg in SKIP_SEGMENTS):
        return 0

    deny(node)
    return 0


if __name__ == "__main__":
    sys.exit(main())
