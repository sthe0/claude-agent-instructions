#!/usr/bin/env python3
"""Agent-Session / Agent-Task commit trailer helper.

Difficulty removed: nothing today records which agent session produced a
given commit. A commit subprocess (git, or arc via arc-land-pr.sh) never
receives the PostToolUse hook's stdin JSON, so session_id cannot be read the
way hook-scope-track.py reads it. What IS available to a commit subprocess is
the env var CLAUDE_CODE_SESSION_ID (exported into every Bash call the harness
makes) plus the agentctl state file that session id keys into
(<agentctl_state_dir()>/<session_id>.json — task_id/tracker_key/goal). This
module is the single source of truth for turning that into trailer lines;
both the git commit-msg hook and arc-land-pr.sh call it so the trailer format
is byte-identical across all VCS contexts.

Emits at most two trailer lines:
  Agent-Session: <session_id>   — the immutable pointer back to the transcript.
  Agent-Task: <tracker_key-or-task_id>  — best-effort; OMITTED when neither
    field is present. Deliberately NEVER derived from `goal`: goal is a
    free-text prompt that can carry private/internal detail, and this trailer
    lands in the PUBLIC Core repo (and, via arc-land-pr.sh, in arc history).

Emits nothing (empty list) for a human commit: no CLAUDE_CODE_SESSION_ID, or
no matching agentctl state file. Never raises — a lookup failure here must
never block a commit.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402


def trailers(session_id: "str | None" = None) -> "list[str]":
    """Trailer lines for `session_id` (default: $CLAUDE_CODE_SESSION_ID).

    Empty list when there is no session id, no state file for it, or the
    state file is unreadable/malformed — the caller then injects nothing.
    """
    sid = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        return []
    state_file = config_root.resolve_agentctl_state_file(sid)
    if state_file is None:
        return []
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    lines = [f"Agent-Session: {sid}"]
    task = data.get("tracker_key") or data.get("task_id") or ""
    if task:
        lines.append(f"Agent-Task: {task}")
    return lines


def main() -> int:
    for line in trailers():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
