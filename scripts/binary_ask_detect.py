#!/usr/bin/env python3
"""Structural precondition helper: does this turn's assistant text END with a
question (rather than a statement)?

Difficulty removed: CLAUDE.md § Escalation mandates AskUserQuestion for every
confirmation and every defined-set choice — apply/skip, push, scope, resolution —
so the user clicks instead of typing. The recurring lapse is the inverse of the
one hook-ask-text-split.py guards: there the AskUserQuestion tool IS called but
mis-positioned; here the tool is NOT called at all and the binary confirm ends up
as a trailing prose question ("записать?", "публикуем v11?", "should I push?",
"считаем решённой?"). There is no tool call to gate, so only a Stop-boundary text
scan can catch it.

Split of labor (rule vs perception): the per-language natural-language cue regexes
that used to decide "is that trailing question a BINARY/CONFIRM ask vs an
open-ended free-text one?" have been RETIRED and moved to the model-backed
semantic_judge.py ('binary_ask' kind), which classifies MEANING in any language.
This module keeps ONLY the language-agnostic STRUCTURAL gate `final_question_segment`
— "does the text end with a question?" — the cheap precondition the Stop shell
checks before it spends a judge call. The prose_binary_ask turn guardian of
hook-turn-end-gate.py then reads the judge-derived boolean from its frozen context.
"""
from __future__ import annotations

import re

# Sentence terminators that bound the FINAL question segment. The final segment is
# everything after the last of these (before the trailing question mark). The
# full-width '？' is included so a segment ending one sentence earlier is bounded
# for either question-mark form.
_TERMINATOR_RE = re.compile(r"[.!?？\n]")


def final_question_segment(text: str) -> str | None:
    """The last sentence of ``text`` when the text (stripped) ends with a question
    mark (ASCII '?' or full-width '？'), else None. Bounded on the left by the
    previous sentence terminator.

    Language-agnostic and purely structural: it decides only that the last thing
    said is a question (a confirm buried mid-turn, with substantive text after it,
    yields None). Whether that question is a binary/confirm ask is the semantic
    judge's call, not this helper's.
    """
    if not isinstance(text, str):
        return None
    stripped = text.rstrip()
    if not stripped.endswith(("?", "？")):
        return None
    body = stripped[:-1]  # drop the trailing question mark
    # Left bound: char after the last sentence terminator in the remaining body.
    last_term = -1
    for m in _TERMINATOR_RE.finditer(body):
        last_term = m.end()
    segment = body[last_term:] if last_term >= 0 else body
    return segment.strip()
