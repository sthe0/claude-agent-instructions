"""Submission-time plan requirements — the seam where plan bytes enter a live session.

WHY A SEAM AND NOT THE LOADER. `plan.load_plan` is reached with `strict=True` from seven
in-session call sites, several of which RE-READ a plan the session already accepted (the
approve-time refresh, both replan sides, the premise gate's own fresh load). A requirement
added to `parse_plan`'s `if strict:` branches is therefore RETROACTIVE: every live session
whose plan predates the requirement starts failing on a re-read of bytes it was already
approved on, with no recovery edge. Migrating those plans is not an option either — five
things are computed over a plan's bytes, three of them attestations (the approved-plan
snapshot hash, the plan-review binding, the plan-presentation delivery receipt) that a
rewrite would invalidate and that cannot be re-signed on the plan's behalf.

So the requirement goes HERE instead, and `load_plan` stays exactly as permissive as it was.
This module is called from exactly three points — the three places plan bytes ENTER a
session rather than get re-read inside one:

  (a) cmd_submit_plan             — the first entry
  (b) cmd_replan, NEW side        — the single `_load(args.plan)` before `diff_plans`,
                                    so all three diff outcomes are covered by one check
  (c) cmd_approve, via cli._refresh_caches_from_plan_path — the in-place edit the
                                    plan-review cycle makes at plan-mutable PLAN_READY

At (c) the refusal MUST reach the caller as data, never as a raised PlanError: approve is
the point where the plan_approval gate is armed, and an exception escaping there wedges the
session at PLAN_READY with an armed gate and no edge back. `submission_violations` is
therefore the primary entry point and returns a LIST — every violation at once, so one
round trip shows the author everything to fix. `validate_submission` is the raising wrapper
for callers that want fail-fast.

WHAT IS NOT CHECKED HERE, DELIBERATELY. `Subject.material_refs` and `Subject.knowledge_refs`
divide the symbols a stage touches into what it TRANSFORMS and what it RELIES ON and leaves
alone. A symbol in both is a smell the plan must justify in prose — and it stays prose, not
a refusal, for three reasons: (1) no element of the order asks for the disjointness, so
enforcing it would be the engine inventing a requirement nobody set; (2) the check would be
a set intersection over names written at whatever granularity the author found useful
(`cli.py`, `cli.cmd_approve`, `cmd_approve`), so it would fire on coincidental spellings and
miss real overlaps — punishing the precise author and passing the vague one, the exact
inversion of what a gate is for; (3) it is a relation BETWEEN the values of two fields,
which is outside the domain of both enumerators that exist here — one asks whether a field
is declared, the other whether a value is in a closed vocabulary — so it would have to be a
third, one-off rule with no home.
"""
from __future__ import annotations

# Required of a SUBSTANTIVE stage at submission. Kept as a table rather than inline `if`s so
# a later stage extending the submission grade adds a row, and so a test can enumerate the
# requirement set from the code instead of restating it.
#
#   attribute      — dotted path from the Stage
#   label          — the TOML key an author writes
#   supplied_by    — the Supply.element that fills this place from an EARLIER stage, or None
#                    when the place has no supply form
_SUBSTANTIVE_SUBMISSION_FIELDS = (
    ("knowledge", "knowledge", "knowledge"),
    ("subject.material_refs", "material_refs", None),
    ("subject.knowledge_refs", "knowledge_refs", None),
)

_WHY = {
    "knowledge": (
        "what must already be known for the declared method over the declared means to "
        "reach the result image — a place of its own, not a restatement of the principle "
        "or the means (or supply it from an earlier stage with "
        "[[stage.supplies]] element = \"knowledge\")"
    ),
    "material_refs": (
        "the symbols this stage TRANSFORMS — the structural projection of `material`, so "
        "the plan states what it changes rather than only describing it (an empty list "
        "reads the same as an absent key here: a substantive stage that transforms "
        "nothing is not a case this grade admits)"
    ),
    "knowledge_refs": (
        "the symbols this stage RELIES ON and leaves alone — read to be understood, not "
        "rewritten (an empty list reads the same as an absent key here: a substantive "
        "stage that relies on nothing is not a case this grade admits)"
    ),
}


