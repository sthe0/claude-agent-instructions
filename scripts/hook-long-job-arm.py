#!/usr/bin/env python3
"""PreToolUse(Bash) hook: nudge to arm the monitoring recipe when a Bash
command looks like it launches a long-running external job.

Rule (CLAUDE.md § Long-running jobs + memory leaf long-job-monitoring):
after starting a long external workflow you must drive it to terminal state
yourself — detached OS poller (zero model tokens) for durability + a
harness-tracked `Bash(run_in_background)` waiter for auto-wake (ScheduleWakeup
works only inside /loop, so it silently no-ops in an ordinary session) —
never offload the monitoring cadence to the user. This hook lifts the "did I
remember to arm the watcher?" recall to a deterministic launch-pattern scan.

Detection (any one fires):
  - `nohup ` — a detached background process.
  - an orchestrator launch verb: a name from the orchestrator list paired with
    (start|launch|submit|create|run|exec|operation) in either order, e.g.
    `nirvana ... start`, `yt start-op`, `sandbox create`.

The orchestrator name list is operator-configurable so this works in any org:
set `long_job_orchestrators=name1,name2` (comma/space-separated) in the system
config root's `agent-identity.local` (resolved via scripts/lib/config_root.py:
`~/.claude-agent` when isolated). When the key is absent the built-in default
(Yandex orchestrators) is used, so an unconfigured machine behaves unchanged.

Advisory only: prints to stdout (model context), exit 0 always, never blocks.
Fires once per session (state file) so a launch loop doesn't flood context.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The launch-pattern scan lives in the importable sibling module so the turn-end
# guardian (hook-turn-end-gate.py) consumes the SAME detect() and cannot drift
# from this advisory. Re-export the exact names the advisory's tests import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from long_job_detect import (  # noqa: E402,F401
    NOHUP_RE,
    DEFAULT_ORCHESTRATORS,
    TOOL_RE,
    VERB_RE,
    detect,
    _orchestrator_names,
    _build_tool_re,
)


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
        "    for durability across sessions,\n"
        "  → AND a harness-tracked Bash(run_in_background:true) waiter that blocks on the job\n"
        "    and prints a terminal marker — the harness auto-wakes you when it exits.\n"
        "    (ScheduleWakeup only works inside /loop; outside it, it silently no-ops.)\n"
        "  Do NOT offload the monitoring cadence to the user ('ping me when done')."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
