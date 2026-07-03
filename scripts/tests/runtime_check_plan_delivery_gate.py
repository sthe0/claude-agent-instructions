#!/usr/bin/env python3
"""Standalone runtime check for hook-plan-delivery-gate.py, driven against a
GIVEN hook file path (argv[1]) rather than an importlib-loaded module — this is
the check `agentctl verify-final` runs against the DEPLOYED shared-tree copy of
the hook, the exact file the live PreToolUse registration executes, to confirm
the fix reached the runtime path and not just the worktree's own copy.

Builds a fixture agentctl state file + session transcript in a tempdir, execs
the given hook via subprocess with the real PreToolUse stdin payload shape, and
asserts: the timer-split sequence (user prompt, plan submitted, then a
`queued_command` turn boundary) produces NO deny, and the same-turn sequence
(plan submitted after the last boundary) produces the deny JSON. Exits 0 only
if both hold; prints a one-line diagnosis and exits 1 otherwise.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def write_state(config_dir: Path, session_id: str, plan_submitted_ts: float, last_user_prompt_ts: float) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "node": "PLAN_READY",
        "plan_submitted_ts": plan_submitted_ts,
        "last_user_prompt_ts": last_user_prompt_ts,
    }
    (state_dir / f"{session_id}.json").write_text(json.dumps(data))


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def run_hook(hook_path: Path, config_dir: Path, session_id: str, transcript_path: Path) -> dict | None:
    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "tool_input": {"questions": [{"question": "Approve the plan?", "options": []}]},
    }
    env = {"PATH": "/usr/bin:/bin", "HOME": str(config_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        print(f"FAIL: hook exited {proc.returncode}, stderr={proc.stderr!r}")
        sys.exit(1)
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        print(f"FAIL: non-JSON hook stdout: {proc.stdout!r}")
        sys.exit(1)


def is_deny(result: dict | None) -> bool:
    return bool(result) and result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: runtime_check_plan_delivery_gate.py <path-to-hook-plan-delivery-gate.py>")
        return 1
    hook_path = Path(sys.argv[1]).resolve()
    if not hook_path.is_file():
        print(f"FAIL: hook file not found: {hook_path}")
        return 1

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp)

        # Timer-split case: user prompt @100, plan submitted @105, the
        # timer-notification queued_command boundary opens the next turn @110.
        write_state(config_dir, "runtime-timer-split", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
        transcript = write_transcript(
            config_dir / "timer-split.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "покажи план"}]}, "timestamp": iso(100.0)},
                {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "<task-notification>timer done</task-notification>"}, "timestamp": iso(110.0)},
            ],
        )
        result = run_hook(hook_path, config_dir, "runtime-timer-split", transcript)
        if is_deny(result):
            print(f"FAIL: timer-split case was denied: {result}")
            ok = False
        else:
            print("PASS: timer-split PLAN_READY ask allowed")

        # Same-turn case: plan submitted @105 with no later turn boundary yet
        # (the last boundary is the user prompt @100) -> still the submitting turn.
        write_state(config_dir, "runtime-same-turn", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
        transcript = write_transcript(
            config_dir / "same-turn.jsonl",
            [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "покажи план"}]}, "timestamp": iso(100.0)}],
        )
        result = run_hook(hook_path, config_dir, "runtime-same-turn", transcript)
        if not is_deny(result):
            print(f"FAIL: same-turn case was NOT denied: {result}")
            ok = False
        else:
            print("PASS: same-turn PLAN_READY ask denied")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
