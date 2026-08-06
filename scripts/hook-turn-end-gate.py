#!/usr/bin/env python3
"""Stop hook: the end-of-turn GATE — one loop-safe shell, N pure guardians.

Difficulty removed: an advisory `UserPromptSubmit` reminder merely prints a nudge,
which the model can (and repeatedly did) ignore while absorbed in a technical
sub-thread — so a turn that carried an unmet obligation ends silently. This Stop
hook turns those advisories into a structural turn-boundary check.

Architecture (mirrors agentctl/gates.py one level up):

  - An IMPURE shell does all I/O: read the stdin payload, read the transcript,
    stat/write the durable marker, load the agentctl session state. It freezes
    every fact a decision needs into a frozen `TurnContext`.
  - `TURN_GUARDIANS` maps a guardian name to a PURE `(TurnContext) -> list[str]`
    of blocker strings ([] == this obligation is met).

A TURN guardian is pure in the STRONG sense — no subprocess, no network, and no
file I/O — which is affordable precisely because `TurnContext` exists. This is
deliberately stricter than agentctl/gates.py, whose "pure" means only "no
subprocess" (plan_review_blockers legitimately reads the plan file to rebind its
sha256). The purity is enforced BEHAVIORALLY by the test suite, not by grep: a
guardian that delegates its I/O one call deep passes a source search and fails
the test.

Contract (Claude Code Stop hook):
  - stdin: JSON payload with `transcript_path`, `stop_hook_active`, `session_id`.
  - `stop_hook_active` True → exit 0 immediately (loop guard: never block twice
    inside the same stop cycle).
  - On a block: print {"decision":"block","reason":"..."} on stdout, exit 0.
  - Otherwise: exit 0 with no output.

Bounding the cost (never wedge the session). The three loop-safety properties
live HERE, in the shell, exactly once — not once per obligation:

  1. The `stop_hook_active` guard caps a single stop cycle to one extra turn.
  2. A durable per-message marker (hash of session + triggering user text under
     <agent-home>/state/turn-gate/) makes the SAME triggering message block at
     most once, even across stop cycles where `stop_hook_active` is not set. It
     keys on the MESSAGE ALONE and deliberately not on the set of guardians that
     fired: keying on the fired set would cap blocks at one per subset, i.e. up
     to 2**N-1 for a single message, and would degrade on exactly the path the
     marker exists to cover.
  3. Fail-open everywhere: any malformed / empty / unreadable input, or any
     unexpected error, results in exit 0 with no block. A guardian that raises
     contributes no blocker rather than wedging the session.

Because the marker keys on the message alone, blockers from ALL fired guardians
are aggregated into ONE block whose reason numbers every unmet obligation. N
obligations therefore cost exactly ONE extra model turn.

THE TRADE, stated rather than hidden: aggregation buys turn-boundedness by giving
up per-obligation enforcement. If the model addresses only obligation 1 of the 2
named in the block, the next stop is allowed and obligation 2 goes unenforced for
that message. This is accepted because the blocker text names every unmet
obligation in one place, and because a Stop hook that can block a session more
than once per message has unbounded blast radius across every session on this
machine. The bounded-at-N alternative (one marker per guardian) is the named
follow-up.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Make the sibling shared detector and lib/ importable whether this hook is run
# directly (scripts/ on sys.path[0]) or loaded via importlib in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from si_feedback_detect import find_signals, strip_injected_context  # noqa: E402
from long_job_detect import detect as _detect_long_job  # noqa: E402
from outage_escalation_detect import detect as _detect_outage  # noqa: E402
import transcript_read  # noqa: E402
from timer_arm_detect import (  # noqa: E402
    closure_sought as _closure_sought,
    waiter_armed as _waiter_armed,
    iter_bash_commands as _iter_bash_commands,
)
from agentctl import advisor  # noqa: E402
from agentctl.advisor import judge_binary_ask  # noqa: E402
from lib import judge_budget  # noqa: E402

# Whole-invocation deadline covering ALL judge calls this hook makes, and the
# registration that must accommodate it (install-reminder-hooks.sh: 35s = this
# budget plus interpreter-start headroom). Larger than the single-judge gates'
# 20s because this one invocation runs up to THREE judges; before it existed the
# hook was registered at 5s and called every judge with advisor's 8s default,
# against a MEASURED judge latency of 10.5 / 11.5 / 12.2 / 12.6 / 13.7 / 14.2 /
# 15.4 / 47.0s — so every verdict was computed after the hook was already dead,
# or never computed at all.
_TURN_JUDGE_BUDGET_S = 30
# Below this the remainder cannot plausibly fit a call (fastest measured run:
# 10.5s), so the tail is skipped rather than spent on a guaranteed timeout.
_TURN_JUDGE_MIN_CALL_S = 12
# Ceiling on any ONE call, so a single slow judge (the 47s outlier) cannot eat
# the whole budget and starve the two behind it. Comfortably above the ~12.9s
# median, so it binds only on the tail.
_TURN_JUDGE_CALL_CAP_S = 20

try:
    from lib import config_root  # noqa: E402
except Exception:  # pragma: no cover - fail-open if the resolver is unavailable
    config_root = None

try:
    import self_diagnose_store as _sd_store  # noqa: E402
except Exception:  # pragma: no cover - fail-open if the store is unavailable
    _sd_store = None

# Kill-switches for the semantic judges: set to "0" to force a given judge off
# without a code change. Direction is safe-by-default: an unset or
# unrecognised value leaves the detector ENABLED, so a forgotten wire never
# silently disables the obligation.
_BINARY_ASK_KILLSWITCH_ENV = "CLAUDE_BINARY_ASK_SEMANTIC"
_SI_FEEDBACK_KILLSWITCH_ENV = "CLAUDE_SI_FEEDBACK_SEMANTIC"
_OUTAGE_ESCALATION_KILLSWITCH_ENV = "CLAUDE_OUTAGE_ESCALATION_SEMANTIC"

# Skills whose invocation this turn satisfies the self-improvement discipline.
_SATISFYING_SKILLS = frozenset({"self-improvement", "overcome-difficulty"})


# ---------------------------------------------------------------------------
# The frozen fact bundle every guardian reads (built by the impure shell)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TurnContext:
    """Every fact a guardian may need, frozen by the shell so no guardian does I/O.

    last_user_text : the human-authored text of the message that opened this turn.
    invocations    : every tool name, `Skill` id and `subagent_type` the assistant
                     invoked after that message.
    transcript_path: the transcript this turn was read from.
    session_key    : dedup identity of the conversation (harness session_id when
                     present, else the transcript path).
    agentctl_state : the engine's SessionState for this session, or None when no
                     session is readable. Read once, here, by the shell.
    closure_sought : whether this turn already seeks closure — it emitted an
                     inline AskUserQuestion OR armed a deferral timer. Computed by
                     the shell (transcript I/O) via the shared timer_arm_detect
                     detector, so the resolution guardian stays pure.
    long_job_launched : whether this turn ran a Bash command that looks like a
                     detached / orchestrator long-job launch (shared long_job_detect
                     scan over the turn's Bash commands). Computed by the shell.
    autowake_armed : whether this turn armed a harness-tracked auto-wake (a
                     `Bash(run_in_background:true)` waiter or `CronCreate`). Computed
                     by the shell via the shared waiter_armed predicate.
    outage_escalation_sought : whether this turn's assistant text surfaces an
                     external-service failure to the user without a recorded
                     diagnosis (shared outage_escalation_detect prefilter over
                     the concatenated assistant text AND
                     agentctl.advisor.judge_outage_escalation semantic model
                     verdict). Computed by the shell.
    difficulty_declared : whether the engine's SessionState carries a declared
                     difficulty (`state.difficulty.declaration` set). Read once,
                     here, by the shell from agentctl_state.
    self_improvement_feedback : whether the user's message carried genuine
                     agent-behavior feedback (shared si_feedback_detect.find_signals
                     prefilter over the injection-stripped user text AND
                     agentctl.advisor.judge_feedback_signal semantic model
                     verdict). Computed by the shell.
    prose_binary_ask : whether this turn's assistant text ENDS with a binary /
                     confirm question posed in prose instead of via an
                     AskUserQuestion click-gate (language-independent punctuation
                     prefilter + agentctl.advisor.judge_binary_ask semantic model
                     verdict over the concatenated assistant text). Computed by
                     the shell.
    self_diagnose_findings : one pre-formatted line per distinct OPEN, ACTIONABLE
                     self-diagnose CONDITION older than the store's debounce
                     (rows differing only in directory collapse into one line),
                     or empty when this session has already been blocked on them.
                     The store read (and the per-session marker stat) happen in
                     the shell; the guardian only reads the frozen tuple.
    judges_skipped : names of the judges whose prefilter FIRED but whose call
                     was dropped because the invocation's shared judge budget
                     could no longer fit one. No guardian reads it — it exists
                     so that "the judge never ran" is a recorded fact rather
                     than indistinguishable from "the judge said no", which is
                     precisely the confusion this hook was found living in.
    """

    last_user_text: str
    invocations: frozenset[str]
    transcript_path: str
    session_key: str
    agentctl_state: Any | None
    closure_sought: bool = False
    long_job_launched: bool = False
    autowake_armed: bool = False
    outage_escalation_sought: bool = False
    difficulty_declared: bool = False
    self_improvement_feedback: bool = False
    prose_binary_ask: bool = False
    self_diagnose_findings: tuple[str, ...] = ()
    judges_skipped: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Guardians — PURE: (TurnContext) -> list[str]. No subprocess, network, file I/O.
# ---------------------------------------------------------------------------
def self_improvement_blockers(ctx: TurnContext) -> list[str]:
    """The user's message carried agent-behavior feedback (regex prefilter AND
    semantic model judge, both resolved by the shell into self_improvement_feedback),
    but neither the self-improvement nor the overcome-difficulty skill was engaged
    this turn. Pure: reads only the frozen ctx booleans / the invocations set."""
    if not ctx.self_improvement_feedback:
        return []
    if ctx.invocations & _SATISFYING_SKILLS:
        return []
    return [
        "The user's message carried an agent-behavior-feedback signal, but "
        "neither the self-improvement nor the overcome-difficulty skill was "
        "engaged this turn. Invoke the self-improvement skill now, or "
        "explicitly state in your reply why it does not apply."
    ]


def escalation_without_diagnosis_blockers(ctx: TurnContext) -> list[str]:
    """This turn surfaced an external-service failure to the user (a present-tense
    outage cue plus a user-facing escalation frame) without a recorded diagnosis.
    The PreToolUse hook-escalation-diagnosis-gate.py denies the same shape at
    AskUserQuestion time; this is the Stop backstop for a TEXT escalation that
    never went through an ask.

    Fires only under the full conjunction, all read from the frozen context:
      - outage_escalation_sought (the shell's outage_escalation_detect scan over
        this turn's assistant text fired);
      - overcome-difficulty was NOT invoked this turn; AND
      - no declared difficulty exists (difficulty_declared).

    Pure: reads only frozen ctx booleans / the invocations set.
    """
    if not ctx.outage_escalation_sought:
        return []
    if "overcome-difficulty" in ctx.invocations:
        return []
    if ctx.difficulty_declared:
        return []
    return [
        "This turn surfaced an external-service failure to the user without a "
        "recorded overcome-difficulty declare. Before ending the turn, reproduce "
        "it with the real client and run overcome-difficulty (>=2 hypotheses, each "
        "with a cheap falsifier); do not leave an unverified outage claim standing."
    ]


def prose_binary_ask_blockers(ctx: TurnContext) -> list[str]:
    """This turn ends with a binary / confirm decision posed to the user in PROSE
    (a trailing "записать?", "публикуем v11?", "should I push?", "считаем
    решённой?") instead of through an AskUserQuestion click-gate. CLAUDE.md
    § Escalation mandates AskUserQuestion for every confirmation / defined-set
    choice so the user clicks instead of typing; the recurring lapse is to type
    the confirm as prose. No ask tool is called at all here, so only this Stop
    text-scan can catch it.

    Fires ONLY under the full conjunction, all read from the frozen context:
      - prose_binary_ask (the shell's judge_binary_ask semantic scan over this
        turn's assistant text fired: the final utterance is a confirm question,
        not open-ended or rhetorical);
      - no AskUserQuestion was invoked this turn (the click-gate the norm wants);
      - the turn is not already seeking closure (ctx.closure_sought) — a turn
        that already posed the ask inline must not be nagged.

    Pure: reads only frozen ctx booleans / the invocations set.
    """
    if not ctx.prose_binary_ask:
        return []
    if "AskUserQuestion" in ctx.invocations:
        return []
    if ctx.closure_sought:
        return []
    return [
        "This turn ends with a binary / confirm decision posed to the user in "
        "prose instead of via AskUserQuestion. CLAUDE.md § Escalation mandates "
        "AskUserQuestion for every confirmation / defined-set choice (apply/skip, "
        "push, scope, resolution) so the user clicks instead of typing. Re-pose "
        "the decision as an AskUserQuestion with the recommended option first — "
        "it may share this turn with any preceding text and tool calls. If the "
        "question is genuinely open-ended (a free-text name/path/sentence), state "
        "in your reply why AskUserQuestion does not apply."
    ]


def self_diagnose_findings_blockers(ctx: TurnContext) -> list[str]:
    """Open ACTIONABLE self-diagnose findings are still open at this turn boundary.

    The SessionStart scan used to print its worklist to stderr, where it scrolled
    away unread — the same broken hook registration every session for four days.
    The findings now persist in a keyed store, and this guardian is what makes
    them re-surface until each is closed.

    Every gate that keeps this from becoming noise is applied by the shell before
    the tuple is frozen: only kinds in the store's explicit ACTIONABLE table (never
    `ceiling-proximity`, which already has its own debounced channel), only rows
    older than ACTIONABLE_MIN_AGE_DAYS, only rows still `open` after ack/snooze
    expiry, and at most once per session. Pure: reads only the frozen tuple.
    """
    if not ctx.self_diagnose_findings:
        return []
    listing = "\n".join(f"  - {f}" for f in ctx.self_diagnose_findings)
    return [
        "Open self-diagnose findings are still unclosed — each has been detected "
        "on every scan and nothing has acted on it:\n"
        f"{listing}\n"
        "Fix each one (the remediation is named above), or dispose of it "
        "explicitly: `python3 scripts/self_diagnose_store.py --ack <key> --reason "
        "<why>` / `--snooze <key> --days N`."
    ]


def resolution_turn_blockers(ctx: TurnContext) -> list[str]:
    """A substantive plan whose every stage has PASSED sits at the resolution gate
    with the gate still open, yet this turn shows no sign closure is being sought
    (no AskUserQuestion, no armed deferral timer). The engine drives the closing
    sequence but cannot make the model actually seek confirmation; this turns that
    advisory into a turn-boundary check.

    Fires ONLY under the full conjunction, all read from the frozen context:
      - a readable agentctl SessionState exists (absent -> fail open, never fire);
      - weight_class is SUBSTANTIVE (CHAT / SMALL_CHANGE never resolve this way);
      - a plan is submitted AND every stage's outcome is PASSED
        (all_stages_passed(): False for an empty plan or any unpassed stage);
      - the resolution gate has NOT passed;
      - the turn is not already seeking closure (ctx.closure_sought).

    Named LAST in TURN_GUARDIANS so, when it co-fires with self_improvement, the
    aggregated block numbers the resolution obligation last.
    """
    state = ctx.agentctl_state
    if state is None:
        return []
    # weight_class is persisted as the plain WeightClass value string.
    if getattr(state, "weight_class", None) != "SUBSTANTIVE":
        return []
    all_passed = getattr(state, "all_stages_passed", None)
    if not callable(all_passed) or not all_passed():
        return []
    resolution = getattr(state, "resolution", None)
    if resolution is None or getattr(resolution, "passed", False):
        return []
    if ctx.closure_sought:
        return []
    return [
        "Every stage of this substantive plan has PASSED and the resolution gate "
        "is still open, but this turn is not seeking closure. Run "
        "`agentctl verify-final`, then confirm the task is resolved with the user "
        "via an AskUserQuestion — it may share this turn with the recap text. Do "
        "not close the task until the user confirms; on any verification failure, "
        "run overcome-difficulty instead."
    ]


def long_job_autowake_blockers(ctx: TurnContext) -> list[str]:
    """This turn launched a detached long-running external job but armed no
    harness-tracked auto-wake, so the main thread would idle silently until the
    user pings. The PreToolUse advisory `hook-long-job-arm.py` merely nudges at
    launch time; this turns the deterministically-decidable part — a long-job
    launch this turn WITHOUT a harness-tracked waiter — into a turn-boundary check.

    Fires only under the conjunction, both read from the frozen context:
      - a long-job launch was detected in this turn's Bash commands;
      - no auto-wake was armed this turn (`waiter_armed`: no
        `Bash(run_in_background:true)`, no `CronCreate`).

    A detached poller alone does NOT satisfy it (it logs transitions but never
    re-invokes the main thread), and neither does `ScheduleWakeup` (it no-ops
    outside /loop). Pure: reads only frozen ctx booleans.
    """
    if not (ctx.long_job_launched and not ctx.autowake_armed):
        return []
    return [
        "This turn launched a detached long-running external job but armed no "
        "harness-tracked auto-wake, so the main thread will idle silently until the "
        "user pings. Arm a harness-tracked `Bash(run_in_background:true)` waiter "
        "that BLOCKS on the job (waits on its PID / polls to a terminal marker) so "
        "the harness auto-wakes you when it exits — or a `CronCreate` peek. A "
        "detached poller alone does NOT auto-wake (it only logs transitions), and "
        "`ScheduleWakeup` no-ops outside /loop."
    ]


# guardian name -> pure predicate. Order matters: format_reason numbers blockers
# in iteration order, and the resolution obligation is named LAST.
TURN_GUARDIANS: dict[str, Callable[[TurnContext], list[str]]] = {
    "self_improvement": self_improvement_blockers,
    "escalation_without_diagnosis": escalation_without_diagnosis_blockers,
    "long_job_autowake": long_job_autowake_blockers,
    "prose_binary_ask": prose_binary_ask_blockers,
    "self_diagnose_findings": self_diagnose_findings_blockers,
    "resolution": resolution_turn_blockers,
}


def collect_blockers(ctx: TurnContext) -> list[str]:
    """Run every guardian. A guardian that raises contributes no blocker — an
    obligation is never worth a wedged turn boundary."""
    out: list[str] = []
    for guardian in TURN_GUARDIANS.values():
        try:
            out.extend(guardian(ctx) or [])
        except Exception:
            continue
    return out


def format_reason(blockers: list[str]) -> str:
    """Aggregate every unmet obligation into one reason, so N obligations cost
    exactly one extra model turn."""
    if len(blockers) == 1:
        return blockers[0]
    lines = ["Unmet turn-boundary obligations — address each before ending the turn:"]
    lines.extend(f"{i}. {b}" for i, b in enumerate(blockers, 1))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The impure shell: all I/O lives below this line
# ---------------------------------------------------------------------------
def _state_dir() -> Path:
    """Durable marker dir: <agent-home>/state/turn-gate/ (created on demand)."""
    if config_root is not None:
        try:
            return config_root.hook_state_dir("turn-gate")
        except Exception:
            pass
    return Path.home() / ".claude-agent" / "state" / "turn-gate"


# Transcript parsing is shared with the sibling Stop hooks — see
# transcript_read.py for why it lives in an importable module. `_user_text` is
# role-agnostic on purpose: the same `type=="text"` extraction is what the
# human's prompt and this turn's assistant prose both need.
_iter_transcript = transcript_read.iter_transcript
_user_text = transcript_read.message_text


def _invocations_in(msg: dict) -> set[str]:
    """Every tool name / skill id / subagent_type invoked by an assistant message."""
    out: set[str] = set()
    for item in transcript_read.tool_use_blocks(msg):
        tool_input = item.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        for value in (item.get("name"), tool_input.get("skill"), tool_input.get("subagent_type")):
            if isinstance(value, str) and value:
                out.add(value)
    return out


def analyze(entries: list[dict]) -> tuple[str, set[str], list[dict]]:
    """Return (last_user_text, invocations_after_it, turn_entries) for the current
    turn.

    The current turn is delimited by the last user entry that carries human
    text; assistant messages after it are this turn's response. `turn_entries` is
    that response slice, handed to the shared closure detector by the shell.
    """
    last_user_idx = -1
    last_user_text = ""
    for i, entry in enumerate(entries):
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _user_text(msg)
        if text.strip():
            last_user_idx = i
            last_user_text = text

    if last_user_idx < 0:
        return "", set(), []

    turn_entries = entries[last_user_idx + 1:]
    invocations: set[str] = set()
    for entry in turn_entries:
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        invocations |= _invocations_in(msg)
    return last_user_text, invocations, turn_entries


def _assistant_text_of(turn_entries: list[dict]) -> str:
    """Concatenate the human-facing text blocks of this turn's assistant messages
    (mirrors how analyze() walks turn_entries). Fed to the outage-escalation
    detector by the shell so the guardian stays pure."""
    parts: list[str] = []
    for entry in turn_entries:
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        text = _user_text(msg)  # same type=="text" extraction works for either role
        if text:
            parts.append(text)
    return "\n".join(parts)


def _difficulty_declared(state) -> bool:
    """True iff the SessionState carries a declared difficulty (a Difficulty whose
    `.declaration` is set) — mirrors gates.difficulty_blockers' `d = state.difficulty;
    d.declaration` access. False for None state or an empty/undeclared difficulty."""
    if state is None:
        return False
    difficulty = getattr(state, "difficulty", None)
    if difficulty is None:
        return False
    return getattr(difficulty, "declaration", None) is not None


def _session_marker_path(session_key: str) -> Path:
    """Per-SESSION marker for the self-diagnose guardian.

    The generic marker keys on (session, triggering message), which caps a block
    at one per MESSAGE — but a standing finding is present all session, so that
    would fire on every message. This second, session-scoped marker is what makes
    it once per session instead."""
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    return _state_dir() / f"selfdiag-{digest}"


def _open_self_diagnose_findings(session_key: str) -> tuple[str, ...]:
    """The open actionable findings this session has not yet been blocked on.

    All the store I/O the guardian must not do. Fail-open in every direction: no
    store module, an unreadable or corrupt store, or any unexpected error yields
    an empty tuple and therefore no blocker."""
    if _sd_store is None:
        return ()
    try:
        if _session_marker_path(session_key).exists():
            return ()
        rows = _sd_store.load_rows()
        return tuple(_sd_store.describe_rows(_sd_store.open_actionable(rows)))
    except Exception:
        return ()


def _load_agentctl_state(session_id: str | None):
    """Best-effort read of the engine's SessionState. None on any failure.

    Imported lazily: agentctl.store computes its DEFAULT_ROOT at module import,
    so importing it at hook-import time would freeze a root captured before the
    environment (CLAUDE_AGENT_HOME) is in its final state.
    """
    if not session_id:
        return None
    try:
        from agentctl.store import FileStateStore

        return FileStateStore().load(session_id)
    except Exception:
        return None


def build_context(
    payload: dict, *, runner: Callable | None = None, budget: "judge_budget.JudgeBudget | None" = None
) -> TurnContext | None:
    """Freeze this turn's facts, or None when the turn cannot be read (fail-open).

    ``runner`` is injected straight into the agentctl.advisor judges (None ->
    each judge fails open to False, exactly like advisor absent). The real
    invocation point (main()) passes advisor.subprocess_runner; tests inject a
    fake runner or omit it entirely to keep the suite free of live model calls.

    ``budget`` defaults to a fresh whole-invocation JudgeBudget opened right
    here, at the top, BEFORE the transcript is read — not after, as it used to
    be. The registered timeout (35s) already covers interpreter start, transcript
    parsing and the guardian run on top of the judge budget itself (30s), and a
    deadline that opens only after parsing is done quietly spends part of that
    5s headroom without the budget ever knowing. main() passes its own budget
    through instead of relying on this default, so the deadline covers the same
    span the harness actually times.

    The three judges are evaluated SEQUENTIALLY against one _TURN_JUDGE_BUDGET_S
    deadline, not as three eager arguments to the TurnContext constructor. As
    constructor arguments they had no short circuit at all: Python evaluates
    every one before the object exists, so three calls of unbounded length ran
    on every turn that tripped their prefilters, and the harness killed the hook
    somewhere in the middle. Sequential evaluation lets the budget drop the TAIL
    instead — and the drop is RECORDED (`judges_skipped`) rather than swallowed,
    because a judge that silently never ran is exactly the invisible failure
    this whole change is about."""
    if budget is None:
        budget = judge_budget.JudgeBudget(
            _TURN_JUDGE_BUDGET_S, _TURN_JUDGE_MIN_CALL_S, clock=time.monotonic
        )

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return None

    try:
        entries = list(_iter_transcript(path))
    except OSError:
        return None
    if not entries:
        return None

    last_user_text, invocations, turn_entries = analyze(entries)
    if not last_user_text:
        return None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = None
    session_key = session_id or transcript_path
    agentctl_state = _load_agentctl_state(session_id)
    assistant_text = _assistant_text_of(turn_entries)

    skipped: list[str] = []

    def _judged(name: str, prefilter_fires: bool, call: Callable[[float], bool]) -> bool:
        """One budgeted judge call. Returns the verdict, or False (fail open,
        the direction every judge here already fails in) when the prefilter did
        not fire or the budget can no longer fit a call."""
        if not prefilter_fires:
            return False
        call_timeout = budget.next_call_timeout(_TURN_JUDGE_CALL_CAP_S)
        if call_timeout is None:
            skipped.append(name)
            return False
        return call(call_timeout)

    # Order is a priority order, since the budget drops from the tail. feedback
    # first: its guardian blocks the turn on an obligation the user just raised
    # and nothing else re-raises it. binary_ask second. outage_escalation last —
    # it is the only one of the three with a PreToolUse gate of its own
    # (hook-escalation-diagnosis-gate.py), so losing it here costs a backstop
    # rather than the sole guard.
    self_improvement_feedback = _judged(
        "feedback_signal",
        bool(find_signals(last_user_text)),
        lambda t: advisor.judge_feedback_signal(
            strip_injected_context(last_user_text),
            runner,
            enabled=os.environ.get(_SI_FEEDBACK_KILLSWITCH_ENV) != "0",
            timeout=t,
        ),
    )
    prose_binary_ask = _judged(
        "binary_ask",
        advisor.binary_ask_prefilter(assistant_text),
        lambda t: judge_binary_ask(
            assistant_text,
            runner,
            enabled=os.environ.get(_BINARY_ASK_KILLSWITCH_ENV) != "0",
            timeout=t,
        ),
    )
    outage_escalation_sought = _judged(
        "outage_escalation",
        bool(_detect_outage(assistant_text)),
        lambda t: advisor.judge_outage_escalation(
            assistant_text,
            runner,
            enabled=os.environ.get(_OUTAGE_ESCALATION_KILLSWITCH_ENV) != "0",
            timeout=t,
        ),
    )

    if skipped:
        # stderr, not stdout: stdout carries this hook's JSON directive to the
        # harness and a stray line there would corrupt the contract. stderr
        # reaches the human's terminal, which is where an "enforcement quietly
        # degraded this turn" note belongs.
        print(
            "hook-turn-end-gate: judge budget exhausted, skipped: "
            + ", ".join(skipped),
            file=sys.stderr,
        )

    return TurnContext(
        last_user_text=last_user_text,
        invocations=frozenset(invocations),
        transcript_path=transcript_path,
        session_key=session_key,
        agentctl_state=agentctl_state,
        closure_sought=_closure_sought(turn_entries),
        long_job_launched=any(
            _detect_long_job(c) for c in _iter_bash_commands(turn_entries)
        ),
        autowake_armed=_waiter_armed(turn_entries),
        outage_escalation_sought=outage_escalation_sought,
        difficulty_declared=_difficulty_declared(agentctl_state),
        self_improvement_feedback=self_improvement_feedback,
        prose_binary_ask=prose_binary_ask,
        self_diagnose_findings=_open_self_diagnose_findings(session_key),
        judges_skipped=tuple(skipped),
    )


def _marker_path(session_key: str, user_text: str) -> Path:
    digest = hashlib.sha256(
        (session_key + "\0" + user_text).encode("utf-8")
    ).hexdigest()[:32]
    return _state_dir() / digest


def decide(
    payload: dict, *, runner: Callable | None = None, budget: "judge_budget.JudgeBudget | None" = None
) -> dict | None:
    """Core decision. Returns a block-directive dict, or None to allow.

    ``runner`` and ``budget`` are threaded straight through to build_context();
    see its docstring for what each means and defaults to."""
    if payload.get("stop_hook_active"):
        return None

    # Turn-end obligations (self-improvement discipline, resolution-seeking) are
    # ROOT-coordinator obligations; a spawned specialist's turn-end contract is to
    # emit its return marker. Inert in a specialist session (spawn-specialist.py
    # exports AGENT_RECURSION_DEPTH>=1), else a brief that merely mentions
    # "self-improvement" hijacks the marker into MALFORMED.
    try:
        if int(os.environ.get("AGENT_RECURSION_DEPTH", "0") or 0) >= 1:
            return None
    except ValueError:
        pass

    ctx = build_context(payload, runner=runner, budget=budget)
    if ctx is None:
        return None

    blockers = collect_blockers(ctx)
    # No obligation unmet: write no marker, so a guardian that would fire on a
    # later stop of the same message still gets its chance.
    if not blockers:
        return None

    # Block-once dedup, keyed on (session, triggering user text) — never on the
    # set of guardians that fired.
    marker = _marker_path(ctx.session_key, ctx.last_user_text)
    try:
        if marker.exists():
            return None
    except OSError:
        pass  # cannot stat — rely on stop_hook_active guard, still block once
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass  # best-effort; stop_hook_active still caps the loop

    # A standing self-diagnose finding is present all session, so the per-message
    # marker above would let it fire on every message. Consume its session-scoped
    # marker here, once the block that carries it is actually issued.
    if ctx.self_diagnose_findings:
        try:
            session_marker = _session_marker_path(ctx.session_key)
            session_marker.parent.mkdir(parents=True, exist_ok=True)
            session_marker.touch()
        except OSError:
            pass

    return {"decision": "block", "reason": format_reason(blockers)}


def main() -> int:
    # Opened first, before the stdin payload is even read, so the deadline
    # honestly covers transcript parsing too — not only the judge calls.
    budget = judge_budget.JudgeBudget(
        _TURN_JUDGE_BUDGET_S, _TURN_JUDGE_MIN_CALL_S, clock=time.monotonic
    )
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        directive = decide(payload, runner=advisor.subprocess_runner, budget=budget)
    except Exception:
        return 0  # fail-open — a hook must never wedge the session
    if directive is not None:
        print(json.dumps(directive, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
