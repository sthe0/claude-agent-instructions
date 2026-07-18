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


def delivered_final_texts(transcript_path: Path) -> list[tuple[str, float | None]] | None:
    """(text, epoch_ts) for every assistant message that was the FINAL assistant
    output of a COMPLETED turn — the only shape that proves the text actually
    rendered to the user.

    THE TRAP: the transcript faithfully records assistant text that was NEVER
    RENDERED. An assistant message routinely carries [thinking, text, tool_use]
    (or [text, tool_use]) in ONE entry's content blocks — the text preceding a
    same-message tool_use is pre-tool-call text the harness may never show
    (CLAUDE.md's AskUserQuestion turn-split). So mere presence of text in the
    transcript proves nothing; only TERMINAL POSITION, at BLOCK granularity (not
    entry granularity), in a COMPLETED turn counts. "No tool_use ENTRY follows"
    is the wrong test: a message that itself carries [text, tool_use] IS the
    last assistant entry of its segment with no further entry after it, yet its
    text is exactly the pre-tool-call text that never rendered. The test is:
    the terminal assistant message of the segment contains NO tool_use block AT
    ALL, and the collected text is that message's LAST block.

    Walks the JSONL once, segmenting on _is_real_user_prompt boundaries. The
    trailing segment (from the last boundary to end-of-file) is the CURRENT,
    still-open turn — its text may still never render, so it is EXCLUDED
    entirely, never contributing an entry to the result.

    Returns None iff the transcript could not be read/parsed at all — a MISSING
    OBSERVABLE, and the caller must fail open. Returns a list — possibly empty —
    once the transcript was read: an empty list is an OBSERVED NEGATIVE ("read
    fine, nothing was ever delivered") and must DENY, not fail open. Conflating
    the two into a bare [] would force the caller to choose between re-opening
    the bypass (fail open on a genuine negative) or wedging on a transient read
    error (fail closed on a missing observable) — neither acceptable, so the
    distinction is preserved here rather than discarded.

    A qualifying entry's timestamp may itself be missing/unparsable; that entry
    is still returned with epoch_ts=None rather than dropped, since the text DID
    land — only the exact landing time is unknown. The caller (hook_decision)
    treats a None ts as a weaker observable and may degrade to a plain byte
    check rather than the stricter post-dating comparison; dropping the entry
    outright would silently discard a real delivery.
    """
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            entries.append(entry)

    boundary_idx = [i for i, e in enumerate(entries) if _is_real_user_prompt(e)]
    if not boundary_idx:
        return []

    results: list[tuple[str, float | None]] = []
    # Completed segments only: [boundary_idx[i], boundary_idx[i+1]) for each
    # consecutive pair. zip() stops before the final boundary, so the trailing
    # (current, incomplete) segment is never iterated — it is excluded by
    # construction, not by an extra check.
    for start, end in zip(boundary_idx, boundary_idx[1:]):
        segment = entries[start:end]
        last_assistant = None
        for entry in segment:
            if entry.get("type") == "assistant":
                last_assistant = entry
        if last_assistant is None:
            continue
        message = last_assistant.get("message")
        if not isinstance(message, dict):
            continue
        blocks = _content_items(message)
        if not blocks:
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            continue
        last_block = blocks[-1]
        if not (isinstance(last_block, dict) and last_block.get("type") == "text"):
            continue
        text = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        ts = last_assistant.get("timestamp")
        epoch = iso_to_epoch(ts) if isinstance(ts, str) else None
        results.append((text, epoch))
    return results
