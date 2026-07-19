"""Question-provenance model: a pure structural closure check over raised questions.

Substantive plan construction raises questions ("is X true?", "should we do Y?").
An unanswered question left implicit is a free premise smuggled into the plan —
validate_questions is the type-check that catches it, in the same DFS-free,
per-item closure form as ledger.validate_ledger (mirrored deliberately: same
tolerant dict round-trip, same per-disposition required-field table, same
disposition-gated Candidate cross-check). Unlike ledger.py this module has no
graph/cycle structure to check — a Question's only edge is its `target`, which
points OUT at the plan/stage it was raised against, not at another Question — so
there is no DFS pass here, only per-question field and cross-reference checks.

Vocabulary: a Question's `target` names an activity-ontology element via
ELEMENT_NAMES, imported from text_shape.py rather than restated here or pulled
from plan.py. plan.py's TOML/tomllib/state.py machinery is heavier than this
module needs (premise.py is pure: no filesystem, subprocess, or network access),
and ledger.py's precedent is to stay dependency-free of the plan/session
machinery — so ELEMENT_NAMES was LIFTED out of plan.py into text_shape.py (its
value is unchanged) as the single shared home, and both plan.py and this module
import it from there. This satisfies "reuse plan._ELEMENT_NAMES (or a vocabulary
lifted out of it)" via the lift branch.

Fail-closed asymmetry vs validate_ledger: validate_ledger treats an EMPTY claim
bag as itself a blocker (a ledger with nothing to gate on would vacuously close).
validate_questions does NOT — a plan stage that raised no questions is not
thereby suspect; the empty bag is the normal case for a stage that didn't need to
ask anything, not a smuggled free variable. What must never be empty is a
question's disposition once raised (rule 4: 'open' always blocks) — the
per-question closure is enforced, not the existence of questions at all.

Per-stage binding (F6): a disposed Question that targets `stage:<n>.<element>`
carries `disposed_at_key`, a digest of stage n's OWN current definition, supplied
by the caller as `stage_keys[n]` (opaque to this module — whatever the caller
hashes, e.g. a sha256 of the stage's TOML fields). A question is invalidated only
when the value at ITS OWN bound stage's key changes, never by an edit to any
other stage — the whole-plan-sha design this replaces would invalidate every
question on any unrelated stage edit. `plan.goal` / `plan.done_criterion`
targets are exempt from this check (there is no per-goal key to compare against;
the plan-level target does not repeat under a stage index).

Pure module: no filesystem, subprocess, or network access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text_shape import ELEMENT_NAMES
from .text_shape import PLACEHOLDER_SET as _PLACEHOLDER_SET
from .text_shape import normalize_string as _normalize_string

VALID_DISPOSITIONS = frozenset({"open", "researched", "escalated", "assumed", "retired"})

# Dispositions that bind to a specific stage definition (F6) — 'open' has nothing
# to bind yet, 'retired' has deliberately walked away from the target.
_KEY_BOUND_DISPOSITIONS = frozenset({"researched", "escalated", "assumed"})

TARGET_RE = re.compile(r"^stage:(\d+)\.([a-z_]+)$")


def parse_target(target: str) -> tuple[str, int | None, str | None] | None:
    """Parse a Question.target address. Returns (kind, stage_index, element) where
    kind is 'goal' | 'done_criterion' | 'stage', or None if the target does not
    parse as a legal address (unknown literal, malformed stage form, or an
    element outside ELEMENT_NAMES)."""
    if target == "plan.goal":
        return ("goal", None, None)
    if target == "plan.done_criterion":
        return ("done_criterion", None, None)
    m = TARGET_RE.match(target or "")
    if not m:
        return None
    element = m.group(2)
    if element not in ELEMENT_NAMES:
        return None
    return ("stage", int(m.group(1)), element)


@dataclass
class Question:
    id: str
    target: str
    question: str
    own_research: str = ""
    disposition: str = "open"
    answer: str = ""
    source: str = ""
    derivation: str = ""
    basis: str = ""
    risk: str = ""
    reason: str = ""
    disposed_at_key: str = ""


def questions_from_dicts(raw: list[dict]) -> list[Question]:
    return [
        Question(**{
            "id": d["id"],
            "target": d["target"],
            "question": d.get("question", ""),
            "own_research": d.get("own_research", ""),
            "disposition": d.get("disposition", "open"),
            "answer": d.get("answer", ""),
            "source": d.get("source", ""),
            "derivation": d.get("derivation", ""),
            "basis": d.get("basis", ""),
            "risk": d.get("risk", ""),
            "reason": d.get("reason", ""),
            "disposed_at_key": d.get("disposed_at_key", ""),
        })
        for d in raw
    ]


def questions_to_dicts(questions: list[Question]) -> list[dict]:
    return [
        {
            "id": q.id,
            "target": q.target,
            "question": q.question,
            "own_research": q.own_research,
            "disposition": q.disposition,
            "answer": q.answer,
            "source": q.source,
            "derivation": q.derivation,
            "basis": q.basis,
            "risk": q.risk,
            "reason": q.reason,
            "disposed_at_key": q.disposed_at_key,
        }
        for q in questions
    ]


# Per-disposition required free-text fields (rules 5-9), reused for the
# anti-template placeholder check (rule 10).
_REQUIRED_FIELDS = {
    "escalated": ("own_research", "answer"),
    "researched": ("own_research", "answer", "source", "derivation"),
    "assumed": ("own_research", "basis", "risk"),
    "retired": ("reason",),
}


def validate_questions(questions: list[Question], *, stage_keys: dict[int, str]) -> list[str]:
    """Pure: a question bag + the caller's {stage_index: current_key} map ->
    blockers (empty iff every raised question is closed). An empty question bag
    is NOT itself a blocker (see module docstring) — only an individual raised,
    undisposed, or malformed question is.

    `stage_keys` is opaque to this module: the caller decides what "current key"
    means for a stage (typically a digest of its own fields) and this module only
    compares a disposed question's stamped `disposed_at_key` against it. Passing
    an empty `stage_keys` dict skips BOTH the dangling-target check (rule 2) and
    the key-mismatch check (rule 12) — the caller who cannot yet compute keys
    (e.g. before a plan exists) gets a validator that checks disposition-shape
    only, not binding.
    """
    blockers: list[str] = []

    for q in questions:
        parsed = parse_target(q.target)
        if parsed is None:
            blockers.append(f"question {q.id!r} has an unparseable target {q.target!r}")
            continue
        kind, stage_index, _element = parsed

        if (
            kind == "stage"
            and stage_keys
            and stage_index not in stage_keys
            and q.disposition != "retired"
        ):
            # 'retired' has deliberately walked away from the target (see the
            # _KEY_BOUND_DISPOSITIONS comment): a dangling edge is the expected
            # end-state of retiring a question whose stage was dropped, and it must
            # not itself keep blocking — retire IS the route out of this blocker.
            # A retired question still falls through to the reason-required check.
            blockers.append(
                f"question {q.id!r} is bound to stage {stage_index}, which the current "
                f"plan does not contain (dangling edge) — retire it with a reason or "
                f"rebind it to a stage that exists"
            )
            continue

        if q.disposition not in VALID_DISPOSITIONS:
            blockers.append(f"question {q.id!r} has unknown disposition {q.disposition!r}")
            continue

        if q.disposition == "open":
            blockers.append(f"question {q.id!r} is open (undispositioned)")
            continue

        required = _REQUIRED_FIELDS[q.disposition]
        for f in required:
            value = getattr(q, f)
            if not value:
                note = (
                    " (own research must precede escalation to the user)"
                    if q.disposition == "escalated" and f == "own_research"
                    else ""
                )
                blockers.append(f"{q.disposition} question {q.id!r} has no {f}{note}")
            elif _normalize_string(value) in _PLACEHOLDER_SET:
                blockers.append(f"question {q.id!r} field {f!r} is a placeholder value {value!r}")

        if q.derivation:
            if q.answer and _normalize_string(q.derivation) == _normalize_string(q.answer):
                blockers.append(
                    f"question {q.id!r} derivation echoes its answer instead of "
                    "explaining the inference"
                )
            if q.source and _normalize_string(q.derivation) == _normalize_string(q.source):
                blockers.append(
                    f"question {q.id!r} derivation echoes its source instead of "
                    "explaining the inference"
                )

        if (
            kind == "stage"
            and q.disposition in _KEY_BOUND_DISPOSITIONS
            and stage_keys
            and stage_index in stage_keys
            and q.disposed_at_key != stage_keys[stage_index]
        ):
            blockers.append(
                f"question {q.id!r} is bound to stage {stage_index}, whose definition "
                "changed since this question was disposed — re-confirm it against the "
                "current stage or leave it open for re-disposition"
            )

    return blockers


VALID_CANDIDATE_DISPOSITIONS = frozenset({"raised", "recorded", "dismissed"})


@dataclass
class QuestionCandidate:
    id: str
    statement: str
    disposition: str = "raised"
    reason: str = ""
    question: str = ""


def question_candidates_from_dicts(raw: list[dict]) -> list[QuestionCandidate]:
    return [
        QuestionCandidate(
            id=d["id"],
            statement=d.get("statement", ""),
            disposition=d.get("disposition", "raised"),
            reason=d.get("reason", ""),
            question=d.get("question", ""),
        )
        for d in raw
    ]


def question_candidates_to_dicts(candidates: list[QuestionCandidate]) -> list[dict]:
    return [
        {
            "id": c.id,
            "statement": c.statement,
            "disposition": c.disposition,
            "reason": c.reason,
            "question": c.question,
        }
        for c in candidates
    ]


def validate_question_candidates(
    candidates: list[QuestionCandidate], questions: list[Question]
) -> list[str]:
    """Pure: a candidate bag (raised by an enumeration cross-check pass, stage 5)
    + the question bag it references -> blockers (empty iff every candidate is
    dispositioned). Mirrors ledger.validate_candidates' FORM — a bare 'raised'
    candidate always blocks, 'dismissed' needs a reason, 'recorded' needs a
    pointer that resolves — but does not import ledger.Candidate: its `claim`
    field names the wrong referent for a question-enumeration candidate.
    """
    blockers: list[str] = []
    by_id = {q.id: q for q in questions}

    for cand in candidates:
        if cand.disposition not in VALID_CANDIDATE_DISPOSITIONS:
            blockers.append(
                f"question candidate {cand.id!r} has unknown disposition {cand.disposition!r}"
            )
        elif cand.disposition == "raised":
            blockers.append(f"undispositioned enumeration candidate {cand.id!r}")
        elif cand.disposition == "dismissed":
            if not cand.reason:
                blockers.append(f"dismissed question candidate {cand.id!r} has no reason")
        elif cand.disposition == "recorded":
            if not cand.question:
                blockers.append(f"recorded question candidate {cand.id!r} has no linked question")
                continue
            if cand.question not in by_id:
                blockers.append(
                    f"recorded question candidate {cand.id!r} points at unknown "
                    f"question {cand.question!r} (dangling edge)"
                )

    return blockers
