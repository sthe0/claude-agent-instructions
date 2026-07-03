"""Shared turn-boundary primitive for hooks that read the session transcript.

Difficulty removed: two hooks (hook-ask-text-split.py, hook-plan-delivery-
gate.py) both need "when did the current turn start?" — the transcript is the
only observable that captures a turn opened by a background task-notification
(a `queued_command` attachment entry), which may not fire UserPromptSubmit —
harness/version-dependent (observed firing live 2026-07-03 on this machine;
the stale-timestamp premise held elsewhere) — so engine-side state timestamps
cannot be relied on to advance for it. This module is the single structural
home for that predicate instead of duplicating it per hook.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _content_items(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _is_real_user_prompt(entry: dict) -> bool:
    """A turn boundary: an entry that opens a new turn. Two shapes exist in real
    transcripts: (a) a user entry carrying actual text (a typed prompt, an
    interjection) — not a bare tool_result batch; (b) an injected queued prompt —
    an `attachment` entry with attachment.type == "queued_command" (how a
    background task-notification opens the next turn; verified live 2026-07-03:
    the notification is NOT a user-typed entry, and the mid-turn `queue-operation`
    enqueue record is not the boundary — the queued_command injection is)."""
    etype = entry.get("type")
    if etype == "attachment":
        att = entry.get("attachment")
        return isinstance(att, dict) and att.get("type") == "queued_command"
    if etype != "user":
        return False
    if entry.get("isMeta"):
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        for item in _content_items(message)
    )


def iso_to_epoch(ts: str) -> float | None:
    """Parse a transcript ISO-8601 timestamp (trailing `Z`) to epoch seconds.
    None on any unparsable value — callers must fall open, never crash."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def latest_turn_start(transcript_path: Path) -> float | None:
    """Epoch timestamp of the most recent turn-boundary entry (see
    _is_real_user_prompt), or None when the observable is unavailable: unreadable
    file, no boundary found, or the boundary entry's own timestamp is missing/
    unparsable."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        if _is_real_user_prompt(entry):
            ts = entry.get("timestamp")
            if not isinstance(ts, str):
                return None
            return iso_to_epoch(ts)
    return None
