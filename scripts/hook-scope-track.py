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

It also resolves and records the durable session process's pid (once per
session, cached thereafter — see session_pid()) so hook-scope-conflict.py's
live_pid_check can tell a genuinely dead session from a merely TTL-fresh one.
This hook process's own pid is useless for that: it is spawned fresh per tool
call and is already gone by the time anyone probes it later. Rather than
hardcode how many transient wrapper layers (e.g. a shell -c invoking this
script) sit between the durable session process and this hook — which is
harness-version-dependent and not something this sandbox could verify against
a real invocation — session_pid() walks the ancestor chain and picks the
first ancestor whose elapsed run time is measurably older than this hook's
own, i.e. a process that already existed before this tool call started. That
self-adapts to however many wrapper layers actually exist instead of
guessing a fixed depth.

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


MAX_ANCESTOR_DEPTH = 8
MIN_EXTRA_AGE_S = 2.0


def _ppid_of(pid: int) -> "int | None":
    out = _run(["ps", "-o", "ppid=", "-p", str(pid)], cwd="/")
    if not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _etime_of(pid: int) -> "str | None":
    out = _run(["ps", "-o", "etime=", "-p", str(pid)], cwd="/")
    if not out or not out.strip():
        return None
    return out.strip()


def parse_etime(text: str) -> "float | None":
    """Parse ps's etime format ([[DD-]HH:]MM:SS) into elapsed seconds."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    fields = text.split(":")
    try:
        fields = [int(f) for f in fields]
    except ValueError:
        return None
    if len(fields) == 3:
        hours, minutes, seconds = fields
    elif len(fields) == 2:
        hours, minutes, seconds = 0, fields[0], fields[1]
    elif len(fields) == 1:
        hours, minutes, seconds = 0, 0, fields[0]
    else:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _elapsed_s(pid: int) -> "float | None":
    etime = _etime_of(pid)
    return parse_etime(etime) if etime is not None else None


def session_pid(
    max_depth: int = MAX_ANCESTOR_DEPTH, min_extra_age_s: float = MIN_EXTRA_AGE_S
) -> "int | None":
    """Best-effort pid of the durable session process hosting this hook
    invocation. See the module docstring for why this can't just be
    os.getppid(). Returns None (safe fallback: pid unknown, heartbeat-only
    semantics preserved) when ps is unavailable, ancestry can't be resolved,
    or nothing qualifies within max_depth — never guesses when uncertain,
    since a wrongly-identified pid mistaken for a live session's identity
    would suppress real conflict detection for that session for its entire
    life (see registry.live_pid_check's asymmetry note).
    """
    my_age = _elapsed_s(os.getpid())
    if my_age is None:
        return None
    pid = os.getpid()
    for _ in range(max_depth):
        parent = _ppid_of(pid)
        if parent is None or parent <= 1:
            return None
        age = _elapsed_s(parent)
        if age is not None and age - my_age >= min_extra_age_s:
            return parent
        pid = parent
    return None


def track(payload: dict, now_ts: float) -> None:
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    existing = registry.load(registry.DEFAULT_SCOPES_DIR, session_id)
    if existing is None or existing.cwd != cwd:
        repo_root, vcs = resolve_repo_root_vcs(cwd)
        registry.set_context(session_id, cwd, repo_root, vcs)

    lineage_ids = registry.parse_lineage(os.environ.get("AGENT_LINEAGE_IDS"))
    if lineage_ids:
        registry.record_lineage(session_id, lineage_ids)

    if tool_name in TRACKED_TOOLS:
        file_path = tool_input.get("file_path") or ""
        if file_path:
            abspath = os.path.realpath(file_path)
            if not is_engine_exempt(abspath):
                registry.record_touch(session_id, abspath)

    # Resolve the session pid once (first fire) and reuse it thereafter — it
    # never changes for the life of the session, and re-walking ancestry via
    # ps on every single tool call would be needless overhead.
    pid = existing.pid if existing is not None else None
    if pid is None:
        pid = session_pid()

    registry.heartbeat(session_id, now_ts, pid=pid)


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
