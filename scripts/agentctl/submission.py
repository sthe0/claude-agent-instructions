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

Both channels can ask a MODEL, and neither depends on getting an answer. The two differ
only in what a verdict buys: an echo warns (`submission_advice`), while a `conditions`
that merely restates `depends_on` (`_conditions_restatement`) and a `procedure` that
merely restates `method` (`_procedure_collapse`) refuse. Every path where the judge is
absent, disabled, slow or unreadable yields the same thing in both — nothing — so an
engine running without a reachable advisor validates plans exactly as it did before any
of these checks existed.

WHAT IS NOT CHECKED HERE, DELIBERATELY. `Subject.material_refs` and `Subject.knowledge_refs`
divide the symbols a stage touches into what it TRANSFORMS and what it RELIES ON and leaves
alone. A symbol in both is a smell the plan must justify in prose — and it stays prose, not
a refusal, for three reasons: (1) no element of the order asks for the disjointness, so
enforcing it would be the engine inventing a requirement nobody set; (2) the check would be
a set intersection over names written at whatever granularity the author found useful
(`cli.py`, `cli.cmd_approve`, `cmd_approve`), so it would fire on coincidental spellings and
miss real overlaps — punishing the precise author and passing the vague one, the exact
inversion of what a gate is for; (3) it is a relation BETWEEN the values of two fields,
which is outside the domain of every enumerator here — one asks whether a field is declared,
one whether an edge states what it provides, one whether a value is in a closed vocabulary
— so it would have to be a one-off rule with no home. Reasons (1) and (2) are the
load-bearing ones and neither weakens as enumerators are added.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from . import conditions_securing, exempt_paths, grant_shadow, sub_plan
from . import grants as _grants
from .conditions import judge_restatement, restatement_prefilter
from .plan import (
    _declared_absolute_venue, _remote_head_for, is_two_sided, venue_declaration_blockers,
)
from .plan_resources import RESOURCE_REQUEST_KINDS, plan_time_resource_set, uncovered_request_entries
from .procedure import collapse_prefilter, judge_collapse
from .result_image import echo_prefilter, judge_echo
from .state import (
    AGENT_ACTOR, CHECK_KINDS, CHECK_MEDIATED, ORDER_CRITERION_TYPES, PROPOSAL_VERDICTS,
    TYPE_ENTRY_KINDS, TYPE_ENTRY_NONE, CheckKind, CheckVenue, CriterionType, WeightClass,
    asserts_landing,
)
from .text_shape import ELEMENT_NAMES
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
    ("preconditions", "preconditions", None),
    ("means.method", "method", None),
    ("means.procedure", "procedure", None),
)

# `knowledge` is what the declared method over the declared means must already know (see
# `_WHY`), so on a stage description, which declares neither, it is the executor's too.
_EXECUTOR_OWNED_PATHS = frozenset({"means.method", "means.procedure", "knowledge"})

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
    "goal": (
        "what this plan is for, in one sentence. The engine caches it as the session's "
        "goal and every observation of a result is compared against it"
    ),
    "done_criterion": (
        "the plan-level result image — what will be true when this plan is finished. "
        "Without it the resolution gate has nothing to hold the delivery to"
    ),
    "final_check": (
        "at least one [[final_check]] table. A plan whose only controls are per-stage "
        "asserts that each step went as declared and nothing about the whole; the "
        "end-to-end checks are what verify-final re-runs against the assembled product"
    ),
    "method": (
        "the REQUIREMENT on the way of acting — what this stage's transformation must be "
        "an instance of (the pattern to follow, the abstraction to extend, where the "
        "change lands). Already required by the loader for a plan that declares itself "
        "substantive; required here too because this seam also grades the plan that "
        "declares NO class while the session is substantive, and there the loader's "
        "requirement never armed"
    ),
    "procedure": (
        "the SEQUENCE of operations proposed for meeting that requirement — the ordered "
        "sub-actions, each naming the file or symbol it touches. A place of its own "
        "because it is the executor's to replace (agentctl replan --renormalize) while "
        "`method` is not: with one field for both, whatever is written there is either a "
        "norm anyone may quietly rewrite or a proposal the executor is held to"
    ),
    "preconditions": (
        "what must already be true before this stage may START — an access, a clean tree, "
        "an earlier stage's artifact in place. A separate place from `conditions`, which "
        "is what must hold of the WORLD for this stage's own transformation to go "
        "through; with one field for both, `conditions` degenerates into \"the stage "
        "before me is done\", which `depends_on` already records"
    ),
}


# Required of a SUBSTANTIVE plan's [meta] table itself — the plan-level sibling of
# `_SUBSTANTIVE_SUBMISSION_FIELDS` above, and a separate table because these range over
# `doc.meta` once rather than over every stage. Falsiness is the test throughout, so an
# empty string and an empty [[final_check]] list read the same as an absent key.
#
#   attribute — dotted path from PlanMeta
#   label     — the TOML key an author writes (and the `_WHY` key)
_SUBSTANTIVE_META_FIELDS = (
    ("goal", "goal"),
    ("done_criterion", "done_criterion"),
    ("final_check", "final_check"),
)

# The parts of `[meta.order]` a substantive plan must fill, each paired with why. Every
# field of `state.Order` is either a row here, or `coverage` — whose requirement is a
# relation rather than a presence and is checked separately below — or `malformed`, which
# no plan author writes at all. That totality is not left to whoever edits this tuple:
# test_meta_order.py parametrizes its refusal cases over `dataclasses.fields(Order)`
# itself, against a declared exemption set, so a part added to the type and not covered
# here turns red rather than shipping unrequired.
_ORDER_PARTS = (
    (
        "customer_id",
        "the machine-comparable identifier of the position the order came from. The "
        "identifier half of the customer pair, because an acceptance author is checked "
        "against it — comparing an author against a paragraph is either vacuous or absurd",
    ),
    (
        "customer",
        "the position that identifier names, in prose. The other half of the pair: "
        "customer_id is what a machine compares, this is what a reader needs to know "
        "whose requirements these are",
    ),
    (
        "functional_place",
        "the place this plan's product fills, and in what respect its current filling is "
        "inadequate — a need is a functional place stripped of adequate filling, which is "
        "why the two are one field and not two",
    ),
    (
        "requirements",
        "the requirements on the product, as { id = \"R1\", text = \"...\" } PAIRS. The id "
        "is the key the coverage map and every acceptance verdict range over; a list of "
        "bare sentences leaves the load-bearing key as prose someone has to parse back out",
    ),
)

# What each malformable part must LOOK like. Only the container parts appear: the
# scalars are `str(...)`-coerced by Order.from_dict, so whatever was written survives in
# some readable form and there is nothing to have dropped. `order` itself (the raw
# `[meta]` key, not a part of the order) rides here too — a present, non-dict `order`
# degrades to `Order(malformed=("order",))` in plan.parse_plan, and is reported through
# this same register rather than as a missing table.
_ORDER_SHAPE_HINTS = {
    "order": (
        "a table — `[meta.order]` with customer_id, customer, functional_place, "
        "requirements, and a [meta.order.coverage] table. A scalar value, or "
        "`[[meta.order]]` written as an array of tables, reads as no order at all"
    ),
    "requirements": (
        "an array of tables — `requirements = [ { id = \"R1\", text = \"...\" } ]`, or "
        "repeated [[meta.order.requirements]] sections. A list of bare sentences, a single "
        "[meta.order.requirements] table, or a string all read as no requirement at all"
    ),
    "coverage": (
        "a table of requirement id -> controls — a [meta.order.coverage] section carrying "
        "one `R1 = [\"stage 1 verify_command\"]` line per declared requirement"
    ),
    "proposals": (
        "an array of tables — repeated [[meta.order.proposals]] sections, each "
        "{ id = \"P1\", text = \"...\", verdict = \"adopt|adapt|reject\", critique = \"...\", "
        "against = [\"R1\"] }. A bare sentence among the tables, a single table, or a "
        "string reads as no proposal"
    ),
    "constraints": (
        "an array of tables — repeated [[meta.order.constraints]] sections, each "
        "{ id = \"K1\", text = \"...\" }. A bare sentence among the "
        "tables, a single table, or a string reads as no constraint"
    ),
}

# `order` lives one level up from every other malformable name here: the raw key sits
# directly under `[meta]`, not under `[meta.order]` (which, for a malformed `order`,
# never existed as a table at all). Every other name is a PART of the order and lives
# under `[meta.order]`.
_ORDER_MALFORMED_LOCATION = {"order": "[meta]"}


def _order_malformed(name: str, *, requirements_dropped: tuple[int, int] | None = None) -> str:
    """The message for a part the raw table CARRIED but `Order.from_dict` could not read.

    Separate from the missing-part message because it names a different defect and needs a
    different repair: the key is there, its shape is wrong, and telling that author to
    'add requirements' sends them to write again what is already written.

    `requirements_dropped`, when given (only ever for name="requirements"), distinguishes
    the PARTIAL case — some entries were tables and survived, some were not — from the
    TOTAL case the shape-hint sentence below is true of: an author with four good
    requirements and one bare sentence is told exactly which entries were dropped, not
    that "the engine read nothing usable from it", which is false of their plan and sends
    them looking for a wholly-broken key instead of the one bad element."""
    location = _ORDER_MALFORMED_LOCATION.get(name, "[meta.order]")
    if name == "requirements" and requirements_dropped is not None:
        dropped, total = requirements_dropped
        return (
            f"{location} {dropped} of {total} entries under 'requirements' are not "
            f"tables and were dropped. Write each as {{ id = \"R1\", text = \"...\" }} — "
            f"the coverage map and every acceptance verdict key on the id, so a bare "
            f"sentence among the tables is silently lost rather than read"
        )
    return (
        f"{location} {name!r} is present but is not {_ORDER_SHAPE_HINTS[name]}. The "
        f"engine read nothing usable from it, so the key is present and its content is "
        f"absent — fix the shape rather than adding the key again"
    )


