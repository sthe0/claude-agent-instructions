"""Element-bearing supply edges, and a vocabulary that can name every place.

Defect 7 of the SMD act-modelling rework. A stage edge is one stage handing another a
PLACE of its activity, and `Supply.element` is where the plan says which place. In the
frozen corpus of 55 substantive plans, 3 of 151 edges name an element — 2.0%, in 2 files.
The other 148 are bare orderings: the plan draws a dependency graph and says nothing about
what flows along it, so "the stage before me is done" is the whole content of the edge.

The remedy is two halves in one change, and the ORDER of the two is itself pinned here:

  * FIRST the vocabulary. `text_shape.ELEMENT_NAMES` had no name for a control instrument,
    for the order or its parts, for the procedure, or for preconditions. Requiring an
    element while the menu is short of the places real edges hand over produces a plausible
    WRONG name, not a right one — an audit of 13 mistyped edges found 11 of them reaching
    for a place the vocabulary could not spell.
  * THEN the mandate. At the submission seam, a substantive plan's every edge must name an
    element from that vocabulary. It cannot be a loader requirement — see submission.py's
    module docstring for why every new requirement binds at submission instead.

A warning was rejected deliberately: `external_research` is already required of a
substantive plan outright, so the codebase can plainly demand this much; and a warning
leaves the untyped edge the cheapest path an author can take, which IS the defect.

What these tests do NOT claim, and what no test here could: that an author picks the RIGHT
element. A validator sees whether a name is in the vocabulary, never whether it names what
the edge really provides. The claim is narrower — a wrong element becomes VISIBLE to a
reader and to review, where a bare `depends_on` left nothing to be wrong about.

Four carve-outs stay lifted, one test each, because each one is a way this change could
have become retroactive over plans already accepted: a non-substantive plan, a
`strict=False` load, an already-accepted untyped plan re-read strict, and anything reached
through `load_plan`/`parse_plan` at all.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plan as plan_mod, submission as submission_mod, text_shape
from agentctl.plan import PlanError, load_plan
from agentctl.state import Node
from agentctl.submission import submission_violations, validate_submission

# The three edge shapes an author can write, spliced into stage 2 of `_PLAN`. The first two
# are indistinguishable by the time a PlanDoc exists — `plan._build_supplies` lifts a bare
# `depends_on` into exactly the element-less edge the second one writes — and the tests
# below assert that ONE refusal covers both rather than pinning two messages.
BARE_DEPENDS_ON = "depends_on = [1]\n"
UNTYPED_SUPPLY = "[[stage.supplies]]\non = 1\n"
TYPED_SUPPLY = '[[stage.supplies]]\non = 1\nelement = "material"\n'
FOREIGN_ELEMENT = '[[stage.supplies]]\non = 1\nelement = "vibes"\n'
EMPTY_ELEMENT = '[[stage.supplies]]\non = 1\nelement = ""\n'

# The label→element rule below is a derivation with ONE declared exception, not a wildcard.
# A first-underscore-token prefix match would cover this single field and, silently, any
# future `material_budget` / `means_window` too — marking a genuinely unnamed place covered
# by a name that does not spell it, which is the failure the totality check exists to catch.
_LABEL_ALIASES = {"capability_required": "capability"}

_PLAN = """
[meta]
task_id = "se"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
{weight_class}external_research = "read the corpus audit; no prior art applies"

[[stage]]
index = 1
title = "the stage that goes first"
executor = "in_thread"
expected_result_image = "The vocabulary can name a control instrument."
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "pytest -q"
material = "m1"
means = "bash"
method = "run"
conditions = "The tree is clean."
preconditions = "The branch is checked out."
invariants = "n1"
capability_required = "cap"
material_refs = ["scripts/agentctl/text_shape.py"]
knowledge_refs = ["scripts/agentctl/premise.py"]
knowledge = "which places an edge really hands over"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"

