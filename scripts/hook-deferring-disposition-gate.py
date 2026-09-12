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
from lib import judge_ledger  # noqa: E402

# judge_ledger itself must import cleanly above for this to record anything —
# it is stdlib-only (see its own module docstring) and is what every other
# import failure here needs a working ledger to be recorded against.
try:
    from agentctl import advisor  # noqa: E402
    from lib import ask_text  # noqa: E402
    from lib import judge_budget  # noqa: E402
    from lib.host_llm import JUDGE_CHILD_ENV_VAR  # noqa: E402
except BaseException as exc:
    judge_ledger.import_failed("deferring_disposition", f"{type(exc).__name__}: {exc}")
    raise

# Whole-ask budget for the judge, and the ceiling handed to the one call it
# funds. Two superseded numbers are worth naming, because both were set from the
# same four-sample note ("a live judge call takes 11.6-13.5s"): the budget was
# 20s and the ceiling was borrowed from advisor._DEFERRING_DISPOSITION_TIMEOUT_S.
# The real distribution over n=18 (lib/judge_latency.py) is median 17.43, p90
# 37.58, max 39.99 — so 20s was BELOW this judge's own p90 and the gate was
# failing open on most asks it fired on, silently.
#
# The height is a judgement (how long a gate may hold an interactive menu); what
# is machine-checked against lib/judge_latency.py is that it clears this judge's
# per-call ceiling `ceil(max) + 1` = 41s, so the budget can never be what
# truncates the call. This hook makes exactly ONE judged call per invocation, so
# this number is also that call's ceiling — see decide() for why the second fired
# menu is not judged.
#
# Cost of this design, named rather than hidden. (1) A multi-question ask is
# judged on its FIRST fired menu only; the alternative (full multi-question
# recall) needs a ~130s ceiling ahead of an interactive menu, which is not an
# acceptable UX trade. (2) Even a single-menu ask is dropped on the tail: the
# budget covers this judge's p90, not its maximum, and every drop fails OPEN.
# No run in the n=18 sample exceeded 45s, which by the rule of three bounds the
# exceedance rate at roughly 3/18 (~17%) with 95% confidence — NOT at zero, and
# the plan's own final check refuses a claim that reads it as zero.
_ASK_JUDGE_BUDGET_S = 45

# Below this remaining budget a judge call cannot plausibly finish and would only
# spend the wait on a guaranteed timeout; stop judging and fail open instead,
# exactly like every other unreachable-judge path in this hook. This is
# lib/judge_latency.py's floor rule, `ceil(p90)` over n=18 — comfortably above
# the fastest run observed (10.29s), which is what makes a call started with
# exactly the floor left reachable rather than doomed.
_ASK_JUDGE_MIN_CALL_S = 38

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

    A single _ASK_JUDGE_BUDGET_S deadline bounds the WHOLE call (not each judge
    call individually): each judge call gets whatever of the budget remains,
    capped at _ASK_JUDGE_BUDGET_S — this hook's OWN ceiling, not
    advisor._DEFERRING_DISPOSITION_TIMEOUT_S, which is the last-resort default for
    a caller with no budget at all and is therefore not a bound this hook can
    honour. Once the remainder can no longer fit a meaningful call
    (_ASK_JUDGE_MIN_CALL_S), judging stops and the ask is allowed — fail-open,
    same posture as every other unreachable-judge path.

    That floor is what makes the loop single-call in practice, and the limit is
    declared rather than discovered: a second fired menu is reached only if the
    first call returned with _ASK_JUDGE_MIN_CALL_S still left, i.e. in under 7s —
    faster than the fastest run ever measured for this judge. So a multi-menu ask
    is judged on its first fired menu and allowed on the rest.

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
    # Opened here, after the payload's own parsing above — unlike
    # hook-turn-end-gate.py's transcript read, extracting ask_text from an
    # already-parsed tool_input is no file I/O, so nothing above is worth
    # docking from the judge budget.
    budget = judge_budget.JudgeBudget(_ASK_JUDGE_BUDGET_S, _ASK_JUDGE_MIN_CALL_S, clock=time.monotonic)
    for index, (full_text, opt_text, stem) in enumerate(zip(full_texts, opt_texts, stems), start=1):
        prefilter_fired = _prefilter(opt_text)
        judge_ledger.entered("deferring_disposition", prefilter_fired=prefilter_fired)
        if not prefilter_fired:
            continue  # cheap common path: this menu's options defer nothing
        remaining_before_call, call_timeout = budget.remaining_and_timeout(_ASK_JUDGE_BUDGET_S)
        if call_timeout is None:
            judge_ledger.decided(
                "deferring_disposition", stage="budget", verdict=False,
                reason="budget exhausted before call (fail-open)",
                remaining=remaining_before_call, threshold=None, ceiling=_ASK_JUDGE_BUDGET_S,
            )
            break  # budget exhausted — fail open, same as every other unreachable-judge path
        fires, _reason = advisor.judge_deferring_disposition(
            full_text, runner, enabled=enabled, timeout=call_timeout,
            remaining=remaining_before_call, ceiling=_ASK_JUDGE_BUDGET_S,
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
    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0  # a sandboxed judge subprocess, not a real user turn — allow, no opinion
    judge_ledger.hook_start("deferring_disposition")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    judge_ledger.source_from_payload(payload)

    try:
        decision = decide(payload, runner=advisor.subprocess_runner)
        has_directive = decision is not None
        judge_ledger.final(has_directive=has_directive)
        emit_ok = True
        try:
            if has_directive:
                print(json.dumps(decision))
        except Exception:
            emit_ok = False
        judge_ledger.emitted(ok=emit_ok, had_directive=has_directive)
    except Exception as exc:
        judge_ledger.discarded(reason=repr(exc))
        return 0  # fail-open — a hook must never wedge the ask
    return 0


if __name__ == "__main__":
    sys.exit(main())
