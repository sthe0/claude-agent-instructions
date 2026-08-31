"""Warn-only advisory LLM judge for the semantic cognition residue.

The advisor NEVER sets directive.ok=False, NEVER changes directive.node, and NEVER
blocks a transition. With the advisor returning [] (disabled / errored / stubbed),
control flow is byte-identical to advisor-absent. Fail-open: any exception yields [].
Default-off: only active when AGENTCTL_ADVISOR=1 is set in the environment, OR when
resolve_enabled()'s config-mode + weight-class rule turns it on for a substantive
session (see resolve_enabled).
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import time

from lib import host_llm
from lib import judge_ledger
from lib.runtime_models import HOST_CLAUDE, model_for

from . import premise
from .config import Thresholds
from .dispatch import RunResult
from .text_shape import ELEMENT_NAMES

# Cheap model + hard cap: the advisor auto-activates for every substantive session's
# cognition points, so each call must stay bounded in cost and can never hang a
# coordination step.
_ADVISOR_COMPLEXITY = "medium"
_ADVISOR_MODEL = model_for(HOST_CLAUDE, _ADVISOR_COMPLEXITY)
_ADVISOR_TIMEOUT_S = 20

# The one literal for "the runner hit its timeout": emitted by subprocess_runner and
# read back by classify_runner_failure. Shared rather than restated at each end so the
# classifier cannot drift into silently classifying every timeout as advisor_error.
_TIMEOUT_STDERR_PREFIX = "advisor timed out after"

# The same shared-literal arrangement for "the child had no way to authenticate":
# emitted by subprocess_runner only when a call FAILED and the isolated child held
# neither a borrowed OAuth token nor an environment API key, and read back by
# classify_runner_failure. Written by us, never matched against the CLI's own words,
# because the CLI's not-logged-in phrasing is not a stable contract.
_CREDENTIAL_STDERR_PREFIX = "advisor had no credential to lend the isolated child"

# Written into the sidecar's `stderr` field by enumerate_questions_health when the
# OS raises E2BIG before the judge subprocess can start — the plan's prompt text
# exceeded ARG_MAX in the judge's argv. Read back by classify_runner_failure so a
# caller that only has the stored stderr string can still detect the oversize class.
_E2BIG_STDERR_MARKER = "Argument list too long"

# Whole-plan enumeration (enumerate_claims / enumerate_questions_health) is a
# DIFFERENT cost class from a judge/advisor call: it re-reads an entire plan in one
# shot, and calibration (docs/operations/advisor-timeout-calibration.md, 15 rows =
# 5 sizes x 3 repeats) measured 15-170s of real latency under a 600s measurement
# cap, high enough that no row was truncated by the bound under measurement -- far
# past _ADVISOR_TIMEOUT_S=20, which would truncate nearly every whole-plan call.
# 480 is DERIVED from that dataset rather than being a property of it:
# 480 = ceil_to_minute(largest within-size max/min spread (size
# 23018, 96.513/23.127 = 4.173174x, the refutation check's own number) * the min
# elapsed_s at the largest sampled size (103.213s, size 203681)) =
# ceil_to_minute(430.726) = 480. The spread is quoted here to six figures, not as
# the 4.173x it is displayed as elsewhere, because the product of the ROUNDED
# factors is 430.708 -- close enough to be indistinguishable after ceiling, far
# enough that recomputing from the printed digits reads as an arithmetic error.
# The literal below is that computed value, checked by test against the raw
# committed dataset (advisor-calibration.jsonl) at full precision rather than
# re-derived at runtime, so a calibration-note edit can never silently drift the
# shipped timeout.
_ENUMERATE_TIMEOUT_S_DEFAULT = 480
_ENUMERATE_TIMEOUT_ENV = "AGENTCTL_ENUMERATE_TIMEOUT_S"


def _positive_int_env(name: str, default: int) -> int:
    """Read an integer override off the environment, falling back to `default` on
    anything unusable and saying so on stderr.

    This runs at IMPORT time and `cli` imports this module at module scope, so a bare
    `int(os.environ[...])` makes `AGENTCTL_ENUMERATE_TIMEOUT_S=8m` kill every agentctl
    command with a ValueError traceback that names the variable nowhere. Non-positive
    values are rejected too, in the other direction: `0` is fail-CLOSED (every
    enumeration times out instantly) and so silently converts the fleet to permanent
    escape-taking — a state nobody chose by typing a number."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        print(f"agentctl: ignoring {name}={raw!r} — expected a positive integer "
              f"number of seconds; using {default}", file=sys.stderr)
        return default
    return value


