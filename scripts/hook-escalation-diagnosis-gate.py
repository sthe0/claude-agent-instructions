#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny an escalation of an external-
service failure to the user that has NOT been through a recorded diagnosis.

Difficulty removed: the coordinator, hitting an apparent external-service outage
(a bare probe returns 504 / "unreachable"), sometimes fires an AskUserQuestion
straight at the user ("сервис лежит — к кому за доступом?") — or launders the
unverified premise into a sub-agent question — WITHOUT first reproducing the
failure with the REAL client and enumerating ≥2 hypotheses. A bare probe is not a
diagnosis; the premise is often false (stale snapshot, wrong client, transient),
and a sub-agent asked about it will circularly confirm it. This gate is the
PRE-EMPTIVE PRIMARY guard: it denies the ask BEFORE it renders. The Stop-hook
guardian escalation_without_diagnosis is the backstop for TEXT escalations that
never reach an AskUserQuestion.

DENY when ALL hold:
  1. outage_escalation_detect.detect(question + every option label/description)
     fires (present-tense external-failure cue AND user-facing escalation frame)
     — a high-recall PREFILTER — AND agentctl.advisor.judge_outage_escalation
     (a fail-open semantic model judge) confirms it is a genuine escalation, not
     a paraphrase/meta-mention that merely trips the regex;
  2. the overcome-difficulty skill was NOT invoked anywhere in this session's
     transcript; AND
  3. no active agentctl `declare` record exists for the session (a declared
     difficulty whose `.declaration` is set).

Precision-first: a false DENY is more disruptive than a false Stop-nudge, so the
conjunction is strict and every observable failure FAILS OPEN (allow) — a missing
transcript, unreadable state, a disabled/errored judge, or any unexpected error
never wedges the ask. Always exits 0.

DENY uses the same PreToolUse permissionDecision JSON contract as
hook-plan-delivery-gate.py.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from outage_escalation_detect import detect as _detect_outage  # noqa: E402
from agentctl import advisor  # noqa: E402
from lib import ask_text  # noqa: E402
from lib import judge_budget  # noqa: E402

# Whole-invocation deadline for the judge call, and the registration that must
# accommodate it (install-reminder-hooks.sh: 35s = this budget plus interpreter-
# start headroom, the same shape the deferring-disposition gate already uses).
# Before this existed the hook was registered at 5s and called the judge with
# advisor's 8s default, both under the outage judge's own FASTEST measured run —
# so the harness killed the hook before any verdict could come back, on every
# call. The height itself is a judgement (how much of a turn may a gate spend);
# what is machine-checked against lib/judge_latency.py is that it clears this
# judge's per-call ceiling `ceil(max) + 1` = 27s over n=16, so the budget can
# never be what truncates the call. With exactly one call there is no later call
# to protect, so this number is ALSO the ceiling handed to it: capping the only
# call lower would forfeit budget for nothing.
_JUDGE_BUDGET_S = 30
# Below this the remaining budget cannot plausibly fit a call, so spending the
# wait on a guaranteed timeout buys nothing: stop and fail open, the same posture
# as every other unreachable-judge path. lib/judge_latency.py's floor rule for
# this judge, `ceil(p90)` over n=16 (p90 19.16) — well above the fastest run
# observed (7.19s), which is what makes a call started at the floor reachable.
_JUDGE_MIN_CALL_S = 20

# Kill-switch for the semantic outage-escalation judge: set to "0" to force it
# off without a code change. Safe-by-default: unset/unrecognised leaves the
# judge ENABLED. Shared name with hook-turn-end-gate.py's Stop-hook backstop —
# both gate the same underlying escalation-without-diagnosis obligation.
_OUTAGE_ESCALATION_KILLSWITCH_ENV = "CLAUDE_OUTAGE_ESCALATION_SEMANTIC"

_DENY_REASON = (
    "You are escalating an external-service failure to the user without a recorded "
    "diagnosis. Reproduce the failure with the REAL client and enumerate >=2 "
    "hypotheses (each with a <=3-call falsifier) via the overcome-difficulty skill, "
    "then re-ask. A bare probe is not a diagnosis."
)


# Byte-identical alias to lib.ask_text.flat_text — kept under this name so the
# existing unit tests (_mod._ask_text) need no change; shared with
# hook-deferring-disposition-gate.py (lib/ask_text.py).
_ask_text = ask_text.flat_text