_ORDER_ABSENT = (
    "[meta] missing the 'order' table (required for substantive plans): a plan is an "
    "answer to an order, and with the order unstated there is nothing for the product to "
    "be accepted against. Declare [meta.order] with customer_id, customer, "
    "functional_place, requirements as { id, text } pairs, and a [meta.order.coverage] "
    "table mapping each requirement id to the controls that decide it"
)

# The literal fragments an order-related refusal message can contain. Exported so a test
# asserting "a strict LOAD never raises about the order" (order refusals live only here,
# at the submission seam, never in plan.parse_plan) binds to this module's real messages
# rather than to a second, hand-typed copy in the test file that could drift the day one
# of the messages above is reworded.
ORDER_REFUSAL_MARKERS = ("[meta.order]", "[meta] 'order'", "the 'order' table")


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


def is_substantive(doc) -> bool:
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

    The one deliberate exception is the venue declaration (`plan.venue_declaration_blockers`),
    which `submission_violations` keys on the session's class alone: it is not a field of
    the substantive grade but the precondition of every verdict the engine reaches by
    running or probing a path, and `verify_command_reachability_blockers` is armed on the
    session the same way.

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


def _requirement_violations(order) -> list[str]:
    """The id-less, textless and duplicated-id requirements of `order` — the part of the
    order's grade that is about the requirements alone, shared by `_order_violations` and
    the completeness check of an agreed understanding."""
    out: list[str] = []
    unnamed = [i for i, r in enumerate(order.requirements, start=1) if not r.id]
    if unnamed:
        out.append(
            f"[meta.order] requirement(s) at position {', '.join(str(i) for i in unnamed)} "
            f"carry no 'id'. Write each as {{ id = \"R1\", text = \"...\" }} — the coverage "
            f"map and every acceptance verdict key on the id, so an id-less requirement "
            f"cannot be covered or accepted at all"
        )
    textless = sorted(
        {r.id for r in order.requirements if r.id and not _normalize_string(r.text)}
    )
    if textless:
        out.append(
            f"[meta.order] requirement(s) {', '.join(textless)} carry an id but no "
            f"'text'. An id names WHICH requirement; the text is the requirement itself "
            f"— an id with nothing to accept against is a coverage-map key standing in "
            f"for a requirement the plan never actually states"
        )
    declared = [r.id for r in order.requirements if r.id]
    duplicated = sorted({rid for rid in declared if declared.count(rid) > 1})
    if duplicated:
        out.append(
            f"[meta.order] requirement id(s) {', '.join(duplicated)} are declared more "
            f"than once. Ids must be unique: the coverage map and every acceptance verdict "
            f"are keyed by id, so two requirements sharing one hide behind a single "
            f"coverage entry and a single verdict — the second is accepted without ever "
            f"having been decided"
        )
    return out


def _order_violations(meta) -> list[str]:
    """Every way `[meta.order]` fails the substantive grade. [] == clean.

    Presence checks first, then the four that are relations rather than presences:

      * no order table at all — the plan states no requirements to be accepted against;
      * a missing PART of the order (`_ORDER_PARTS`), including both halves of the
        customer pair, since the identifier and the position it names do different jobs —
        unless `Order.from_dict` recorded the part as MALFORMED, in which case the key is
        present and only its shape is wrong, and saying 'missing' would name a defect the
        author does not have;
      * an id-less requirement, refused separately from an uncovered one because it is a
        strictly worse case: an entry with no id is not merely uncovered, it is
        uncoverABLE — the coverage map, the acceptance verdicts and this check all key on
        the id, so a requirement without one cannot be named by any of them;
      * an id with no 'text' — an id names which requirement, the text is the
        requirement itself, so an empty one is a coverage-map key with nothing behind
        it. Refused here, unconditionally, rather than left to `_requirement_derivation_
        violations` to notice: that check is gated on `order.requires_traceability` (see
        `Order.requires_traceability` in state.py) and skips a textless requirement
        outright, so an ungated presence check is what actually catches it on every plan;
      * a DUPLICATED requirement id, refused for the same reason one step on: the id is
        the key, so two requirements sharing one collapse into a single coverage entry and
        a single acceptance verdict, and the second rides in accepted without ever having
        been decided;
      * a requirement the coverage map says nothing about, and a coverage key naming no
        declared requirement — the two directions of the same totality. It is deliberately
        the weaker of the two things a reader might expect: the resolver checks that every
        declared id HAS an entry, never that the control the entry names actually decides
        the requirement. Sufficiency is review — an entry can be wrong, and nothing
        mechanical will say so.

    Pure over `doc.meta`, like every other enumerator in this module, and it never reads
    the ORDER's own truth: whether these are the right requirements is the customer's
    question, not the engine's."""
    order = meta.order
    if order is None:
        return [_ORDER_ABSENT]
    if "order" in order.malformed:
        # The raw `order` key itself was unreadable (a scalar, or an array of tables) —
        # `Order.from_dict` never ran, so every other part is unpopulated by construction
        # and would otherwise each report "missing" on top of this. One message, naming
        # the actual defect, replaces what would else be one per _ORDER_PARTS entry.
        return [_order_malformed("order")]
    out: list[str] = []
    for name, why in _ORDER_PARTS:
        if name in order.malformed:
            out.append(_order_malformed(name, requirements_dropped=order.requirements_dropped))
        elif not getattr(order, name):
            out.append(
                f"[meta.order] missing {name!r} (required for substantive plans): {why}"
            )
    if "coverage" in order.malformed:
        # Not in _ORDER_PARTS — coverage is graded as a relation below, so its shape has
        # no presence check to hang off and needs its own line here.
        out.append(_order_malformed("coverage"))
    out.extend(_requirement_violations(order))
    declared = [r.id for r in order.requirements if r.id]
    if order.customer_side is not None:
        declared.extend(c.id for c in order.customer_side.checks if c.id)
    uncovered = sorted({i for i in declared if not order.coverage.get(i)})
    if uncovered:
        out.append(
            f"[meta.order.coverage] says nothing about {', '.join(uncovered)}. Every "
            f"declared requirement needs an entry naming the control that decides it "
            f"(a stage's verify_command, a final_check); an uncovered requirement is one "
            f"the plan asks to be accepted on without saying what would establish it"
        )
    stray = sorted(set(order.coverage) - set(declared))
    if stray:
        out.append(
            f"[meta.order.coverage] names {', '.join(stray)}, which no requirement "
            f"or acceptance check declares. A key matching no id covers nothing — most often a requirement id "
            f"renamed on one side only, which leaves the real requirement uncovered while "
            f"the map still looks full"
        )
    if order.customer_id and order.customer_id.strip().casefold() == AGENT_ACTOR:
        out.append(
            f"[meta.order] customer_id={order.customer_id!r} is the reserved engine actor "
            f"identity ({AGENT_ACTOR!r}). The order-approvals ledger (order_approvals.py) "
            f"only ever stamps a record for a CUSTOMER-authored approve; a plan naming the "
            f"engine as its own customer would let the engine's self-grant path read its "
            f"own approval back as if a real customer had given it"
        )
    return out


def _invalid_verdicts(proposals) -> list[str]:
    """The ids of proposals whose verdict is outside PROPOSAL_VERDICTS. A proposal without
    an id is reported by position, as `_order_understanding_violations` does for ids.
    A module-level function so the `verdict_unchecked` test mutant can replace it."""
    return [
        p.id or f"#{i}"
        for i, p in enumerate(proposals, start=1)
        if p.verdict not in PROPOSAL_VERDICTS
    ]


def _ambiguous_ids(order) -> list[str]:
    """Ids that name more than one thing across requirements, proposals and constraints.
    The user agrees by id and `against` points at requirement ids, so an id shared by a
    requirement and a proposal is ambiguous even though each kind is keyed apart in the
    digest. A requirement id repeated only among requirements is `_order_violations`'s
    finding, not reported twice."""
    kinds = [("R", r.id) for r in order.requirements] + [
        ("P", p.id) for p in order.proposals] + [("K", c.id) for c in order.constraints]
    ids = [i for _, i in kinds if i]
    return sorted({
        i for i in ids
        if ids.count(i) > 1 and {k for k, j in kinds if j == i} != {"R"}
    })


