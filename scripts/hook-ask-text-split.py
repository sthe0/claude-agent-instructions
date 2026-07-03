#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny an ask preceded by substantive
same-turn assistant text.

Difficulty removed: the Claude Code client does not render assistant text emitted
before a subsequent tool call in the same turn (system-knowledge leaf
claude-code-drops-pre-tool-call-text). An AskUserQuestion fired after a long
answer/plan/proposal in the SAME turn therefore arrives with nothing behind it —
the user sees bare buttons and asks "а где текст?". hook-plan-delivery-gate.py
guards only the PLAN_READY node via engine timestamps; this hook is the general
case for EVERY ask, decided from the session transcript itself.

Decision: sum the assistant text emitted in the current turn (transcript entries
after the last real user message — a user entry carrying actual text, not a bare
tool_result batch). If the total exceeds THRESHOLD_CHARS, deny with a directive
to deliver the text as the turn's FINAL message and re-issue the ask next turn
(the text-then-buttons timer split: end the turn after starting a background
`sleep 2`; its completion notification opens the next turn, which starts directly
with the AskUserQuestion). Short status lines under the threshold are allowed —
losing them is harmless.

Fails open (allow) on any missing observable: no transcript_path, unreadable
file, no turn boundary found. Always exits 0 — a hook crash must never wedge the
workflow. DENY uses the same PreToolUse permissionDecision JSON contract as
hook-plan-delivery-gate.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Same-turn assistant text above this many chars is substantive: it was written
# for the user and will be silently dropped by the client, so the ask is denied.
# At or below it (a short status line) the loss is harmless and the ask proceeds.
THRESHOLD_CHARS = 200


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


def _assistant_text_len(entry: dict) -> int:
    if entry.get("type") != "assistant":
        return 0
    message = entry.get("message")
    if not isinstance(message, dict):
        return 0
    return sum(
        len(item.get("text", ""))
        for item in _content_items(message)
        if isinstance(item, dict) and item.get("type") == "text"
    )


def current_turn_text_len(transcript_path: Path) -> int | None:
    """Chars of assistant text since the last real user prompt; None if the
    observable is unavailable (unreadable file / no boundary found)."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    total = 0
    seen_boundary = False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        if _is_real_user_prompt(entry):
            seen_boundary = True
            break
        total += _assistant_text_len(entry)
    return total if seen_boundary else None


def gate_decision(turn_text_len: int | None) -> tuple[str, str]:
    """Pure decision. Returns ("allow"|"deny", reason)."""
    if turn_text_len is None or turn_text_len <= THRESHOLD_CHARS:
        return "allow", ""
    return "deny", (
        f"this turn already carries ~{turn_text_len} chars of assistant text, which the "
        "client will NOT render before a tool call — the ask would arrive with nothing "
        "behind it. Deliver the text as this turn's FINAL message (no tool calls after), "
        "start `sleep 2` with run_in_background=true, and open the NEXT turn directly "
        "with this AskUserQuestion (text-then-buttons split; see leaf "
        "claude-code-drops-pre-tool-call-text)"
    )


def deny_with(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "AskUserQuestion":
        return 0

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0

    turn_len = current_turn_text_len(Path(transcript_path))
    decision, reason = gate_decision(turn_len)
    if decision == "deny":
        deny_with(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
