#!/usr/bin/env python3
"""Shared DETERMINISTIC feedback-signal detector for the self-improvement discipline.

Difficulty removed: the same "is this an agent-behavior-feedback turn?" decision
is needed in two places — the advisory `UserPromptSubmit` reminder
(`hook-self-improvement-reminder.py`) and the end-of-turn `Stop` gate
(`hook-turn-end-gate.py`). Keeping the shared deterministic match in one importable
module means the instant nudge and the gate can never drift apart on it.

Split of labor (rule vs perception): only Tier 1 — the explicit 'self-improvement'
proper-name mention — is a language-agnostic DETERMINISTIC rule and stays here. The
former Tier-2 natural-language corrective cues (imperatives, agent-directed
'you shouldn't' / RU 'почему ты', …) were per-language regexes that were RETIRED
and moved to the model-backed semantic_judge.py ('si_feedback' kind), which
classifies MEANING in any language. The Stop shell consults that judge behind a
precondition gate (skill not already invoked, short human text, not a bare
affirmation); the instant reminder deliberately does NOT (it must stay latency-free
on the prompt path).

Detection design:

  Tier 1 — explicit self-improvement reference (near-certain, deterministic):
    'self-improvement' / 'selfimprovement' / 'self improvement' as a substring,
    after excising harness-injected context. Covers "did you run self-improvement?",
    "запусти self-improvement", etc.

This module also exports `is_neutral_affirmation` — a small closed multilingual set
of bare affirmations / gratitude ("ok", "спасибо", …) that are never feedback — so
the Stop shell can skip a judge call on them (a pure affirmation is not a correction).
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

# Bare neutral affirmations / gratitude — a small closed multilingual set. A whole
# message that is one of these is never agent-behavior feedback, so the Stop shell
# skips the si_feedback judge on it (case- and trailing-punctuation-insensitive).
_NEUTRAL_AFFIRMATIONS = frozenset({
    "ok", "okay", "k", "kk", "yes", "yep", "yeah", "yup", "sure", "fine",
    "thanks", "thank you", "thx", "ty", "great", "perfect", "nice", "cool",
    "done", "good", "got it", "ok thanks", "ok thx", "sounds good", "lgtm",
    "да", "ага", "угу", "ок", "окей", "спасибо", "спс", "хорошо", "ладно",
    "отлично", "супер", "класс", "понял", "поняла", "принято", "ясно", "верно",
})


def is_neutral_affirmation(text: str) -> bool:
    """True iff the whole message is a bare neutral affirmation / gratitude (a small
    closed multilingual set) — never agent-behavior feedback. Case and trailing
    punctuation are ignored. An empty message is treated as neutral (nothing to
    judge)."""
    if not isinstance(text, str):
        return False
    norm = text.strip().lower().strip(".!…,:;) ")
    if not norm:
        return True
    return norm in _NEUTRAL_AFFIRMATIONS


def find_signals(prompt: str) -> list[str]:
    """Return the DETERMINISTIC Tier-1 feedback signal for ``prompt`` (empty if none).

    Tier 1 only: the explicit 'self-improvement' proper-name mention (any
    spacing/hyphen variant), after excising harness-injected context. The former
    Tier-2 natural-language corrective cues were retired from regex matching and
    moved to the semantic judge (semantic_judge.py, 'si_feedback'); the Stop shell
    consults that judge behind a precondition gate. Keeping this match deterministic
    is what lets the instant UserPromptSubmit reminder stay latency-free and
    judge-free.
    """
    if not isinstance(prompt, str) or not prompt:
        return []

    # Strip harness-injected context first — only the human-authored text
    # should drive feedback detection (see _SYSTEM_REMINDER_RE above).
    prompt = strip_injected_context(prompt)

    # Tier 1 — explicit self-improvement mention
    if _TIER1_RE.search(prompt):
        return ["explicit self-improvement mention"]

    return []