def _order_understanding_violations(meta) -> list[str]:
    """Every way the understanding tables of `[meta.order]` are ill-formed. [] == clean.

    Grades only what is PRESENT: an order that states no proposal and no constraint is
    clean here, because whether the understanding is complete is a separate question
    (`order_understanding_completeness_violations`) that only the call asking for an
    agreed understanding puts. Well-formedness binds at every substantive submission for
    the reason every part of the order does — a table written wrong is silently
    misread, and the seam is where the author can be told which entry and why.

    A proposal is { id, text, verdict, critique, against } with the verdict one of
    PROPOSAL_VERDICTS, a critique for it, and the requirements it stands against: a verdict
    with no criticism behind it is the user's idea waved through or waved off, and a
    critique naming no requirement cannot be checked against what the order asks. A
    constraint is { id, text }. Ids are unique across requirements, proposals and
    constraints. Pure over `meta`, like `_order_violations`, whose malformed-shape
    register it reuses."""
    order = meta.order
    if order is None or "order" in order.malformed:
        return []
    out: list[str] = []
    for name in ("proposals", "constraints"):
        if name in order.malformed:
            out.append(_order_malformed(name))
    unnamed = [i for i, p in enumerate(order.proposals, start=1) if not p.id]
    if unnamed:
        out.append(
            f"[meta.order] proposal(s) at position {', '.join(str(i) for i in unnamed)} carry "
            f"no 'id'. Write each as {{ id = \"P1\", text = \"...\", verdict = \"adopt\", "
            f"critique = \"...\", against = [\"R1\"] }} — the user agrees a proposal by its id"
        )
    untexted = sorted({p.id for p in order.proposals if p.id and not _normalize_string(p.text)})
    if untexted:
        out.append(
            f"[meta.order] proposal(s) {', '.join(untexted)} carry an id but no 'text': "
            f"state the user's idea, or there is nothing for the verdict to be about"
        )
    bad_verdicts = _invalid_verdicts(order.proposals)
    if bad_verdicts:
        out.append(
            f"[meta.order] proposal(s) {', '.join(bad_verdicts)} carry a verdict outside "
            f"{'/'.join(PROPOSAL_VERDICTS)}: each of the user's ideas is adopted as stated, "
            f"adapted into something else, or rejected — a verdict of any other kind decides "
            f"nothing about the idea"
        )
    uncriticized = sorted({p.id for p in order.proposals if p.id and not _normalize_string(p.critique)})
    if uncriticized:
        out.append(
            f"[meta.order] proposal(s) {', '.join(uncriticized)} carry no 'critique': a "
            f"verdict without the criticism behind it cannot be agreed or disputed"
        )
    declared = {r.id for r in order.requirements if r.id}
    unanchored = sorted({p.id for p in order.proposals if p.id and not p.against})
    if unanchored:
        out.append(
            f"[meta.order] proposal(s) {', '.join(unanchored)} carry no 'against': name the "
            f"requirement id(s) the critique stands on, as against = [\"R1\"]"
        )
    stray = sorted({
        f"{p.id or '#' + str(i)}->{a}"
        for i, p in enumerate(order.proposals, start=1)
        for a in p.against if a not in declared
    })
    if stray:
        out.append(
            f"[meta.order] proposal 'against' entries name no declared requirement: "
            f"{', '.join(stray)}. A critique resting on an id the order does not declare "
            f"rests on nothing"
        )
    unnamed_c = [i for i, c in enumerate(order.constraints, start=1) if not c.id]
    if unnamed_c:
        out.append(
            f"[meta.order] constraint(s) at position {', '.join(str(i) for i in unnamed_c)} "
            f"carry no 'id'. Write each as {{ id = \"K1\", text = \"...\" }}"
        )
    textless = sorted({c.id for c in order.constraints if c.id and not _normalize_string(c.text)})
    if textless:
        out.append(
            f"[meta.order] constraint(s) {', '.join(textless)} carry an id but no 'text': "
            f"an id with nothing stated is a constraint the plan never actually names"
        )
    ambiguous = _ambiguous_ids(order)
    if ambiguous:
        out.append(
            f"[meta.order] id(s) {', '.join(ambiguous)} name more than one requirement, "
            f"proposal or constraint. Ids must be unique across all three: the user's "
            f"agreement is recorded per id, and 'against' points at requirement ids"
        )
    return out


order_understanding_violations = _order_understanding_violations


_UNDERSTANDING_PARTS = (
    (
        "activity",
        "what the user is doing, in the user's terms — the activity this order serves, so "
        "the product is judged against it and not against the requirements alone",
    ),
    (
        "difficulty",
        "the divergence between the state the user wants and the state they have, which "
        "this order exists to remove",
    ),
)


def order_understanding_completeness_violations(meta) -> list[str]:
    """Every way the understanding of the order is not yet COMPLETE enough to put to the
    user for agreement. [] == complete.

    Exported for the call that asks for an agreed understanding (stage 2's order-submit);
    the submission seam does not call it, because a plan may carry an order that states
    no understanding at all and still be well-formed. Complete means: an order exists; it
    states the customer pair, the functional place, its activity and its difficulty, and
    at least one requirement, each requirement with an id, a text and no repeated id; and
    what it states about proposals and constraints is well-formed
    (`_order_understanding_violations`). The coverage map, `goal` and `done_criterion`
    are the agent's answer to the order, not part of its understanding, and are not asked
    for. Complete implies well-formed, so a caller needs only this one function."""
    order = meta.order
    if order is None:
        return [_ORDER_ABSENT]
    if "order" in order.malformed:
        return [_order_malformed("order")]
    out: list[str] = []
    for name, why in (*_ORDER_PARTS, *_UNDERSTANDING_PARTS):
        if name in order.malformed:
            out.append(_order_malformed(name, requirements_dropped=order.requirements_dropped))
        else:
            value = getattr(order, name)
            if not (_normalize_string(value) if isinstance(value, str) else value):
                out.append(
                    f"[meta.order] missing {name!r} (required of an agreed understanding): {why}"
                )
    out.extend(_requirement_violations(order))
    if not order.requires_traceability:
        out.append(
            "[meta.order] requires_traceability is not true (required of an agreed "
            "understanding): every requirement must be traced to the difficulty it is "
            "derived from, and the traceability grade is gated on this flag"
        )
    out.extend(_underived_requirements(order))
    out.extend(_order_understanding_violations(meta))
    return out


def _underived_requirements(order) -> list[str]:
    """The named, texted requirements of `order` with no derivation — ungated, unlike
    `_requirement_derivation_violations`, whose `requires_traceability` gate would let an
    understanding that also lacks the flag pass as complete."""
    return [
        f"[meta.order] requirement {r.id} has no derivation (required of an agreed "
        f"understanding): state how it follows from the difficulty"
        for r in order.requirements
        if r.id and _normalize_string(r.text) and not _normalize_string(r.derivation)
    ]


_SIDES_MALFORMED = (
    "customer_side", "customer_side.checks", "executor_side", "executor_side.type_entry",
    "binding_conditions", "method_ideas", "means_conditions", "planning_budget",
)


def order_customer_side_missing(meta) -> list[str]:
    """The refusal of an order-path draft that has no customer side: an order put to the user
    is new-form, and an order is new-form exactly when its customer side is present."""
    order = meta.order
    if order is None or "customer_side" in order.malformed:
        return []
    if order.customer_side is None:
        return [
            "[meta.order] missing 'customer_side' (required of an order put to the user): "
            "write [meta.order.customer_side] with step, missing, product, criterion_type and "
            "[[meta.order.customer_side.checks]] — the user agrees that side and its acceptance "
            "checks; the requirements, how-built proposals and conditions on the product are the "
            "executor side, shown and not agreed"
        ]
    return []


def _duplicate_ids(ids: list[str]) -> list[str]:
    return sorted({i for i in ids if i and ids.count(i) > 1})


def _customer_side_violations(side) -> list[str]:
    out: list[str] = []
    for name in ("step", "missing", "product"):
        if not _normalize_string(getattr(side, name)):
            out.append(f"[meta.order.customer_side] missing {name!r}")
    if side.criterion_type not in ORDER_CRITERION_TYPES:
        out.append(
            f"[meta.order.customer_side] criterion_type must be one of "
            f"{'/'.join(ORDER_CRITERION_TYPES)}, got {side.criterion_type!r}: the order's "
            f"criterion type decides whether it is accepted by a command or on review"
        )
    if not side.checks:
        out.append("[meta.order.customer_side] states no acceptance check: add "
                   "[[meta.order.customer_side.checks]] with id, kind and text")
    for i, c in enumerate(side.checks, start=1):
        label = c.id or f"#{i}"
        if not c.id:
            out.append(f"[meta.order.customer_side] acceptance check at position {i} has no 'id'")
        if c.kind not in CHECK_KINDS:
            out.append(f"[meta.order.customer_side] acceptance check {label} has kind "
                       f"{c.kind!r}, not one of {'/'.join(CHECK_KINDS)}")
        if not _normalize_string(c.text):
            out.append(f"[meta.order.customer_side] acceptance check {label} has no 'text'")
        if c.kind == CHECK_MEDIATED and not _normalize_string(c.reason):
            out.append(f"[meta.order.customer_side] mediated acceptance check {label} has no "
                       f"'reason': say why observing the stand-in decides the criterion")
    dup = _duplicate_ids([c.id for c in side.checks])
    if dup:
        out.append(f"[meta.order.customer_side] acceptance check id(s) {', '.join(dup)} repeat")
    return out


def _executor_side_shape_violations(order) -> list[str]:
    executor = order.executor_side
    if executor is None:
        return ["[meta.order] missing 'executor_side' (required of a two-sided order): write "
                "[meta.order.executor_side] with a specifics entry and a type_entry"]
    out: list[str] = []
    if not _normalize_string(executor.specifics):
        out.append("[meta.order.executor_side] missing 'specifics': the specifics entry says "
                   "what is particular to this order beside what it has in common with its kind")
    entry = executor.type_entry
    if entry is None:
        out.append("[meta.order.executor_side] missing 'type_entry': name the experience or "
                   "regularity record the order is of a kind with, or the explicit none entry")
    elif entry.kind not in TYPE_ENTRY_KINDS:
        out.append(f"[meta.order.executor_side] type_entry kind {entry.kind!r} is not one of "
                   f"{'/'.join(TYPE_ENTRY_KINDS)}")
    elif entry.kind == TYPE_ENTRY_NONE:
        if not _normalize_string(entry.search_id):
            out.append("[meta.order.executor_side] a none type_entry names the recorded search "
                       "that found no record, as search_id (record it with order-search)")
    elif not _normalize_string(entry.ref):
        out.append(f"[meta.order.executor_side] a {entry.kind} type_entry names its record as 'ref'")
    return out


def _beside_violations(order) -> list[str]:
    out: list[str] = []
    for b in order.binding_conditions:
        label = b.id or "#?"
        for name in ("id", "text", "set_by"):
            if not _normalize_string(getattr(b, name)):
                out.append(f"[meta.order] binding condition {label} has no {name!r}")
        if b.objection_text and not b.objection_by:
            out.append(f"[meta.order] binding condition {label} carries an objection with no "
                       f"'raised_by'")
    dup = _duplicate_ids([b.id for b in order.binding_conditions])
    if dup:
        out.append(f"[meta.order] binding condition id(s) {', '.join(dup)} repeat")
    for i, p in enumerate(order.method_ideas, start=1):
        label = p.id or f"#{i}"
        if not p.id or not _normalize_string(p.text):
            out.append(f"[meta.order] method idea {label} needs an 'id' and a 'text'")
        if p.verdict not in PROPOSAL_VERDICTS:
            out.append(f"[meta.order] method idea {label} has verdict {p.verdict!r}, not one of "
                       f"{'/'.join(PROPOSAL_VERDICTS)}")
    for i, c in enumerate(order.means_conditions, start=1):
        if not c.id or not _normalize_string(c.text):
            out.append(f"[meta.order] means condition #{i} needs an 'id' and a 'text'")
    taken = [r.id for r in order.requirements] + [p.id for p in order.proposals] + [
        c.id for c in order.constraints]
    beside = [p.id for p in order.method_ideas] + [c.id for c in order.means_conditions] + [
        b.id for b in order.binding_conditions]
    clash = sorted({i for i in beside if i and (i in taken or beside.count(i) > 1)})
    if clash:
        out.append(f"[meta.order] id(s) {', '.join(clash)} are used by more than one record of "
                   f"the order, or by a record beside it and a part of it")
    return out


