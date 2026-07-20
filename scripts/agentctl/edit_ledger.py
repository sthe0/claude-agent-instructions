"""Durable append-only session -> edited-file ledger.

Difficulty removed: the session_scope registry (session_scope/registry.py)
records touched paths, but it exists for LIVE cross-session conflict
detection — capped at MAX_TOUCHED_PATHS, deduped, no per-touch timestamp, and
pruned when a session's heartbeat goes stale. It cannot answer "which session
edited file Y at time T" after the fact. This module is the durable,
uncapped, append-only record that can: one JSON line per Edit|Write, written
at the same hook chokepoint (hook-scope-track.py) that already observes every
tool call, following the same append-only jsonl idiom as gate-log.jsonl
(agentctl/cli.py's _log_gate) and ~/.local/log/claude-spawn-costs.jsonl.

Each row carries two ids (see hook-scope-track.py's track() call site for the
rationale): ``session_id`` is the hook-stdin id of the agent that actually
made the edit (may be a subagent); ``env_session_id`` is the root session's
CLAUDE_CODE_SESSION_ID, which is what a commit trailer (agent_commit_trailer.py)
keys on. Recording both lets a by-session query keyed on either id join a
commit's trailer back to the subagent edits made under it.

Fail-open like gate-log.jsonl: append() never raises — a ledger write failure
must never block or fail the calling hook.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from lib import config_root


def _ledger_path(path: "Path | None") -> Path:
    if path is not None:
        return path
    override = os.environ.get("AGENTCTL_EDIT_LEDGER")
    if override:
        return Path(override).expanduser()
    return config_root.agentctl_edit_log()


def append(
    session_id: str,
    env_session_id: str,
    file: str,
    tool: str,
    cwd: str,
    ts: float,
    path: "Path | None" = None,
) -> None:
    """Append one edit record. Never raises: any I/O error is swallowed so a
    ledger write can never wedge the calling hook (mirrors _log_gate's
    fail-open contract)."""
    row = {
        "ts": ts,
        "session_id": session_id,
        "env_session_id": env_session_id,
        "tool": tool,
        "file": file,
        "cwd": cwd,
    }
    target = _ledger_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_records(path: "Path | None" = None) -> Iterator[dict]:
    """Yield parsed row dicts in file order, skipping malformed lines. Yields
    nothing (not an error) when the ledger file doesn't exist yet."""
    target = _ledger_path(path)
    try:
        f = target.open("r", encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row
