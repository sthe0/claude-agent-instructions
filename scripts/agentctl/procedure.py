"""Is a stage's `procedure` the same thing as its `method`, said twice?

The defect: a stage has two different things to say about how it acts -- the
REQUIREMENT the transformation must satisfy (`method`, the planner's norm, moved only
through the review and approval a replan re-arms) and the SEQUENCE of operations
proposed for meeting it (`procedure`, the executor's own, replaceable without
re-approval). With one text in both places the separation is nominal: whatever is
written there is either a norm the executor may silently rewrite, or a proposal he is
held to as if it were a norm. Both are the collapse the field split exists to remove,
and a plan carrying it passes every structural check while declaring nothing new.

Deciding that sameness is a judgement about MEANING, and NOT normalized string
equality. That comparison was measured against this repo's own frozen corpus while the
split was being designed: of 200 stages it caught 0 -- an author who writes the same
thought twice does not write it in the same words. A refusal resting on it would be a
gate that has never fired and cannot. So this module follows `result_image.py` (the
first instance of the split) and `conditions.py` (the first instance that REFUSES):

* ``collapse_prefilter`` -- pure, structural, high-recall. It keeps the model off pairs
  that plainly say different things; a returned reason means "worth a model's
  attention", never "this is a collapse". Equality is one of its marks, not its rule.
* ``judge_collapse`` -- the model judgement that decides, fail-open in the
  ``advisor.judge_binary_ask`` mould: disabled, no runner, a non-zero exit, an
  unparseable answer or any exception all yield False, i.e. no refusal at all.

CALIBRATION. The overlap threshold below is not a guess and not a corpus of labelled
collapses (none exists -- the field is new, so there is nothing yet written in it to
label). What was measurable is the NULL case the threshold must not fire on: how much
wording two DIFFERENT places of one stage share when both were written honestly. Every
(method, means), (method, conditions), (method, invariants) and (method, material) pair
in the 55-plan frozen corpus is such a case -- distinct places, one author, one subject
matter, 800 of them. `test_renormalization.py::
test_the_overlap_threshold_clears_the_corpus_null_case` re-measures that distribution on
every run and pins the threshold above its maximum, so a corpus plan whose honest prose
would start summoning a judge turns the suite red instead.

The recall direction stays unmeasured, exactly as `conditions.py` says of its own
prefilter: a collapse phrased with no shared vocabulary at all is missed silently. That
is the status quo before this file, not a new failure.

Nothing here runs a subprocess except ``judge_collapse``, reached only from the
submission seam. The plan loader stays pure.
"""

from __future__ import annotations

import re

from .text_shape import normalize_string as _normalize_string

# Same tier and budget as result_image's and conditions' judges: a one-bit
# classification over a sentence or two.
_JUDGE_MODEL = "haiku"
_JUDGE_TIMEOUT_S = 8

#: Word-overlap (Jaccard over token sets) at or above which the pair is worth a model's
#: attention. Bounded below by the corpus null case rather than chosen: the highest
#: overlap any honest method/other-place pair in the frozen corpus reaches is 0.30
#: (maritime-charter-specialization.toml stage 4, method vs invariants), so 0.50 clears
#: every measured distinct pair with room to spare while still catching a rewording that
#: keeps half its vocabulary. The gap is deliberate and not free precision: 800 pairs
#: written by two or three authors do not bound what a null case CAN reach, and the cost
#: of the two errors is asymmetric -- a false positive spends one `claude -p` call on a
#: judge who then says DISTINCT, while a false negative is silent. See CALIBRATION above
#: and the test that re-measures it.
OVERLAP_THRESHOLD = 0.50

#: Below this many characters a containment mark says nothing -- "run pytest" inside a
#: longer sentence is a shared phrase, not a restated requirement.
_MIN_CONTAINED_CHARS = 40

#: Tokens shorter than this are dropped from the overlap sets: articles, prepositions
#: and glue words are shared by any two English sentences and would lift every pair's
#: score by a constant that carries no information about sameness.
_MIN_TOKEN_CHARS = 3

