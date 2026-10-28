#!/usr/bin/env python3
"""PostToolUse hook (matcher: AskUserQuestion): append the user's answer to the session's
answer ledger (agentctl/answer_ledger.py).

Difficulty removed: `agentctl order-agree` and `order-challenge-dismiss` act for the user, so they
need proof that the user chose a marked option of an ask posed after the engine event. The proof
must be readable without a transcript and unwritable through any agentctl verb; this hook is its
only producer. It records the offered option labels with the answers so the engine judges the
choice with `ask_evidence.chose_marked_option`, the content-waiver gate's own rule.

Nothing is written without a state file: a harness session that drives no engine session leaves
no trace. The harness session may drive several engine sessions; each gets the row, as the
delivery hook decides each candidate in turn.

Fail-open: any error is swallowed and the hook exits 0, so it can never block a tool call.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _offered_questions(tool_input: dict) -> list[dict]:
    questions = []
    for q in tool_input.get("questions") or []:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str):
            continue
        options = [
            {"label": o.get("label"), "description": o.get("description")}
            for o in q.get("options") or []
            if isinstance(o, dict) and isinstance(o.get("label"), str)
        ]
        questions.append({"question": q["question"], "options": options})
    return questions


def record(payload: dict, *, now: float | None = None) -> int:
    from agentctl import answer_ledger
    from lib import config_root, engine_session_binding
    from lib.ask_evidence import parse_ask_answers

    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    answers = parse_ask_answers(payload.get("tool_response"))
    if not answers:
        return 0
    row = {
        "at": time.time() if now is None else now,
        "questions": _offered_questions(tool_input),
        "answers": answers,
    }
    harness = payload.get("session_id") or ""
    try:
        candidates = engine_session_binding.driven_sessions(harness)
    except Exception:
        candidates = []
    written = 0
    for session_id in candidates or [harness]:
        if not session_id:
            continue
        state_file = config_root.resolve_agentctl_state_file(session_id)
        if state_file is None:
            continue
        answer_ledger.append(state_file, row)
        written += 1
    return written


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record(payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
