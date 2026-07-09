#!/usr/bin/env python3
"""Stop hook: warn when a turn defers an AskUserQuestion to "next message" via
buttons but never arms the background timer that is supposed to open that next
turn.

Difficulty removed: the delivery-split rule (CLAUDE.md § Escalation —
"Long-artifact exception") requires that a long artifact or a turn that
already ran a tool deliver its `AskUserQuestion` on a FRESH turn, opened by a
`sleep 2` background timer's completion notification. CLAUDE.md states the
atomicity explicitly: "Arming the `sleep 2` timer and deferring the ask are
one atomic act — a prose promise to 'ask next turn' *without* the timer armed
in the **same** turn silently strands the ask, because no next turn ever
fires." That rule itself already covers the failure ("arm the timer"); it was
observed violated twice on 2026-07-09 anyway — the coordinator wrote the
deferral promise in prose but did not start the timer, so no next turn ever
opened and the promised ask never reached the user. This hook is the
structural backstop: it cannot un-strand the ask (the turn is already over by
the time Stop fires), but it makes the failure observable rather than silent.

Detection (current turn only — the slice of the transcript from the most
recent turn-boundary entry to the end; see lib.transcript_turns for the
boundary predicate). All three must hold to warn:
  1. The turn's assistant text contains a deferral-promise pattern (a small,
     tunable regex list below — RU+EN phrasings of "I'll ask via buttons next
     message").
  2. No timer was armed this turn: no backgrounded `Bash` tool_use whose
     command contains `sleep`, and no `ScheduleWakeup`/`CronCreate` tool_use.
  3. No `AskUserQuestion` tool_use was already emitted this turn (if the ask
     already fired, nothing was deferred).

Action: WARN only, never deny. Prints a plain message and exits 0 — mirrors
the existing UserPromptSubmit nudge hooks (hook-self-improvement-reminder.py,
hook-resolution-reminder.py): a hook crash or an unreadable/absent transcript
must never wedge the workflow, so any parse failure fails open (silent, exit
0). Deterministic, offline, no network.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.transcript_turns import _content_items, _is_real_user_prompt  # noqa: E402

# Deferral-promise phrasings (RU+EN). Kept as a flat list of independent
# patterns so new phrasings are easy to add/tune without touching the scan
# logic. Matched case-insensitively against the turn's concatenated assistant
# text; DOTALL so ".*" spans newlines within one message.
_PROMISE_PATTERNS = [
    r"кнопк\w*.*(следующ|next)",
    r"(следующ\w* сообщени\w*|next message).*(ask|вопрос|кнопк\w*|question)",
    r"задам.*(кнопк\w*|вопрос)",
    r"ask\w* .*next (turn|message)",
    r"buttons? next",
]
_PROMISE_RE = [
    re.compile(p, re.IGNORECASE | re.UNICODE | re.DOTALL) for p in _PROMISE_PATTERNS
]

# A backgrounded Bash command that arms the delivery-split timer.
_SLEEP_RE = re.compile(r"\bsleep\b", re.IGNORECASE)

# Tool names that also count as "timer armed" (a scheduled wakeup instead of
# a raw `sleep`).
_TIMER_TOOL_NAMES = {"ScheduleWakeup", "CronCreate"}

_WARNING = (
    "[ask-defer-timer] This turn's text promises to ask via buttons next "
    "message, but no `sleep 2` background timer (or ScheduleWakeup/CronCreate) "
    "was armed this turn — no next turn will fire, so the ask is stranded. Per "
    "CLAUDE.md, arming the timer and deferring the ask are one atomic act: arm "
    "the timer now, or ask inline instead of deferring."
)


def _current_turn_entries(transcript_path: Path) -> list[dict] | None:
    """Entries of the current (ending) turn: everything after the most recent
    turn-boundary entry (see _is_real_user_prompt) up to the end of the
    transcript. None when the observable is unavailable (unreadable file, no
    entries, no boundary found) — callers must fail open."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    entries: list[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    if not entries:
        return None
    boundary_idx = None
    for i in range(len(entries) - 1, -1, -1):
        if _is_real_user_prompt(entries[i]):
            boundary_idx = i
            break
    if boundary_idx is None:
        return None
    return entries[boundary_idx + 1 :]


def _assistant_tool_uses(entries: list[dict]):
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        for item in _content_items(message):
            if isinstance(item, dict) and item.get("type") == "tool_use":
                yield item


def _turn_assistant_text(entries: list[dict]) -> str:
    parts = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        for item in _content_items(message):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
    return "\n".join(parts)


def has_deferral_promise(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PROMISE_RE)


def timer_armed(entries: list[dict]) -> bool:
    for tool_use in _assistant_tool_uses(entries):
        name = tool_use.get("name")
        if name in _TIMER_TOOL_NAMES:
            return True
        if name != "Bash":
            continue
        tool_input = tool_use.get("input")
        if not isinstance(tool_input, dict):
            continue
        command = tool_input.get("command")
        if not isinstance(command, str) or not _SLEEP_RE.search(command):
            continue
        if tool_input.get("run_in_background") is True:
            return True
    return False


def ask_already_emitted(entries: list[dict]) -> bool:
    return any(
        tool_use.get("name") == "AskUserQuestion" for tool_use in _assistant_tool_uses(entries)
    )


def should_warn(entries: list[dict]) -> bool:
    """Pure decision over one turn's entries."""
    if ask_already_emitted(entries):
        return False
    if not has_deferral_promise(_turn_assistant_text(entries)):
        return False
    return not timer_armed(entries)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0

    entries = _current_turn_entries(Path(transcript_path))
    if entries is None:
        return 0

    if should_warn(entries):
        print(_WARNING)
    return 0


if __name__ == "__main__":
    sys.exit(main())