ENUMERATE_TIMEOUT_S = _positive_int_env(_ENUMERATE_TIMEOUT_ENV, _ENUMERATE_TIMEOUT_S_DEFAULT)

# The acceptance judge is a SEPARATE, cheaper tier than the warn-only advisor: it
# gates a real transition (via the pure acceptance-review guardian), so it runs on the
# cheapest model and is fail-open (a missing verdict blocks at the gate, never passes).
_JUDGE_COMPLEXITY = "low"
_JUDGE_MODEL = model_for(HOST_CLAUDE, _JUDGE_COMPLEXITY)
JUDGE_REVIEWER = "judge:haiku"
# Last-resort ceiling for a judge call made outside any hook budget, by the rule
# in lib/judge_latency.py::last_resort_ceiling_s — one second past the slowest
# run this model has been seen to make on ANY judge prompt. Its row in that
# module is UNMEASURED, so this default is the only number available to it; the
# test-suite asserts the literal still equals what that rule computes.
_ACCEPTANCE_JUDGE_TIMEOUT_S = 41
def _prompt_argv(runtime_host: str, complexity: str, prompt: str) -> list[str]:
    model = model_for(runtime_host, complexity)
    return host_llm.build_prompt_argv(runtime_host, model, prompt)

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


def enumerate_subprocess_runner(
    argv: list[str], *, timeout: int = ENUMERATE_TIMEOUT_S
) -> RunResult:
    """subprocess_runner bound to ENUMERATE_TIMEOUT_S -- the default runner for the
    two whole-plan enumeration entry points (enumerate_claims,
    enumerate_questions_health), whose calls run far longer than a judge/advisor
    call and would be truncated by subprocess_runner's own _ADVISOR_TIMEOUT_S=20
    default. subprocess_runner itself stays untouched: every judge_* caller keeps
    the original 20s bound.

    The keyword-only `timeout` mirrors subprocess_runner's own signature so both
    enumeration entry points can name their ceiling AT THE CALL SITE, which is the
    norm trunk settled on. Baking the ceiling into the runner instead would make
    the two call sites read as if they carried the 20s advisory bound, and a
    call-site keyword would then raise TypeError into the bare `except Exception`
    below -- reported as an unhealthy runner rather than as the signature mismatch
    it is. `test_enumerate_runner_signature` pins this.

    Defined here, ahead of enumerate_claims/enumerate_questions_health, only
    because Python evaluates a default-argument value at `def` time -- this name
    must already exist when their `runner=enumerate_subprocess_runner` defaults
    are bound. The `subprocess_runner` call inside the body resolves at CALL time,
    so it is free to reference the module-level function defined later below."""
    return subprocess_runner(argv, timeout=timeout)


def enumerate_claims(artifact_text: str, runner=enumerate_subprocess_runner, *, runtime_host: str = HOST_CLAUDE) -> list[str]:
    """Independent semantic re-reading of an outgoing deliverable that RAISES the
    load-bearing decisions/judgments/claims it detects, one statement per line.

    This is a recall-widener for the coordinator's OWN enumeration, never
    authoritative and never complete — model perception with recall < 100%. The
    deterministic disposition gate (ledger.validate_candidates) is what turns each
    raised item into a blocker; this call only supplies the candidates.

    Cost-bounded like the warn-only advisor, but with ENUMERATE_TIMEOUT_S named at
    the call site rather than _ADVISOR_TIMEOUT_S: this is a whole-artifact read, not
    a binary judge, and 20s truncated it. Fail-open:
    a None runner, a non-zero exit, or any exception returns [] — an empty
    enumeration is a valid (if unhelpful) result; the mandatory-cross-check blocker
    is discharged by the `enumerated` flag the caller sets, not by the count."""
    if runner is None:
        return []
    judge_ledger.begin_attributed_call("enumerate_claims")
    try:
        prompt = _ENUMERATE_PROMPT.format(payload=artifact_text)
        result = runner(
            _prompt_argv(runtime_host, _ADVISOR_COMPLEXITY, prompt),
            timeout=ENUMERATE_TIMEOUT_S,
        )
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []
    finally:
        judge_ledger.set_current_judge(None)


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
    # Derived, never restated. This prompt is what BOUNDS the independent enumerator's
    # reach: a name absent here is a place it is told it may not raise a question
    # against, so a restated copy does not merely rot — it silently narrows the premise
    # gate to the vocabulary of whenever the copy was last edited. It had, and was six
    # names short (knowledge, preconditions, control, order, requirements, procedure) —
    # exactly the places the surrounding work had just introduced.
    "  stage:<n>.<element>   where <n> is a stage index and <element> is one of: "
    + ", ".join(sorted(ELEMENT_NAMES))
    + "\n"
    "No numbering, no bullets, no prose, no preamble. Return nothing if the plan raises "
    "no implicit questions.\n\n{payload}"
)


