"""Warn-only advisory LLM judge for the semantic cognition residue.

The advisor NEVER sets directive.ok=False, NEVER changes directive.node, and NEVER
blocks a transition. With the advisor returning [] (disabled / errored / stubbed),
control flow is byte-identical to advisor-absent. Fail-open: any exception yields [].
Default-off: only active when AGENTCTL_ADVISOR=1 is set in the environment, OR when
resolve_enabled()'s config-mode + weight-class rule turns it on for a substantive
session (see resolve_enabled).
"""
from __future__ import annotations

import os
import subprocess
import time

from lib import judge_ledger

from .config import Thresholds
from .dispatch import RunResult

# Cheap model + hard cap: the advisor auto-activates for every substantive session's
# cognition points, so each call must stay bounded in cost and can never hang a
# coordination step.
_ADVISOR_MODEL = "sonnet"
_ADVISOR_TIMEOUT_S = 20

# The acceptance judge is a SEPARATE, cheaper tier than the warn-only advisor: it
# gates a real transition (via the pure acceptance-review guardian), so it runs on the
# cheapest model and is fail-open (a missing verdict blocks at the gate, never passes).
_JUDGE_MODEL = "haiku"
JUDGE_REVIEWER = "judge:haiku"
# Last-resort ceiling for a judge call made outside any hook budget, by the rule
# in lib/judge_latency.py::last_resort_ceiling_s — one second past the slowest
# run this model has been seen to make on ANY judge prompt. Its row in that
# module is UNMEASURED, so this default is the only number available to it; the
# test-suite asserts the literal still equals what that rule computes.
_ACCEPTANCE_JUDGE_TIMEOUT_S = 41
_JUDGE_PASS = "pass"
_JUDGE_REVISE = "revise"

_ADVISOR_MODE_SUBSTANTIVE = "substantive"
_SUBSTANTIVE_WEIGHT_CLASS = "SUBSTANTIVE"

_PROMPTS: dict[str, str] = {
    "weight_classification": (
        "Review this task classification. Flag any concerns about whether the weight class "
        "or route is correct. Return each concern as one line. Return nothing if none.\n{payload}"
    ),
    "plan_completeness": (
        "Review this plan for completeness: do the stages cover the goal? "
        "Flag missing coverage, hand-waving, or omitted prerequisites as one concern per line. "
        "Return nothing if the plan looks complete.\n{payload}"
    ),
    "hypothesis_distinctness": (
        "Review these hypotheses for genuine distinctness in MEANING (not just string difference). "
        "Flag if any two hypotheses describe the same failure mode, or if the declaration does not "
        "capture a real divergence. One concern per line; nothing if all look distinct.\n{payload}"
    ),
    "acceptance_observation": (
        "Review this acceptance observation: does it describe what was actually observed, "
        "or is it vague, generic, or a rephrase of the expected result? "
        "One concern per line; nothing if the observation is concrete and adequate.\n{payload}"
    ),
}

_ENUMERATE_PROMPT = (
    "You are given a reasoning/research deliverable. List every LOAD-BEARING "
    "decision, judgment, or claim a reader would take as established fact — a "
    "choice made, a recommendation proposed, a causal or quantitative claim. "
    "One item per line, no numbering, no bullets, no prose, no preamble. Return "
    "nothing if the text makes no load-bearing claims.\n\n{payload}"
)


def enumerate_claims(artifact_text: str, runner) -> list[str]:
    """Independent semantic re-reading of an outgoing deliverable that RAISES the
    load-bearing decisions/judgments/claims it detects, one statement per line.

    This is a recall-widener for the coordinator's OWN enumeration, never
    authoritative and never complete — model perception with recall < 100%. The
    deterministic disposition gate (ledger.validate_candidates) is what turns each
    raised item into a blocker; this call only supplies the candidates.

    Cost-bounded exactly like the warn-only advisor: `claude -p --model sonnet`
    with an explicit _ADVISOR_TIMEOUT_S at the call site. Fail-open:
    a None runner, a non-zero exit, or any exception returns [] — an empty
    enumeration is a valid (if unhelpful) result; the mandatory-cross-check blocker
    is discharged by the `enumerated` flag the caller sets, not by the count."""
    if runner is None:
        return []
    try:
        prompt = _ENUMERATE_PROMPT.format(payload=artifact_text)
        result = runner(
            ["claude", "-p", "--model", _ADVISOR_MODEL, prompt],
            timeout=_ADVISOR_TIMEOUT_S,
        )
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


