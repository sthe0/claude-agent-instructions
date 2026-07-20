#!/usr/bin/env python3
"""Structural pre-filter: does this text carry an external-service-failure
PROTOCOL TOKEN (an HTTP 4xx/5xx status run or a fixed English outage token)?

Difficulty removed: the coordinator, hitting an apparent external-service outage
(a probe returns 504 / "unreachable"), sometimes surfaces the failure straight to
the user ("сервис лежит — что делать?") — or launders the unverified premise into
a sub-agent question — WITHOUT first reproducing it with the real client and
enumerating hypotheses. A bare probe is not a diagnosis; the premise is often
false (stale snapshot, wrong client, transient).

Split of labor (rule vs perception): the per-language natural-language cue
conjunction that used to decide "this reads like an un-diagnosed outage
escalation" (a present-tense failure cue AND a user-facing escalation frame) has
been RETIRED and moved to the model-backed semantic_judge.py ('outage_escalation'
kind), which classifies MEANING in any language. This module keeps ONLY the
language-agnostic protocol-token PRE-FILTER.

Two consumers, one pre-filter:
  - hook-turn-end-gate.py (Stop shell): uses `protocol_prefilter` as the cheap
    precondition, then consults the semantic judge on the assistant text.
  - hook-escalation-diagnosis-gate.py (PreToolUse on AskUserQuestion): uses
    `protocol_prefilter` as its INSTANT deterministic gate. It deliberately does
    NOT call the judge — a per-AskUserQuestion `claude -p` call would add latency
    to every ask and risk hook->claude->hook recursion. The cost is a recall
    narrowing (precision-first, as this detector always was): a pure-NL escalation
    with NO protocol token ("сервис лежит, к кому за доступом?") is no longer
    pre-empted at ask time; the Stop guardian's judge is the backstop for the text
    form of that escalation.
"""
from __future__ import annotations

import re

# External-service-failure protocol tokens: an HTTP 4xx/5xx status run, or a fixed
# English outage token. Language-agnostic (a bare status code / gateway phrase),
# so no per-language cue list — the meaning-level judgment is the semantic judge's.
_PROTOCOL_RE = re.compile(
    r"\b[45]\d\d\b"          # HTTP 4xx / 5xx status code
    r"|\btimed? out\b"       # "timeout" is caught by the alt below; "timed out" here
    r"|\btimeout\b"
    r"|\bunreachable\b"
    r"|no upstreams",
    re.IGNORECASE,
)


def protocol_prefilter(text: str) -> bool:
    """True iff ``text`` contains an external-service-failure protocol token (an
    HTTP 4xx/5xx run or one of 'timeout' / 'timed out' / 'unreachable' /
    'no upstreams'). The cheap language-agnostic precondition gate; the meaning-level
    "is this an un-diagnosed escalation" judgment belongs to the semantic judge."""
    if not isinstance(text, str) or not text:
        return False
    return _PROTOCOL_RE.search(text) is not None