_TOKEN = re.compile(r"[\w/.-]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(_normalize_string(text)) if len(t) >= _MIN_TOKEN_CHARS}


def overlap(method: str, procedure: str) -> float:
    """Jaccard over the two texts' significant token sets; 0.0 when either is empty.

    Exposed rather than private because the calibration test measures exactly this
    number over the corpus: a threshold justified by a measurement whose function the
    test re-implements would be justified by nothing.
    """
    a, b = _tokens(method), _tokens(procedure)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def collapse_prefilter(method: str, procedure: str) -> tuple[str, ...]:
    """Structural reasons to suspect `procedure` merely restates `method`; () if none.

    High recall by construction. Each string is phrased so it can be shown to the plan's
    author as WHAT the two places share.

    Pure: no I/O, no subprocess. The submission seam calls it on every stage of every
    incoming plan.
    """
    if not isinstance(method, str) or not isinstance(procedure, str):
        return ()
    m, p = _normalize_string(method), _normalize_string(procedure)
    if not m or not p:
        # Nothing to compare. An absent field is the SUBMISSION requirement's case, and
        # reporting it twice would tell an author to distinguish a place he has not
        # written yet.
        return ()

    reasons: list[str] = []
    if m == p:
        reasons.append("they are the same text")
    elif (m in p or p in m) and min(len(m), len(p)) >= _MIN_CONTAINED_CHARS:
        reasons.append("one is contained in the other verbatim")

    score = overlap(method, procedure)
    if score >= OVERLAP_THRESHOLD:
        reasons.append(f"they share {score:.0%} of their significant wording")
    return tuple(reasons)


_COLLAPSE_JUDGE_PROMPT = (
    "A plan stage declares two separate things about how it acts.\n\n"
    "METHOD is the REQUIREMENT on the way of acting: what the transformation must be "
    "an instance of -- the pattern to follow, the abstraction to extend, where the "
    "change lands. It is the planner's norm, and the executor is held to it.\n\n"
    "PROCEDURE is the SEQUENCE of operations proposed for meeting that requirement. It "
    "is the executor's own proposal, and he may replace it with a better order without "
    "asking anyone.\n\n"
    "Decide whether the PROCEDURE below says anything the METHOD does not: is there an "
    "ordering, a sub-action, a concrete operation or a step that a reader learns from "
    "the procedure and could not have read off the method?\n\n"
    "Answer SAME when there is nothing -- when the procedure is the method reworded, "
    "however differently it is phrased.\n"
    "Answer DISTINCT when there is something. A procedure that follows obviously from "
    "the method is still DISTINCT if it states operations the method does not; being "
    "predictable is not being the same.\n\n"
    "Answer on the FIRST line with exactly SAME or DISTINCT, nothing else.\n\n"
    "METHOD:\n{method}\n\nPROCEDURE:\n{procedure}"
)

_SAME = "SAME"


def judge_collapse(
    method: str,
    procedure: str,
    runner,
    *,
    enabled: bool = True,
    timeout: int = _JUDGE_TIMEOUT_S,
) -> bool:
    """Does the model read `procedure` as saying nothing `method` does not?

    Fail-open in every direction: disabled, no runner, a non-zero exit, an empty or
    unparseable answer, or any exception returns False -- no refusal at all, submission
    unaffected. This is the direction the whole check must fail in: the verdict it feeds
    REFUSES a plan, and a fabricated True would turn an unreachable judge into a wall.
    """
    if not enabled or runner is None:
        return False
    if not isinstance(method, str) or not isinstance(procedure, str):
        return False
    if not method.strip() or not procedure.strip():
        return False
    try:
        prompt = _COLLAPSE_JUDGE_PROMPT.format(method=method, procedure=procedure)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        if result.returncode != 0:
            return False
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return False
        return lines[0].upper().startswith(_SAME)
    except Exception:
        return False