_ENUMERATE_QUESTIONS_PROMPT = (
    "You are given a plan's goal, done-criterion, and full stage text. Independently "
    "re-read it and list the QUESTIONS the plan's construction SHOULD have raised but "
    "may have left implicit — an unstated assumption, an ambiguous term, a choice made "
    "without justification, a premise smuggled in as fact. Do NOT answer them; only "
    "raise them.\n\n"
    "Emit one question per line as `<target>\\t<question>` (a literal TAB between the "
    "two). `<target>` names the plan element the question is raised against and MUST be "
    "one of:\n"
    "  plan.goal\n"
    "  plan.done_criterion\n"
    "  stage:<n>.<element>   where <n> is a stage index and <element> is one of: "
    "material, result, invariants, means, method, executor, capability, criterion, "
    "done_criterion, principle, conditions\n"
    "No numbering, no bullets, no prose, no preamble. Return nothing if the plan raises "
    "no implicit questions.\n\n{payload}"
)


def enumerate_questions_health(
    goal: str, done_criterion: str, plan_text: str, runner
) -> tuple[bool | None, list[tuple[str, str]]]:
    """Independent re-reading of a WHOLE plan that RAISES the questions its
    construction should have provoked, as (target, question) pairs, together with a
    runner-health flag.

    ONE bounded `claude -p --model sonnet` call over the goal + done-criterion + full
    plan text — deliberately whole-plan, not one call per element: the questions worth
    raising are overwhelmingly cross-element (a stage's method contradicting the goal, a
    done-criterion an invariant can't hold) and per-element calls would both miss those
    and multiply the cost/latency by the element count for no recall gain.

    Fail-open, exactly like enumerate_claims. The returned flag reports whether the
    runner produced a usable answer, so the caller can record runner health and attach a
    non-blocking advisory when the pass was vacuous — WITHOUT ever re-gating on it:

      * runner is None        -> (None, [])   advisor absent (disabled/stubbed)
      * non-zero exit          -> (False, [])  runner reachable but failed
      * exception              -> (False, [])  timeout/crash swallowed
      * success (0 exit)       -> (True, pairs) pairs may still be empty

    The mandatory cross-check blocker is discharged by the `enumerated` flag the caller
    sets REGARDLESS of the pair count — never by the count itself. Gating discharge on a
    non-empty result would let a single 20 s timeout (or a genuinely question-free plan)
    wedge approve permanently with no route out; fail-open buys that liveness, and the
    silent-discharge cost it incurs is paid back non-blockingly by the caller's advisory,
    not by making approve un-passable on infra failure."""
    if runner is None:
        return None, []
    try:
        payload = f"GOAL:\n{goal}\n\nDONE CRITERION:\n{done_criterion}\n\nPLAN:\n{plan_text}"
        prompt = _ENUMERATE_QUESTIONS_PROMPT.format(payload=payload)
        result = runner(
            ["claude", "-p", "--model", _ADVISOR_MODEL, prompt],
            timeout=_ADVISOR_TIMEOUT_S,
        )
        if result.returncode != 0:
            return False, []
        pairs: list[tuple[str, str]] = []
        for ln in (result.stdout or "").splitlines():
            if not ln.strip():
                continue
            target, sep, question = ln.partition("\t")
            target, question = target.strip(), question.strip()
            if not sep or not target or not question:
                continue
            pairs.append((target, question))
        return True, pairs
    except Exception:
        return False, []


def enumerate_questions(
    goal: str, done_criterion: str, plan_text: str, runner
) -> list[tuple[str, str]]:
    """Thin wrapper over enumerate_questions_health returning only the (target,
    question) pairs — the recall-widener surface, symmetric with enumerate_claims. A
    caller that also needs to record runner health calls the _health variant directly."""
    return enumerate_questions_health(goal, done_criterion, plan_text, runner)[1]


