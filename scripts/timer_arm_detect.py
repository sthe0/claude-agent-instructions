#!/usr/bin/env python3
"""Shared "is this turn seeking closure?" detector.

Difficulty removed: two consumers need the SAME predicate over one turn's
transcript slice and must never drift apart —

  - the advisory `hook-ask-defer-timer.py` (Stop) warns when a turn PROMISES to
    ask via buttons next message but never armed the `sleep 2` background timer
    that would open that next turn;
  - the resolution guardian in `hook-turn-end-gate.py` (Stop) must NOT fire when
    the turn is already seeking closure — i.e. it emitted an `AskUserQuestion`
    inline OR armed a deferral timer to carry the ask on the next turn.

If each reimplemented the armed-timer predicate, one could treat a turn as
"timer armed / closure sought" while the other treated it as stranded. Keeping
the predicate in ONE importable module (mirroring si_feedback_detect.py) makes
that divergence impossible; scripts/tests/test_timer_arm_detect.py pins that both
consumers agree on the same fabricated transcripts.

A timer is "armed" this turn when the assistant emitted EITHER:
  - a `ScheduleWakeup` / `CronCreate` tool_use, OR
  - a backgrounded `Bash` tool_use (`run_in_background` is True) whose command
    contains `sleep`.

The predicate deliberately matches ANY backgrounded `sleep`, not only `sleep 2`:
a monitoring poller (`sleep 60`) reads as "timer armed" too. That is an accepted
FALSE NEGATIVE for the resolution guardian (a poller is not really seeking
closure) — one that self-heals, because the FOLLOWING turn without a timer is
re-evaluated and fires. Widening the match would instead risk FALSE POSITIVES on
the ask-defer warn path, which is the more costly error there.

Both predicates are pure over a list of transcript entries (one turn's slice) and
tolerate BOTH entry shapes seen in the wild: a top-level `type == "assistant"`
field and/or a nested `message.role == "assistant"`.
"""
from __future__ import annotations

import re

# A backgrounded Bash command that arms a delivery-split / monitoring timer.
_SLEEP_RE = re.compile(r"\bsleep\b", re.IGNORECASE)

# Tool names that count as "timer armed" on their own (a scheduled wakeup
# instead of a raw backgrounded `sleep`).
_TIMER_TOOL_NAMES = frozenset({"ScheduleWakeup", "CronCreate"})


def _content_items(message: dict) -> list:
    """The content blocks of a message, normalizing a bare string to one text
    block. Inlined (not imported from lib) so this detector stays dependency-free,
    matching the si_feedback_detect.py template."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _is_assistant_entry(entry: dict) -> bool:
    """True for either transcript shape: a top-level `type == "assistant"` (the
    ask-defer-timer fixtures / real transcripts) or a nested
    `message.role == "assistant"` (the turn-end-gate fixtures / real transcripts)."""
    if entry.get("type") == "assistant":
        return True
    message = entry.get("message")
    return isinstance(message, dict) and message.get("role") == "assistant"


def _assistant_tool_uses(entries: list[dict]):
    """Yield every tool_use content block emitted by an assistant entry."""
    for entry in entries:
        if not isinstance(entry, dict) or not _is_assistant_entry(entry):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        for item in _content_items(message):
            if isinstance(item, dict) and item.get("type") == "tool_use":
                yield item


def timer_armed(entries: list[dict]) -> bool:
    """True when a deferral / wakeup timer was armed in these entries."""
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


def ask_emitted(entries: list[dict]) -> bool:
    """True when an `AskUserQuestion` tool_use was emitted in these entries — the
    inline way a turn signals it is seeking closure rather than deferring it."""
    return any(
        tool_use.get("name") == "AskUserQuestion"
        for tool_use in _assistant_tool_uses(entries)
    )


def closure_sought(entries: list[dict]) -> bool:
    """True when this turn is already seeking closure: an inline AskUserQuestion
    OR an armed deferral timer to open the turn that carries the ask."""
    return ask_emitted(entries) or timer_armed(entries)
