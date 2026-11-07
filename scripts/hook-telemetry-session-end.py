#!/usr/bin/env python3
"""SessionEnd hook: mark the session ended for the telemetry record and start its sync detached.

Difficulty removed: a session's events sit in its spool until somebody replays them, and the
session that wrote them is the one party that knows it is over. This hook writes the end marker
`<spool dir>/<session>.end` (the sync's proof the session ended, and its time E) and starts
`telemetry.py sync --session <id>` in its own session, output discarded, then returns without
waiting; a sync that wedges on a locked record must not hold the harness's exit. It spawns by
subprocess and imports neither the record nor sqlite3: a hook never opens the record.

Fail-open and silent: it prints nothing and exits 0 on every path, malformed stdin and a spawn
failure included. `AGENTCTL_TELEMETRY=off` makes it do nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lib import hook_telemetry
from lib.telemetry import emit, live

SCRIPTS_DIR = Path(__file__).resolve().parent


def _session_id() -> "str | None":
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return None
    value = payload.get("session_id") if isinstance(payload, dict) else None
    return emit.session_file_stem(value)


def _write_end_marker(session: str) -> None:
    emit.ensure_private_dir(live.spool_dir())
    live.append_line(live.end_marker_path(session), emit.now_iso().encode("utf-8") + b"\n")


def _start_sync(session: str) -> None:
    subprocess.Popen(
        [sys.executable, str(SCRIPTS_DIR / "telemetry.py"), "sync", "--session", session],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=str(SCRIPTS_DIR))


def main() -> int:
    session = _session_id()
    if session is None or not live.enabled():
        return 0
    try:
        _write_end_marker(session)
    except Exception:
        pass
    try:
        _start_sync(session)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(hook_telemetry.run(main, mechanism="hook-telemetry-session-end"))
