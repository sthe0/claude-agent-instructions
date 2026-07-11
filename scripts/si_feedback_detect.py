#!/usr/bin/env python3
"""Shared feedback-signal detector for the self-improvement discipline.

Difficulty removed: the same "is this an agent-behavior-feedback turn?" decision
is needed in two places — the advisory `UserPromptSubmit` reminder
(`hook-self-improvement-reminder.py`) and the end-of-turn `Stop` gate
(`hook-self-improvement-gate.py`). Keeping the regexes in one importable module
means the reminder and the gate can never drift apart.

Detection design (precision-first; two tiers):

  Tier 1 — explicit self-improvement reference (near-certain):
    'self-improvement' / 'selfimprovement' / 'self improvement' as a substring.
    Covers "did you run self-improvement?", "запусти self-improvement", etc.

  Tier 2 — agent-directed correction.  Two sub-tiers:
    (a) Strong imperative patterns — self-sufficient, fire on their own:
        "don't do that", "stop doing", "перестань", "не делай так",
        "я же просил", "я же говорил".
        (Imperative forms and "I-already-told-you" phrases inherently
        address the agent; no separate pronoun check needed.)
    (b) Context-dependent patterns — fire only with an explicit agent-reference
        (you|your|ты|тебя|тебе|тобой) co-occurring in the prompt:
        EN: "you shouldn't", "you should have", "you didn't", "why did you",
            "next time", "you always", "you keep", "that's wrong", "instead of".
        RU: "не так", "неправильно", "не надо было", "не нужно было", "зачем ты",
            "почему ты", "опять ты", "так нельзя", "в следующий раз",
            "следовало", "должен был".

V1 exclusions (documented precision choice):
  Bare 'always'/'never'/'prefer'/'всегда'/'никогда' without an agent-reference
  cue are excluded — they appear constantly in normal task specs ("always
  validate input") and would dominate false positives.
"""
from __future__ import annotations

import re

# Harness-injected context (recalled memory, the skill list, the CLAUDE.md dump)
# rides inside <system-reminder>...</system-reminder> spans of the same user
# message the human authored. Those spans mention "self-improvement" many times
# as background — matching them would fire Tier 1 on injected context, not on
# user feedback, and falsely block the turn. Excise the spans before detection.
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)

# Beyond <system-reminder>, the harness injects other user-role content that is
# NOT human-authored and must not drive feedback detection. The Stop gate's
# analyze() picks the last user entry carrying non-empty text as "the user's
# message"; on a turn driven by a background event (no fresh human message) that
# entry can be one of these injected blocks, whose 'self-improvement' mentions
# then fire Tier 1 falsely (observed 2026-07-11):
#   - <task-notification>...</task-notification> — background-task completion events.
#   - The post-compaction continuation summary ("This session is being continued
#     from a previous conversation ...") — a synthetic recap, not user text.
#   - The post-compaction skill-context replay ("The following skills were invoked
#     EARLIER in this session ...") — full SKILL.md bodies shown "for context only",
#     dense with 'self-improvement'.
# The last two are always TRAILING injected regions (the harness appends them; no
# human text follows in the same message), so they are excised from their marker
# to end-of-text — human feedback authored BEFORE the marker still survives.
_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?</task-notification>", re.DOTALL | re.IGNORECASE
)
_CONTINUATION_SUMMARY_RE = re.compile(
    r"This session is being continued from a previous conversation.*\Z",
    re.DOTALL | re.IGNORECASE,
)
_SKILL_REPLAY_RE = re.compile(
    r"The following skills were invoked EARLIER in this session.*\Z",
    re.DOTALL | re.IGNORECASE,
)


def strip_injected_context(text: str) -> str:
    """Remove harness-injected, non-human-authored spans from ``text``.

    Excises ``<system-reminder>`` and ``<task-notification>`` spans plus the two
    trailing post-compaction injections (continuation summary, skill-context
    replay). Only the human-authored remainder should feed the feedback detector.
    """
    text = _SYSTEM_REMINDER_RE.sub(" ", text)
    text = _TASK_NOTIFICATION_RE.sub(" ", text)
    text = _CONTINUATION_SUMMARY_RE.sub(" ", text)
    text = _SKILL_REPLAY_RE.sub(" ", text)
    return text


# Tier 1 — explicit self-improvement mention (any spacing/hyphenation variant)
_TIER1_RE = re.compile(r"self.?improvement", re.IGNORECASE | re.UNICODE)

# Tier 2(a) — strong imperative / "I-already-told-you" patterns (self-sufficient)
_STRONG_CORRECTIVE_RE = re.compile(
    r"\bdon'?t do that\b"
    r"|\bstop doing\b"
    r"|\bперестань\b"
    r"|\bне делай так\b"
    r"|\bя же просил\b"
    r"|\bя же говорил\b",
    re.IGNORECASE | re.UNICODE,
)

# Tier 2(b) — context-dependent corrective patterns (need agent-ref co-occurrence)
_WEAK_CORRECTIVE_RE = re.compile(
    r"\byou shouldn'?t\b"
    r"|\byou should have\b"
    r"|\byou didn'?t\b"
    r"|\bwhy did you\b"
    r"|\bnext time\b"
    r"|\byou always\b"
    r"|\byou keep\b"
    r"|\bthat'?s wrong\b"
    r"|\binstead of\b"
    r"|\bне так\b"
    r"|\bнеправильно\b"
    r"|\bне надо было\b"
    r"|\bне нужно было\b"
    r"|\bзачем ты\b"
    r"|\bпочему ты\b"
    r"|\bопять ты\b"
    r"|\bтак нельзя\b"
    r"|\bв следующий раз\b"
    r"|\bследовало\b"
    r"|\bдолжен был\b",
    re.IGNORECASE | re.UNICODE,
)

# Explicit 2nd-person agent-reference (required by Tier 2(b))
_AGENT_REF_RE = re.compile(
    r"\b(?:you|your|ты|тебя|тебе|тобой)\b",
    re.IGNORECASE | re.UNICODE,
)


def find_signals(prompt: str) -> list[str]:
    """Return a list of feedback-signal descriptions for ``prompt`` (empty if none).

    Precision-first: at most one signal is returned, naming the tier that fired.
    """
    if not isinstance(prompt, str) or not prompt:
        return []

    # Strip harness-injected context first — only the human-authored text
    # should drive feedback detection (see _SYSTEM_REMINDER_RE above).
    prompt = strip_injected_context(prompt)

    # Tier 1 — explicit self-improvement mention
    if _TIER1_RE.search(prompt):
        return ["explicit self-improvement mention"]

    # Tier 2(a) — strong imperative (self-sufficient, no agent-ref check)
    m = _STRONG_CORRECTIVE_RE.search(prompt)
    if m:
        return [f"agent-directed correction: '{m.group(0)}'"]

    # Tier 2(b) — context-dependent (agent-ref required)
    if _AGENT_REF_RE.search(prompt):
        m = _WEAK_CORRECTIVE_RE.search(prompt)
        if m:
            return [f"agent-directed correction: '{m.group(0)}'"]

    return []
