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
    with the timeout carried by the runner (advisor.subprocess_runner). Fail-open:
    a None runner, a non-zero exit, or any exception returns [] — an empty
    enumeration is a valid (if unhelpful) result; the mandatory-cross-check blocker
    is discharged by the `enumerated` flag the caller sets, not by the count."""
    if runner is None:
        return []
    try:
        prompt = _ENUMERATE_PROMPT.format(payload=artifact_text)
        result = runner(["claude", "-p", "--model", _ADVISOR_MODEL, prompt])
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
        result = runner(["claude", "-p", "--model", _ADVISOR_MODEL, prompt])
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
        result = runner(["claude", "-p", "--model", _ADVISOR_MODEL, prompt])
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def acceptance_judge(observation: str, expected: str, runner, *, enabled: bool) -> tuple[str | None, str]:
    """Cheap external judge for an acceptance observation, backing the acceptance-review
    gate. Returns (verdict, reason) where verdict is 'pass' | 'revise' | None.

    Fail-OPEN: a disabled judge, a None runner, a non-zero exit, an unparseable answer,
    or any exception returns (None, <reason>) — NEVER a false 'pass'. The caller records
    a StageReview only for a non-None verdict, and the PURE gate fails CLOSED on the
    resulting absence, so an unavailable judge stalls the pass safely.

    The prompt is lifted from _PROMPTS['acceptance_observation'] (the same criterion the
    warn-only advisor applies) and wrapped with a strict YES/NO + one-line-reason
    protocol so the deterministic gate has a machine-decidable verdict rather than a
    free-text concern list."""
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
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt])
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

# Interactive end-of-turn call: bounded well under _ADVISOR_TIMEOUT_S=20 so a
# fail-open timeout tail stays short on an ordinary turn.
_BINARY_ASK_TIMEOUT_S = 8

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


def judge_binary_ask(
    final_text: str, runner, *, enabled: bool = True, timeout: int = _BINARY_ASK_TIMEOUT_S
) -> bool:
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

    Fail-open, mirroring judge()/acceptance_judge(): disabled, no runner, a
    non-zero exit, an empty/unparseable answer, or any exception all return False
    -- the guardian this feeds is a Stop-gate BLOCKER, so a confident False (never
    a fabricated True) is the safe failure direction."""
    if not enabled or not isinstance(final_text, str) or not final_text:
        return False
    stripped = final_text.rstrip(_BINARY_ASK_TRAILING_DECORATION)
    if not stripped or stripped[-1] not in _BINARY_ASK_QUESTION_MARKS:
        return False
    if runner is None:
        return False
    try:
        prompt = _BINARY_ASK_PROMPT.format(text=final_text)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        if result.returncode != 0:
            return False
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return False
        return lines[0].upper().startswith("YES")
    except Exception:
        return False


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
    `runner=None -> []` contract in judge() stays byte-identical to advisor-absent."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return RunResult(1, "", f"advisor timed out after {timeout}s")