def _overcome_difficulty_invoked(transcript_path: str | None) -> bool:
    """True iff any assistant tool_use in the session transcript invoked the
    overcome-difficulty skill (as a Skill call, tool name, or subagent_type).
    Fail-safe False on any read error so the OTHER conditions still guard."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    path = Path(transcript_path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    for line in lines:
        line = line.strip()
        if "overcome-difficulty" not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            tool_input = item.get("input")
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            for value in (
                item.get("name"),
                tool_input.get("skill"),
                tool_input.get("subagent_type"),
            ):
                if value == "overcome-difficulty":
                    return True
    return False


def _difficulty_declared(session_id: str | None) -> bool:
    """True iff the engine's SessionState carries a declared difficulty (a
    Difficulty whose `.declaration` is set — mirrors gates.difficulty_blockers'
    `d = state.difficulty; d.declaration` access). Fail-safe False on any error
    (no session, unreadable state) so the gate falls back to the other guards.

    Lazy-import agentctl.store for the same reason hook-turn-end-gate does: the
    store computes its DEFAULT_ROOT at import time, so importing it before the
    environment is settled would freeze a stale root."""
    if not session_id:
        return False
    try:
        from agentctl.store import FileStateStore

        state = FileStateStore().load(session_id)
    except Exception:
        return False
    if state is None:
        return False
    difficulty = getattr(state, "difficulty", None)
    if difficulty is None:
        return False
    return getattr(difficulty, "declaration", None) is not None


def gate_decision(
    fires: bool, overcome_invoked: bool, difficulty_declared: bool
) -> tuple[str, str]:
    """Pure decision. Deny only when the escalation fires AND neither an
    overcome-difficulty invocation nor a declared difficulty is present."""
    if fires and not overcome_invoked and not difficulty_declared:
        return "deny", _DENY_REASON
    return "allow", ""


def deny_with(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def decide(payload: dict, *, runner: Callable | None = None) -> str | None:
    """Core decision. Returns the deny reason string, or None to allow.

    ``runner`` is injected straight into agentctl.advisor.judge_outage_escalation
    (None -> that judge fails open to False, never denies) — the same contract
    build_context follows in hook-turn-end-gate.py. The prefilter
    (outage_escalation_detect.detect) runs first and short-circuits to None
    (allow) without ever invoking the judge when it doesn't fire.

    A _JUDGE_BUDGET_S deadline bounds the whole invocation. With a single judge
    call that degenerates into a per-call ceiling, which is the point: the call
    gets an EXPLICIT timeout drawn from the budget instead of advisor's
    last-resort default, which is sized for a call with no harness timeout above
    it and so is wider than this hook's registration allows."""
    if payload.get("tool_name") != "AskUserQuestion":
        return None
    tool_input = payload.get("tool_input") or {}
    text = _ask_text(tool_input)
    if not _detect_outage(text):
        return None  # cheap common path: nothing to gate
    # Opened here, after the payload's own parsing above — same reasoning as
    # hook-deferring-disposition-gate.py: no file I/O precedes this point, so
    # nothing above is worth docking from the judge budget.
    budget = judge_budget.JudgeBudget(
        _JUDGE_BUDGET_S, _JUDGE_MIN_CALL_S, clock=time.monotonic
    )
    call_timeout = budget.next_call_timeout(_JUDGE_BUDGET_S)
    if call_timeout is None:
        return None  # budget exhausted — fail open, as on every unreachable judge
    fires = advisor.judge_outage_escalation(
        text,
        runner,
        enabled=os.environ.get(_OUTAGE_ESCALATION_KILLSWITCH_ENV) != "0",
        timeout=call_timeout,
    )
    if not fires:
        return None
    transcript_path = payload.get("transcript_path")
    session_id = payload.get("session_id") or ""
    decision, reason = gate_decision(
        fires,
        _overcome_difficulty_invoked(transcript_path),
        _difficulty_declared(session_id),
    )
    return reason if decision == "deny" else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        reason = decide(payload, runner=advisor.subprocess_runner)
    except Exception:
        return 0  # fail-open — a hook must never wedge the ask
    if reason is not None:
        deny_with(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
