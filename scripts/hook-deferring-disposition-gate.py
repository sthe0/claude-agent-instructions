#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny an ask whose EVERY option
defers or refuses work the agent could have done itself, right now.

Difficulty removed: the agent finds a defect, holds the rights, the tools and a
finished diagnosis — and instead of fixing it hands the user a menu in which no
branch does the work ("завести отдельной задачей (Рекомендую)" / "не трогать").
The user is then forced to spend a turn asking why the fix wasn't simply offered.
The norm against this (memory-global/leaves/capability-before-offload.md) was
live in the session snapshot when it happened, so this is a norm that did NOT
fire, not a missing norm — and a norm that does not fire is repaired
structurally, by a gate, not by another line of prose.

Decided PER QUESTION (an AskUserQuestion payload may carry more than one menu):
DENY as soon as ONE question's menu satisfies BOTH:
  1. _prefilter fires on THAT QUESTION'S OPTION TEXT ONLY (labels/descriptions,
     never the question/header stem) — a deliberately HIGH-RECALL regex over
     deferral/refusal vocabulary ("тикет", "бэклог", "позже", "оставить как
     есть", "backlog", "later", "leave as is", …), which answers only the cheap
     question "is this menu's options worth a closer look". Scoping to option
     text (not the stem) matters: a question like "Считаем задачу решённой?"
     carries the cue word "задачу" in its own wording while every option is a
     plain confirm — gating on the stem would false-positive that ask into an
     unnecessary judge call. AND
  2. agentctl.advisor.judge_deferring_disposition — given that question's FULL
     text (stem + options, for context) — a fail-open semantic model judge —
     confirms that not one option does the work now AND that the question
     names no reason the work is beyond the agent.
A question whose prefilter doesn't fire, or whose judge call returns NO, is
skipped and the next question is checked; ALLOW only once every question has
been checked and none fired.

The split is load-bearing (memory-global/leaves/regex-not-for-semantic-
classification.md): the SAME option vocabulary is defective when the agent could
act and legitimate when the work is someone else's ("передать владельцу
сервиса"), so a regex deciding that by itself would hard-block on wording its
author never foresaw. The regex may only widen recall; the meaning is the
model's call.

Every observable failure FAILS OPEN (allow): no runner, a disabled judge, a
timeout, an unparseable answer, a malformed payload, any unexpected error. The
hook always exits 0; DENY is delivered through the PreToolUse permissionDecision
JSON contract, as in hook-escalation-diagnosis-gate.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import advisor  # noqa: E402
from lib import ask_text  # noqa: E402
from lib import judge_budget  # noqa: E402

# Whole-ask budget for the judge, distinct from advisor._DEFERRING_DISPOSITION_
# TIMEOUT_S=30 (the per-CALL ceiling). Measured 2026-08-05: a live judge call
# takes 11.6-13.5s, so a multi-question ask that reaches a second/third fired
# menu at 30s-per-call could stall the interactive ask well past this hook's
# own harness timeout (install-reminder-hooks.sh, 25s). 20s keeps the WHOLE
# decide() call inside that harness timeout with headroom for the interpreter
# start; individual calls get whatever of the 20s remains (see decide()).
#
# Cost of this design, named rather than hidden: at a 20s budget and an
# 11.6-13.5s call, an ask with ONE fired menu is always judged; a multi-
# question ask is only reliably judged on its FIRST fired menu — later fired
# menus may be skipped on budget exhaustion (allowed, not denied). The
# alternative (full multi-question recall) needs a ~130s hook ceiling ahead of
# an interactive menu, which is not an acceptable UX trade.
#
# The latency distribution has a HEAVY TAIL, so this budget also drops a share
# of SINGLE-menu asks: 8 calls on the founding ask, measured 2026-08-05 with no
# timeout, came in at 10.5 / 11.5 / 12.2 / 12.6 / 13.7 / 14.2 / 15.4 / 47.0s —
# seven inside the budget, one far outside. That outlier fails OPEN, so the
# gate's real recall is about 7/8, not 1. Raising the budget does not recover
# it: no ceiling a user tolerates ahead of an interactive menu covers a 47s
# call, and the extra seconds would be paid on exactly the runs that are
# already pathological. Losing that one is the deliberate trade.
_ASK_JUDGE_BUDGET_S = 20

# Below this remaining budget a judge call cannot plausibly finish (measured
# latency 11.6-13.5s) and would only spend the wait on a guaranteed timeout;
# stop judging and fail open instead, exactly like every other unreachable-
# judge path in this hook.
_ASK_JUDGE_MIN_CALL_S = 12

# Upper bound on a question stem embedded in the deny reason (_truncate_stem)
# — long enough to identify the offending menu, short enough not to reproduce
# it verbatim.
_STEM_MAX_CHARS = 80

# Kill-switch for the semantic deferring-disposition judge: set to "0" to force
# it off without a code change. Safe-by-default — unset/unrecognised leaves the
# judge ENABLED, and with it off the gate can only allow, never deny.
_DEFERRING_DISPOSITION_KILLSWITCH_ENV = "CLAUDE_DEFERRING_DISPOSITION_SEMANTIC"

_DENY_REASON = (
    "Every option in this ask defers or refuses the work (ticket / backlog / "
    "later / leave as is) — none of them does it now. If you hold the rights, "
    "the tools and the diagnosis, the work is yours to do: add an option that "
    "performs it now and make that the recommended one. If the work genuinely "
    "is not yours to do, say so inside the ask (whose it is, which right or "
    "resource you lack), then re-ask."
)

# HIGH-RECALL prefilter over deferral / refusal vocabulary, in the two languages
# this fleet's asks are written in. It exists to keep the model off asks that
# postpone nothing at all; it deliberately over-fires (a plain "задача" is
# enough) because every false positive costs one cheap judge call, while a false
# negative silently disables the gate.
_DEFER_CUE_RE = re.compile(
    r"тикет\w*|задач\w*|бэклог\w*|беклог\w*|отдельн\w*|позже|позднее|потом\b"
    r"|не сейчас|отлож\w*|не трог\w*|как есть|не мен\w*|ничего не дела\w*"
    r"|оставить как|оставим как|вернём?ся\b|вернуться\b"
    r"|\bbacklog\b|\bticket\w*|\bissue\w*|follow[- ]?up|\btodo\b|\blater\b"
    r"|\bdefer\w*|\bpostpone\w*|leave (?:it )?as[- ]is|no change|\bskip\b"
    r"|do not touch|don'?t touch|separate task|another task|next time",
    re.IGNORECASE | re.UNICODE,
)


def _truncate_stem(stem: str, limit: int = _STEM_MAX_CHARS) -> str:
    """Truncate a question stem to a reasonable length for embedding in the
    deny reason — a full stem could itself run long, and the reason is meant
    to point the agent at the right menu, not reproduce it verbatim."""
    stem = stem.strip()
    if len(stem) <= limit:
        return stem
    return stem[: limit - 1].rstrip() + "…"


def _offending_menu_note(index: int, stem: str) -> str:
    """One-sentence pointer at WHICH question fired, appended to _DENY_REASON.
    Without it, a multi-question ask gives the agent no way to tell which of
    several menus is the defective one — it could rewrite the wrong menu and
    hit the same deny again."""
    return f' The offending menu is question #{index}: "{_truncate_stem(stem)}".'


def _prefilter(text: str) -> bool:
    """True when the given text carries any deferral/refusal cue worth a judge
    call. Called on ONE question's OPTION text at a time (see decide()) — never
    on the question/header stem, which routinely contains the same vocabulary
    ("задачу", "не менее") without deferring anything itself."""
    if not isinstance(text, str) or not text:
        return False
    return _DEFER_CUE_RE.search(text) is not None


# Byte-identical alias to lib.ask_text.flat_text — kept under this name so the
# existing unit tests (_mod._ask_text) need no change; shared with
# hook-escalation-diagnosis-gate.py (lib/ask_text.py). decide() itself uses the
# per-question lib.ask_text.question_texts/option_texts, not this flat form.
_ask_text = ask_text.flat_text


def decide(payload: dict, *, runner: Callable | None = None) -> dict | None:
    """Core decision. Returns the PreToolUse deny payload to print, or None to
    allow.

    Decided PER QUESTION: an AskUserQuestion payload may carry more than one
    menu, and the defer/refuse predicate is a property of ONE menu, not of the
    payload as a whole (module docstring). For each question, the prefilter
    checks that question's OPTION text only (ask_text.option_texts) so a
    deferral-shaped word in the question's own stem never costs a judge call by
    itself; the judge itself gets that question's FULL text (ask_text.
    question_texts) since it needs the stem for context (e.g. to tell a
    defective menu from a forced-deferral one). The first question whose
    prefilter fires AND whose judge call returns True denies the whole ask;
    remaining questions are only reached if none has fired yet.

    A single _ASK_JUDGE_BUDGET_S deadline bounds the WHOLE call (not each
    judge call individually): each judge call gets whatever of the budget
    remains, capped at advisor._DEFERRING_DISPOSITION_TIMEOUT_S (that ceiling
    stays as the single-call cap; the deadline is what makes it non-binding in
    practice, since the budget is smaller). Once the remainder can no longer
    fit a meaningful call (_ASK_JUDGE_MIN_CALL_S), judging stops and the ask
    is allowed — fail-open, same posture as every other unreachable-judge path.

    ``runner`` is injected straight into advisor.judge_deferring_disposition
    (None -> that judge fails open to False, never denies).

    The deny payload is BUILT HERE, in the same scope as the judge call, instead
    of in a separate emitter: the repo's mechanical audit (crutch-inventory.py,
    tests/test_no_semantic_unguarded.py) reads guard and sink per SCOPE, so a
    deny split from its fail-open judge reads as unguarded and needs a
    hand-written allowlist ground to stay green. Co-locating them keeps the
    guard machine-visible instead."""
    if payload.get("tool_name") != "AskUserQuestion":
        return None
    tool_input = payload.get("tool_input") or {}
    enabled = os.environ.get(_DEFERRING_DISPOSITION_KILLSWITCH_ENV) != "0"
    full_texts = ask_text.question_texts(tool_input)
    opt_texts = ask_text.option_texts(tool_input)
    stems = ask_text.question_stems(tool_input)
    budget = judge_budget.JudgeBudget(_ASK_JUDGE_BUDGET_S, _ASK_JUDGE_MIN_CALL_S, clock=time.monotonic)
    for index, (full_text, opt_text, stem) in enumerate(zip(full_texts, opt_texts, stems), start=1):
        if not _prefilter(opt_text):
            continue  # cheap common path: this menu's options defer nothing
        call_timeout = budget.next_call_timeout(advisor._DEFERRING_DISPOSITION_TIMEOUT_S)
        if call_timeout is None:
            break  # budget exhausted — fail open, same as every other unreachable-judge path
        fires = advisor.judge_deferring_disposition(
            full_text, runner, enabled=enabled, timeout=call_timeout
        )
        if fires:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _DENY_REASON + _offending_menu_note(index, stem),
                }
            }
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        decision = decide(payload, runner=advisor.subprocess_runner)
    except Exception:
        return 0  # fail-open — a hook must never wedge the ask
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
