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

The seam also has a second, non-refusing channel: `submission_advice` returns what it has
to SAY about a plan it is letting through. Not every submission-time finding is worth a
refusal — see that function for why the echo check in particular is one of them — and a
seam that could only raise would have had to either block on such a finding or drop it.

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

from .result_image import echo_prefilter, judge_echo
from .state import WeightClass
from .text_shape import normalize_string as _normalize_string

# The closed vocabulary a plan's [meta] weight_class may name, spelled as an author writes
# it in TOML. Derived from the session-side enum rather than restated, so the plan's
# declaration and the session's classification cannot drift into two vocabularies.
_WEIGHT_CLASSES = tuple(wc.value.lower() for wc in WeightClass)

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
    stays the single key and the session's only power is to force it to speak.

    Normalized rather than `.lower()`-ed on purpose, and that choice is load-bearing in
    BOTH directions: it is what makes `"substantive "` substantive here but not at the
    loader-side enumerator, and the values that would make the two disagree are therefore
    refused outright by `_malformed_weight_class` below. Simplifying this back to
    `.lower()`, or dropping that refusal, re-opens the split — they are one decision."""
    wc = doc.meta.weight_class
    return wc is not None and _normalize_string(wc) == "substantive"


def _malformed_weight_class(doc) -> str | None:
    """A DECLARED weight_class this seam cannot read as the closed vocabulary's member.

    The declaration is the sole load-bearing key of the whole substantive grade, so a value
    outside the vocabulary is not a non-substantive plan — it is a plan whose class nobody
    stated, and a typo is nobody's claim. `_undeclared_weight_class` cannot catch it: the
    key is present.

    Two shapes are refused, and the second one is refused for a reason that is entirely
    about the OTHER enumerator. `plan._validate_substantive_stage` arms on the same key
    through a bare `.lower()` (`parse_plan`'s `is_substantive` local), and it cannot move —
    it lives on the loader path this module exists to keep lenient. So the set they arm on
    is kept identical by refusing here exactly what they would disagree about: `.lower()`
    absorbs case, so `"SUBSTANTIVE"` is left alone; it does not absorb whitespace, so
    `"substantive "` — which this seam would grade and the loader would not — is refused
    rather than silently splitting one grade into two arming conditions.

    Returns the author-readable violation, or None when the value is fine (or absent, which
    is `_undeclared_weight_class`'s case, not this one)."""
    raw = doc.meta.weight_class
    if raw is None:
        return None
    norm = _normalize_string(raw)
    if norm not in _WEIGHT_CLASSES:
        return (
            f"[meta] weight_class = {raw!r} is not one of "
            f"{'/'.join(repr(w) for w in _WEIGHT_CLASSES)}. The substantive plan grade is "
            "keyed on this declaration, so an unreadable value escapes the grade exactly "
            "as silence would — declare the class this plan really is"
        )
    if raw.lower() != norm:
        return (
            f"[meta] weight_class = {raw!r} carries whitespace. This seam normalizes it "
            f"and plan._validate_substantive_stage — which compares the raw string and "
            f"cannot move off the lenient loader path — does not, so the two halves of "
            f"one grade would arm on different plans: write it as {norm!r}"
        )
    return None


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
    out: list[str] = []
    malformed = _malformed_weight_class(doc)
    if malformed:
        out.append(malformed)
    undeclared = _undeclared_weight_class(doc, session_weight_class)
    if undeclared:
        out.append(_UNDECLARED)
    # The field loop also runs for the UNDECLARED case, where the plan itself never claimed
    # to be substantive. That is not the seam arming on the session — the refusal is still
    # the missing declaration, and a plan that answers it with a non-substantive class keeps
    # every field violation below moot. It is the module's one-round-trip contract: the
    # author whose plan the session will hold to the substantive grade sees the declaration
    # AND the places it will require in one answer, instead of one round trip per layer.
    if not (_is_substantive(doc) or undeclared):
        return out
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


def submission_advice(doc, *, judge_runner=None, judge_enabled: bool = True) -> list[str]:
    """Everything this seam has to SAY about `doc` without refusing it. [] == nothing.

    Advice is a strictly separate channel from `submission_violations`, because the two
    have opposite failure directions. A violation is a claim the engine can make from the
    plan's own bytes, so it refuses. An echo verdict — a stage whose expected_result_image
    merely restates the check that judges it — rests on a model's reading of what a
    sentence MEANS, and the corpus this was calibrated against says the defect is rare (11
    images in 200). Refusing a submission on a judgement that shaky would spend an author's
    round trip on a coin flip, so an echo NEVER refuses: it names the stage, says what its
    image restates, and the submission proceeds.

    Fail-open throughout: no runner, a disabled judge, or a judge that errors yields no
    advice at all. Silence here is indistinguishable from the feature being absent.
    """
    if not judge_enabled or judge_runner is None:
        return []
    out: list[str] = []
    for stage in doc.stages:
        image = (stage.subject.result if stage.subject else "") or ""
        command = (stage.criterion.verify_command or "") if stage.criterion else ""
        reasons = echo_prefilter(image, verify_command=command)
        if not reasons:
            continue
        if not judge_echo(
            image,
            judge_runner,
            verify_command=command,
            done_criterion=(stage.criterion.done_criterion if stage.criterion else ""),
        ):
            continue
        out.append(
            f"stage {stage.index} ({stage.title!r}): expected_result_image restates the "
            f"stage's own check — {'; '.join(reasons)}. Say what the stage's result IS, "
            f"so a reader who has not run the check learns what now exists"
        )
    return out


def validate_submission(
    doc,
    *,
    session_weight_class: str | None = None,
    judge_runner=None,
    judge_enabled: bool = True,
) -> list[str]:
    """Raise PlanError on the first submission-grade violation; otherwise RETURN the advice.

    The fail-fast wrapper for callers that have no Directive to answer with; the approve
    seam uses `submission_violations` instead, and must keep doing so. The return value is
    the advice channel (`submission_advice`) — a caller that ignores it, and any call that
    passes no judge, behaves exactly as before: [] is the default answer.

    `judge_runner`/`judge_enabled` are a DELIBERATE API completion with no production
    caller today: all three engine seams reach `submission_advice` directly (via
    cli._submission_advice) because they hold the Directive this wrapper is defined not to
    have. They exist so this wrapper cannot become the one entry point that silently drops
    the advice channel, and the tests are their only exercise. Delete them if a caller
    never appears, rather than growing a second convention around them."""
    from .plan import PlanError

    problems = submission_violations(doc, session_weight_class=session_weight_class)
    if problems:
        raise PlanError(problems[0])
    return submission_advice(doc, judge_runner=judge_runner, judge_enabled=judge_enabled)