def _attr(obj, dotted: str):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


_UNDECLARED = (
    "[meta] weight_class is not declared, but this session was classified SUBSTANTIVE. "
    "The substantive plan grade is keyed on the PLAN's declaration (here and in "
    "plan._validate_substantive_stage alike), so an undeclared plan silently escapes both "
    "— declare weight_class = \"substantive\" and meet the grade, or declare the class "
    "this plan really is"
)


def _is_substantive(doc) -> bool:
    """Keyed on the PLAN's own [meta] weight_class, not the session's, so this function
    stays pure and session-free — and so it agrees with `plan._validate_substantive_stage`,
    the sibling enumerator of substantive-stage requirements, which keys the same way. A
    plan that does not declare itself substantive is not held to the substantive grade by
    either of them.

    Deliberately NOT widened to "or the session is substantive". That reading closes the
    same escape `_undeclared_weight_class` closes below, but it splits the substantive
    grade in half: this seam would arm on the session while
    `plan._validate_substantive_stage` — which cannot move, because it lives on the loader
    path this module exists to keep lenient — would still arm on the plan. One grade, two
    arming conditions, is a worse disagreement than the one being removed, so the plan
    stays the single key and the session's only power is to force it to speak."""
    wc = doc.meta.weight_class
    return wc is not None and wc.lower() == "substantive"


def _undeclared_weight_class(doc, session_weight_class: str | None) -> bool:
    """A SUBSTANTIVE session whose plan declares no weight_class at all.

    This is the escape that made the whole grade author-opt-in: `weight_class` is optional,
    so a plan omitting it met neither this seam's requirements nor
    `plan._validate_substantive_stage`'s, while the gate right beside seam (a)
    (`verify_command_reachability_blockers`) armed on the SESSION regardless. Silence is
    what the two adjacent gates disagreed about, so silence is what is refused — a plan
    that declares a non-substantive class is making a claim an author can be held to, and
    `git grep weight_class` finds it.

    The session's class arrives as a VALUE, not a SessionState, so the seam stays a pure
    function of its arguments; `None` — every caller that has no session — is a no-op."""
    return (
        doc.meta.weight_class is None
        and session_weight_class is not None
        and session_weight_class.lower() == "substantive"
    )


def submission_violations(doc, *, session_weight_class: str | None = None) -> list[str]:
    """Every submission-grade violation in `doc`, as author-readable strings. [] == clean.

    Returns rather than raises: the approve seam must answer with a Directive (see the
    module docstring), and returning the full list lets one round trip show everything.
    """
    if _undeclared_weight_class(doc, session_weight_class):
        return [_UNDECLARED]
    if not _is_substantive(doc):
        return []
    out: list[str] = []
    for stage in doc.stages:
        # Supplies are already built by the time a PlanDoc exists, so the "supplied by an
        # earlier stage" alternative is decidable here — evaluating the knowledge
        # requirement against the parsed doc rather than the raw TOML table is what makes
        # an incoming `knowledge` edge count as filling the place.
        supplied = {s.element for s in stage.supplies if s.element}
        for dotted, label, supply_element in _SUBSTANTIVE_SUBMISSION_FIELDS:
            if _attr(stage, dotted):
                continue
            if supply_element is not None and supply_element in supplied:
                continue
            out.append(
                f"stage {stage.index} missing {label!r} (required for substantive plans): "
                f"{_WHY[label]}"
            )
    return out


def validate_submission(doc, *, session_weight_class: str | None = None) -> None:
    """Raise PlanError on the first submission-grade violation. The fail-fast wrapper for
    callers that have no Directive to answer with; the approve seam uses
    `submission_violations` instead, and must keep doing so."""
    from .plan import PlanError

    problems = submission_violations(doc, session_weight_class=session_weight_class)
    if problems:
        raise PlanError(problems[0])
