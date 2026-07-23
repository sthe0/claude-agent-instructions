"""Durable append-only session -> edited-file ledger.

Difficulty removed: the session_scope registry (session_scope/registry.py)
records touched paths, but it exists for LIVE cross-session conflict
detection — capped at MAX_TOUCHED_PATHS, deduped, no per-touch timestamp, and
pruned when a session's heartbeat goes stale. It cannot answer "which session
edited file Y at time T" after the fact. This module is the durable,
uncapped, append-only record that can: one JSON line per canon write,
following the same append-only jsonl idiom as gate-log.jsonl
(agentctl/cli.py's _log_gate) and ~/.local/log/claude-spawn-costs.jsonl.

Two feeders write to it: the Edit|Write hook chokepoint (hook-scope-track.py),
which observes every tool call, and ``stamp()`` below, the entry point for
direct-IO canon writers that bypass that chokepoint entirely (a Python writer
imports and calls it directly; a shell writer calls it via
``edit-ledger.py stamp``). Both funnel through ``append()``.

Each row carries two ids (see hook-scope-track.py's track() call site for the
rationale): ``session_id`` is the hook-stdin id of the agent that actually
made the edit (may be a subagent); ``env_session_id`` is the root session's
CLAUDE_CODE_SESSION_ID, which is what a commit trailer (agent_commit_trailer.py)
keys on. Recording both lets a by-session query keyed on either id join a
commit's trailer back to the subagent edits made under it.

Fail-open like gate-log.jsonl: append() and stamp() never raise — a ledger
write failure must never block or fail the calling hook or writer.
"""
from __future__ import annotations

import json
import os
import time
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


def stamp(
    file: str,
    tool: str,
    session: "str | None" = None,
    path: "Path | None" = None,
) -> None:
    """Entry point for direct-IO canon writers that bypass the Edit/Write hook
    chokepoint (hook-scope-track.py) — a Python writer calls this directly, a
    shell writer calls it via `edit-ledger.py stamp`. `tool` is a synthetic
    writer marker (e.g. "record-experience:new", "script:apply-settings"), not
    a Claude Code tool name; it is what makes rows filterable by writer at read
    time.

    Resolves env_id from $CLAUDE_CODE_SESSION_ID; an explicit `session` (e.g.
    from a spawned specialist that knows its own id) takes precedence over the
    inherited env for `session_id`, while `env_session_id` always carries the
    env value — the same two-id join edit_ledger's module docstring describes
    for the hook path.

    Fail-open like append(): the whole body is wrapped so a canon write's
    attribution can never fail the write itself. append() already swallows
    OSError, but stamp also does realpath/getcwd/environ work on the caller's
    behalf, so the wider guard covers that too.
    """
    try:
        env_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
        session_id = session if session is not None else env_id
        abspath = os.path.realpath(file)
        append(session_id, env_id, abspath, tool, os.getcwd(), time.time(), path=path)
    except Exception:
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
