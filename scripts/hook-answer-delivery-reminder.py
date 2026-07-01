#!/usr/bin/env python3
"""PostToolUse hook (matcher: AskUserQuestion): remind on question timeout.

Difficulty removed: an answer written mid-turn (before tool calls) may never
render to the user — only the turn's final message is guaranteed delivery.
When an AskUserQuestion times out (user AFK) and the agent continues
autonomously, any answer content written before the question silently
disappears and the user's open question dies with the timeout
(CLAUDE.md § Escalation — "An unanswered user question survives the turn").

This hook fires on every AskUserQuestion result and nudges only when the
result is the harness timeout message. Non-blocking: exit 0 always.
"""
from __future__ import annotations

import json
import sys

TIMEOUT_MARKER = "No response after"


def _response_text(payload: dict) -> str:
    resp = payload.get("tool_response")
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return json.dumps(resp)
    if isinstance(resp, list):
        return json.dumps(resp)
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    if TIMEOUT_MARKER not in _response_text(payload):
        return 0

    print(
        "hook-answer-delivery-reminder: this AskUserQuestion timed out (user AFK).\n"
        "  rule: CLAUDE.md § Escalation — an unanswered user question survives the turn.\n"
        "  action: your turn's FINAL message must (1) restate any answer content you\n"
        "          wrote earlier this turn (mid-turn text may never have rendered),\n"
        "          and (2) restate the open question so the user can answer it on\n"
        "          return. Do not let the question die with the timeout.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
