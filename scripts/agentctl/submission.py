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

from .conditions import judge_restatement, restatement_prefilter
from .procedure import collapse_prefilter, judge_collapse
from .result_image import echo_prefilter, judge_echo
from .state import WeightClass
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
    unnamed = [i for i, r in enumerate(order.requirements, start=1) if not r.id]
    if unnamed:
        out.append(
            f"[meta.order] requirement(s) at position {', '.join(str(i) for i in unnamed)} "
            f"carry no 'id'. Write each as {{ id = \"R1\", text = \"...\" }} — the coverage "
            f"map and every acceptance verdict key on the id, so an id-less requirement "
            f"cannot be covered or accepted at all"
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
    uncovered = sorted({r.id for r in order.requirements if r.id and not order.coverage.get(r.id)})
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
            f"declares. A key matching no id covers nothing — most often a requirement id "
            f"renamed on one side only, which leaves the real requirement uncovered while "
            f"the map still looks full"
        )
    return out


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


def _edge_violations(stage) -> list[str]:
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
    return out


def submission_violations(
    doc,
    *,
    session_weight_class: str | None = None,
    judge_runner=None,
    judge_enabled: bool = True,
) -> list[str]:
    """Every submission-grade violation in `doc`, as author-readable strings. [] == clean.

    Returns rather than raises: the approve seam must answer with a Directive (see the
    module docstring), and returning the full list lets one round trip show everything.

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
    # The field loop also runs for the UNDECLARED case, where the plan itself never claimed
    # to be substantive. That is not the seam arming on the session — the refusal is still
    # the missing declaration, and a plan that answers it with a non-substantive class keeps
    # every field violation below moot. It is the module's one-round-trip contract: the
    # author whose plan the session will hold to the substantive grade sees the declaration
    # AND the places it will require in one answer, instead of one round trip per layer.
    if not (_is_substantive(doc) or undeclared):
        return out
    # Plan-level first, then per-stage: an author reading one round trip meets the whole
    # of what the plan owes before the list turns into stage numbers.
    for dotted, label in _SUBSTANTIVE_META_FIELDS:
        if not _attr(doc.meta, dotted):
            out.append(
                f"[meta] missing {label!r} (required for substantive plans): {_WHY[label]}"
            )
    out.extend(_order_violations(doc.meta))
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
        out.extend(_edge_violations(stage))
        restatement = _conditions_restatement(stage, judge_runner, judge_enabled)
        if restatement:
            out.append(restatement)
        collapse = _procedure_collapse(stage, judge_runner, judge_enabled)
        if collapse:
            out.append(collapse)
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
    )
    if problems:
        raise PlanError(problems[0])
    return submission_advice(doc, judge_runner=judge_runner, judge_enabled=judge_enabled)