def order_sides_violations(meta) -> list[str]:
    """Every way the two-sided parts of `[meta.order]` are ill-formed. [] == clean, and []
    for an old-form order (no customer side). Pure over `meta`; the checks that need the
    session's state (a type entry resolving to a record) are `cli_order`'s."""
    order = meta.order
    if order is None or "order" in order.malformed:
        return []
    out = [_order_malformed_sides(name) for name in _SIDES_MALFORMED if name in order.malformed]
    if order.customer_side is None:
        return out
    if order.planning_estimate is not None:
        out.append("[meta.order] a two-sided order states its budget in [meta.order.planning_budget]; "
                   "drop the old-form 'planning_estimate', which binds nothing beside it")
    out.extend(_customer_side_violations(order.customer_side))
    out.extend(_executor_side_shape_violations(order))
    out.extend(_beside_violations(order))
    return out


def _order_malformed_sides(name: str) -> str:
    return (f"[meta.order] {name!r} is present but not in the shape the two-sided order reads "
            f"(a table, or a list of tables): fix the shape rather than adding it again")


def _names_requirement_id(rid: str, norm: str) -> bool:
    """Whether normalized text `norm` names requirement id `rid` as a TOKEN, not merely
    as a run of characters some other word happens to contain.

    A plain substring test (`_normalize_string(rid) in norm`) is wrong in both
    directions: `id="R1"` reads as present in `material="dir1/unrelated.py"` (`"r1"` is
    a substring of `"dir1"`) though the material never names the requirement, and a
    single-character id can vanish into ordinary prose the same way. Anchored on
    `[0-9a-z]` rather than `\\b` because `_normalize_string` already casefolds and
    strips to that alphabet plus whitespace/punctuation, so the two boundary classes
    coincide for every normalized id this engine emits."""
    return re.search(
        rf"(?<![0-9a-z]){re.escape(_normalize_string(rid))}(?![0-9a-z])", norm
    ) is not None


def _element_traceability_violations(doc) -> list[str]:
    """Every stage whose declared elements name no order-requirement id. [] == clean.

    Order-traceability is not only the `[stage.principle]` element's job: every element
    that exists because the order asked for something must say which requirement it
    traces to, or it is unaccountable scope. This ranges over the seven prose elements a
    stage actually carries — material/result/invariants, means/method/procedure,
    conditions/preconditions/knowledge, capability_required, done_criterion — joined into
    one normalized blob per stage, so a stage naming its requirement id ANYWHERE among
    them passes; the check is deliberately not per-field, since a plan is free to place
    the traceability note wherever it reads best.

    Structural, not semantic: the vocabulary being matched is the plan author's own
    controlled vocabulary (`[meta.order].requirements[*].id`), not free text being read
    for meaning. See `_requirement_derivation_violations` for the element this is NOT —
    a check over prose content — and why that one is routed to review instead.

    Gated on `order.requires_traceability` (see its docstring in state.py): a check
    that would newly fail every order-bearing plan authored before it existed cannot
    bind unconditionally the way a smaller addition does — it applies only to a plan
    that declares itself authored under the convention it grades.

    An optional stage is exempt: it traces to its `backlog_issue`, not to an order
    requirement (R8), which is the whole reason a user may decline it."""
    order = doc.meta.order
    if (
        order is None
        or "order" in order.malformed
        or not order.requirements
        or not order.requires_traceability
    ):
        return []
    ids = [r.id for r in order.requirements if r.id]
    if not ids:
        return []
    out: list[str] = []
    for stage in doc.stages:
        if stage.optional:
            continue
        blob = " ".join(
            str(part)
            for part in (
                stage.subject.material,
                stage.subject.result,
                stage.subject.invariants or "",
                stage.means.means,
                stage.means.method,
                stage.means.procedure,
                stage.conditions or "",
                stage.preconditions or "",
                stage.knowledge or "",
                stage.actor.capability_required or "",
                stage.criterion.done_criterion,
            )
        )
        norm = _normalize_string(blob)
        if not any(_names_requirement_id(rid, norm) for rid in ids):
            out.append(
                f"stage {stage.index} names no order-requirement id ({', '.join(ids)}) "
                f"anywhere in material/result/invariants/means/method/procedure/"
                f"conditions/preconditions/knowledge/capability_required/done_criterion. "
                f"Order-traceability is not only the [stage.principle] element's job -- "
                f"every element that exists because the order asked for something must "
                f"say which requirement it traces to, or it is unaccountable scope"
            )
    return out


def _requirement_derivation_violations(order) -> list[str]:
    """Every declared requirement whose derivation is empty. [] == clean.

    A requirement's TEXT alone shows only WHAT it says, never HOW it was reached. This
    checks only the STRUCTURAL fact that a derivation exists and carries content — it
    performs no textual-similarity, substring, or edit-distance comparison against
    `order.customer` or against anything else. Whether a given derivation genuinely shows
    problematization (order -> problematization -> reconstruction of the functional
    place -> task), rather than disguised transcription dressed up as reasoning, is not a
    fact recoverable from the derivation's surface form -- it is routed to the mandatory
    thinker review instead. See the module docstring's split of a check into a rule part
    (decidable from form, mechanized here) and a perception part (judgment, left to
    review) -- and `_element_traceability_violations` above for the sibling check this
    deliberately does NOT repeat that split incorrectly.

    Gated on `order.requires_traceability`, for the same reason
    `_element_traceability_violations` is: see that function's docstring and
    `Order.requires_traceability`'s in state.py."""
    if (
        order is None
        or "order" in order.malformed
        or not order.requirements
        or not order.requires_traceability
    ):
        return []
    out: list[str] = []
    for r in order.requirements:
        # An id-less or empty-text requirement is refused by `_order_violations`
        # (id-less: "carry no 'id'"; empty-text: below) -- this loop's own job is
        # grading a requirement that has already cleared presence, not re-refusing
        # one that has not.
        if not r.id or not _normalize_string(r.text):
            continue
        if not _normalize_string(r.derivation):
            out.append(
                f"[meta.order] requirement {r.id} has no derivation -- state how it was "
                f"reached via order -> problematization -> reconstruction of the "
                f"functional place -> task (see [[function-place-difficulty]]); a "
                f"requirement's resulting TEXT alone does not show HOW it was reached, "
                f"only WHAT it says"
            )
    return out


def _resource_request_armed(doc) -> bool:
    order = doc.meta.order
    return order is not None and "order" not in order.malformed and order.requires_resource_request


def _resource_request_violations(doc) -> list[str]:
    """For an armed plan, every request-bearing spawned stage needs its own
    `[[meta.resource_request]]` row (kind + stage), and every entry of a row must be covered
    by what that stage's child already holds: declared + derived grants, the kind baseline
    and the workdir; the coverage rules are `uncovered_request_entries`'. Gated on `order.requires_resource_request`, the
    same opt-in shape as `requires_traceability`: plans written before the request existed
    are never held to it."""
    if not _resource_request_armed(doc):
        return []
    venue = _declared_absolute_venue(doc.meta)
    stages = {s.index: s for s in doc.stages}
    head = _remote_head_for(doc, venue=venue)
    out: list[str] = []
    by_key: dict[tuple, list] = {}
    for row in doc.meta.resource_requests:
        by_key.setdefault((row.kind, row.stage), []).append(row)
        label = f"[[meta.resource_request]] kind={row.kind!r} stage={row.stage!r}"
        out.extend(f"{label}: {d}" for d in row.defects)
        stage = stages.get(row.stage)
        if stage is None:
            if row.stage is not None:
                out.append(f"{label}: no such stage")
            continue
        if stage.spawn_kind() != row.kind:
            out.append(f"{label}: stage {stage.index} runs {stage.actor.executor!r}, not spawn:{row.kind}")
            continue
        held, held_rules = plan_time_resource_set(stage, venue, row.kind, head)
        missing = uncovered_request_entries(row.allow, row.add_dirs, held, held_rules, venue)
        if missing:
            out.append(
                f"{label}: not covered by the stage's grants (declared + derived + kind "
                f"baseline + workdir): {'; '.join(missing)}. Declare the grant on the stage "
                f"or drop the entry from the request"
            )
    for stage in doc.stages:
        kind = stage.spawn_kind()
        if kind in RESOURCE_REQUEST_KINDS and (kind, stage.index) not in by_key:
            out.append(
                f"stage {stage.index} (spawn:{kind}) has no [[meta.resource_request]] row: "
                f"[meta.order] requires_resource_request is set, so the {kind} consult's "
                f"RESOURCE-REQUEST for this stage must be recorded as a row "
                f"{{kind, stage, consult_ref, allow, add_dirs}}"
            )
    return out


def resource_request_advice(doc) -> list[str]:
    """Armed plans only: a stage of a spawned kind outside `RESOURCE_REQUEST_KINDS` carries
    no request, which is worth saying but never grounds to refuse."""
    if not _resource_request_armed(doc):
        return []
    return [
        f"stage {s.index} (spawn:{s.spawn_kind()}) is outside the request-bearing kinds "
        f"({', '.join(RESOURCE_REQUEST_KINDS)}): its permission surface is not checked "
        f"against an executor's request"
        for s in doc.stages
        if s.is_spawn() and s.spawn_kind() not in RESOURCE_REQUEST_KINDS
    ]


