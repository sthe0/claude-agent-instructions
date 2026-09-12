#!/usr/bin/env python3
"""Heuristic detector: does this text carry CLOSURE-shaped language — a decision
taken at a fork point, or requested work declared complete?

Difficulty removed: a turn can reach closure — commit to a choice among several
plausible options, or declare the requested work finished — while posing NO
question at all, not even in prose. `prose_binary_ask` only catches a turn that
DOES pose a question (just not through AskUserQuestion); `resolution_turn_blockers`
only catches the narrow case where a readable agentctl SessionState exists AND
weight_class is SUBSTANTIVE AND every stage has PASSED. Neither guardian sees a
turn that silently decides or silently finishes outside those two shapes.

This is the shared PERCEPTION half of the `silent_closure` judge gate (mirrors
si_feedback_detect.py / outage_escalation_detect.py): a cheap, high-recall,
language-independent (EN + RU) keyword scan that WIDENS candidates for the model
judge (agentctl.advisor.judge_silent_closure) — it never decides a block on its
own. Per CLAUDE.md's "separate rule from perception" principle and
memory-global/leaves/regex-not-for-semantic-classification.md, the actual
classification (is this a genuine silent closure, or routine narration / an
already-posed question / a decision framed as the only reasonable option) is the
judge's job, not this regex's.

Deliberately OR, not AND, across the two clusters: either a decision-commitment
cue or a completion cue is enough to widen the candidate to the judge — the two
clusters name different obligations (Cluster A: a fork-point decision taken
silently; Cluster C: completion narrated with no confirmation sought) and a
turn only needs to match one to be worth a judge call.

Precision is intentionally NOT this module's job (see the module docstring
above) — false positives here cost one judge call, gated by the shared judge
budget; false negatives here mean a silent closure never even reaches the
judge. High recall is the correct bias.
"""
from __future__ import annotations

import re

# Cluster A — a decision committed to at its own authority, at a point the text
# itself frames as having more than one plausible option. Deliberately broad:
# "I'll go with X" / "going with X" covers both a genuine fork-point decision and
# ordinary narration of a routine step — the judge tells the two apart.
_DECISION_RE = re.compile(
    r"i'?ll go with"
    r"|going with"
    r"|i'?ve decided"
    r"|i decided to"
    r"|opting for"
    r"|i'?ll choose"
    r"|i'?m choosing"
    r"|settl(?:ing|ed) on"
    r"|picking\b"
    r"|let'?s go with"
    r"|i'?ll pick"
    r"|решил использовать"
    r"|решил\w* пойти"
    r"|остановлюсь на"
    r"|остановил\w* выбор"
    r"|выбираю вариант"
    r"|пойду с"
    r"|буду использовать",
    re.IGNORECASE | re.UNICODE,
)

# Cluster C — requested work declared complete, terminal-sounding, with no
# confirmation sought. Broad completion phrasing in both languages; the judge
# distinguishes a genuine terminal declaration from a routine sub-step ("I
# finished reading the file") or a status update that says more work remains.
_COMPLETION_RE = re.compile(
    r"\bdone\b"
    r"|\ball set\b"
    r"|\bthat'?s it\b"
    r"|\bcompleted?\b"
    r"|\bfinished\b"
    r"|\bwrapped up\b"
    r"|\bfully resolved\b"
    r"|\btask is complete\b"
    r"|готово"
    r"|завершил\w*"
    r"|заверш\w* работ\w*"
    r"|выполнено"
    r"|выполнил\w*"
    r"|закончил\w*"
    r"|задача решена"
    r"|всё сделано"
    r"|все сделано",
    re.IGNORECASE | re.UNICODE,
)


def detect(text: str) -> list[str]:
    """Return a one-element signal list when ``text`` carries closure-shaped
    language (a decision-commitment cue OR a completion cue), else []."""
    if not isinstance(text, str) or not text:
        return []
    dmatch = _DECISION_RE.search(text)
    if dmatch:
        return [f"decision-commitment cue: {dmatch.group(0)!r}"]
    cmatch = _COMPLETION_RE.search(text)
    if cmatch:
        return [f"completion cue: {cmatch.group(0)!r}"]
    return []
