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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl.exempt_paths import is_gated_path  # noqa: E402
from lib import config_root  # noqa: E402

EDIT_THRESHOLD = 3

PLAN_DIR = config_root.plans_dir()
LEGACY_PLAN_DIR = config_root.legacy_home() / "plans"
PLAN_PATH_RE = re.compile(r"(^|/)\.claude(-agent)?/plans/")


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
    # A plan may be a TOML (the planner's deliverable the engine tracks) or a
    # markdown file (the non-substantive prose form). Detect either — globbing
    # only *.md silently missed every substantive session's TOML plan.
    dirs = {PLAN_DIR, LEGACY_PLAN_DIR}
    return any(
        d.exists() and (any(d.glob("*.toml")) or any(d.glob("*.md")))
        for d in dirs
    )


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

    # Only count gated edits: a production file the engine governs (memory /
    # scratch / plan artifacts are exempt — see agentctl.exempt_paths).
    if not is_gated_path(file_path):
        return 0

    state["edit_count"] = state.get("edit_count", 0) + 1
    save_state(sp, state)

    if state["edit_count"] < EDIT_THRESHOLD:
        return 0

    # Fire the one-time nudge
    state["nudged"] = True
    save_state(sp, state)

    try:
        ledger = config_root.agentctl_dir() / "prewrite-fallback.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as _fh:
            _fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "edit_count": state["edit_count"],
                "cwd": os.getcwd(),
            }) + "\n")
    except Exception:
        pass

    print(
        f"[plan-check] {state['edit_count']} code edits in this session without a plan file.\n"
        "If this is a substantive task (multi-file, architectural decision, tracker ticket),\n"
        "an approved plan is required BEFORE editing — per CLAUDE.md § Carve-out.\n"
        "  Check: ls ~/.claude/plans/\n"
        "  If no plan exists: stop editing, invoke `planner`, get user approval, then continue.\n"
        "  'Approved plan' = a ~/.claude/plans/<slug>.toml (the planner's deliverable the engine\n"
        "  tracks) shown to the user, OR an in-conversation plan the user explicitly confirmed.\n"
        "  Deciding what to do in your head is NOT a plan."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
