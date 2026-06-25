#!/usr/bin/env python3
"""PreToolUse(Bash) hook: nudge to arm the monitoring recipe when a Bash
command looks like it launches a long-running external job.

Rule (CLAUDE.md § Long-running jobs + memory leaf long-job-monitoring):
after starting a long external workflow you must drive it to terminal state
yourself — detached OS poller (zero model tokens) + self-scheduled
ScheduleWakeup wakeups — never offload the monitoring cadence to the user.
This hook lifts the "did I remember to arm the watcher?" recall to a
deterministic launch-pattern scan.

Detection (any one fires):
  - `nohup ` — a detached background process.
  - an orchestrator launch verb: (nirvana|sandbox|reactor|vh3|hitman|yt)
    paired with (start|launch|submit|create|run|exec|operation) in either
    order, e.g. `nirvana ... start`, `yt start-op`, `sandbox create`.

Advisory only: prints to stdout (model context), exit 0 always, never blocks.
Fires once per session (state file) so a launch loop doesn't flood context.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

NOHUP_RE = re.compile(r"\bnohup\b")
# orchestration tool + a launch verb, in either order, within the command
TOOL_RE = re.compile(r"\b(nirvana|sandbox|reactor|vh3|hitman|yt)\b", re.IGNORECASE)
VERB_RE = re.compile(
    r"\b(start[-_]?op|start|launch|submit|create|run|exec|operation|vanilla)\b",
    re.IGNORECASE,
)


def detect(cmd: str) -> str | None:
    """Return a short reason string if the command looks like a long-job launch."""
    if NOHUP_RE.search(cmd):
        return "detached process (nohup)"
    if TOOL_RE.search(cmd) and VERB_RE.search(cmd):
        tool = TOOL_RE.search(cmd).group(1).lower()
        return f"orchestrator launch ({tool})"
    return None


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return Path(f"/tmp/cc-longjob-arm-{safe or 'nosession'}.flag")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd.strip():
        return 0

    reason = detect(cmd)
    if not reason:
        return 0

    sp = state_path(payload.get("session_id") or "")
    if sp.exists():
        return 0  # already nudged this session
    try:
        sp.write_text("1")
    except Exception:
        pass

    print(
        f"[long-job-arm] This command looks like a long external job launch — {reason}.\n"
        "Per CLAUDE.md § Long-running jobs: drive it to terminal state yourself.\n"
        "  → Arm a detached OS poller (nohup watcher, logs every transition, 0 model tokens)\n"
        "    + a self-scheduled ScheduleWakeup to report transitions proactively.\n"
        "  Do NOT offload the monitoring cadence to the user ('ping me when done')."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