def judge(kind: str, payload: dict, runner, *, enabled: bool | None = None) -> list[str]:
    """Return advisory strings for the given cognition point, or [] if disabled/failed.

    Warn-only: callers MUST NOT branch on the return value for control flow.
    Fail-open: runner=None, non-zero exit, or any exception returns [].
    """
    if enabled is None:
        enabled = os.environ.get("AGENTCTL_ADVISOR") == "1"
    if not enabled or runner is None:
        return []
    try:
        template = _PROMPTS.get(kind)
        if not template:
            return []
        prompt = template.format(payload=payload)
        result = runner(
            ["claude", "-p", "--model", _ADVISOR_MODEL, prompt],
            timeout=_ADVISOR_TIMEOUT_S,
        )
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def acceptance_judge(
    observation: str,
    expected: str,
    runner,
    *,
    enabled: bool,
    timeout: int = _ACCEPTANCE_JUDGE_TIMEOUT_S,
) -> tuple[str | None, str]:
    """Cheap external judge for an acceptance observation, backing the acceptance-review
    gate. Returns (verdict, reason) where verdict is 'pass' | 'revise' | None.

    Fail-OPEN: a disabled judge, a None runner, a non-zero exit, an unparseable answer,
    or any exception returns (None, <reason>) — NEVER a false 'pass'. The caller records
    a StageReview only for a non-None verdict, and the PURE gate fails CLOSED on the
    resulting absence, so an unavailable judge stalls the pass safely.

    The prompt is lifted from _PROMPTS['acceptance_observation'] (the same criterion the
    warn-only advisor applies) and wrapped with a strict YES/NO + one-line-reason
    protocol so the deterministic gate has a machine-decidable verdict rather than a
    free-text concern list.

    ``timeout`` is explicit at the call site because this judge runs inside the
    engine, not inside a hook: nothing above it kills a hung call, so the number
    cannot be left to the runner's own default. Its latency row is UNMEASURED,
    so the default is the last-resort ceiling rather than a per-judge one."""
    if not enabled or runner is None:
        return None, "judge disabled or no runner (fail-open)"
    try:
        criterion = _PROMPTS["acceptance_observation"].format(
            payload={"expected": expected, "observation": observation}
        )
        prompt = (
            criterion
            + "\n\nAnswer on the FIRST line with exactly YES (the observation is concrete "
            "and adequate) or NO (it is vague, generic, or a rephrase of the expected). "
            "On the SECOND line give a one-line reason."
        )
        result = runner(
            ["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout
        )
        if result.returncode != 0:
            return None, "judge exited non-zero (fail-open)"
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return None, "judge returned no output (fail-open)"
        head = lines[0].upper()
        reason = lines[1] if len(lines) > 1 else lines[0]
        if head.startswith("YES"):
            return _JUDGE_PASS, reason
        if head.startswith("NO"):
            return _JUDGE_REVISE, reason
        return None, f"judge answer unparseable: {lines[0]!r} (fail-open)"
    except Exception:
        return None, "judge raised (fail-open)"


# Language-independent question-mark set for the pre-model prefilter: the ASCII
# '?' plus the fullwidth CJK, Arabic, Greek, and double question marks. Deliberately
# NOT ASCII-only endswith('?') — that would silently miss every CJK/Arabic/Greek
# question, defeating the point of a language-independent detector.
_BINARY_ASK_QUESTION_MARKS = frozenset({
    "?",        # U+003F ASCII question mark
    "？",   # fullwidth CJK question mark "？"
    "؟",   # Arabic question mark "؟"
    ";",   # Greek question mark (looks like ';')
    "⁇",   # double question mark "⁇"
})

# Trailing "decoration" a confirm question is commonly wrapped in: markdown
# emphasis (**bold?**, _em?_, `code?`, ~strike?~), closing brackets/quotes
# ("...ok?)", '...land it?"'), and whitespace. Stripped as a suffix RUN before the
# last-char question-mark prefilter so a bolded/parenthesised ask like
# "**...сделать?**" or "...ok?)" is still recognised — otherwise its literal last
# char is '*' / ')' , the judge is never called, and the prose_binary_ask Stop-gate
# never fires (the concrete miss that motivated this: a turn ending "...потом?**").
# Deliberately disjoint from _BINARY_ASK_QUESTION_MARKS and contains no letters/
# digits, so rstrip() can only consume a trailing punctuation/whitespace run and can
# never chew into real word content or expose a '?' from mid-message. Whitespace is
# listed explicitly because str.rstrip(chars) does NOT also strip whitespace once a
# chars argument is given.
_BINARY_ASK_TRAILING_DECORATION = "*_`~)]}>\"'»”’ \t\r\n"

# LAST-RESORT default for the three judges below, used only when a caller names
# no timeout of its own. Not "bounded well under _ADVISOR_TIMEOUT_S" any more:
# that reading treated the cheap advisory model's cap as an upper bound for a
# call it does not make, and produced 8 s -- under the FASTEST run any of these
# judges has been measured to make (5.93 s for binary_ask, 10.29 s for
# deferring), so an unbudgeted caller was killed before every verdict.
# By lib/judge_latency.py::last_resort_ceiling_s: one second past the slowest run
# this model has made on ANY judge prompt. A caller inside a hook budget passes
# its own, narrower, per-judge ceiling and never reaches this number; the
# test-suite asserts the literal still equals what that rule computes.
_BINARY_ASK_TIMEOUT_S = 41

_BINARY_ASK_PROMPT = (
    "You are given the FINAL message of an AI assistant's turn, written in any "
    "language. Decide whether the message ends with a BINARY or ONE-OF-N CONFIRM "
    "question -- one whose right answer instrument is a button/click (apply, push, "
    "land, save, choose option A/B/C, confirm a resolution) -- as opposed to a "
    "question that expects a free-text answer or is merely rhetorical.\n\n"
    "Answer YES only for decisional / action / resolution / scope confirm "
    "questions, for example (any language): \"Apply this change?\", \"Запустить "
    "бенчмарк?\", \"Push to main or open a PR?\", \"Считаем задачу решённой?\", "
    "\"Land it?\", \"Оставляем как есть или откатываем?\".\n\n"
    "Answer NO for: rhetorical or comprehension checks (\"Понятно?\", \"Makes "
    "sense?\", \"ok?\", \"ясно?\", \"Yeah?\"), open-ended / wh-questions, purely "
    "informational questions, or when the message poses no question at all.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "MESSAGE:\n{text}"
)


_MISSING = object()

_UNAVAILABLE_REASON = "judge disabled or no runner (fail-open)"
_NO_TEXT_REASON = "judge given no text (fail-open)"

_UNATTRIBUTED_JUDGE = "unattributed"


def _judge_unavailable(
    name: str, reason: str, *, timeout, remaining, ceiling
) -> tuple[bool, str]:
    """Terminal outcome for a decision point reached with no call possible:
    disabled by killswitch, no runner injected, or nothing to judge. Shared by
    all four judge functions, which differ here only in their own name."""
    judge_ledger.decided(
        name, stage="disabled", verdict=False, reason=reason,
        remaining=remaining, threshold=timeout, ceiling=ceiling,
    )
    return False, reason


def _classify(result) -> tuple[bool, str, bool, bool | None]:
    """(verdict, reason, malformed, timed_out) from one runner result — the
    single copy of the classification the four judge functions each held
    verbatim. Their ``runner(...)`` call sites stay where they are (frozen by a
    concurrent change); only what happens to the result is shared.

    ``timed_out`` is None when the result object has no such field: a runner
    predating the flag cannot distinguish a timeout from a fast failure, and
    fabricating False would put an unknown into the ledger as a fact.

    ``malformed`` is set ONLY where the call returned an answer that could not
    be parsed (outcome 7a) — not on a timeout, a non-zero exit or an exception,
    each of which produced no answer at all and is named by its own field."""
    raw = getattr(result, "timed_out", _MISSING)
    timed_out = None if raw is _MISSING else bool(raw)
    if timed_out:
        return False, "judge timed out (fail-open)", False, timed_out
    if result.returncode != 0:
        return False, "judge exited non-zero (fail-open)", False, timed_out
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return False, "judge returned no output (fail-open)", True, timed_out
    if lines[0].upper().startswith("YES"):
        return True, "", False, timed_out
    if lines[0].upper().startswith("NO"):
        return False, "", False, timed_out
    return False, f"judge answer unparseable: {lines[0]!r} (fail-open)", True, timed_out


def _record_result(
    name: str, result, *, timeout, remaining, ceiling, duration
) -> tuple[bool, str]:
    verdict, reason, malformed, timed_out = _classify(result)
    judge_ledger.decided(
        name, stage="call", verdict=verdict, reason=reason,
        timed_out=timed_out, malformed=malformed, runner_legacy=timed_out is None,
        remaining=remaining, threshold=timeout, ceiling=ceiling, duration=duration,
    )
    return verdict, reason


def _record_raised(
    name: str, *, timeout, remaining, ceiling, duration
) -> tuple[bool, str]:
    reason = "judge raised (fail-open)"
    judge_ledger.decided(
        name, stage="call", verdict=False, reason=reason,
        timed_out=False, malformed=False,
        remaining=remaining, threshold=timeout, ceiling=ceiling, duration=duration,
    )
    return False, reason


def binary_ask_prefilter(final_text: str) -> bool:
    """The deterministic half of judge_binary_ask: does the message END in a
    question mark once a trailing run of formatting decoration is stripped?

    Public because a caller that budgets its judge calls has to know whether a
    call is going to happen BEFORE it spends budget deciding — asking the judge
    function itself would mean the prefilter's verdict is only observable after
    the (possibly skipped) call. judge_binary_ask still applies it itself, so
    the two cannot disagree.
    """
    if not isinstance(final_text, str) or not final_text:
        return False
    stripped = final_text.rstrip(_BINARY_ASK_TRAILING_DECORATION)
    return bool(stripped) and stripped[-1] in _BINARY_ASK_QUESTION_MARKS


def judge_binary_ask(
    final_text: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _BINARY_ASK_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
) -> tuple[bool, str]:
    """Language-independent semantic judge: does ``final_text`` end with a binary /
    confirm question that should have gone through an AskUserQuestion click-gate?

    Replaces a regex confirm-verb lexicon (leaky in every language -- 'Fix it?',
    'Починить заодно?' both missed it) with a model judgment, per CLAUDE.md's
    "separate rule from perception" principle: perception (is this a confirm
    question?) goes to the model; the deterministic part is a language-independent
    punctuation prefilter (the message must END in a question mark from
    _BINARY_ASK_QUESTION_MARKS once a trailing run of formatting decoration --
    markdown emphasis, closing brackets/quotes, whitespace: _BINARY_ASK_TRAILING_
    DECORATION -- is stripped) that keeps the model off every non-question turn.

    Three-valued fail-open contract mirroring acceptance_judge: returns
    (verdict, reason) where ``reason`` is "" for a genuine model verdict (True
    or False) and a non-empty "...(fail-open)" string on every path where the
    False is FABRICATED rather than judged -- disabled/no runner, non-zero
    exit, empty output, an unparseable answer, a timeout (``result.timed_out``
    -- never derived by matching subprocess_runner's own stderr text), or an
    unexpected exception. The guardian this feeds is a Stop-gate BLOCKER, so a
    fabricated False is still the safe failure direction; the reason exists so
    the execution ledger can tell "the judge said no" from "the judge never
    ran".

    ``remaining``/``ceiling`` are forwarded to the ledger only (the budget
    remainder at entry and the caller's own per-call ceiling, alongside
    ``timeout`` as the active threshold) -- this function never uses them to
    decide anything; the caller already resolved ``timeout`` from its own
    budget before calling in."""
    if not enabled:
        return _judge_unavailable(
            "binary_ask", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not binary_ask_prefilter(final_text):
        return False, ""
    if runner is None:
        return _judge_unavailable(
            "binary_ask", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("binary_ask")
    start = time.monotonic()
    try:
        prompt = _BINARY_ASK_PROMPT.format(text=final_text)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        return _record_result(
            "binary_ask", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "binary_ask", duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    finally:
        judge_ledger.set_current_judge(None)


_FEEDBACK_JUDGE_PROMPT = (
    "You are given a user's message to an AI coding assistant, written in any "
    "language. Decide whether this message carries AGENT-BEHAVIOR FEEDBACK -- a "
    "correction of what the assistant did, a stated principle or preference about "
    "how the assistant should work, or a \"you should have / next time do X\" "
    "evaluation of the assistant's own conduct.\n\n"
    "Answer YES only when the message evaluates or directs the ASSISTANT'S "
    "behavior, for example (any language): \"you shouldn't have done that\", "
    "\"next time ask first\", \"don't use regexes for this\", \"ты не так сделал\".\n\n"
    "Answer NO for: a neutral task instruction, an analytical or meta discussion "
    "that merely mentions corrective-sounding words without evaluating the "
    "assistant (e.g. a description of how a hook works, or a review of someone "
    "else's text), or a plain question.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "MESSAGE:\n{text}"
)

_OUTAGE_ESCALATION_JUDGE_PROMPT = (
    "You are given the final message of an AI assistant's turn, written in any "
    "language. Decide whether this message ESCALATES a live, un-diagnosed "
    "external-service failure to the user -- surfacing an outage and asking how "
    "to proceed -- as opposed to text that merely discusses failure handling.\n\n"
    "Answer YES only when the message reports a CURRENT failure the assistant has "
    "not yet diagnosed and is asking the user how to proceed.\n\n"
    "Answer NO for: a meta-description of how failure handling or an escalation "
    "gate works, a past or already-resolved incident, ordinary prose that "
    "mentions error codes or failures without escalating one, or a message that "
    "reports a diagnosed failure with a proposed fix.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "MESSAGE:\n{text}"
)


# LAST-RESORT default for the deferring-disposition judge, used only when a
# caller names no timeout of its own. Superseded numbers, kept as the reason this
# is now computed: 8 s (the neighbours', below the fastest measured run) and then
# 30 s, set from a four-sample note that read "13.9 +/- 2.4 s, min 12.1, max
# 17.5" -- an n of 4 that missed this judge's real tail by more than 20 s
# (n=18: median 17.43, p90 37.58, max 39.99).
# By lib/judge_latency.py::last_resort_ceiling_s, the same rule and the same
# number as _BINARY_ASK_TIMEOUT_S: outside a hook budget the ceiling covers the
# whole model family, not one prompt. The two constants stay SEPARATE names
# because each judge's in-hook ceiling is derived per row, and a shared name here
# would invite a caller to reuse whichever it imported first.
_DEFERRING_DISPOSITION_TIMEOUT_S = 41

_DEFERRING_DISPOSITION_JUDGE_PROMPT = (
    "You are given the question and every option of a menu an AI assistant is "
    "about to show its user, written in any language. Decide whether EVERY "
    "option DEFERS or REFUSES a piece of work the assistant could carry out "
    "right now -- filing a ticket, parking it in a backlog, \"later\", \"as a "
    "separate task\", \"leave as is\", \"don't touch\" -- so that the menu "
    "offers no branch that does the work now.\n\n"
    "Answer YES only when ALL of these hold: the menu is about a concrete piece "
    "of work the assistant has already identified; not one option does that "
    "work now; and nothing in the menu names a reason the work is beyond the "
    "assistant.\n\n"
    "Answer NO when: at least one option does the work now; or the menu names "
    "any stated reason it cannot be done now (missing rights, another owner, a "
    "required waiting period, a pending external result); or the menu is not "
    "about doing work at all (a preference, a language, a wording or scope "
    "choice).\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "MENU:\n{text}"
)


def judge_feedback_signal(
    user_text: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _BINARY_ASK_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
) -> tuple[bool, str]:
    """Semantic judge behind the self-improvement regex prefilter: does
    ``user_text`` carry genuine agent-behavior feedback (a correction, a stated
    principle, a "should have" evaluation), as opposed to a neutral instruction or
    analytical/meta text that merely mentions corrective-sounding words?

    Caller contract: ``user_text`` MUST already be injection-stripped (the same
    text si_feedback_detect.strip_injected_context() produces and the regex
    prefilter matched on) -- never the raw harness-injected buffer, which is
    dense with feedback-shaped language from replayed CLAUDE.md/SKILL.md content
    and would reintroduce the false-positive class this judge exists to remove.

    This function is a PURE model call with no inline prefilter: unlike
    judge_binary_ask's self-contained punctuation check, the regex prefilter here
    (si_feedback_detect.find_signals) lives at the scripts/ root, outside the
    agentctl package -- the caller runs the prefilter and calls this judge only
    when it fires.

    Three-valued fail-open contract, mirroring judge_binary_ask: returns
    (verdict, reason) with reason "" for a genuine verdict and a non-empty
    "...(fail-open)" string for disabled/no-text/no-runner, non-zero exit,
    empty/unparseable output, a timeout (``result.timed_out``), or an
    exception -- the guardian this feeds is a Stop-gate BLOCKER, so a
    fabricated False is still the safe failure direction; ``remaining``/
    ``ceiling`` are forwarded to the ledger only, alongside ``timeout`` as the
    active threshold."""
    if not enabled:
        return _judge_unavailable(
            "feedback_signal", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(user_text, str) or not user_text:
        return _judge_unavailable(
            "feedback_signal", _NO_TEXT_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "feedback_signal", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("feedback_signal")
    start = time.monotonic()
    try:
        prompt = _FEEDBACK_JUDGE_PROMPT.format(text=user_text)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        return _record_result(
            "feedback_signal", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "feedback_signal", duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    finally:
        judge_ledger.set_current_judge(None)


def judge_outage_escalation(
    assistant_text: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _BINARY_ASK_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
) -> tuple[bool, str]:
    """Semantic judge behind the outage-escalation regex prefilter: does
    ``assistant_text`` escalate a live, un-diagnosed external-service failure to
    the user, as opposed to a meta-description of failure handling, a resolved
    incident, or prose that merely mentions error codes?

    This function is a PURE model call with no inline prefilter -- the caller
    (outage_escalation_detect.detect) runs the regex prefilter outside the
    agentctl package and calls this judge only when it fires -- unlike
    judge_binary_ask, whose punctuation check is self-contained and runs here.

    Three-valued fail-open contract, mirroring judge_binary_ask: returns
    (verdict, reason) with reason "" for a genuine verdict and a non-empty
    "...(fail-open)" string for disabled/no-text/no-runner, non-zero exit,
    empty/unparseable output, a timeout (``result.timed_out``), or an
    exception -- both hard-block consumers of this judge (the Stop guardian and
    the PreToolUse gate) treat a fabricated False the same as an honest one, so
    it is still the safe failure direction; ``remaining``/``ceiling`` are
    forwarded to the ledger only, alongside ``timeout`` as the active
    threshold."""
    if not enabled:
        return _judge_unavailable(
            "outage_escalation", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(assistant_text, str) or not assistant_text:
        return _judge_unavailable(
            "outage_escalation", _NO_TEXT_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "outage_escalation", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("outage_escalation")
    start = time.monotonic()
    try:
        prompt = _OUTAGE_ESCALATION_JUDGE_PROMPT.format(text=assistant_text)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        return _record_result(
            "outage_escalation", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "outage_escalation", duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    finally:
        judge_ledger.set_current_judge(None)


def judge_deferring_disposition(
    ask_text: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _DEFERRING_DISPOSITION_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
) -> tuple[bool, str]:
    """Semantic judge behind the deferring-disposition regex prefilter: does this
    AskUserQuestion menu offer the user nothing but branches that postpone or
    refuse work the assistant itself could do now?

    The distinction the model carries is the one no regex can: a menu of
    ticket/backlog/"leave as is" options is DEFECTIVE when the assistant holds
    the rights and the diagnosis, and LEGITIMATE when the work belongs to
    someone else -- the same option vocabulary in both cases.

    Like judge_feedback_signal / judge_outage_escalation this is a PURE model
    call with no inline prefilter: the caller (the hook) runs its regex
    prefilter and calls this judge only when it fires.

    Three-valued fail-open contract, mirroring judge_binary_ask: returns
    (verdict, reason) with reason "" for a genuine verdict and a non-empty
    "...(fail-open)" string for disabled/no-text/no-runner, non-zero exit,
    empty/unparseable output, a timeout (``result.timed_out``), or an
    exception -- the consumer is a PreToolUse deny, so a fabricated False is
    still the safe failure direction; ``remaining``/``ceiling`` are forwarded
    to the ledger only, alongside ``timeout`` as the active threshold."""
    if not enabled:
        return _judge_unavailable(
            "deferring_disposition", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(ask_text, str) or not ask_text:
        return _judge_unavailable(
            "deferring_disposition", _NO_TEXT_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "deferring_disposition", _UNAVAILABLE_REASON,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("deferring_disposition")
    start = time.monotonic()
    try:
        prompt = _DEFERRING_DISPOSITION_JUDGE_PROMPT.format(text=ask_text)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        return _record_result(
            "deferring_disposition", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "deferring_disposition", duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    finally:
        judge_ledger.set_current_judge(None)


def resolve_enabled(weight_class: str | None, *, thresholds: Thresholds | None = None) -> bool:
    """Resolve whether the advisor should run for this call.

    AGENTCTL_ADVISOR overrides in both directions ("1" forces on, "0" forces off,
    regardless of config or weight class). Absent the env override, the advisor is
    on only when config.md's advisor-mode == "substantive" AND the session's
    weight_class == SUBSTANTIVE — auto-activation is scoped to substantive work,
    never chat/small-change. A missing/unreadable advisor-mode key resolves to off
    (fail-open, same default-off posture as the rest of this module).
    """
    env = os.environ.get("AGENTCTL_ADVISOR")
    if env == "1":
        return True
    if env == "0":
        return False
    thr = thresholds if thresholds is not None else Thresholds()
    try:
        mode = thr.advisor_mode
    except KeyError:
        return False
    return mode == _ADVISOR_MODE_SUBSTANTIVE and weight_class == _SUBSTANTIVE_WEIGHT_CLASS


def subprocess_runner(argv: list[str], *, timeout: int = _ADVISOR_TIMEOUT_S) -> RunResult:
    """Real `claude -p` runner with a hard timeout. Not judge()'s default (a caller
    that wants a live advisor pass this explicitly) — kept separate so the fail-open
    `runner=None -> []` contract in judge() stays byte-identical to advisor-absent.

    ``timeout`` still carries a default, and that is the remaining hole: this
    signature is the last place where forgetting to pass a ceiling is silently
    survivable, and the default is the CHEAP ADVISORY model's cap, which fits no
    judge. Every caller in this module now passes one explicitly; making the
    parameter mandatory in the contract is filed as OOSEVENREPORT-5 and is out of
    this change's scope, because the signature is public and third-party runners
    mirror it.

    Every call is mirrored to the judge execution ledger (lib/judge_ledger.py):
    a `started` line before the subprocess call and a `call` line with the
    mechanical facts (duration, timed_out, returncode, raised) after it — this
    is the SOLE place ``RunResult.timed_out`` is ever set, from the actual
    ``subprocess.TimeoutExpired`` branch, never derived by matching this
    function's own stderr literal below. The judge name comes from the ambient
    ``judge_ledger.take_current_judge()`` carrier (set by the calling judge
    function immediately before invoking the injected ``runner``), because this
    function's own signature is frozen and cannot grow a judge-name parameter."""
    judge_name = judge_ledger.take_current_judge()
    if judge_name is None:
        # No judge function claimed this call: an engine-path advisory call from
        # cli.py, not one of the hooks' judges. It gets its own invocation id so
        # that N such calls in one CLI process do not collapse into one, and the
        # name records that attribution is absent rather than guessing a judge.
        judge_name = _UNATTRIBUTED_JUDGE
        judge_ledger.reset_invocation()
    judge_ledger.started(judge_name)
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        duration = time.monotonic() - start
        judge_ledger.call(judge_name, timed_out=False, duration=duration, returncode=proc.returncode)
        return RunResult(proc.returncode, proc.stdout, proc.stderr, timed_out=False)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        judge_ledger.call(judge_name, timed_out=True, duration=duration, returncode=None)
        return RunResult(1, "", f"advisor timed out after {timeout}s", timed_out=True)
    except Exception as exc:
        duration = time.monotonic() - start
        judge_ledger.call(judge_name, timed_out=False, duration=duration, returncode=None, raised=repr(exc))
        raise