def enumerate_questions_health(
    goal: str, done_criterion: str, plan_text: str, runner=enumerate_subprocess_runner,
    *, runtime_host: str = HOST_CLAUDE,
) -> tuple[bool | None, list[tuple[str, str]], str]:
    """Independent re-reading of a WHOLE plan that RAISES the questions its
    construction should have provoked, as (target, question) pairs, together with a
    runner-health flag and the runner's captured stderr.

    ONE bounded `claude -p --model sonnet` call over the goal + done-criterion + full
    plan text — deliberately whole-plan, not one call per element: the questions worth
    raising are overwhelmingly cross-element (a stage's method contradicting the goal, a
    done-criterion an invariant can't hold) and per-element calls would both miss those
    and multiply the cost/latency by the element count for no recall gain.

    Fail-open, exactly like enumerate_claims. The returned flag reports whether the
    runner produced a usable answer, so the caller can record runner health and attach a
    non-blocking advisory when the pass was vacuous — WITHOUT ever re-gating on it:

      * runner is None        -> (None, [], "")        advisor absent (disabled/stubbed)
      * non-zero exit          -> (False, [], stderr)   runner reachable but failed
      * exception              -> (False, [], "")       timeout/crash swallowed
      * success (0 exit)       -> (True, pairs, stderr) pairs may still be empty

    Fail-open here means this function RETURNS on a failed run instead of raising — it
    does not mean the failure is forgiven. Those are two different layers and they are
    deliberately split: the mandatory obligation lives in the GATE, so that is where
    refusing belongs. plugins_premise.premise_blockers now blocks approve whenever the
    recorded `enumerated_runner_ok` is False, discharged only by a typed escape
    (`agentctl question-enumerate-escape`) counted against the plan's content digest.
    Two things survive that change unaltered. The pair COUNT still never gates
    discharge: a genuinely question-free plan is a healthy run, and gating on the count
    would wedge approve on a pass that worked. And `None` — the advisor absent — still
    discharges on the flag alone with a non-blocking advisory, since blocking a fleet
    that never had an advisor would refuse approve for a check it cannot run. `stderr`
    is carried so a background caller (the detached enumeration worker) can surface WHY
    a run failed without the caller needing its own capture path — and so the blocker
    can pre-select the escape reason from it."""
    if runner is None:
        return None, [], ""
    judge_ledger.begin_attributed_call("enumerate_questions_health")
    try:
        payload = f"GOAL:\n{goal}\n\nDONE CRITERION:\n{done_criterion}\n\nPLAN:\n{plan_text}"
        prompt = _ENUMERATE_QUESTIONS_PROMPT.format(payload=payload)
        result = runner(
            _prompt_argv(runtime_host, _ADVISOR_COMPLEXITY, prompt),
            timeout=ENUMERATE_TIMEOUT_S,
        )
        if result.returncode != 0:
            return False, [], result.stderr or ""
        pairs: list[tuple[str, str]] = []
        for ln in (result.stdout or "").splitlines():
            if not ln.strip():
                continue
            target, sep, question = ln.partition("\t")
            target, question = target.strip(), question.strip()
            if not sep or not target or not question:
                continue
            pairs.append((target, question))
        return True, pairs, result.stderr or ""
    except OSError as exc:
        # E2BIG: the judge subprocess argv (which carries the prompt text) exceeded
        # ARG_MAX. Preserve the error in stderr so the sidecar's classify_runner_failure
        # can surface ESCAPE_ADVISOR_OVERSIZE instead of the generic ESCAPE_ADVISOR_ERROR.
        if exc.errno == errno.E2BIG:
            return False, [], f"{_E2BIG_STDERR_MARKER}: {exc}"
        return False, [], ""
    except Exception:
        return False, [], ""
    finally:
        judge_ledger.set_current_judge(None)


def enumerate_questions(
    goal: str, done_criterion: str, plan_text: str, runner,
    *, runtime_host: str = HOST_CLAUDE,
) -> list[tuple[str, str]]:
    """Thin wrapper over enumerate_questions_health returning only the (target,
    question) pairs — the recall-widener surface, symmetric with enumerate_claims. A
    caller that also needs to record runner health calls the _health variant directly."""
    return enumerate_questions_health(goal, done_criterion, plan_text, runner, runtime_host=runtime_host)[1]


