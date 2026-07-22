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
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from outage_escalation_detect import detect as _detect_outage  # noqa: E402
from agentctl import advisor  # noqa: E402

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


def _ask_text(tool_input: dict) -> str:
    """Concatenate every user-facing string in an AskUserQuestion payload: each
    question's text/header plus every option's label and description. Tolerant of
    missing keys and schema drift — an absent field contributes nothing."""
    if not isinstance(tool_input, dict):
        return ""
    parts: list[str] = []
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return ""
    for q in questions:
        if not isinstance(q, dict):
            continue
        for key in ("question", "header"):
            val = q.get(key)
            if isinstance(val, str):
                parts.append(val)
        options = q.get("options")
        if isinstance(options, list):
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                for key in ("label", "description"):
                    val = opt.get(key)
                    if isinstance(val, str):
                        parts.append(val)
    return "\n".join(parts)


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
    (allow) without ever invoking the judge when it doesn't fire."""
    if payload.get("tool_name") != "AskUserQuestion":
        return None
    tool_input = payload.get("tool_input") or {}
    text = _ask_text(tool_input)
    if not _detect_outage(text):
        return None  # cheap common path: nothing to gate
    fires = advisor.judge_outage_escalation(
        text,
        runner,
        enabled=os.environ.get(_OUTAGE_ESCALATION_KILLSWITCH_ENV) != "0",
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
