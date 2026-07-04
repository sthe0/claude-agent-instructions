#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny an ask that follows a
completed tool call this turn, or that is preceded by substantive same-turn
assistant text.

Difficulty removed: the Claude Code client does not render — and does not even
write to the transcript — assistant text emitted in the SAME message as a
subsequent tool call (system-knowledge leaf claude-code-drops-pre-tool-call-
text). This was first measured as a text-length problem (a long preamble before
an ask gets dropped), but live forensics on session e00ff3b4 (2026-07-04, entries
1350-1353) showed the drop is total and structural: a ~2500-char report sharing
one assistant message with an AskUserQuestion tool_use is absent from the
transcript itself, not merely unrendered — so a hook that counts same-message
text is measuring an observable that does not exist. There is no way to recover
it after the fact.

Decision, in order: (1) if a tool call has already completed THIS turn (a user
entry carrying a tool_result, seen since the last turn boundary), deny — any
text the assistant writes before this ask, in this turn, risks landing in that
same invisible bucket, so the ask must not fire until a fresh turn opens.
(2) else, if the assistant text emitted since the last render checkpoint (a
completed tool call, which flushes/renders prior narration) exceeds
THRESHOLD_CHARS, deny — the older two-message rule, for turns with no completed
tool call yet but a long preamble immediately before the ask. (3) else allow.

Both deny reasons point to the same remedy: deliver the content as the turn's
FINAL message (no tool calls after), start a background `sleep 2`, and let its
completion notification open the NEXT turn directly with the AskUserQuestion —
a fresh turn boundary, zero preceding text, nothing to drop.

Fails open (allow) on any missing observable: no transcript_path, unreadable
file, no turn boundary found. Always exits 0 — a hook crash must never wedge the
workflow. DENY uses the same PreToolUse permissionDecision JSON contract as
hook-plan-delivery-gate.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.transcript_turns import _content_items, _is_real_user_prompt  # noqa: E402

# Same-turn assistant text above this many chars is substantive: it was written
# for the user and will be silently dropped by the client, so the ask is denied.
# At or below it (a short status line) the loss is harmless and the ask proceeds.
THRESHOLD_CHARS = 200

# Shared remedy for both deny reasons below — the fix is identical regardless of
# which rule fired (mid-turn tool call vs. over-threshold preamble text).
_REMEDY = (
    "Deliver the content as this turn's FINAL message (no tool calls after), start "
    "`sleep 2` with run_in_background=true, and open the NEXT turn directly with "
    "this AskUserQuestion — see leaf claude-code-drops-pre-tool-call-text"
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


def _is_render_checkpoint(entry: dict) -> bool:
    """A completed-tool-call entry: a user entry carrying a tool_result. Any
    assistant text emitted before it was flushed/rendered when the tool ran, so
    it does not count towards at-risk text — but the checkpoint itself means a
    tool call has completed this turn, which is what makes any FURTHER ask
    mid-turn (see module docstring, decision step 1)."""
    if entry.get("type") != "user":
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "tool_result"
        for item in _content_items(message)
    )


def scan_transcript(transcript_path: Path) -> tuple[bool | None, int | None]:
    """Walk backward from the ask to the last turn boundary (a real user
    prompt). Returns (has_tool_result_this_turn, at_risk_text_len):
    - has_tool_result_this_turn: whether a completed tool call (render
      checkpoint) occurred anywhere since the turn boundary.
    - at_risk_text_len: chars of assistant text emitted since the most recent
      render checkpoint (or since the turn boundary if none) — the text a
      two-message-shaped ask would drop.
    Both None if the observable is unavailable (unreadable file / no boundary
    found) — callers must fail open."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None, None
    has_tool_result = False
    at_risk_total = 0
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
        if _is_render_checkpoint(entry):
            has_tool_result = True
            continue
        if not has_tool_result:
            at_risk_total += _assistant_text_len(entry)
    if not seen_boundary:
        return None, None
    return has_tool_result, at_risk_total


def gate_decision(has_tool_result_this_turn: bool | None, at_risk_text_len: int | None) -> tuple[str, str]:
    """Pure decision. Returns ("allow"|"deny", reason)."""
    if has_tool_result_this_turn is None:
        return "allow", ""
    if has_tool_result_this_turn:
        return "deny", (
            "this turn already completed a tool call — any assistant text written before "
            "this ask, in this same turn, is dropped from the transcript entirely (not just "
            "unrendered) and cannot be recovered. " + _REMEDY
        )
    if at_risk_text_len is not None and at_risk_text_len > THRESHOLD_CHARS:
        return "deny", (
            f"this turn already carries ~{at_risk_text_len} chars of assistant text, which the "
            "client will NOT render before a tool call — the ask would arrive with nothing "
            "behind it. " + _REMEDY
        )
    return "allow", ""


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

    has_tool_result, at_risk_len = scan_transcript(Path(transcript_path))
    decision, reason = gate_decision(has_tool_result, at_risk_len)
    if decision == "deny":
        deny_with(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
