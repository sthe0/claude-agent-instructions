#!/usr/bin/env python3
"""Heuristic detector: does this turn's assistant text END with a BINARY / CONFIRM
question posed in PROSE (rather than through an AskUserQuestion click-gate)?

Difficulty removed: CLAUDE.md § Escalation mandates AskUserQuestion for every
confirmation and every defined-set choice — apply/skip, push, scope, resolution —
so the user clicks instead of typing. The recurring lapse is the inverse of the
one hook-ask-text-split.py guards: there the AskUserQuestion tool IS called but
mis-positioned; here the tool is NOT called at all and the binary confirm ends up
as a trailing prose question ("записать?", "публикуем v11?", "should I push?",
"считаем решённой?"). There is no tool call to gate, so only a Stop-boundary text
scan can catch it. This module is the shared PERCEPTION half; the deterministic
part (no AskUserQuestion invoked this turn, not already seeking closure) lives in
the prose_binary_ask turn guardian of hook-turn-end-gate.py.

This is a heuristic backstop, not a determinization: paraphrases it cannot match
are expected misses, and it deliberately errs toward UNDER-firing.

Design (precision-first; mirrors outage_escalation_detect.py / si_feedback_detect.py):

  Fires ONLY when ALL hold for the text's FINAL utterance:
    1. the text, stripped, ENDS with "?" — the last thing said is a question
       (a confirm buried mid-turn, with substantive text after it, does not fire);
    2. that final question segment contains a CONFIRM / ACTION cue
       ("записать", "публикуем", "считаем решённой", "push", "apply", "land", …); AND
    3. the final question does NOT begin with an OPEN wh-word
       ("что", "какой", "how", "which", …) — an open-ended question is answered by
       free text, which AskUserQuestion is NOT for, so it is out of scope here.

  Returns AT MOST ONE signal string naming what matched (empty list == no fire).

Precision choices (documented misses, not bugs):
  - "что записать?" contains the confirm verb "записать" but begins with the open
    wh-word "что" -> suppressed by rule 3 (genuinely open, free-text answer).
  - "how should I proceed?" begins with "how" -> suppressed, correctly.
  - "Это удалось?" does not fire: the cue is narrowed to удали\\w*|удаля\\w*, which
    does not match удалось / удалённый (thinker review, first pass).
  - Bare high-frequency tokens (да / ок / верно) are deliberately NOT cues — they
    would dominate false positives (same lesson as si_feedback_detect's V1
    exclusions). Kept only unambiguous confirms (годится, подтверждаешь).
  - готов\\w* and счита\\w* are the loosest cues: a non-wh informational question
    like "Есть ли готовое решение?" or "Ты так считаешь?" can fire. Bounded by the
    conjunction (ends-'?' + not-wh-front + final-segment-only), block-once-per-
    message and fail-open in the guardian; pinned as regression negatives in tests.
  - A confirm question followed by more prose ("Записать? Ниже детали…") does not
    end with "?" -> rule 1 misses it. Prefer under-fire over a fragile matcher.
"""
from __future__ import annotations

import re

# Sentence terminators that bound the FINAL question segment. The final segment is
# everything after the last of these (before the trailing "?").
_TERMINATOR_RE = re.compile(r"[.!?\n]")

# Confirm / action cues: the verb family of "do this action?" and the resolution /
# landing asks that are this mechanism's headline motivating case. Every token is
# word-boundary / stem anchored — no bare high-frequency tokens, and RU stems
# narrowed so they do not match unrelated words (удали|удаля, not удал which also
# matches удалось / удалённый).
_CONFIRM_RE = re.compile(
    r"записать|записыва\w*|сохран\w*|публику\w*|опубликова\w*"
    r"|примен\w*|делаем\b|сделать|продолж\w*|запуск\w*|запустить"
    r"|пушим\b|запуш\w*|мерж\w*|смерж\w*|коммит\w*|закоммит\w*|фиксир\w*"
    r"|оставля\w*|оставить|удали\w*|удаля\w*|перезапис\w*|обновля\w*|обновить"
    r"|создад\w*|создаём|создать|закрыв\w*|закрыт\w*|отправ\w*"
    r"|реш(?:ено|ена|ённой|или|аем|аете)|счита\w*|готов\w*|вливаем\b|льём\b"
    r"|годится\b|подтвержда\w*"
    r"|\bapply\b|\bproceed\b|\bcontinue\b|go ahead|should i\b|shall i\b"
    r"|do you want me to|want me to|\bpush\b|\bmerge\b|\bcommit\b|\bsave\b|\brecord\b"
    r"|\bcreate\b|\bdelete\b|overwrite|\bconfirm\b|\bok to\b|\bland\b|\bship\b",
    re.IGNORECASE | re.UNICODE,
)

# Open wh-interrogatives: a final question opening with one of these is answered by
# free text (a name, a path, a choice among an OPEN set), which AskUserQuestion is
# not the right instrument for — so it is out of this detector's scope.
_OPEN_WH_RE = re.compile(
    r"^\W*(?:"
    r"что|чего|чему|какой|кака\w*|какие|каких|каком|каку\w*|куда|где|когда"
    r"|почему|зачем|как\b|сколько|кто|кого|кому|чей|чь\w*|чем\b"
    r"|what|which|where|when|why|how|who|whom|whose"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def _final_question_segment(text: str) -> str | None:
    """The last sentence of ``text`` when the text (stripped) ends with '?', else
    None. Bounded on the left by the previous sentence terminator."""
    stripped = text.rstrip()
    if not stripped.endswith("?"):
        return None
    body = stripped[:-1]  # drop the trailing '?'
    # Left bound: char after the last sentence terminator in the remaining body.
    last_term = -1
    for m in _TERMINATOR_RE.finditer(body):
        last_term = m.end()
    segment = body[last_term:] if last_term >= 0 else body
    return segment.strip()


def detect(text: str) -> list[str]:
    """Return a one-element signal list when ``text`` ends with a binary/confirm
    question posed in prose, else []. Precision-first: the final utterance must be
    a question (rule 1), carry a confirm/action cue (rule 2), and not open with a
    wh-word (rule 3)."""
    if not isinstance(text, str) or not text:
        return []
    segment = _final_question_segment(text)
    if not segment:
        return []
    if _OPEN_WH_RE.match(segment):
        return []
    cmatch = _CONFIRM_RE.search(segment)
    if not cmatch:
        return []
    return [f"binary/confirm question posed in prose (cue: {cmatch.group(0)!r})"]
