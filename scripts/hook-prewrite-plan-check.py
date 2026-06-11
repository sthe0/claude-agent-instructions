#!/usr/bin/env python3
"""PreToolUse hook: warn when production code is edited without an approved plan.

The substantive-task coordination cycle in CLAUDE.md requires an approved plan
before editing production files. This hook detects when Edit/Write calls
accumulate on non-plan, non-script files without a plan file being written
first, and emits a one-time nudge.

Threshold: 3 Edit/Write calls on production files in the same session without
a plan file present under ~/.claude/plans/. Three is high enough to ignore
small-change carve-out (one or two edits are fine); low enough to catch
multi-file substantive work before it goes too far.

State is persisted in /tmp/cc-plan-check-<session_id> as a JSON blob:
  {
    "edit_count": <int>,     # Edit/Write calls on production files
    "nudged": <bool>,        # whether the one-shot nudge has fired
    "plan_written": <bool>   # whether a plan file was written this session
  }

The hook also detects Write calls to ~/.claude/plans/**  and sets plan_written
so it stops nudging once a plan exists.

Always exits 0 — failures never block the workflow.
PreToolUse stdout is surfaced to the model as additional context before the
tool runs (unlike PostToolUse stderr).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

EDIT_THRESHOLD = 3

PRODUCTION_FILE_RE = re.compile(
    r"\.(py|sh|yaml|yml|json|ts|tsx|js|jsx|go|rs|cpp|c|h|java|kt|rb|tf|toml|cfg|conf|ini)$",
    re.IGNORECASE,
)

PLAN_DIR = Path.home() / ".claude" / "plans"
PLAN_PATH_RE = re.compile(r"(^|/)\.claude/plans/")


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return Path(f"/tmp/cc-plan-check-{safe or 'nosession'}.json")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"edit_count": 0, "nudged": False, "plan_written": False}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state))
    except Exception:
        pass


def plan_files_exist() -> bool:
    if not PLAN_DIR.exists():
        return False
    return any(PLAN_DIR.glob("*.md"))


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
    state = load_state(sp)

    # Track when a plan file is written
    if PLAN_PATH_RE.search(file_path) or "plan" in file_path.lower():
        state["plan_written"] = True
        save_state(sp, state)
        return 0

    # Check if plan files exist on disk (may have been written before session)
    if plan_files_exist():
        state["plan_written"] = True
        save_state(sp, state)
        return 0

    if state.get("plan_written") or state.get("nudged"):
        return 0

    # Only count production-like files
    if not PRODUCTION_FILE_RE.search(file_path):
        return 0

    # Skip files clearly inside the agent instructions repo or temp dirs
    if any(seg in file_path for seg in ("claude-agent-instructions", "/tmp/", "/.claude/", "/memory/")):
        return 0

    state["edit_count"] = state.get("edit_count", 0) + 1
    save_state(sp, state)

    if state["edit_count"] < EDIT_THRESHOLD:
        return 0

    # Fire the one-time nudge
    state["nudged"] = True
    save_state(sp, state)

    print(
        f"[plan-check] {state['edit_count']} code edits in this session without a plan file.\n"
        "If this is a substantive task (multi-file, architectural decision, tracker ticket),\n"
        "an approved plan is required BEFORE editing — per CLAUDE.md § Carve-out.\n"
        "  Check: ls ~/.claude/plans/\n"
        "  If no plan exists: stop editing, invoke `planner`, get user approval, then continue.\n"
        "  'Approved plan' = a ~/.claude/plans/<slug>.md shown to the user, OR an in-conversation\n"
        "  plan the user explicitly confirmed. Deciding what to do in your head is NOT a plan."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
