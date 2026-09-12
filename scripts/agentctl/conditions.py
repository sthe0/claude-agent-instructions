"""Does a stage's `conditions` merely restate the stages it depends on?

The defect: a stage has two different things to say about what must hold around
it -- what must already be true before it may START (`preconditions`), and what
must hold OF THE WORLD for its own transformation to go through
(`conditions`) -- and the second collapses into the first's degenerate case, "the
stage before me is finished". That sentence is already recorded, structurally and
checkably, as `depends_on`; written into `conditions` it displaces the one place
the plan had for naming the state of the tooling, the data or the environment the
transformation actually needs, and the plan then declares no conditions at all
while appearing to.

Deciding that collapse is a judgement about MEANING. A condition may legitimately
name an earlier stage ("the schema-24 migration stage 2 landed is applied to the
live store") and still be a condition. So this module follows `result_image.py`
-- the first instance of the same split (CLAUDE.md "separate rule from
perception"; memory leaf regex-not-for-semantic-classification) -- and does not
decide:

* ``restatement_prefilter`` -- pure, structural, keeps the model off conditions
  that plainly cannot be restatements. It fires only where there is something to
  restate: a stage with no `depends_on` has no dependency for its conditions to
  collapse into, whatever else they say.
* ``judge_restatement`` -- the model judgement that decides, fail-open in the
  ``advisor.judge_binary_ask`` mould: disabled, no runner, a non-zero exit, an
  unparseable answer or any exception all yield False.

WHERE THIS DIVERGES FROM THE FIRST INSTANCE, and it is the divergence that
matters: an echo verdict WARNS, and this one REFUSES (submission.py). A refusal
is the stronger claim, so read the two halves' failure directions together --
False from the judge means the plan goes through exactly as it would have before
this file existed, and False is what every unreachable, slow or confused judge
returns. The refusal is also paired: it can only fire on a plan graded against
`preconditions`, so the field the message tells the author to move the sentence
into is one the same submission demands they declare. Refusing a restatement
while offering no other place to put it would be a trap, not a gate.

The prefilter here is NOT calibrated against a labelled corpus the way
`echo_prefilter` is (scripts/tests/fixtures/result_image_echo_labels.json). Its
recall is therefore unmeasured, and a restatement phrased without any of the
structural marks below is missed silently -- the status quo before this file, not
a new failure. Calibrate it if the check is ever asked to carry more weight than
"ask a model about this one".

Nothing here runs a subprocess except ``judge_restatement``, which is reached only
from the submission seam. The plan loader stays pure.
"""

from __future__ import annotations

import re

# Same tier and budget as result_image's judge and advisor.judge_binary_ask: a
# one-bit classification over a sentence or two.
_JUDGE_MODEL = "haiku"
_JUDGE_TIMEOUT_S = 8

# A stage named by its index -- "stage 2", "stages 1 and 3". The index is captured
# so the prefilter can ask whether it is one this stage ALREADY declares as a
# dependency; naming some unrelated stage is not the defect.
_INDEXED_STAGE = re.compile(r"\bstages?\s+(\d+)", re.IGNORECASE)

# The same reference made relatively, which carries no index to compare -- so it
# is evidence only together with a completion word below.
_RELATIVE_STAGE = re.compile(
    r"\b(?:previous|prior|preceding|earlier|upstream)\s+stages?\b", re.IGNORECASE
)

# How a clause says that earlier work is FINISHED, as opposed to what is true of
# the world. Word-bounded so "landed" is not "landedspec" and "done" is not
# "abandoned". Deliberately without "exists": "the venv exists" is a state of the
# world, i.e. exactly the genuine shape this vocabulary must distinguish itself
# FROM.
_COMPLETION = re.compile(
    r"\b(?:done|complete|completes|completed|finish(?:es|ed)?"
    r"|pass(?:es|ed)|land(?:s|ed)|merged|delivered|shipped)\b",
    re.IGNORECASE,
)


def restatement_prefilter(conditions: str, depends_on=()) -> tuple[str, ...]:
    """Structural reasons to suspect `conditions` restates `depends_on`; () if none.

    High recall by construction -- a returned reason means "worth a model's
    attention", never "this is a restatement". Each string is phrased so it can be
    shown to the plan's author as WHAT the conditions restate.

    Pure: no I/O, no subprocess. The submission seam calls it on every stage of
    every incoming plan.
    """
    if not isinstance(conditions, str) or not conditions.strip():
        return ()
    deps = {int(d) for d in depends_on}
    if not deps:
        # Nothing to restate. A stage that waits on nothing may still write a poor
        # `conditions`, but it cannot be writing THIS defect, and a check that
        # fired here would be judging conditions in general.
        return ()

    reasons: list[str] = []

    named = sorted({int(m) for m in _INDEXED_STAGE.findall(conditions)} & deps)
    if named:
        reasons.append(
            "it names " + ", ".join(f"stage {n}" for n in named)
            + ", already declared as a dependency"
        )

    if _RELATIVE_STAGE.search(conditions) and _COMPLETION.search(conditions):
        reasons.append("it says an earlier stage is finished, which `depends_on` already says")

    return tuple(reasons)


_RESTATEMENT_JUDGE_PROMPT = (
    "A plan stage declares two separate things about what must hold around it: "
    "PRECONDITIONS -- what must already be true before the stage may START -- and "
    "CONDITIONS -- what must hold of the WORLD for the stage's own transformation "
    "to go through. It separately declares, structurally, which stages it waits "
    "on: DEPENDS_ON.\n\n"
    "Decide whether the CONDITIONS below are exhausted by restating DEPENDS_ON: "
    "delete from them everything that only says an earlier stage is finished, and "
    "see what is left.\n\n"
    "Answer RESTATEMENT when nothing is left -- when a reader learns only that the "
    "stages this one waits on are done, which DEPENDS_ON already told them.\n"
    "Answer CONDITION when something is left: a state of the tooling, the data, "
    "the environment, an access, or something that must hold WHILE the stage runs. "
    "Naming an earlier stage does NOT by itself make it a restatement -- a "
    "condition may say where a thing came from and still be a condition.\n\n"
    "Answer on the FIRST line with exactly RESTATEMENT or CONDITION, nothing "
    "else.\n\n"
    "DEPENDS_ON: {depends_on}\n\nCONDITIONS:\n{conditions}"
)

_RESTATEMENT = "RESTATEMENT"


def judge_restatement(
    conditions: str,
    runner,
    *,
    depends_on=(),
    enabled: bool = True,
    timeout: int = _JUDGE_TIMEOUT_S,
) -> bool:
    """Does the model read `conditions` as exhausted by restating `depends_on`?

    Fail-open in every direction: disabled, no runner, a non-zero exit, an empty or
    unparseable answer, or any exception returns False -- no refusal at all,
    submission unaffected. This is the direction the whole check must fail in: the
    verdict it feeds REFUSES a plan, and a fabricated True would turn an
    unreachable judge into a wall.
    """
    if not enabled or not isinstance(conditions, str) or not conditions.strip():
        return False
    if runner is None:
        return False
    try:
        deps = ", ".join(str(int(d)) for d in depends_on) or "(none)"
        prompt = _RESTATEMENT_JUDGE_PROMPT.format(depends_on=deps, conditions=conditions)
        result = runner(["claude", "-p", "--model", _JUDGE_MODEL, prompt], timeout=timeout)
        if result.returncode != 0:
            return False
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return False
        return lines[0].upper().startswith(_RESTATEMENT)
    except Exception:
        return False
