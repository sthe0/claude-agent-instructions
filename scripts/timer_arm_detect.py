#!/usr/bin/env python3
"""Shared "is this turn seeking closure?" detector.

Difficulty removed: the `hook-turn-end-gate.py` (Stop) guardians need a stable
predicate over one turn's transcript slice —

  - the resolution guardian must NOT fire when the turn is already seeking
    closure — i.e. it emitted an `AskUserQuestion` inline OR armed a deferral
    timer;
  - the `long_job_autowake` guardian reads `waiter_armed` to tell whether a
    detached long job armed a harness-tracked auto-wake.

Keeping the armed-timer predicate in ONE importable module (mirroring
si_feedback_detect.py) keeps every guardian on the same definition of "timer
armed"; scripts/tests/test_timer_arm_detect.py pins that behavior on fabricated
transcripts.

A timer is "armed" this turn when the assistant emitted EITHER:
  - a `ScheduleWakeup` / `CronCreate` tool_use, OR
  - a backgrounded `Bash` tool_use (`run_in_background` is True) whose command
    contains `sleep`.

The predicate deliberately matches ANY backgrounded `sleep`, not only `sleep 2`:
a monitoring poller (`sleep 60`) reads as "timer armed" too. That is an accepted
FALSE NEGATIVE for the resolution guardian (a poller is not really seeking
closure) — one that self-heals, because the FOLLOWING turn without a timer is
re-evaluated and fires.

Both predicates are pure over a list of transcript entries (one turn's slice) and
tolerate BOTH entry shapes seen in the wild: a top-level `type == "assistant"`
field and/or a nested `message.role == "assistant"`.
"""
from __future__ import annotations

import re

# A backgrounded Bash command that arms a deferral / monitoring timer.
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


def iter_bash_commands(entries: list[dict]) -> list[str]:
    """Every assistant `Bash` tool_use command string in these entries, in order.

    Reuses the shared `_assistant_tool_uses` walk so the turn-end guardian can run
    the long-job launch scan over a turn's Bash commands without re-implementing
    the transcript walk."""
    out: list[str] = []
    for tool_use in _assistant_tool_uses(entries):
        if tool_use.get("name") != "Bash":
            continue
        tool_input = tool_use.get("input")
        if not isinstance(tool_input, dict):
            continue
        command = tool_input.get("command")
        if isinstance(command, str):
            out.append(command)
    return out


def waiter_armed(entries: list[dict]) -> bool:
    """The AUTO-WAKE predicate: did this turn arm a HARNESS-TRACKED waiter that
    re-invokes the main thread when a long external job ends?

    True when EITHER:
      - any assistant `Bash` tool_use has `run_in_background` is True — the harness
        auto-wakes the main thread on ANY backgrounded Bash exit, not only `sleep`;
      - any tool_use is `CronCreate` — a cron job fires while the REPL is idle
        regardless of /loop (and `durable` jobs survive session restarts), so it is
        a genuine self-scheduled auto-wake.

    Deliberately DIFFERENT from `timer_armed` on two axes, so it is a sibling rather
    than a widening of that predicate:
      - broader: any backgrounded Bash, not only one whose command contains `sleep`
        (`timer_armed` carries delivery-split / closure semantics and must NOT
        change — widening it would make an arbitrary backgrounded job read as
        "closure sought");
      - narrower: `ScheduleWakeup` is EXCLUDED. ScheduleWakeup only resumes work in
        /loop dynamic mode and silently no-ops in an ordinary session, so counting
        it as an auto-wake would let a coordinator that armed only ScheduleWakeup
        outside /loop pass while the main thread never wakes — the exact silent-idle
        failure the auto-wake guardian exists to catch.
    """
    for tool_use in _assistant_tool_uses(entries):
        name = tool_use.get("name")
        if name == "CronCreate":
            return True
        if name != "Bash":
            continue
        tool_input = tool_use.get("input")
        if isinstance(tool_input, dict) and tool_input.get("run_in_background") is True:
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
