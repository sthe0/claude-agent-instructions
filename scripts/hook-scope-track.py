#!/usr/bin/env python3
"""PostToolUse hook: accumulate a session's filesystem scope into the
session_scope registry (Component A wiring — see scripts/session_scope/registry.py).

Fires on Edit|Write and on Bash. On every fire it heartbeats the session and
refreshes cwd/repo_root/vcs (resolved via `git rev-parse --show-toplevel`, then
`arc root`, else "none" — resolution is skipped and the prior value reused when
cwd has not changed, so it is effectively computed once per session cwd). On
Edit|Write only, it also records the touched file's realpath — a Bash fire never
parses the command string for paths (that stays out of the deterministic rule
part; see hook-scope-conflict.py's future Component B for why).

Memory and scratch paths (agentctl.exempt_paths.is_engine_exempt: /tmp/, /memory/,
/memory-global/, /agent-memory/) are never recorded as touched scope — those
writes are gate-exempt and carry no contention risk.

Strictly non-blocking: always exits 0, never emits a permissionDecision, and
swallows every internal error so a hook failure can never wedge a tool call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl.exempt_paths import is_engine_exempt  # noqa: E402
from session_scope import registry  # noqa: E402

TRACKED_TOOLS = ("Edit", "Write")


def _run(args: "list[str]", cwd: str) -> "str | None":
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=4)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def resolve_repo_root_vcs(cwd: str) -> "tuple[str | None, str]":
    """Detect the VCS root for cwd: git first, then arc, else (None, 'none')."""
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    if root and root.strip():
        return root.strip(), "git"
    root = _run(["arc", "root"], cwd)
    if root and root.strip():
        return root.strip(), "arc"
    return None, "none"


def track(payload: dict, now_ts: float) -> None:
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    existing = registry.load(registry.DEFAULT_SCOPES_DIR, session_id)
    if existing is None or existing.cwd != cwd:
        repo_root, vcs = resolve_repo_root_vcs(cwd)
        registry.set_context(session_id, cwd, repo_root, vcs)

    if tool_name in TRACKED_TOOLS:
        file_path = tool_input.get("file_path") or ""
        if file_path:
            abspath = os.path.realpath(file_path)
            if not is_engine_exempt(abspath):
                registry.record_touch(session_id, abspath)

    registry.heartbeat(session_id, now_ts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        track(payload, time.time())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
