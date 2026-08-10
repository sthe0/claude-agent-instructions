"""Shared AskUserQuestion payload text extraction for PreToolUse gate hooks.

Difficulty removed: hook-escalation-diagnosis-gate.py and
hook-deferring-disposition-gate.py both gate on the same AskUserQuestion
payload contract (a `tool_input.questions` list, each with `question`/
`header` and an `options` list of `label`/`description`), but at different
granularities — the escalation gate's predicate is inherently textual (one
flat blob is enough: outage_escalation_detect.detect matches cues anywhere in
the ask), while the deferring-disposition gate's predicate is per-menu (EVERY
option of ONE question must defer). A byte-identical copy of the parser lived
in each hook; one parser here removes the double-maintenance hazard of the
AskUserQuestion schema drifting and being fixed in only one copy.
"""
from __future__ import annotations


def _parse_questions(tool_input: dict):
    """Yield (header_parts, option_parts) for each well-formed question dict in
    tool_input['questions'], skipping malformed entries. Tolerant of missing
    keys and schema drift — an absent field contributes nothing; a malformed
    tool_input or questions value yields no items at all."""
    if not isinstance(tool_input, dict):
        return
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return
    for q in questions:
        if not isinstance(q, dict):
            continue
        header_parts = [q[k] for k in ("question", "header") if isinstance(q.get(k), str)]
        option_parts: list[str] = []
        options = q.get("options")
        if isinstance(options, list):
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                for key in ("label", "description"):
                    val = opt.get(key)
                    if isinstance(val, str):
                        option_parts.append(val)
        yield header_parts, option_parts


def question_texts(tool_input: dict) -> list[str]:
    """Per-question user-facing text: one entry per well-formed question, each
    the question's own question/header text plus its options' label/
    description, joined with "\\n" — the granularity a per-menu predicate
    needs."""
    return ["\n".join(header + options) for header, options in _parse_questions(tool_input)]


def option_texts(tool_input: dict) -> list[str]:
    """Per-question option-only text: one entry per well-formed question,
    containing just its options' label/description (no question/header
    stem) — for a prefilter that must not fire on the question's own
    wording, only on what its options actually offer."""
    return ["\n".join(options) for _, options in _parse_questions(tool_input)]


def question_stems(tool_input: dict) -> list[str]:
    """Per-question header/question text only (no options): one entry per
    well-formed question, its own question/header field(s) joined with " " —
    the inverse slice of option_texts, for a caller that names WHICH menu
    fired without dumping its full option list."""
    return [" ".join(header) for header, _ in _parse_questions(tool_input)]


def flat_text(tool_input: dict) -> str:
    """Every user-facing string in the payload concatenated into one blob,
    across every question — the escalation gate's granularity, byte-identical
    to the parser each hook used to carry independently."""
    parts: list[str] = []
    for header, options in _parse_questions(tool_input):
        parts.extend(header)
        parts.extend(options)
    return "\n".join(parts)
