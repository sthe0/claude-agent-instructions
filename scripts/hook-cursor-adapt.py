#!/usr/bin/env python3
"""Cursor hook adapter around existing hook-*.py scripts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import config_root, cursor_hook_contract, hook_registry  # noqa: E402

STATE_DIR = config_root.agent_home() / "state" / "cursor-hook-sessions"


def _session_id(payload: dict) -> str:
    for key in ("conversation_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bind_session(session_id: str) -> None:
    agentctl_session = os.environ.get("AGENTCTL_SESSION", "").strip()
    if not session_id or not agentctl_session:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{session_id}.json").write_text(
        json.dumps({"agentctl_session": agentctl_session}), encoding="utf-8"
    )


def _resolve_session_id(payload: dict) -> str:
    session_id = _session_id(payload)
    if session_id:
        bound = STATE_DIR / f"{session_id}.json"
        if bound.is_file():
            try:
                mapped = json.loads(bound.read_text(encoding="utf-8")).get("agentctl_session")
                if isinstance(mapped, str) and mapped.strip():
                    return mapped.strip()
            except Exception:
                pass
        return session_id
    return os.environ.get("AGENTCTL_SESSION", "").strip()


def _rows(cursor_event: str) -> list[dict]:
    return [
        entry
        for entry in hook_registry.load_registry()["hooks"]
        if entry.get("cursor_event") == cursor_event and entry.get("claude_event")
    ]


def _run(command: str, claude_payload: dict, timeout: int) -> tuple[int, str, str]:
    parts = command.split()
    cmd = [sys.executable, str(SCRIPTS_DIR / parts[0]), *parts[1:]]
    proc = subprocess.run(
        cmd,
        input=json.dumps(claude_payload),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    try:
        cursor_payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"permission": "allow"}))
        return 0

    cursor_event = os.environ.get("CURSOR_HOOK_EVENT") or cursor_payload.get("hook_event_name") or ""
    if cursor_event == "sessionStart":
        _bind_session(_session_id(cursor_payload))

    rows = _rows(str(cursor_event))
    if not rows:
        print(json.dumps(cursor_hook_contract.fail_open_output(str(cursor_event))))
        return 0

    session_id = _resolve_session_id(cursor_payload)
    denied = False
    reasons: list[str] = []
    texts: list[str] = []
    try:
        for row in rows:
            matcher = row.get("cursor_matcher") or cursor_hook_contract.cursor_matcher_for(
                row.get("claude_matcher")
            )
            tool_name = str(cursor_payload.get("tool_name") or "")
            if matcher:
                import re

                if not re.search(matcher, tool_name):
                    continue
            claude_payload = cursor_hook_contract.to_claude_payload(
                cursor_payload, row["claude_event"], session_id
            )
            code, stdout, stderr = _run(row["command"], claude_payload, int(row.get("timeout") or 30))
            parsed = cursor_hook_contract.parse_claude_result(code, stdout, stderr)
            denied = denied or parsed["denied"]
            if parsed["reason"]:
                reasons.append(parsed["reason"])
            if parsed["text"]:
                texts.append(parsed["text"])
    except subprocess.TimeoutExpired:
        if any(row.get("cursor_fail_closed") for row in rows):
            print(json.dumps(cursor_hook_contract.fail_closed_output(str(cursor_event), "timeout")))
        else:
            print(json.dumps(cursor_hook_contract.fail_open_output(str(cursor_event))))
        return 0
    except Exception:
        if any(row.get("cursor_fail_closed") for row in rows):
            print(json.dumps(cursor_hook_contract.fail_closed_output(str(cursor_event), "hook failed")))
        else:
            print(json.dumps(cursor_hook_contract.fail_open_output(str(cursor_event))))
        return 0

    output = cursor_hook_contract.to_cursor_output(str(cursor_event), denied, reasons, texts)
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