def _conditions_restatement(stage, judge_runner, judge_enabled: bool) -> str | None:
    """The violation for a `conditions` exhausted by restating `depends_on`, or None.

    The one refusal on this side that does NOT rest on the plan's own bytes alone: a
    structural prefilter proposes and a model disposes (conditions.py), so a genuine
    condition that happens to name an earlier stage is not refused for its wording. Both
    halves fail towards None — no runner, a disabled judge, a judge that errors or answers
    unusably, and this returns nothing at all, leaving the submission byte-identical to
    what it was before the check existed.

    Reached only from inside the substantive branch below, and that placement is the
    safety property, not an optimization: the same branch is what requires
    `preconditions`, so the field this message tells the author to move the sentence into
    is one this very submission is demanding they declare. A refusal that fired where
    `preconditions` were not required would be telling an author to move text into a place
    the grade does not give them."""
    reasons = restatement_prefilter(stage.conditions or "", stage.depends_on)
    if not reasons:
        return None
    if not judge_restatement(
        stage.conditions or "",
        judge_runner,
        depends_on=stage.depends_on,
        enabled=judge_enabled,
    ):
        return None
    return (
        f"stage {stage.index} ({stage.title!r}): `conditions` only restates the stages "
        f"this one depends on — {'; '.join(reasons)}. `conditions` is what must hold of "
        f"the world for this stage's transformation to go through; that an earlier stage "
        f"is finished belongs in `preconditions` (and `depends_on` already records it "
        f"structurally)"
    )


def _procedure_collapse(stage, judge_runner, judge_enabled: bool) -> str | None:
    """The violation for a `procedure` that only restates `method`, or None.

    The second refusal on this side that does not rest on the plan's own bytes alone,
    and it is judged for a measured reason rather than by analogy: normalized string
    equality — the comparison this check could have been — was run over the 200 stages of
    the frozen corpus and caught 0 of them. An author writing one thought into two fields
    does not write it in the same words, so a structural comparison here would be a gate
    that cannot fire. A prefilter proposes on shared wording and a model disposes
    (procedure.py); both halves fail towards None.

    Reached only from inside the substantive branch below, on the same pairing argument
    `_conditions_restatement` documents: that branch is what requires BOTH fields, so an
    author told his sequence merely restates his requirement has both places to write
    into. Refusing the collapse while requiring only one of the two would be a trap."""
    reasons = collapse_prefilter(stage.means.method or "", stage.means.procedure or "")
    if not reasons:
        return None
    if not judge_collapse(
        stage.means.method or "",
        stage.means.procedure or "",
        judge_runner,
        enabled=judge_enabled,
    ):
        return None
    return (
        f"stage {stage.index} ({stage.title!r}): `procedure` says nothing `method` does "
        f"not — {'; '.join(reasons)}. `method` is the REQUIREMENT this stage's "
        f"transformation must satisfy and only a re-approved plan may change it; "
        f"`procedure` is the SEQUENCE of operations proposed for meeting it, which the "
        f"executor may replace on his own authority (agentctl replan --renormalize). "
        f"With one text in both, either the norm is silently editable or the proposal is "
        f"binding — write the ordered sub-actions, each naming the file or symbol it "
        f"touches, and leave the requirement in `method`"
    )


def _edge_violations(stage, stages_by_index) -> list[str]:
    """Every edge of `stage` that states an ordering without stating a provision. [] == clean.

    A supply edge is one stage handing another a PLACE of its activity — the material to
    transform, the criterion to be judged by, the means to work with. An edge that names no
    element says only "stage N is done first", which the ordering already records; the plan
    then shows a dependency graph while saying nothing about what actually flows along it.

    Both authoring shapes of that defect arrive here as one, and deliberately so: a bare
    `depends_on = [1]` is lifted by `plan._build_supplies` into exactly the element-less
    edge that an explicit `[[stage.supplies]] on = 1` with no `element` produces. One
    refusal covers both because after the lift there is nothing left to tell them apart.

    A REFUSAL rather than advice. The engine already requires `external_research` of a
    substantive plan outright, so demanding this much of a plan's own structure is within
    what the grade plainly asks; and a warning would leave the untyped edge the cheapest
    path an author can take, which IS the defect — 3 of 151 edges in the frozen corpus name
    an element, and no warning was going to move the other 148.

    The vocabulary half is not redundant with `plan._validate_graph`'s. That check arms on
    the loader's raw `.lower()` reading of weight_class; this one runs under the seam's
    normalized reading and also under the undeclared-weight_class branch, so it covers
    exactly the plans where those two readings disagree — the ones the loader let past.

    What this cannot check: whether a legal element name is the RIGHT one for the edge. A
    validator sees the vocabulary, not the provision. Naming the element makes a wrong name
    visible to a reader and to review; it does not stop one from being written."""
    out: list[str] = []
    for sup in stage.supplies:
        # An empty string is the absent case, not a foreign name: `element = ""` states no
        # provision, so it earns the message that says how to state one. Reachable only on
        # the undeclared-weight_class path — for a plan that declares substantive,
        # `_validate_graph` has already refused the empty string as an unknown NAME, which
        # is the wrong reading but the loader's to keep (it is old enough that every plan a
        # live session may re-read already passes it).
        if not sup.element:
            out.append(
                f"stage {stage.index} ({stage.title!r}): the edge to stage {sup.on} names "
                f"no element (required for substantive plans) — say WHAT stage {sup.on} "
                f"hands this stage, as [[stage.supplies]] on = {sup.on}, element = "
                f"\"<one of {', '.join(sorted(ELEMENT_NAMES))}>\". A bare `depends_on = "
                f"[{sup.on}]` lifts into this same element-less edge: it records that the "
                f"other stage goes first and nothing about what it provides"
            )
        elif sup.element not in ELEMENT_NAMES:
            out.append(
                f"stage {stage.index} ({stage.title!r}): the edge to stage {sup.on} names "
                f"element {sup.element!r}, which is not an activity element — one of "
                f"{', '.join(sorted(ELEMENT_NAMES))}"
            )
        out.extend(_delivery_violations(stage, sup, stages_by_index or {}))
    return out


def _delivery_violations(stage, sup, stages_by_index) -> list[str]:
    """An edge that DECLARES how its provision arrives must be able to arrive that way.
    An edge without `delivery` is never a violation here: the field is optional, and a
    missing one keeps the engine's inferred behaviour. An unknown value never reaches this
    point — `plan.parse_plan` refuses it at load."""
    if sup.delivery is None:
        return []
    where = f"stage {stage.index} ({stage.title!r}): the edge to stage {sup.on}"
    supplier = stages_by_index.get(sup.on)
    if sup.delivery == "artifact":
        declared = list(supplier.output_artifacts) if supplier is not None else []
        if not sup.artifact:
            return [f"{where} has delivery = \"artifact\" but names no artifact — add "
                    f"artifact = \"<path>\", one of stage {sup.on}'s output_artifacts "
                    f"{declared}"]
        if sup.artifact not in declared:
            return [f"{where} delivers artifact {sup.artifact!r}, which is not among stage "
                    f"{sup.on}'s output_artifacts {declared}"]
    elif sup.delivery == "continuation":
        if supplier is not None and not supplier.is_spawn():
            return [f"{where} has delivery = \"continuation\" but stage {sup.on} is not a "
                    f"spawn stage — there is no working copy to continue; use \"artifact\" "
                    f"or \"report\""]
    return []


def _negative_control_violations(stage) -> list[str]:
    """A measurable, shell-kind verify_command with no way to be shown to fail. [] == clean.

    A check that always passes -- including one that would pass on garbage -- certifies
    nothing about the state it claims to verify; it only proves the command runs. Requiring
    a `negative_control` (the same command family, fed a known-bad input, run in the same
    venue) fed through `cmd_record_result` at record time is the mutation-testing discipline
    applied to the engine's own gates rather than left to the author's discretion.

    Scoped to exactly the stages that carry an executable positive check: an acceptance_review
    criterion has no command to discriminate with, and a `landed` criterion's command is
    engine-synthesized (plan.py's R1 already forbids an author `verify_command` there), so
    neither is asked for a second one. A `negative_control_waiver` naming a reason stands in
    for a control that genuinely cannot be built."""
    crit = stage.criterion
    if crit.criterion_type != CriterionType.MEASURABLE.value:
        return []
    if crit.verify_kind != CheckKind.SHELL.value:
        return []
    if not crit.verify_command:
        return []
    waiver = str(crit.negative_control_waiver or "").strip()
    if crit.negative_control and waiver:
        return [
            f"stage {stage.index} ({stage.title!r}) declares both 'negative_control' and "
            f"'negative_control_waiver' — ambiguous: a waiver says no control can be built, "
            f"but one is present. Keep whichever is actually true and drop the other."
        ]
    if crit.negative_control or waiver:
        return []
    return [
        f"stage {stage.index} ({stage.title!r}) has a measurable verify_command with no "
        f"'negative_control' (required for substantive plans): a check that has never been "
        f"shown to fail on bad input certifies nothing — add negative_control = \"<command "
        f"that must NOT exit expected_exit>\", run in the same venue, or "
        f"negative_control_waiver = \"<reason it cannot be built>\""
    ]


