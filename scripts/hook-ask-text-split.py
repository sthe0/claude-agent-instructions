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

Caveat (measured 2026-07-15): a Stop-hook block does NOT open a fresh turn — it
CONTINUES the current one, so tool calls made *before* the block are legitimately
"this turn" and a subsequent ask is denied correctly (not a false positive). The
sleep-2 remedy above is also version-sensitive: the background command's own
completion can land a tool_result between the turn boundary and the ask, tripping
decision step (1). To confirm the true entry shape behind a suspected false
positive — the live root-session transcript is otherwise unreachable from a
sub-agent — every DENY is appended, fail-open, to
`~/.local/log/claude-ask-gate-denials.jsonl` as a compact entry-shape tail.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
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


# Fail-open observability sink: the compact entry-shape tail behind every DENY,
# so a suspected false-positive denial can be diagnosed after the fact (the live
# root-session transcript is otherwise unreachable from a sub-agent).
_DENIAL_LOG = Path(
    os.environ.get("CLAUDE_ASK_GATE_DENIAL_LOG")
    or Path.home() / ".local" / "log" / "claude-ask-gate-denials.jsonl"
)


def _log_denial(transcript_path: Path, reason: str) -> None:
    """Append one fail-open record of the transcript tail that produced this
    denial (entry types / attachment types / tool_result & text presence — no
    text bodies). Never raises: observability must not wedge the gate."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        tail = []
        for line in lines:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            att = entry.get("attachment")
            origin = entry.get("origin")
            items = _content_items(entry["message"]) if isinstance(entry.get("message"), dict) else []
            tail.append({
                "type": entry.get("type"),
                "att_type": att.get("type") if isinstance(att, dict) else None,
                "isMeta": bool(entry.get("isMeta")),
                "origin_kind": origin.get("kind") if isinstance(origin, dict) else None,
                "has_tool_result": any(isinstance(i, dict) and i.get("type") == "tool_result" for i in items),
                "has_text": any(isinstance(i, dict) and i.get("type") == "text" and i.get("text") for i in items),
            })
        _DENIAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DENIAL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "reason_head": reason[:60],
                "tail": tail,
            }) + "\n")
    except Exception:
        pass


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
        _log_denial(Path(transcript_path), reason)
        deny_with(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