def judge(kind: str, payload: dict, runner, *, enabled: bool | None = None, runtime_host: str = HOST_CLAUDE) -> list[str]:
    """Return advisory strings for the given cognition point, or [] if disabled/failed.

    Warn-only: callers MUST NOT branch on the return value for control flow.
    Fail-open: runner=None, non-zero exit, or any exception returns [].
    """
    if enabled is None:
        enabled = os.environ.get("AGENTCTL_ADVISOR") == "1"
    if not enabled or runner is None:
        return []
    judge_ledger.begin_attributed_call("judge")
    try:
        template = _PROMPTS.get(kind)
        if not template:
            return []
        prompt = template.format(payload=payload)
        result = runner(
            _prompt_argv(runtime_host, _ADVISOR_COMPLEXITY, prompt),
            timeout=_ADVISOR_TIMEOUT_S,
        )
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []
    finally:
        judge_ledger.set_current_judge(None)


def acceptance_judge(
    observation: str,
    expected: str,
    runner,
    *,
    enabled: bool,
    runtime_host: str = HOST_CLAUDE,
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
    judge_ledger.begin_attributed_call("acceptance_judge")
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
            _prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout
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
    finally:
        judge_ledger.set_current_judge(None)


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

_INVARIANTS_JUDGE_PROMPT = (
    "You are given an INVARIANT and a PLAN TEXT. Decide whether the invariant is "
    "semantically preserved by the plan text — even if the exact wording differs.\n\n"
    "INVARIANT:\n{invariant}\n\n"
    "PLAN TEXT:\n{plan_text}\n\n"
    "Answer YES if the invariant's intent is covered by the plan text (preserved, "
    "even as a paraphrase or distributed across multiple places). Answer NO if the "
    "invariant is absent or contradicted. Be strict on genuine absence; lenient on "
    "paraphrase.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else."
)


_MISSING = object()

_KILLSWITCH_REASON = "judge disabled (fail-open)"
_NO_RUNNER_REASON = "judge given no runner (fail-open)"
_NO_TEXT_REASON = "judge given no text (fail-open)"

_UNATTRIBUTED_JUDGE = "unattributed"


def _judge_unavailable(
    name: str, reason: str, *, stage: str, timeout, remaining, ceiling
) -> tuple[bool, str]:
    """Terminal outcome for a decision point reached with no call possible.
    Shared by all four judge functions, which differ here only in their own
    name and which of the three no-call reasons applies:

    ``stage="killswitch"`` — disabled by the hook's own kill switch env var.
    ``stage="no_text"``    — no text was given to judge (before the runner
                              check, since a missing runner is moot if there
                              is nothing to send it).
    ``stage="no_runner"``  — enabled, text present, but no runner injected.

    Kept distinct rather than one shared "disabled" stage so a reader of the
    ledger can tell "the operator turned this off" apart from "the caller
    forgot to give it text" apart from "the runner was never wired up" —
    three different fixes, one indistinguishable free-text reason before."""
    judge_ledger.decided(
        name, stage=stage, verdict=False, reason=reason,
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
    fabricating False would put an unknown into the ledger as a fact. Every
    ``runner`` actually wired in production is this module's own
    ``subprocess_runner``, which always sets the field (see its own
    docstring) — so this branch is unreachable from any real call site
    today. It stays because ``runner`` is an injected parameter, not a fixed
    call: a test double, or a future runner this module does not control,
    can still omit the field, and recording a fabricated ``False`` for it
    would be the exact ledger-fidelity bug this module exists to avoid.

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
        timed_out=None, malformed=False,
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
    runtime_host: str = HOST_CLAUDE,
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
            "binary_ask", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not binary_ask_prefilter(final_text):
        return False, ""
    if runner is None:
        return _judge_unavailable(
            "binary_ask", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("binary_ask")
    start = time.monotonic()
    try:
        prompt = _BINARY_ASK_PROMPT.format(text=final_text)
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
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
    runtime_host: str = HOST_CLAUDE,
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
            "feedback_signal", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(user_text, str) or not user_text:
        return _judge_unavailable(
            "feedback_signal", _NO_TEXT_REASON, stage="no_text",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "feedback_signal", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("feedback_signal")
    start = time.monotonic()
    try:
        prompt = _FEEDBACK_JUDGE_PROMPT.format(text=user_text)
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
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
    runtime_host: str = HOST_CLAUDE,
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
            "outage_escalation", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(assistant_text, str) or not assistant_text:
        return _judge_unavailable(
            "outage_escalation", _NO_TEXT_REASON, stage="no_text",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "outage_escalation", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("outage_escalation")
    start = time.monotonic()
    try:
        prompt = _OUTAGE_ESCALATION_JUDGE_PROMPT.format(text=assistant_text)
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
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
    runtime_host: str = HOST_CLAUDE,
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
            "deferring_disposition", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(ask_text, str) or not ask_text:
        return _judge_unavailable(
            "deferring_disposition", _NO_TEXT_REASON, stage="no_text",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "deferring_disposition", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("deferring_disposition")
    start = time.monotonic()
    try:
        prompt = _DEFERRING_DISPOSITION_JUDGE_PROMPT.format(text=ask_text)
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
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


# LAST-RESORT default for judge_landing_discipline_ask, used only when a caller
# names no timeout of its own. By lib/judge_latency.py::last_resort_ceiling_s,
# the same rule and (today) the same number as _BINARY_ASK_TIMEOUT_S /
# _DEFERRING_DISPOSITION_TIMEOUT_S — outside a hook budget the ceiling covers
# the whole model family, not one prompt. Named distinctly from those two
# (rather than reusing either) for the same reason _DEFERRING_DISPOSITION_
# TIMEOUT_S is not shared with _BINARY_ASK_TIMEOUT_S even though both are 41
# today: each judge's in-hook ceiling is derived per its own measured row, and
# a shared name here would invite a caller to reuse whichever it imported
# first. Deliberately NOT named `_LANDING_DISCIPLINE_TIMEOUT_S` — that name is
# reserved for hook-resolution-reminder.py's own per-call budget constant
# (derived from judge_latency.call_ceiling_s('landing_discipline') with
# headroom, a different number from this family-wide last resort), so the two
# constants in the two files never collide or get mistaken for each other.
_LANDING_DISCIPLINE_LAST_RESORT_TIMEOUT_S = 41

_LANDING_DISCIPLINE_JUDGE_PROMPT = (
    "You are given the question and every option of an AskUserQuestion menu an "
    "AI coding assistant is about to show its user at a task's resolution gate, "
    "written in any language. This repo requires every resolved change to land "
    "by direct push or fast-forward merge into trunk/main -- there is no "
    "distinct human reviewer who gates it, so a pull-request / merge-review "
    "delivery path is never the correct default here. Decide whether the "
    "menu's own content PROPOSES a pull-request / merge-review delivery path "
    "-- an option or wording that offers to open a PR, wait for review, or "
    "land only after a review completes.\n\n"
    "Answer YES only when at least one option or the question's own wording "
    "proposes opening a pull request, waiting for a review, or landing via a "
    "review-gated path.\n\n"
    "Answer NO when every option proposes direct push / fast-forward into "
    "trunk, or the menu does not concern a delivery/landing mechanism at "
    "all.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "MENU:\n{text}"
)


def judge_landing_discipline_ask(
    ask_text: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _LANDING_DISCIPLINE_LAST_RESORT_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
    runtime_host: str = HOST_CLAUDE,
) -> tuple[bool, str]:
    """Semantic judge behind hook-resolution-reminder.py's PreToolUse landing-
    discipline check: does this AskUserQuestion menu's own content propose a
    pull-request / merge-review delivery path, when this repo requires direct
    push/fast-forward into trunk with no distinct human reviewer?

    Unlike judge_deferring_disposition and its neighbours, the caller runs NO
    regex/content-based prefilter ahead of this judge -- every invocation of
    the hint at an open resolution gate consults the judge directly (an
    arbitrary-content regex is a fragile classification mechanism even when
    demoted to a filter rather than the decision-maker, and must not gate
    consultation of the semantic judge either). The deterministic half that
    DOES gate this judge lives entirely in the caller: whether the resolution
    gate is open and whether direct_push_no_pr_hint applies to the delivery
    repo.

    Three-valued fail-open contract mirroring judge_deferring_disposition:
    returns (verdict, reason) with reason "" for a genuine model verdict and a
    non-empty "...(fail-open)" string wherever the False is FABRICATED --
    disabled/no-text/no-runner, non-zero exit, empty/unparseable output, a
    timeout (``result.timed_out``), or an exception. The consumer is a
    PreToolUse deny, so a fabricated False is still the safe failure
    direction; ``remaining``/``ceiling`` are forwarded to the ledger only,
    alongside ``timeout`` as the active threshold."""
    if not enabled:
        return _judge_unavailable(
            "landing_discipline", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not isinstance(ask_text, str) or not ask_text:
        return _judge_unavailable(
            "landing_discipline", _NO_TEXT_REASON, stage="no_text",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if runner is None:
        return _judge_unavailable(
            "landing_discipline", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("landing_discipline")
    start = time.monotonic()
    try:
        prompt = _LANDING_DISCIPLINE_JUDGE_PROMPT.format(text=ask_text)
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
        return _record_result(
            "landing_discipline", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "landing_discipline", duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    finally:
        judge_ledger.set_current_judge(None)


# LAST-RESORT ceiling, by lib/judge_latency.py::last_resort_ceiling_s — the same
# number and the same rule as _BINARY_ASK_TIMEOUT_S, for the same reason as
# _ACCEPTANCE_JUDGE_TIMEOUT_S: this judge runs inside `agentctl question-raise`,
# outside every hook, so no harness budget narrows it and none of the per-row
# in-hook ceilings apply. Its own latency row is UNMEASURED and says so.
_QUESTION_MATERIALITY_TIMEOUT_S = 41

_QUESTION_MATERIALITY_PROMPT = (
    "A plan carries CONTROLS -- the checks that decide whether its stages passed. "
    "Someone raised a QUESTION during that plan's construction and named the "
    "control they believe its answer bears on. Decide whether answering the "
    "question one way rather than another could actually CHANGE that control's "
    "verdict.\n\n"
    "Answer YES when a different answer plausibly changes what the control "
    "checks, what it would accept, or whether it passes at all.\n\n"
    "Answer NO when the question is about something the control does not decide "
    "-- a different part of the plan, background context, a matter of style or "
    "wording, or a detail the control would pass or fail on identically either "
    "way.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "CONTROL: {control}\n"
    "WHAT THE CONTROL SAYS: {control_text}\n"
    "QUESTION: {question}"
)


def question_materiality_prefilter(control: str, question: str) -> bool:
    """The deterministic half: a control was named AND there is a question to weigh
    it against. Whether the NAME resolves against the plan is the caller's own
    check and is not repeated here -- the engine refuses an unresolvable name at
    the write seam, so this judge is only ever reached for a resolved one.

    Public for the same reason binary_ask_prefilter is: a caller has to know
    whether a call will happen before it decides to make one."""
    return bool(isinstance(control, str) and control.strip()
                and isinstance(question, str) and question.strip())


def judge_question_materiality(
    control: str,
    question: str,
    runner,
    *,
    control_text: str = "",
    enabled: bool = True,
    timeout: int = _QUESTION_MATERIALITY_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
    runtime_host: str = HOST_CLAUDE,
) -> tuple[bool, str]:
    """Advisory judge behind the question-materiality check: could this question's
    answer really flip the verdict of the control it names?

    The split this implements is the whole point of the check. Whether the named
    control EXISTS in this plan is decidable from the plan document, so the engine
    decides it (agentctl.controls) and refuses at the write seam. Whether the
    answer would MOVE it is not decidable from any document, so it comes here --
    and the caller surfaces the verdict without ever blocking on it.

    Three-valued fail-open contract, mirroring judge_binary_ask: reason is "" for
    a genuine model verdict and a non-empty "...(fail-open)" string wherever the
    False is FABRICATED. Here the distinction is load-bearing in the OTHER
    direction from its neighbours: their consumers block, so a fabricated False is
    the safe direction and the reason is only for the ledger. This consumer
    surfaces a judged False as "the plan says this question cannot move that
    control" -- a claim a fail-open False has no standing to make -- so the caller
    must surface nothing at all unless the reason is empty."""
    if not enabled:
        return _judge_unavailable(
            "question_materiality", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not question_materiality_prefilter(control, question):
        return False, ""
    if runner is None:
        return _judge_unavailable(
            "question_materiality", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("question_materiality")
    start = time.monotonic()
    try:
        prompt = _QUESTION_MATERIALITY_PROMPT.format(
            control=control, control_text=control_text or "(not rendered)",
            question=question,
        )
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
        return _record_result(
            "question_materiality", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "question_materiality", duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    finally:
        judge_ledger.set_current_judge(None)


_APPROVAL_ASK_TIMEOUT_S = 41

_APPROVAL_ASK_PROMPT = (
    "You are given every user-facing string of an AskUserQuestion an AI coding "
    "assistant is about to show its user, written in any language. Decide "
    "whether this ask is asking the user to APPROVE A PLAN -- the formal "
    "approve/reject decision on a plan of work already presented -- as "
    "opposed to any other kind of question.\n\n"
    "Answer YES only when the ask's substance is approving, rejecting, or "
    "confirming a plan that has been presented (e.g. \"Approve this plan?\", "
    "\"Go ahead with the plan above?\", \"Одобряем план?\"), including when it "
    "also offers to show the full plan text.\n\n"
    "Answer NO for: any other confirm/binary/menu question, a scope or "
    "wording choice, a request for a value, or an ask that does not concern "
    "approving a plan at all.\n\n"
    "Answer on the FIRST line with exactly YES or NO, nothing else.\n\n"
    "ASK:\n{text}"
)


def approval_ask_prefilter(ask_text: str) -> bool:
    """The deterministic half: is there any ask text at all to judge? Mirrors
    question_materiality_prefilter's bar -- genuinely empty input cannot be the
    approval ask, so this is a GENUINE False, not a fail-open one.

    Public for the same reason binary_ask_prefilter is: a caller has to know
    whether a call is going to happen before it decides to make one."""
    return isinstance(ask_text, str) and bool(ask_text.strip())


def judge_approval_ask(
    ask_text: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _APPROVAL_ASK_TIMEOUT_S,
    remaining: float | None = None,
    ceiling: float | None = None,
    runtime_host: str = HOST_CLAUDE,
) -> tuple[bool, str]:
    """Semantic judge behind hook-plan-delivery-gate.py's scope classifier: is
    this AskUserQuestion the plan-approval ask -- the one the receipt/
    freshness/delivery/marker checks must apply to -- as opposed to any other
    ask fired at the PLAN_READY node?

    Self-contained prefilter, like judge_binary_ask / judge_question_materiality:
    the caller passes the ask's own flattened text (lib.ask_text.flat_text) and
    this function decides for itself whether there is anything to send the
    model.

    Three-valued fail-open contract mirroring judge_binary_ask: reason is "" for
    a genuine model verdict and a non-empty "...(fail-open)" string wherever the
    False is FABRICATED -- disabled/no runner, non-zero exit, empty/unparseable
    output, a timeout (``result.timed_out``), or an exception. The consumer
    (hook-plan-delivery-gate.py) is fail-open in the direction that WIDENS what
    is allowed through, never the direction that certifies a delivery: a
    fabricated False only ever skips the strict checks, and the caller stamps a
    delivery receipt on none of those skipped paths. ``remaining``/``ceiling``
    are forwarded to the ledger only, alongside ``timeout`` as the active
    threshold."""
    if not enabled:
        return _judge_unavailable(
            "approval_ask", _KILLSWITCH_REASON, stage="killswitch",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    if not approval_ask_prefilter(ask_text):
        return False, ""
    if runner is None:
        return _judge_unavailable(
            "approval_ask", _NO_RUNNER_REASON, stage="no_runner",
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    judge_ledger.set_current_judge("approval_ask")
    start = time.monotonic()
    try:
        prompt = _APPROVAL_ASK_PROMPT.format(text=ask_text)
        result = runner(_prompt_argv(runtime_host, _JUDGE_COMPLEXITY, prompt), timeout=timeout)
        return _record_result(
            "approval_ask", result, duration=time.monotonic() - start,
            timeout=timeout, remaining=remaining, ceiling=ceiling,
        )
    except Exception:
        return _record_raised(
            "approval_ask", duration=time.monotonic() - start,
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


def _child_was_authenticated(run_kwargs: dict) -> bool:
    """Did the isolated child hold an auth source? Read from the status the
    sandbox seam stamped into the very env the child ran with, so this cannot
    drift from what was actually handed over. An unknown status reads as
    authenticated: this predicate only ever ADDS a failure label, and mislabelling
    a working machine is the worse error."""
    env = run_kwargs.get("env") or {}
    status = env.get(host_llm.JUDGE_TOKEN_STATUS_ENV_VAR)
    if status is None:
        return True
    return status in host_llm.AUTHENTICATED_TOKEN_STATUSES


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
    function's own signature is frozen and cannot grow a judge-name parameter.

    The subprocess itself runs under ``host_llm.isolated_run_kwargs()`` (cwd +
    CLAUDE_CONFIG_DIR pinned to an empty sandbox, rest of the environment
    preserved) — see that function's docstring for why an unisolated judge call
    can recurse into the fleet's own hooks.

    That isolation can itself remove the child's credential (the client resolves
    auth at CLAUDE_CONFIG_DIR), so when a call fails AND the child held no auth
    source at all, its stderr is prefixed with a marker that
    ``classify_runner_failure`` maps to its own escape reason. Only then: a
    machine authenticated by a plain environment API key must never be labelled
    a credential failure, and an authenticated call that fails for any other
    reason keeps its own classification."""
    judge_name = judge_ledger.take_current_judge()
    if judge_name is None:
        # Every caller in this module now self-identifies before invoking the
        # injected runner (the four hook judges, and enumerate_claims/
        # enumerate_questions_health/judge/acceptance_judge on the engine
        # path), so reaching here means a caller OUTSIDE this module invoked
        # the runner directly without going through any of them — a genuinely
        # unattributed call. It gets its own invocation id so that N such
        # calls in one CLI process do not collapse into one, and the name
        # records that attribution is absent rather than guessing a judge.
        judge_name = _UNATTRIBUTED_JUDGE
        judge_ledger.reset_invocation_outside_hook()
    judge_ledger.started(judge_name)
    start = time.monotonic()
    try:
        run_kwargs = host_llm.isolated_run_kwargs()
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, **run_kwargs,
        )
        duration = time.monotonic() - start
        judge_ledger.call(judge_name, timed_out=False, duration=duration, returncode=proc.returncode)
        stderr = proc.stderr
        if proc.returncode != 0 and not _child_was_authenticated(run_kwargs):
            stderr = f"{_CREDENTIAL_STDERR_PREFIX}\n{stderr}"
        return RunResult(proc.returncode, proc.stdout, stderr, timed_out=False)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        judge_ledger.call(judge_name, timed_out=True, duration=duration, returncode=None)
        return RunResult(1, "", f"{_TIMEOUT_STDERR_PREFIX} {timeout}s", timed_out=True)
    except Exception as exc:
        duration = time.monotonic() - start
        judge_ledger.call(judge_name, timed_out=False, duration=duration, returncode=None, raised=repr(exc))
        raise


def classify_runner_failure(stderr: str, *, exc: Exception | None = None) -> str:
    """Map a failed enumeration run's stderr onto the escape reason the ENGINE
    pre-selects, so the human confirms a value rather than typing one the engine
    already knows.

    Five-valued on purpose, up from four. A timeout is the one failure whose
    stderr this process itself wrote (subprocess_runner's TimeoutExpired arm), so
    it is the one this function can recognise with certainty. A missing credential
    is the same kind of certainty from the other end: subprocess_runner writes
    that prefix only when the isolated child held no auth source at all, which is
    a failure THIS seam can itself cause and the only one whose fix is local — so
    it must not collapse into the quota reason, which classifies the SERVICE's
    refusal of an authenticated call and is fixed by waiting. A quota/session-
    limit refusal is the third: its stderr shape is stable (observed verbatim:
    "You've hit your session limit · resets 12am (Europe/Moscow)") and,
    unlike a generic failure, names a resource ceiling rather than a broken
    runner — worth its own reason so a fleet-wide quota exhaustion shows up as
    its own bucket instead of vanishing into the generic-error tally. An oversize
    (E2BIG) failure is the fourth: the plan's prompt text exceeded ARG_MAX in the
    judge subprocess's argv before it could even read stdin; the fix is splitting
    the plan, not retrying the runner or waiting for quota. It is recognised from
    either the exception directly (caller supplies `exc`) or from _E2BIG_STDERR_MARKER
    in the stored stderr (the round-trip path via the sidecar file). Everything
    else — an ordinary non-zero exit, an unparseable reply, an absent advisor
    binary, no stderr at all — is a heterogeneous tail whose members would each
    need a fragile substring rule for no gain, since the escape they take is the
    same. So advisor_error remains the catch-all, including for empty stderr, and
    the operator's --note carries the detail the reason token deliberately does not.

    `exc` is the live exception object for call sites that have it (e.g. an OSError
    raised directly in the worker and not yet serialised to a sidecar); the bare
    `stderr` string suffices for the fold path that reads back a stored sidecar."""
    if exc is not None and isinstance(exc, OSError) and exc.errno == errno.E2BIG:
        return premise.ESCAPE_ADVISOR_OVERSIZE
    text = stderr or ""
    if _TIMEOUT_STDERR_PREFIX in text:
        return premise.ESCAPE_ADVISOR_TIMEOUT
    if _CREDENTIAL_STDERR_PREFIX in text:
        return premise.ESCAPE_ADVISOR_CREDENTIAL
    if "session limit" in text.lower():
        return premise.ESCAPE_ADVISOR_QUOTA
    if _E2BIG_STDERR_MARKER in text:
        return premise.ESCAPE_ADVISOR_OVERSIZE
    return premise.ESCAPE_ADVISOR_ERROR