def _landed_delivery_venue_violations(doc) -> list[str]:
    """A landed check whose delivered stage is not verified in the delivery venue. [] == clean.

    The landed check freezes HEAD of the delivered stage's verify venue. A repo_root-venue
    stage freezes the canonical checkout's HEAD — a commit already on trunk — so the check
    would go green for work that never was this task's delivery. Checked here, at the
    submission seam, like negative controls: `load_plan` stays permissive."""
    specs = [
        (f"stage {s.index} ({s.title!r})", s.criterion.landed)
        for s in doc.stages
        if s.criterion.verify_kind == CheckKind.LANDED.value and s.criterion.landed
    ] + [
        (f"final_check {fc.label!r}" if fc.label else "a final_check", fc.landed)
        for fc in doc.meta.final_check
        if fc.kind == CheckKind.LANDED.value and fc.landed
    ]
    by_index = {s.index: s for s in doc.stages}
    out: list[str] = []
    for where, spec in specs:
        delivered = by_index.get(spec.delivered_stage)
        if delivered is None or delivered.criterion.verify_venue == CheckVenue.DELIVERY.value:
            continue
        out.append(
            f"{where}: landed check names delivered_stage {spec.delivered_stage}, whose "
            f"verify_venue is {delivered.criterion.verify_venue!r} — a repo_root-venue stage "
            f"freezes a commit already on trunk, so the check would prove nothing about this "
            f"task's delivery; name a stage verified in the delivery venue"
        )
    return out


def _landing_declaration_violations(doc) -> list[str]:
    """A plan that delivers in a git worktree with no landing declaration. [] == clean.

    `[meta] delivery_worktree` says the change is authored on a branch that still has to
    reach trunk. Such a plan must say so in one of two reviewed ways: a `kind = "landed"`
    check (stage or final_check) that proves the landing, or a `landing_waiver` naming why
    this change lands nothing. Neither leaves landing unprovable and unexcused; both is the
    same ambiguity `negative_control` + `negative_control_waiver` is — a waiver says no
    landing check applies, but one is present. The waiver is plan-time on purpose: it
    passes through plan review and approval before the work it excuses."""
    if not doc.meta.delivery_worktree:
        return []
    waiver = str(doc.meta.landing_waiver or "").strip()
    landed = asserts_landing(doc.stages, doc.meta.final_check)
    if landed and waiver:
        return [
            "[meta] declares both a kind = \"landed\" check and 'landing_waiver' — "
            "ambiguous: a waiver says no landing check applies, but one is present. "
            "Keep whichever is actually true and drop the other."
        ]
    if landed or waiver:
        return []
    return [
        f"[meta] delivery_worktree {doc.meta.delivery_worktree!r} is set but the plan "
        f"declares no landing: add a kind = \"landed\" check (a stage criterion or a "
        f"[[final_check]]) that proves the delivery reached trunk, or "
        f"landing_waiver = \"<why this change lands nothing>\" in [meta]"
    ]


def _ephemeral_artifacts_violations(stage, venue: str | None) -> list[str]:
    """A declared output_artifacts entry that resolves under an OS-temp scratch root
    (exempt_paths.scratch_roots()) claims durability the path does not have: a green
    verify_command today says nothing about whether the artifact still exists at the
    stage's own review, or the plan's resolution gate, once /tmp (or $TMPDIR) has been
    swept. Checked against output_artifacts entries ONLY, via exempt_paths.
    under_scratch_root (never a substring test) — a verify_command that happens to
    mention /tmp (a test's own tmp fixture, a throwaway diff) is unaffected; that text is
    not inspected here.

    A relative entry is joined to the plan's declared `venue` before the test, never to
    the process cwd (`under_scratch_root` realpaths a relative path against it): a plan
    declaring no venue has no base for a relative entry, so it is skipped here and the
    missing declaration is refused on its own by `plan.venue_declaration_blockers`.

    ephemeral_artifacts_waiver names why a flagged entry is legitimate (e.g. a stage whose
    only observable IS an ephemeral run, with no committed artifact) — beside, not instead
    of, negative_control_waiver."""
    def anchored(a: str) -> str | None:
        if os.path.isabs(a):
            return a
        return os.path.join(venue, a) if venue is not None else None

    flagged = [a for a in stage.output_artifacts
               if (p := anchored(a)) is not None and exempt_paths.under_scratch_root(p)]
    if not flagged:
        return []
    if str(stage.ephemeral_artifacts_waiver or "").strip():
        return []
    return [
        f"stage {stage.index} ({stage.title!r}) declares output_artifacts under an OS-temp "
        f"scratch root ({', '.join(flagged)}) — a green verify_command today says nothing "
        f"about whether the artifact still exists once /tmp (or $TMPDIR) is swept; move it "
        f"under a durable path or declare ephemeral_artifacts_waiver = \"<reason>\""
    ]


@dataclass(frozen=True)
class StageOrderContext:
    """What the stage-order checks cannot read from the plan alone.

    `expected_stage` is the parent stage this plan carries the sub-plan of (the frame's
    originating stage, the child contour's handoff stage); `binding_conditions` are that
    stage's, read from the carrier; `released` are the ids of those conditions the setter
    level set itself (not carried from above) and released in its own record, already decided
    by the carrier reader (`cli_common._setter_level_released`; never the carrying record: a
    plan cannot release a condition set above it); `parent_ancestors` are the transitive
    `depends_on` ancestors of the parent stage in the parent plan, which a `[means: stage N]`
    mark on the record's conditions must name; `search_resolver` tells whether a search id
    resolves in the session that carries the record; `parent_unreadable` says why the parent
    session that was asked for could not be read at all; `engine_approvals` are the approvals the
    engine last wrote for the parent stage (`SessionState.stage_approvals`: an empty mapping when
    it wrote none), which a record's approval fields may only repeat. Each other field left None
    skips the check that needs it."""
    engine_approvals: dict | None = None
    expected_stage: int | None = None
    binding_conditions: tuple[dict, ...] | None = None
    released: frozenset[str] = frozenset()
    parent_ancestors: frozenset[int] | None = None
    search_resolver: Callable[[str], bool] | None = None
    parent_unreadable: str | None = None


@dataclass
class _RecordedConditions:
    index: int
    conditions: str


_STAGE_DESCRIPTION_PARTS = (
    ("product", lambda s: s.subject.result, "`expected_result_image`"),
    ("place", lambda s: s.place, "`place`"),
    ("acceptance checks", lambda s: s.criterion.done_criterion, "`done_criterion`"),
)


def _stage_binding_condition_violations(stage) -> list[str]:
    """The rows of one stage's `binding_conditions`, held to what the order's own binding
    conditions are held to: each is a table with an id, a text and a `set_by`, and an id is
    stated once (compared without regard to case, as the restated-condition check compares)."""
    out: list[str] = []
    for i, row in enumerate(stage.binding_conditions, start=1):
        if row.get("not_a_table"):
            out.append(f"stage {stage.index} binding condition #{i} is not a table (an `id`, "
                       f"a `text` and a `set_by`)")
            continue
        label = row.get("id") or f"#{i}"
        for name in ("id", "text", "set_by"):
            if not _normalize_string(row.get(name)):
                out.append(f"stage {stage.index} binding condition {label} has no {name!r}")
    dup = _duplicate_ids([str(r.get("id") or "").casefold() for r in stage.binding_conditions])
    if dup:
        out.append(f"stage {stage.index} binding condition id(s) {', '.join(dup)} repeat "
                   f"(ids are compared without regard to case)")
    return out


def _stage_description_violations(doc, ctx: StageOrderContext) -> list[str]:
    """Check 4: every stage of a plan on a two-sided order is a stage description. The order
    chooses the form, never a field of the stage: each of its parts (product, place,
    acceptance checks, binding conditions) must be stated and not blank, and it carries no
    means, method, procedure or conditions. A size flag outside `classify.SIZE_FLAGS` is
    refused: the predicate would read it as no signal."""
    from .classify import SIZE_FLAGS

    out: list[str] = []
    if not is_two_sided(doc.meta.order):
        return out
    for stage in doc.stages:
        for name, read, key in _STAGE_DESCRIPTION_PARTS:
            value = read(stage)
            if not (value and str(value).strip()):
                out.append(f"stage {stage.index} is a stage description and states no {name} ({key})")
        if not stage.binding_conditions_stated:
            out.append(f"stage {stage.index} is a stage description and states no binding "
                       f"conditions (`binding_conditions`; an empty list says there are none)")
        out.extend(_stage_binding_condition_violations(stage))
        carried = [name for name, value in (
            ("means", stage.means.means), ("method", stage.means.method),
            ("procedure", stage.means.procedure), ("conditions", stage.conditions),
        ) if value and str(value).strip()]
        if carried:
            out.append(
                f"stage {stage.index} is a stage description and carries "
                f"no {', '.join(carried)}: those belong to the executor's sub-plan record "
                f"(`[meta.sub_plan]` of the plan that builds the stage), not to the order"
            )
        unknown = [f for f in stage.size_flags if str(f).strip().lower() not in SIZE_FLAGS]
        if unknown:
            out.append(
                f"stage {stage.index} names size flag(s) {', '.join(map(repr, unknown))}, which "
                f"are not size flags; they are one of {', '.join(sorted(SIZE_FLAGS))}"
            )
    return out


def _sub_plan_text_violations(record, prefix: str, ctx: StageOrderContext) -> list[str]:
    out = [f"{prefix} has a malformed part: {part}" for part in record.malformed]
    for name in ("means", "method", "procedure"):
        if not getattr(record, name).strip():
            out.append(f"{prefix} states no {name}")
    if record.conditions.strip():
        shim = _RecordedConditions(record.stage, record.conditions)
        ancestors = ctx.parent_ancestors
        if ancestors is None:
            ancestors = frozenset(n for c in conditions_securing.parse_marks(record.conditions)
                                  if (n := c.fields.get("stage")) is not None)
        out.extend(conditions_securing.securing_problems(shim, ancestors))
    return out


def _sub_plan_estimate_violations(record, prefix: str) -> list[str]:
    estimate = record.estimate
    if estimate is None or (estimate.minutes is None and estimate.usd is None):
        return [f"{prefix} has no estimate (minutes or usd)"]
    missing = [role for role, who in (("author", estimate.author),
                                      ("confirmer", estimate.confirmer)) if not who.strip()]
    return [f"{prefix}'s estimate names no {role}" for role in missing]