[[stage]]
index = 2
title = "the stage under test"
executor = "in_thread"
expected_result_image = "The seam refuses an edge that states no provision."
criterion_type = "measurable"
done_criterion = "d2"
verify_command = "pytest -q"
material = "m2"
means = "bash"
method = "run"
conditions = "The corpus fixture directory is present."
preconditions = "Stage 1's vocabulary change is in the tree."
invariants = "the loader stays lenient"
capability_required = "cap"
material_refs = ["scripts/agentctl/submission.py"]
knowledge_refs = ["scripts/agentctl/plan.py"]
knowledge = "where a submission requirement may bind"
{edge}[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
"""


def ns(**kw):
    return Namespace(**kw)


def _write_plan(path: Path, *, edge: str, weight_class: str | None = "substantive") -> str:
    """Write the fixture with `edge` spliced into stage 2. `weight_class=None` omits the
    declaration entirely — the shape that reaches the seam's undeclared branch."""
    line = "" if weight_class is None else f'weight_class = "{weight_class}"\n'
    path.write_text(_PLAN.format(edge=edge, weight_class=line), encoding="utf-8")
    return str(path)


def _submit(store, plan_path, session="se"):
    cli.cmd_start(ns(session=session, task="se", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)
    return cli.cmd_submit_plan(ns(session=session, plan=plan_path), store=store)


def _problems(d) -> list[str]:
    return list(d.data.get("problems", []))


# --- the vocabulary, first ---------------------------------------------------


def test_every_required_place_has_a_vocabulary_entry():
    """THE TOTALITY CHECK. The mandate can force an author to name any place the substantive
    grade already requires of a stage, so a place the grade requires but the vocabulary
    cannot spell is a trap: the engine demands an element for an edge that provides it, and
    no legal answer exists.

    Enumerated from the two requirement tables in the code, never from a list written here —
    a hand-written list is a copy that goes stale the moment a later stage adds a row, which
    is exactly when this check has something to say. `_PRINCIPLE_SUBFIELDS` is deliberately
    excluded: those are parts of the `principle` place, not places of their own, and their
    covering name is `principle`.

    The label-to-element rule is a derivation, not a lookup table, for the same reason: a
    field is covered if the vocabulary holds its label, its label minus a `_refs` projection
    suffix, or the one alias declared in `_LABEL_ALIASES`. A future field none of those
    reach turns this red rather than quietly shipping a place with no name — which is why
    the third clause is a named exception and not a prefix match: a prefix match would cover
    every future `<element>_<qualifier>` field automatically, and this check would go quiet
    exactly where a new place needs a name."""
    labels = set(plan_mod._SUBSTANTIVE_STAGE_FIELDS) | {
        label for _dotted, label, _supply in submission_mod._SUBSTANTIVE_SUBMISSION_FIELDS
    }
    assert labels, "the requirement tables are the domain — an empty one proves nothing"

    uncovered = sorted(
        label for label in labels
        if not ({label, label.removesuffix("_refs"), _LABEL_ALIASES.get(label, label)}
                & set(text_shape.ELEMENT_NAMES))
    )
    assert uncovered == [], (
        f"places the substantive grade requires but no element name can spell: {uncovered}"
    )
    assert "principle" in text_shape.ELEMENT_NAMES, (
        "the excluded principle subfields are parts of this place"
    )


def test_the_enumerating_judge_may_target_every_place_in_the_vocabulary():
    """The vocabulary has a second, EXECUTABLE consumer: the prompt that bounds the premise
    gate's independent question-enumerator (`advisor._ENUMERATE_QUESTIONS_PROMPT`). A name
    absent from that prompt is a place the judge is told it may not raise a question
    against, so a restated copy there does not merely rot — it silently narrows the gate to
    whatever the vocabulary was when the copy was last edited. It had, by six names.

    Pinned rather than left to the derivation, because the prompt is prose to every reader
    and nothing else in the suite reads it."""
    from agentctl import advisor

    missing = sorted(n for n in text_shape.ELEMENT_NAMES
                     if n not in advisor._ENUMERATE_QUESTIONS_PROMPT)

    assert missing == [], (
        f"places the enumerating judge is not allowed to raise a question against: {missing}"
    )


def test_the_vocabulary_names_the_places_the_gaps_audit_found():
    """The four places an edge was found handing over with no name to spell it: a control
    instrument, the order and its parts, and the procedure. Named here rather than left to
    the totality check above because none of them is a stage FIELD — no requirement table
    enumerates them, so nothing else in the suite would notice their removal, and removing
    them re-opens the gap that made the mandate a trap."""
    assert {"control", "order", "requirements", "procedure"} <= set(text_shape.ELEMENT_NAMES)


# --- the mandate: an edge states a provision, not only an ordering -----------


def test_a_bare_depends_on_is_refused_at_submission(store, tmp_path):
    """The 148-of-151 case, and the whole point of the change: an edge that records only
    that another stage goes first is refused for a substantive plan, at submission.

    The session staying at PLANNING is half the assertion — a refusal that advanced the node
    would strand it with an armed gate and no edge back (the same property
    test_preconditions.py pins for its own refusal)."""
    plan = _write_plan(tmp_path / "bare.toml", edge=BARE_DEPENDS_ON)

    d = _submit(store, plan)

    assert d.ok is False
    assert d.action == "fix_plan"
    assert store.load("se").node == Node.PLANNING.value
    assert any("stage 2" in p and "edge to stage 1" in p and "names no element" in p
               for p in _problems(d)), _problems(d)


def test_an_explicit_supply_without_an_element_is_refused(store, tmp_path):
    """The other authoring shape, refused by the SAME rule. `plan._build_supplies` lifts a
    bare `depends_on` into an element-less edge, so by the time a PlanDoc exists the two are
    one object — a change that refused only the explicit form would leave the cheaper form
    (which is also the common one) untouched, and the defect fully intact."""
    plan = _write_plan(tmp_path / "untyped.toml", edge=UNTYPED_SUPPLY)

    d = _submit(store, plan)

    assert d.ok is False
    assert any("edge to stage 1" in p and "names no element" in p for p in _problems(d)), (
        _problems(d)
    )


def test_an_empty_element_is_the_absent_case_not_a_foreign_name(tmp_path):
    """`element = ""` states no provision, so it earns the message that says how to state
    one — not the one that says its name is not an activity element.

    On the undeclared-weight_class path, and NOT by choice of convenience: for a plan that
    declares substantive, `_validate_graph` refuses an empty element as an unknown NAME
    before a PlanDoc exists, so the seam's absent-branch is reachable only where the
    loader's raw `.lower()` reading and the seam's normalized one disagree — the same
    sliver the foreign-element test above covers. Pinned because the routing lives in a
    single falsiness test (`if not sup.element`) that a later tidy into `is None` would
    silently reverse, handing the author whose edge says nothing a message about a
    vocabulary they never misused."""
    doc = load_plan(_write_plan(tmp_path / "empty.toml", edge=EMPTY_ELEMENT,
                                weight_class=None))

    problems = submission_violations(doc, session_weight_class="SUBSTANTIVE")

    assert any("edge to stage 1" in p and "names no element" in p for p in problems), problems
    assert not [p for p in problems if "not an activity element" in p], problems


def test_an_element_bearing_edge_passes(store, tmp_path):
    """The control for the two refusals above: the SAME fixture with an element on the edge
    submits clean. Without this, a refusal could be coming from any other defect in the
    fixture and the two tests above would prove nothing about edges."""
    plan = _write_plan(tmp_path / "typed.toml", edge=TYPED_SUPPLY)

    d = _submit(store, plan)

    assert d.ok is True, _problems(d)


def test_an_element_outside_the_vocabulary_is_refused_at_the_seam(tmp_path):
    """The vocabulary half, on the one path where it is not already the loader's work.
    `plan._validate_graph` refuses an unknown element for a plan whose weight_class reads as
    substantive under a raw `.lower()`; a plan that declares NO weight_class in a substantive
    session is graded by this seam and not by that check, so without this half a foreign
    element rides in through exactly the gap the undeclared branch exists to close."""
    doc = load_plan(_write_plan(tmp_path / "foreign.toml", edge=FOREIGN_ELEMENT,
                                weight_class=None))

    problems = submission_violations(doc, session_weight_class="SUBSTANTIVE")

    assert any("'vibes'" in p and "not an activity element" in p for p in problems), problems


def test_the_refusal_names_the_vocabulary_it_expects(tmp_path):
    """An author refused for not naming an element has to be able to answer without reading
    the engine's source. The message carries the legal names, derived from the vocabulary
    rather than restated in prose — a message listing a stale copy of the menu is worse than
    one listing none, because it reads as authoritative."""
    doc = load_plan(_write_plan(tmp_path / "bare.toml", edge=BARE_DEPENDS_ON))

    problems = submission_violations(doc)

    assert len(problems) == 1, problems
    for name in sorted(text_shape.ELEMENT_NAMES):
        assert name in problems[0], f"{name} missing from the refusal message"


# --- the four carve-outs, one test each --------------------------------------


def test_a_non_substantive_plan_keeps_the_untyped_edge(tmp_path):
    """Carve-out 1. The grade is the substantive one; a small_change plan is not held to it
    here, exactly as it is not held to `knowledge` or `preconditions`. Widening the mandate
    to every plan would make the engine demand an activity ontology of a two-stage errand."""
    doc = load_plan(_write_plan(tmp_path / "small.toml", edge=BARE_DEPENDS_ON,
                                weight_class="small_change"))

    assert doc.stages[1].depends_on == [1]
    assert submission_violations(doc) == []


def test_a_non_strict_load_keeps_the_lift(tmp_path):
    """Carve-out 2. `strict=False` is how the engine re-reads a plan for projections and
    snapshots; it never grades. The lift from `depends_on` to element-less edges must
    survive there untouched, or every non-strict reader inherits a submission-time
    requirement it was built not to have."""
    doc = load_plan(_write_plan(tmp_path / "lenient.toml", edge=BARE_DEPENDS_ON),
                    strict=False)

    assert [(s.on, s.element) for s in doc.stages[1].supplies] == [(1, None)]


def test_an_already_accepted_untyped_plan_still_loads_strict(tmp_path):
    """Carve-out 3, and the reason the mandate lives at the submission seam at all. A plan
    approved before this change is re-read with `strict=True` from several in-session call
    sites (approve's refresh, both replan sides, the premise gate). If the requirement had
    gone into `parse_plan`, every such session would start failing on bytes it was already
    approved on, with no recovery edge — and the bytes cannot be rewritten, because three
    attestations are computed over them."""
    plan = _write_plan(tmp_path / "accepted.toml", edge=BARE_DEPENDS_ON)

    doc = load_plan(plan, strict=True)

    assert doc.stages[1].depends_on == [1]
    assert doc.stages[1].supplies[0].element is None


def test_the_loader_is_untouched_by_the_mandate(tmp_path):
    """Carve-out 4, stated as the hinge: ONE fixture, both directions. `load_plan(strict=True)`
    accepts the untyped edge and `submission_violations` refuses the same document. A later
    change that moves this check into the loader — the tidying that always looks right,
    since that is where the graph is already validated — turns this red rather than silently
    making the requirement retroactive over every accepted plan."""
    plan = _write_plan(tmp_path / "hinge.toml", edge=BARE_DEPENDS_ON)

    doc = load_plan(plan, strict=True)  # must not raise

    assert [p for p in submission_violations(doc) if "names no element" in p]
    with pytest.raises(PlanError, match="names no element"):
        validate_submission(doc)
