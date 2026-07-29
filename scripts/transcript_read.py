#!/usr/bin/env python3
"""Shared session-transcript reader for the Stop-hook family.

Difficulty removed: every hook that reasons about "what happened this session"
must parse the same JSONL transcript the same way — skip malformed lines, read
`type=="text"` blocks of an assistant message as *what the user saw*, read
`tool_result` blocks of a user-role message as *what the agent saw*. That
predicate was being re-derived per hook (`hook-run-url-surfaced-reminder.py`,
`hook-turn-end-gate.py`, `tool-usage-report.py` each grew a private copy), so a
fix to one — e.g. tolerating a string `content` — silently missed the others.
Hook filenames are hyphenated and cannot be imported, so the shared logic lives
in this importable module and the hooks call into it, mirroring how
`long_job_detect.py` holds the shared launch predicate for its two consumers.

Every function is total: malformed input yields an empty result rather than an
exception, because the callers are fail-open advisories that must never crash a
turn.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

# How many identities a turn-end nudge names before summarizing the rest as
# "(+N more)". Shared by the Stop-hook family so two reminders in the same turn
# read as one voice rather than two differently-verbose ones.
MAX_LISTED = 3


def iter_transcript(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL transcript, skipping bad lines."""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _blocks(msg: dict) -> list:
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def message_text(msg: dict) -> str:
    """Concatenate the `type=="text"` blocks of a message, of either role.

    This is the prose channel: what a human typed, or what the agent wrote back
    in chat. A `thinking` block and a tool_use input are deliberately excluded,
    and a `tool_result` turn (whose content carries no `text` block) yields "".
    """
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    parts = [
        item.get("text") or ""
        for item in _blocks(msg)
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n".join(parts)


def assistant_text(msg: dict) -> str:
    """The prose an ASSISTANT message put in front of the user — the only channel
    that counts as *surfacing* something.

    Same extraction as `message_text`; the separate name is what a caller
    enforcing the surfacing rule asserts about its argument, so its call sites
    stay readable when the rule is what is under test.
    """
    return message_text(msg)


def tool_results(msg: dict) -> list[dict]:
    """Every `tool_result` block of a (user-role) message as
    ``{"tool_use_id": str | None, "text": str}``.

    The id is what lets a caller correlate output back to the specific call
    that produced it — "this URL was printed by THAT command" — instead of
    treating a turn's output as one undifferentiated blob. It is absent in
    hand-built fixtures and in some older transcripts, hence `None` rather
    than a raise.
    """
    out: list[dict] = []
    for item in _blocks(msg):
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        inner = item.get("content")
        parts: list[str] = []
        if isinstance(inner, str):
            parts.append(inner)
        elif isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    parts.append(sub.get("text") or "")
        use_id = item.get("tool_use_id")
        out.append({
            "tool_use_id": use_id if isinstance(use_id, str) and use_id else None,
            "text": "\n".join(parts),
        })
    return out


def tool_result_text(msg: dict) -> str:
    """Concatenate the textual content of `tool_result` blocks in a (user-role)
    message — what a command, an API response or a spawned agent's report
    actually printed back to the agent."""
    return "\n".join(block["text"] for block in tool_results(msg))


def tool_use_blocks(msg: dict) -> list[dict]:
    """Every `tool_use` block of an assistant message."""
    return [
        item
        for item in _blocks(msg)
        if isinstance(item, dict) and item.get("type") == "tool_use"
    ]


def bash_tool_uses(msg: dict) -> list[dict]:
    """Every Bash call of an assistant message as ``{"id": str | None,
    "command": str}`` — the id pairs with `tool_results`' `tool_use_id`, so a
    caller can tell which command produced which output."""
    out: list[dict] = []
    for item in tool_use_blocks(msg):
        if item.get("name") != "Bash":
            continue
        tool_input = item.get("input")
        if not isinstance(tool_input, dict):
            continue
        cmd = tool_input.get("command")
        if not isinstance(cmd, str) or not cmd:
            continue
        use_id = item.get("id")
        out.append({
            "id": use_id if isinstance(use_id, str) and use_id else None,
            "command": cmd,
        })
    return out


def bash_commands(msg: dict) -> list[str]:
    """The `command` string of every Bash tool_use block in an assistant
    message — what the agent actually ran, as opposed to what it saw back."""
    return [use["command"] for use in bash_tool_uses(msg)]