def _sub_plan_compared_violations(record, prefix: str, ctx: StageOrderContext) -> list[str]:
    compared = record.compared_plans
    if compared is None or not (compared.entries or compared.none):
        return [f"{prefix} states no compared plans: name the plans compared with and the "
                f"departure from each, or `none = true` with the id of the search that found nothing"]
    out: list[str] = []
    if compared.entries and compared.none:
        out.append(f"{prefix}'s compared plans list entries and also say `none`")
    for entry in compared.entries:
        if not entry["ref"].strip() or not entry["departure"].strip():
            out.append(f"{prefix}'s compared plan {entry['ref'] or '(no ref)'!r} needs a ref "
                       f"and the departure from it")
    if compared.none:
        if not compared.search_id.strip():
            out.append(f"{prefix} says no plan was found but names no search id")
        elif ctx.search_resolver is not None and not ctx.search_resolver(compared.search_id):
            out.append(f"{prefix} names search {compared.search_id!r}, which no search "
                       f"recorded in this session resolves")
    return out


def _sub_plan_binding_violations(doc, record, prefix: str, ctx: StageOrderContext) -> list[str]:
    """Check 13: every binding condition of the stage is kept, or the record of the level that
    set it (`ctx.released`) says it was changed or withdrawn. The carrying record's own
    decisions are checked for form and never release a condition: a plan does not release what
    was set above it. The record carries the decisions of the level that wrote this plan, so it
    may decide the conditions that level set on its own stages (those of `doc` that the stage it
    builds does not carry) besides the ones the stage carries; a decision on any other id is
    refused."""
    out: list[str] = []
    known = None if ctx.binding_conditions is None else {c["id"] for c in ctx.binding_conditions}
    decidable = None if known is None else known | {
        c["id"] for s in doc.stages for c in s.binding_conditions}
    released = set(ctx.released)
    for decision in record.setter_decisions:
        if decision.value not in sub_plan.SETTER_VALUES:
            out.append(f"{prefix}'s decision on {decision.condition!r} has value "
                       f"{decision.value!r}; it is one of {', '.join(sub_plan.SETTER_VALUES)}")
        for role, who in (("setter", decision.setter), ("who decided", decision.decided_by)):
            if not who.strip():
                out.append(f"{prefix}'s decision on {decision.condition!r} names no {role}")
        if decidable is not None and decision.condition not in decidable:
            out.append(f"{prefix}'s decision names condition {decision.condition!r}, which "
                       f"neither the stage nor a stage of this plan carries")
    if known is None:
        return out
    kept = {h.condition for h in record.how_kept if h.how.strip()}
    for cid in sorted(known - kept - released):
        out.append(f"{prefix} does not say how it keeps binding condition {cid!r}: add a "
                   f"`[[meta.how_kept]]` entry (a change or withdrawal is the setter level's "
                   f"record, not this plan's)")
    for held in record.how_kept:
        if held.condition not in known:
            out.append(f"{prefix} says how it keeps {held.condition!r}, which the stage does not carry")
    return out


def _restated_condition_violations(doc, ctx: StageOrderContext) -> list[str]:
    """A stage of this plan that restates a binding condition the stage it builds carries must
    keep its text (compared after trimming and folding whitespace); another text is a change,
    which only the setter level's record may make (`ctx.released`). Ids are compared without
    regard to case: `c1` is the condition `C1`, not another."""
    if ctx.binding_conditions is None:
        return []
    carried = {str(c["id"]).casefold():" ".join(str(c.get("text", "")).split())
               for c in ctx.binding_conditions}
    released = {str(i).casefold() for i in ctx.released}
    out: list[str] = []
    for stage in doc.stages:
        for cond in stage.binding_conditions:
            text = " ".join(str(cond.get("text", "")).split())
            cid = cond.get("id")
            key = str(cid or "").casefold()
            if key in carried and key not in released and text != carried[key]:
                out.append(
                    f"stage {stage.index} restates binding condition {cid!r} with another text "
                    f"than the stage this plan builds carries: only the level that set it can "
                    f"change it, by a `changed` or `withdrawn` decision in its own sub-plan "
                    f"record; keep the text, or ask that level to decide"
                )
    return out


def _sub_plan_record_violations(doc, record, ctx: StageOrderContext) -> list[str]:
    prefix = f"sub-plan record of stage {record.stage}"
    if record.stage <= 0:
        return ["the sub-plan record names no parent stage (`stage = <n>`)"] + _sub_plan_text_violations(
            record, "the sub-plan record", ctx)
    out: list[str] = []
    if ctx.parent_unreadable:
        out.append(f"{prefix}: {ctx.parent_unreadable}; the record cannot be checked against "
                   f"it (the `[means: stage N]` marks, the stage's binding conditions): restore "
                   f"the parent session's state, or submit from the session that holds it")
    if ctx.expected_stage is not None and record.stage != ctx.expected_stage:
        out.append(f"{prefix}: this plan builds stage {ctx.expected_stage}, not stage {record.stage}")
    out.extend(_sub_plan_text_violations(record, prefix, ctx))
    out.extend(_sub_plan_estimate_violations(record, prefix))
    out.extend(_sub_plan_compared_violations(record, prefix, ctx))
    out.extend(_sub_plan_binding_violations(doc, record, prefix, ctx))
    out.extend(_sub_plan_approval_violations(record, prefix, ctx))
    return out


def _sub_plan_approval_violations(record, prefix: str, ctx: StageOrderContext) -> list[str]:
    """The approval fields of a record are the engine's: a record that states one differing from
    what the engine last wrote for the parent stage is refused, so no plan text approves
    itself (the user's approval of a sub-plan and the parent's approval of its resources are
    written by `approve-subplan`, `approve` and `approve-stage-resources` only)."""
    if ctx.engine_approvals is None:
        return []
    out: list[str] = []
    for field in ("parent_resource_approval", "user_subplan_approval"):
        stated = getattr(record, field)
        if stated and stated != ctx.engine_approvals.get(field):
            out.append(
                f"{prefix}: `{field}` is stated and differs from what the engine wrote: the "
                f"approval is the engine's, recorded when the user (approve-subplan, approve) or "
                f"the agent inside the approved set (approve, approve-stage-resources) approves; "
                f"leave the field out")
    return out


def _lazy_below_threshold_violations(doc) -> list[str]:
    """A stage below the size line is asked for no sub-plan, so it is never decided later: the
    `lazy` mark belongs to a stage above the line. Old-form stages carry no size claim."""
    from .config import Thresholds
    from .plan import stage_above_threshold

    thr = Thresholds()
    return [f"stage {s.index} is below the size line and carries the decided-later mark "
            f"(`lazy = true`): a stage that small needs no sub-plan, drop the mark"
            for s in doc.stages
            if s.place is not None and sub_plan.is_decided_later(s)
            and not stage_above_threshold(s, thr)]


def _stage_order_violations(doc, ctx: StageOrderContext | None = None) -> list[str]:
    """The stage-order checks, both carriers in one place: a parent plan's stage descriptions
    (checks 4 and the parent side of 11), and the sub-plan record a plan carries in
    `[meta.sub_plan]` (the content of 11 and check 13), whether that plan runs in a pushed
    frame or in a child contour. A plan with neither draws nothing."""
    ctx = ctx or StageOrderContext()
    out = _stage_description_violations(doc, ctx)
    out.extend(_restated_condition_violations(doc, ctx))
    if is_two_sided(doc.meta.order):
        out.extend(_lazy_below_threshold_violations(doc))
    record = doc.meta.sub_plan
    if record is not None:
        out.extend(_sub_plan_record_violations(doc, record, ctx))
    return out


def submission_violations(
    doc,
    *,
    session_weight_class: str | None = None,
    judge_runner=None,
    judge_enabled: bool = True,
    frozen_edge_stages: frozenset[int] = frozenset(),
    conditions_securing_required: bool = False,
    stage_order: StageOrderContext | None = None,
) -> list[str]:
    """Every submission-grade violation in `doc`, as author-readable strings. [] == clean.

    `conditions_securing_required` arms the securing-mark check on every stage's `conditions`
    (conditions_securing.py); the session decides it once, at its first accepted submission.

    Returns rather than raises: the approve seam must answer with a Directive (see the
    module docstring), and returning the full list lets one round trip show everything.

    `frozen_edge_stages` names stages whose edges are not held to the edge rule: finished
    stages whose supplies a replan leaves unchanged. Their supplies are interface leaves
    the replan freeze refuses to touch without --reopen, so refusing them here as well
    left a plan approved before the edge rule existed no replan at all short of reopening
    work that is already done.

    `judge_runner`/`judge_enabled` reach the violations here that a model decides (see
    `_conditions_restatement` and `_procedure_collapse`). Their defaults make those
    violations unreachable, so every caller that passes no judge — including every
    pre-existing one — gets exactly the list it got before: a judged check can only ever
    ADD a refusal, never remove one.
    """
    out: list[str] = []
    malformed = _malformed_weight_class(doc)
    if malformed:
        out.append(malformed)
    undeclared = _undeclared_weight_class(doc, session_weight_class)
    if undeclared:
        out.append(_UNDECLARED)
    # Whatever the weight class: the spawn refuses a grant collision for every plan.
    out.extend(grant_shadow.plan_grant_shadow_problems(doc))
    out.extend(_stage_order_violations(doc, stage_order))
    # Keyed on the SESSION's class, independent of doc.meta.weight_class, exactly as
    # `verify_command_reachability_blockers` is armed by the caller: a plan declaring a
    # lighter class must not buy a verdict that depends on the invoker's cwd.
    if session_weight_class is not None and _normalize_string(session_weight_class) == "substantive":
        out.extend(venue_declaration_blockers(doc))
    venue = _declared_absolute_venue(doc.meta)
    # The field loop also runs for the UNDECLARED case, where the plan itself never claimed
    # to be substantive. That is not the seam arming on the session — the refusal is still
    # the missing declaration, and a plan that answers it with a non-substantive class keeps
    # every field violation below moot. It is the module's one-round-trip contract: the
    # author whose plan the session will hold to the substantive grade sees the declaration
    # AND the places it will require in one answer, instead of one round trip per layer.
    if not (is_substantive(doc) or undeclared):
        return out
    # Plan-level first, then per-stage: an author reading one round trip meets the whole
    # of what the plan owes before the list turns into stage numbers.
    for dotted, label in _SUBSTANTIVE_META_FIELDS:
        if not _attr(doc.meta, dotted):
            out.append(
                f"[meta] missing {label!r} (required for substantive plans): {_WHY[label]}"
            )
    out.extend(_order_violations(doc.meta))
    out.extend(_order_understanding_violations(doc.meta))
    out.extend(_element_traceability_violations(doc))
    out.extend(_requirement_derivation_violations(doc.meta.order))
    out.extend(_resource_request_violations(doc))
    out.extend(_landed_delivery_venue_violations(doc))
    out.extend(_landing_declaration_violations(doc))
    stage_deps = {s.index: list(s.depends_on or ()) for s in doc.stages}
    new_form = is_two_sided(doc.meta.order)
    for stage in doc.stages:
        # Supplies are already built by the time a PlanDoc exists, so the "supplied by an
        # earlier stage" alternative is decidable here — evaluating the knowledge
        # requirement against the parsed doc rather than the raw TOML table is what makes
        # an incoming `knowledge` edge count as filling the place.
        supplied = {s.element for s in stage.supplies if s.element}
        for dotted, label, supply_element in _SUBSTANTIVE_SUBMISSION_FIELDS:
            if _attr(stage, dotted):
                continue
            if new_form and dotted in _EXECUTOR_OWNED_PATHS:
                continue
            if supply_element is not None and supply_element in supplied:
                continue
            out.append(
                f"stage {stage.index} missing {label!r} (required for substantive plans): "
                f"{_WHY[label]}"
            )
        if stage.index not in frozen_edge_stages:
            out.extend(_edge_violations(stage, {s.index: s for s in doc.stages}))
        out.extend(_negative_control_violations(stage))
        out.extend(_ephemeral_artifacts_violations(stage, venue))
        restatement = _conditions_restatement(stage, judge_runner, judge_enabled)
        if restatement:
            out.append(restatement)
        if conditions_securing_required:
            out.extend(conditions_securing.securing_problems(
                stage, conditions_securing.ancestors(stage.index, stage_deps)))
        collapse = _procedure_collapse(stage, judge_runner, judge_enabled)
        if collapse:
            out.append(collapse)
    return out


