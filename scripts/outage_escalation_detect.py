#!/usr/bin/env python3
"""Heuristic detector: is this text an external-service-failure ESCALATION that
has NOT been through a diagnosis?

Difficulty removed: the coordinator, hitting an apparent external-service outage
(a probe returns 504 / "unreachable"), sometimes surfaces the failure straight to
the user ("сервис лежит — что делать?") — or launders the unverified premise into
a sub-agent question — WITHOUT first reproducing it with the real client and
enumerating hypotheses. A bare probe is not a diagnosis; the premise is often
false (stale snapshot, wrong client, transient). This detector is the shared
PERCEPTION half of the gate: it decides "this reads like an un-diagnosed outage
escalation", so the deterministic hooks (hook-escalation-diagnosis-gate.py,
the escalation_without_diagnosis turn guardian) can act on it.

This is a heuristic backstop, not a determinization: paraphrases it cannot match
are expected misses, and it deliberately errs toward UNDER-firing (see below).

Design (precision-first; mirrors si_feedback_detect.py):

  Fires ONLY when BOTH co-occur in the text:
    1. a PRESENT-TENSE / first-person-inability external-failure cue
       ("не могу получить доступ", "сервис не отвечает", "endpoint недоступен",
        "504 no upstreams", "X лежит" / "is down" / "unreachable"); AND
    2. a user-facing ESCALATION FRAME — a question / permission-request about how
       to proceed ("что делать", "к какому … доступ", "подскажи",
        "нужно ли эскалировать", "what should I do", "who do I ask", …).

  Returns AT MOST ONE signal string naming what matched (empty list == no fire).

Precision choices (documented misses, not bugs):
  - Narrative PAST downtime ("X лежал вчера, но починили") is a precision target
    the matcher will sometimes MISS — present-tense cues use `\\bлежит\\b` /
    `\\bis down\\b`, so the past form `лежал` simply does not match; no brittle
    negative-lookahead is added that would risk the present-tense true positives.
    Prefer under-fire over a fragile matcher.
  - "down" in a non-outage sense ("scroll down", "the down arrow") does not match
    the specific `\\bis down\\b` / `\\bлежит\\b` phrases.
  - A plain outage REPORT with no ask ("сервис не отвечает") does not fire — the
    escalation frame is required by conjunction.
"""
from __future__ import annotations

import re

# Present-tense / first-person-inability external-failure cues. Deliberately
# specific phrases: the conjunction with an escalation frame (below) supplies the
# precision, but each cue is still kept narrow enough not to match everyday prose.
_FAILURE_RE = re.compile(
    r"не могу (?:получить )?доступ"     # first-person inability (RU)
    r"|не отвеча\w*"                    # "не отвечает" / "не отвечают"
    r"|недоступ\w*"                     # недоступен / недоступна / недоступно
    r"|не работа\w*"                    # "не работает" (present)
    r"|\bлежит\b"                       # "X лежит" (present; PAST "лежал" won't match)
    r"|\bis down\b"
    r"|\bis unreachable\b"
    r"|\bunreachable\b"
    r"|\bnot responding\b"
    r"|\btimed? out\b"
    r"|no upstreams"
    r"|\b50[0234]\b",                   # 500/502/503/504 gateway errors
    re.IGNORECASE | re.UNICODE,
)

# User-facing escalation frame: a question / permission-request to the user about
# how to proceed. Explicit phrases only — bare "?" is intentionally excluded
# (too broad), keeping the matcher precision-first.
_ESCALATION_RE = re.compile(
    r"что (?:мне |нам )?делать"
    r"|как (?:мне |нам )?быть"
    r"|подскаж\w*"
    r"|эскалир\w*"                      # "нужно ли эскалировать", "эскалация"
    r"|к ком[уy]\b"                     # "к кому обратиться / за доступом"
    r"|к как(?:ому|ой)\b"              # "к какому сервису … доступ"
    r"|what should i do"
    r"|what do i do"
    r"|how (?:do|should|can) i"
    r"|who (?:do|should|can) i"
    r"|should i escalate"
    r"|need(?:s)? escalat\w*",
    re.IGNORECASE | re.UNICODE,
)


def detect(text: str) -> list[str]:
    """Return a one-element signal list when ``text`` reads like an un-diagnosed
    external-service-failure escalation, else []. Precision-first: fires only on
    the conjunction of a present-tense failure cue and a user-facing escalation
    frame."""
    if not isinstance(text, str) or not text:
        return []
    fmatch = _FAILURE_RE.search(text)
    if not fmatch:
        return []
    ematch = _ESCALATION_RE.search(text)
    if not ematch:
        return []
    return [
        f"external-service-failure escalation without diagnosis "
        f"(failure cue: {fmatch.group(0)!r}; escalation frame: {ematch.group(0)!r})"
    ]
