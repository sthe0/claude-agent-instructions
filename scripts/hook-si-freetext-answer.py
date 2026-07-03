#!/usr/bin/env python3
"""PostToolUse hook (matcher: AskUserQuestion): nudge self-improvement when
the user answered in free text rather than picking an offered option.

Difficulty removed: a correction delivered as an AskUserQuestion answer
never reaches the UserPromptSubmit self-improvement reminder — that hook
only sees the next literal user prompt, and a free-text AskUserQuestion
answer is a trigger channel the reminder does not listen on (live failure
2026-07-02: a correction — "Почему снова по-английски? Напиши по-русски."
— arrived as an AskUserQuestion answer and the self-improvement skill
never fired).

The harness reports AskUserQuestion answers as a single formatted string
on tool_response, e.g.:
  Your questions have been answered: "<question>"="<answer>", "<question
  2>"="<answer 2>". You can now continue with these answers in mind.
This hook parses the "<question>"="<answer>" pairs and compares each
answer against the option labels offered for that question (read from
tool_input.questions[].options[].label). It nudges once when any answer is
not among the offered labels for its question — the "Other"/free-text
path. Advisory only: exit 0 always, never blocks.
"""
from __future__ import annotations

import json
import re
import sys

PAIR_RE = re.compile(r'"([^"]*)"\s*=\s*"([^"]*)"')


def _response_text(payload: dict) -> str:
    resp = payload.get("tool_response")
    if isinstance(resp, str):
        return resp
    if isinstance(resp, (dict, list)):
        return json.dumps(resp)
    return ""


def _question_labels(tool_input: dict) -> dict[str, set[str]]:
    """question text -> set of offered option labels, from tool_input.questions."""
    labels: dict[str, set[str]] = {}
    for q in (tool_input or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        text = q.get("question")
        opts = q.get("options") or []
        if not isinstance(text, str) or not isinstance(opts, list):
            continue
        labels[text] = {
            o.get("label") for o in opts
            if isinstance(o, dict) and isinstance(o.get("label"), str)
        }
    return labels


def free_text_questions(tool_input: dict, response_text: str) -> list[str]:
    """Questions (by text) whose answer is not among that question's offered
    labels. Multi-select answers (comma-joined labels) are split and checked
    individually. A question absent from tool_input is skipped — its label
    set can't be correlated, so it is never flagged."""
    labels_by_question = _question_labels(tool_input)
    hits: list[str] = []
    for question, answer in PAIR_RE.findall(response_text):
        labels = labels_by_question.get(question)
        if labels is None:
            continue
        if answer in labels:
            continue
        parts = [p.strip() for p in answer.split(", ")]
        if not parts or not all(p in labels for p in parts):
            hits.append(question)
    return hits


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "AskUserQuestion":
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    response_text = _response_text(payload)
    if not response_text:
        return 0

    hits = free_text_questions(tool_input, response_text)
    if not hits:
        return 0

    print(
        "hook-si-freetext-answer: user answered in free text rather than "
        f"picking an offered option ({len(hits)} question(s)) — this may be "
        "a behavior correction. Per CLAUDE.md § When the user corrects "
        "agent behavior, consider invoking the self-improvement skill this "
        "turn: a free-text AskUserQuestion answer is a trigger channel the "
        "UserPromptSubmit self-improvement reminder does not see.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