_STAGE_FIELD_PATHS = {
    "title": "title", "executor": "actor.executor", "expected_result_image": "subject.result",
    "done_criterion": "criterion.done_criterion", "cost_tier": "actor.cost_tier",
    "material": "subject.material", "invariants": "subject.invariants",
    "means": "means.means", "method": "means.method", "procedure": "means.procedure",
    "capability_required": "actor.capability_required",
    "criterion_type": "criterion.criterion_type", "verify_command": "criterion.verify_command",
    "verify_venue": "criterion.verify_venue", "verify_kind": "criterion.verify_kind",
    "verify_venue_at_final": "criterion.verify_venue_at_final",
    "negative_control": "criterion.negative_control",
    "negative_control_waiver": "criterion.negative_control_waiver",
    "ephemeral_artifacts_waiver": "ephemeral_artifacts_waiver", "conditions": "conditions",
    "preconditions": "preconditions", "knowledge": "knowledge",
    "backlog_issue": "backlog_issue",
}


def _typed_text(obj, dotted: str) -> str:
    try:
        value = _attr(obj, dotted)
    except AttributeError:
        return ""
    value = getattr(value, "value", value)
    return value if isinstance(value, str) else ""


def _node_field_texts(doc_or_raw):
    """`(node, kind, [(field, text)], position)` for the plan-level node and every stage,
    from a `PlanDoc` or from a raw plan (references already resolved). `node` is "meta" or
    the stage index; `position` is the key `plan_refs.resolve_refs` reports the node's
    references under ("meta", "meta.order", or the stage identity as `resolve_refs`
    reports it: the declared `index`, else the 1-based position)."""
    from .plan_refs import EXTERNALIZABLE_FIELDS, stage_identity

    if isinstance(doc_or_raw, dict):
        meta = doc_or_raw.get("meta") if isinstance(doc_or_raw.get("meta"), dict) else {}
        order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
        stages = doc_or_raw.get("stage") if isinstance(doc_or_raw.get("stage"), list) else []

        def raw_fields(table: dict, kind: str):
            return [(f, table[f]) for f in sorted(EXTERNALIZABLE_FIELDS[kind])
                    if isinstance(table.get(f), str)]

        nodes = [("meta", "meta", raw_fields(meta, "meta"), "meta"),
                 ("meta", "order", raw_fields(order, "order"), "meta.order")]
        for position, table in enumerate(stages, start=1):
            if isinstance(table, dict):
                identity = stage_identity(table, position)
                nodes.append((identity, "stage", raw_fields(table, "stage"), identity))
        return nodes
    meta_fields = [(f, _typed_text(doc_or_raw.meta, f)) for f in sorted(EXTERNALIZABLE_FIELDS["meta"])]
    order_fields = [(f, _typed_text(doc_or_raw.meta.order, f)) for f in sorted(EXTERNALIZABLE_FIELDS["order"])] \
        if doc_or_raw.meta.order is not None else []
    nodes = [("meta", "meta", [p for p in meta_fields if p[1]], "meta"),
             ("meta", "order", [p for p in order_fields if p[1]], "meta.order")]
    for stage in doc_or_raw.stages:
        fields = [(f, _typed_text(stage, path)) for f, path in sorted(_STAGE_FIELD_PATHS.items())]
        nodes.append((stage.index, "stage", [p for p in fields if p[1]], stage.index))
    return nodes


def plan_size_findings(doc_or_raw, ref_positions=frozenset()) -> list[dict]:
    """Size findings of a plan: `{node, field, severity, message}`, node "meta" or a stage
    index. A node whose rendered size exceeds `plan-stage-max-chars` BLOCKS, with no waiver; a
    field of `plan_refs.EXTERNALIZABLE_FIELDS` longer than `plan-field-inline-max-chars` and
    not an external reference (`ref_positions`, as `plan_refs.resolve_refs` reports them)
    WARNS. Other fields, grants lists included, are never reported by field. A stage limit
    that cannot be read blocks the plan (`config.PlanLimitError`), whatever its nodes hold.

    A raw plan's stages are measured against the venue its `[meta]` declares, as a parsed
    one's are, so the two measures agree."""
    return plan_size_warnings(doc_or_raw, ref_positions) + plan_size_blocks(doc_or_raw)


def plan_size_warnings(doc_or_raw, ref_positions=frozenset()) -> list[dict]:
    """The warn findings of `plan_size_findings`, which every seam that admits a plan
    reports through its advice channel."""
    from .config import plan_field_inline_limit

    field_max = plan_field_inline_limit()
    findings: list[dict] = []
    for node, _kind, fields, position in _node_field_texts(doc_or_raw):
        for name, text in fields:
            if len(text) <= field_max or (position, name) in ref_positions:
                continue
            where = "the plan-level node" if node == "meta" else f"stage {node}"
            findings.append({
                "node": node, "field": name, "severity": "warn",
                "message": (
                    f"{where} field {name!r} is {len(text)} chars, over "
                    f"plan-field-inline-max-chars {field_max}: externalize it with "
                    f"`agentctl plan-externalize`"
                ),
            })
    return findings


def plan_size_blocks(doc_or_raw, stage_max: int | None = None) -> list[dict]:
    """The block findings of `plan_size_findings`, which every seam that admits a plan
    reports through its refusing channel. `stage_max` stands in for the configured
    `plan-stage-max-chars` for a caller that already holds the limit it reports against."""
    from .config import PlanLimitError, plan_size_limits
    from .plan import (
        _venue_for, meta_rendered_size, oversize_node_message, raw_plan_venue, stage_rendered_size,
    )

    if stage_max is None:
        try:
            stage_max, _ = plan_size_limits()
        except PlanLimitError as exc:
            return [{"node": "meta", "field": None, "severity": "block", "message": str(exc)}]
    if isinstance(doc_or_raw, dict):
        venue = raw_plan_venue(doc_or_raw)
        stages = doc_or_raw.get("stage") if isinstance(doc_or_raw.get("stage"), list) else []
        from .plan_refs import stage_identity
        stage_nodes = [(stage_identity(table, position), table)
                       for position, table in enumerate(stages, start=1) if isinstance(table, dict)]
    else:
        venue = _venue_for(doc_or_raw)
        stage_nodes = [(s.index, s) for s in doc_or_raw.stages]
    sizes = [("meta", meta_rendered_size(doc_or_raw))]
    sizes += [(node, stage_rendered_size(stage, venue)) for node, stage in stage_nodes]
    return [
        {"node": node, "field": None, "severity": "block", "chars": size,
         "message": oversize_node_message(node, size, stage_max)}
        for node, size in sizes if size > stage_max
    ]


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

    The model-judged half is fail-open: no runner, a disabled judge, or a judge that
    errors yields none of its advice, and that silence is indistinguishable from the
    feature being absent. The resource-request advice is mechanical and needs no judge, so
    it is returned either way.
    """
    out: list[str] = resource_request_advice(doc)
    if not judge_enabled or judge_runner is None:
        return out
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
    conditions_securing_required: bool = False,
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
    never appears, rather than growing a second convention around them.

    The same pair is threaded into `submission_violations` as well, where a judge decides
    one refusal rather than one warning — so a caller that supplies a judge to this
    wrapper gets both channels judged, and a caller that supplies none gets neither."""
    from .plan import PlanError

    problems = submission_violations(
        doc,
        session_weight_class=session_weight_class,
        judge_runner=judge_runner,
        judge_enabled=judge_enabled,
        conditions_securing_required=conditions_securing_required,
    )
    if problems:
        raise PlanError(problems[0])
    return submission_advice(doc, judge_runner=judge_runner, judge_enabled=judge_enabled)
