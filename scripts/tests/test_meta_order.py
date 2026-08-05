"""A typed order, required of what is offered and not of what was already accepted.

Defect 8 of the SMD act-modelling rework. A plan is an answer to an ORDER — the aggregate
of requirements on the product, held by a customer, filling a functional place. The engine
held that as `[meta] goal` and `done_criterion`: two free-text strings with no requirement
the acceptance could range over, no customer to accept, and no map from a requirement to
the control that decides it. So `[meta.order]` becomes a typed object (state.Order) and a
substantive plan must fill it.

TWO PROPERTIES ARE PINNED HERE AND EACH IS A DIFFERENT KIND OF CLAIM.

The first is UNIVERSAL — "each part of the order is required" — and a universal claim is
not established by the cases someone thought of. So the refusal cases are PARAMETRIZED
over `dataclasses.fields(Order)` itself, with a guard (the first test below) asserting the
parametrization is non-empty and that it, plus a DECLARED exception set carrying a reason
per exception, exhausts that field set. A hand-written list of five would pass every
individual case here and go quiet the day a sixth part is added, which is the substitution
that costs a universal claim its universality.

The second is a NEGATIVE claim about everything already accepted — no plan authored before
this change is affected — and it rests on where the requirement binds. The order is parsed
by the loader (additively, unable to refuse) and REQUIRED at the submission seam, so a plan
a live session already approved re-reads exactly as it did. That is asserted over the WHOLE
frozen corpus, mechanically enumerated, not a sample: nothing was migrated, and nothing
could be — three attestations are computed over a plan's bytes and cannot be re-signed on
the plan's behalf.

What no test here claims: that the requirements are the RIGHT ones, or that a coverage
entry's named control really decides the requirement it is filed under. The resolver
decides TOTALITY — every declared id has an entry — and sufficiency stays review.
"""
from __future__ import annotations

import dataclasses
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.plan import PlanError, diff_plans, load_plan
from agentctl.state import Node, Order
from agentctl.submission import submission_violations, validate_submission

CORPUS_DIR = Path(__file__).resolve().parent / "fixtures" / "plan_corpus"

# Parts of `Order` exempt from the "one refusing case per part" rule, each with the reason
# it is exempt. EMPTY today — every part of the order is required of a substantive plan.
# It exists as a DECLARED seam so that a future exemption has to be written down and
# justified here, rather than made by quietly leaving a field out of the case list (which
# the guard below would not be able to tell from an oversight).
_NOT_REQUIRED: dict[str, str] = {}

# THE PARAMETRIZATION. Derived from the type, never typed out beside it.
_ORDER_PARTS = tuple(
    f.name for f in dataclasses.fields(Order) if f.name not in _NOT_REQUIRED
)

_ORDER_SCALARS = {
    "customer_id": 'customer_id = "user"',
    "customer": 'customer = "the position that posed the critique task"',
    "functional_place": (
        'functional_place = "the norm that governs an act of activity in this engine"'
    ),
    "requirements": (
        'requirements = [\n'
        '  { id = "R1", text = "the order is a typed object, not one free-text string" },\n'
        '  { id = "R2", text = "each part of it is required of a substantive plan" },\n'
        ']'
    ),
}

_COVERAGE = {"R1": ["stage 1 verify_command"], "R2": ["final_check 1"]}

_FINAL_CHECK = '[[final_check]]\ncommand = "pytest -q"\nexpected_exit = 0\n'

_PLAN = """
[meta]
task_id = "mo"
goal = {goal}
done_criterion = {done_criterion}
criterion_type = "measurable"
{weight_class}external_research = "read the corpus audit; no prior art applies"

{order}{final_check}
[[stage]]
index = 1
title = "the stage under test"
executor = "in_thread"
expected_result_image = "The seam requires a typed order of a substantive plan."
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "pytest -q"
material = "m1"
means = "bash"
method = "run"
conditions = "The corpus fixture directory is present."
preconditions = "The branch is checked out."
invariants = "the loader stays lenient"
capability_required = "cap"
material_refs = ["scripts/agentctl/submission.py"]
knowledge_refs = ["scripts/agentctl/plan.py"]
knowledge = "where a submission requirement may bind"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
"""


def _order_block(omit=(), coverage=None) -> str:
    """The `[meta.order]` table, with each named part left out.

    Omission is by DROPPING the author's line, not by writing an empty value: an absent
    key is the shape a real plan that never declared the part has, and it is the shape the
    parametrized refusal has to hold against."""
    if "order" in omit:
        return ""
    lines = [text for name, text in _ORDER_SCALARS.items() if name not in omit]
    out = "[meta.order]\n" + "".join(f"{line}\n" for line in lines)
    if "coverage" not in omit:
        cov = _COVERAGE if coverage is None else coverage
        out += "\n[meta.order.coverage]\n" + "".join(
            f"{k} = {json.dumps(v)}\n" for k, v in cov.items()
        )
    return out + "\n"


def _write_plan(
    path: Path,
    *,
    omit=(),
    coverage=None,
    goal="remove the eighth defect",
    done_criterion="the seam refuses a plan that states no order",
    final_check=True,
    weight_class: str | None = "substantive",
) -> str:
    line = "" if weight_class is None else f'weight_class = "{weight_class}"\n'
    path.write_text(
        _PLAN.format(
            goal=json.dumps(goal),
            done_criterion=json.dumps(done_criterion),
            weight_class=line,
            order=_order_block(omit, coverage),
            final_check=_FINAL_CHECK if final_check else "",
        ),
        encoding="utf-8",
    )
    return str(path)


def ns(**kw):
    return Namespace(**kw)


def _submit(store, plan_path, session="mo"):
    cli.cmd_start(ns(session=session, task="mo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=session, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=session), store=store)
    return cli.cmd_submit_plan(ns(session=session, plan=plan_path), store=store)


def _problems(d) -> list[str]:
    return list(d.data.get("problems", []))


# --- the guard on the enumeration, before anything it guards ------------------


def test_meta_order_refusal_cases_are_the_order_s_own_field_set():
    """THE GUARD. Everything below rests on `_ORDER_PARTS` being the order's parts, so
    this is what makes "each missing part is refused" a claim about the TYPE rather than
    about the cases someone remembered.

    Three assertions, each closing a different way the enumeration could go vacuous: a
    parametrization that collapsed to nothing would make every case below pass by not
    running; a case list that stopped tracking the type — the hand-written copy this test
    exists to forbid — would leave a new part unrequired and silent; and an exemption made
    by omission rather than by declaration is indistinguishable from an oversight, so an
    entry in `_NOT_REQUIRED` has to carry the reason it is there."""
    assert _ORDER_PARTS, "an empty parametrization proves nothing about any part"

    declared = set(_ORDER_PARTS) | set(_NOT_REQUIRED)
    assert declared == {f.name for f in dataclasses.fields(Order)}, (
        "parts of the order neither required nor declared exempt: "
        f"{sorted({f.name for f in dataclasses.fields(Order)} - declared)}; "
        f"named here but not parts of the order: "
        f"{sorted(declared - {f.name for f in dataclasses.fields(Order)})}"
    )
    assert not set(_ORDER_PARTS) & set(_NOT_REQUIRED), (
        "a part cannot be both required and exempt"
    )
    assert all(reason.strip() for reason in _NOT_REQUIRED.values()), (
        "an exemption with no reason is an oversight wearing a declaration's clothes"
    )


# --- one refusing case per part of the order ---------------------------------


@pytest.mark.parametrize("part", _ORDER_PARTS)
def test_meta_order_missing_a_part_is_refused_at_submission(part, tmp_path):
    """One case per part, generated from the type. The plan LOADS — strict — and is then
    refused by the seam, which is the hinge this whole change turns on: had the
    requirement gone into `parse_plan`, the load would raise here instead, and every
    already-approved plan would start failing on its own re-read."""
    plan = _write_plan(tmp_path / f"no_{part}.toml", omit=(part,))

    doc = load_plan(plan, strict=True)  # must not raise

    with pytest.raises(PlanError):
        validate_submission(doc)


def test_meta_order_absent_altogether_is_refused(store, tmp_path):
    """The order table missing entirely — distinct from a missing PART, and the case a
    plan written before this change actually has. End-to-end through submit, because the
    session staying at PLANNING is half of what a refusal has to be: one that advanced the
    node would strand the session with an armed gate and no edge back."""
    plan = _write_plan(tmp_path / "no_order.toml", omit=("order",))

    d = _submit(store, plan)

    assert d.ok is False
    assert d.action == "fix_plan"
    assert store.load("mo").node == Node.PLANNING.value
    assert any("missing the 'order' table" in p for p in _problems(d)), _problems(d)


def test_meta_order_coverage_omitting_a_declared_requirement_is_refused(tmp_path):
    """The one requirement here that is a RELATION, not a presence: the coverage map is
    complete but for R2. Refused by id, so the author is told which requirement nothing
    would establish rather than that "coverage is incomplete".

    Totality is the entire claim. That the named control decides what it is filed under is
    review, and no assertion here pretends otherwise."""
    plan = _write_plan(tmp_path / "partial_cov.toml",
                       coverage={"R1": ["stage 1 verify_command"]})

    problems = submission_violations(load_plan(plan))

    assert any("R2" in p and "[meta.order.coverage]" in p for p in problems), problems
    assert not any("R1" in p and "[meta.order.coverage]" in p for p in problems), problems


def test_meta_order_requirements_without_an_id_are_refused(tmp_path):
    """An id-less requirement is worse than an uncovered one: the coverage map, the
    acceptance verdicts and the totality check all key on the id, so it is not merely
    uncovered but uncoverABLE. Refused on its own message rather than folded into the
    coverage one, which would tell the author to write a map entry for a key that does
    not exist."""
    path = tmp_path / "no_id.toml"
    _write_plan(path, omit=("requirements",))
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[meta.order]\n",
            '[meta.order]\nrequirements = [ { text = "a bare sentence" } ]\n',
        ),
        encoding="utf-8",
    )

    problems = submission_violations(load_plan(str(path)))

    assert any("carry no 'id'" in p for p in problems), problems


# --- the plan-level requirements that are not parts of the order -------------


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"goal": ""}, "missing 'goal'"),
        ({"done_criterion": ""}, "missing 'done_criterion'"),
        ({"final_check": False}, "missing 'final_check'"),
    ],
    ids=["empty_goal", "empty_done_criterion", "zero_final_check"],
)
def test_meta_required_plan_level_field_is_refused(kwargs, needle, tmp_path):
    """The three plan-level places that are not parts of `[meta.order]`. Enumerated as
    cases rather than over a type because they are not one object's fields — they are
    `[meta]` scalars and a top-level table array — and `submission._SUBSTANTIVE_META_FIELDS`
    is checked against by the totality test in test_supply_edges.py, not here.

    An empty string and an absent key are one case on purpose: the seam tests falsiness, so
    `goal = ""` is refused exactly as silence is. A plan that says nothing about its goal
    and one that says it in zero characters make the same claim."""
    plan = _write_plan(tmp_path / "meta.toml", **kwargs)

    problems = submission_violations(load_plan(plan))

    assert any(needle in p for p in problems), problems


# --- acceptance, and the order as readable data ------------------------------


def test_meta_order_compliant_plan_is_accepted(store, tmp_path):
    """The control for every refusal above: the SAME fixture, complete, submits clean.
    Without it a refusal could be coming from any other defect in the fixture and none of
    the tests above would prove anything about the order."""
    plan = _write_plan(tmp_path / "ok.toml")

    d = _submit(store, plan)

    assert d.ok is True, _problems(d)


def test_order_parse_round_trips_the_order_as_data(tmp_path):
    """The order is DATA after parsing, not a string a later consumer re-parses. Stage 8
    checks an acceptance author against `customer_id` and ranges verdicts over the
    requirement ids, and neither is possible if the id is the first token of a sentence.

    `coverage` is asserted as a dict-of-lists keyed by id — the shape the coverage
    resolver ranges over — rather than merely non-empty, because "readable as data" is the
    property under test and a str would satisfy a truthiness check."""
    order = load_plan(_write_plan(tmp_path / "rt.toml")).meta.order

    assert order.customer_id == "user"
    assert order.customer.startswith("the position")
    assert order.functional_place.startswith("the norm")
    assert [(r.id, r.text.split(",")[0]) for r in order.requirements] == [
        ("R1", "the order is a typed object"),
        ("R2", "each part of it is required of a substantive plan"),
    ]
    assert order.coverage == _COVERAGE
    assert order.coverage["R1"] == ["stage 1 verify_command"]


def test_order_parse_survives_a_malformed_order_table(tmp_path):
    """`Order.from_dict` is TOTAL, and that totality is a property of the loader path, not
    a convenience: a plan whose `[meta.order]` is nonsense must still LOAD — the loader
    refuses nothing here — and be refused where refusals belong. Without this, a
    malformed order in an already-approved plan would raise on the session's own re-read,
    which is the retroactivity the seam exists to prevent."""
    path = tmp_path / "malformed.toml"
    _write_plan(path, omit=("requirements", "coverage"))
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[meta.order]\n",
            '[meta.order]\nrequirements = "a sentence where a list belongs"\n'
            'coverage = "and a string where a map belongs"\n',
        ),
        encoding="utf-8",
    )

    order = load_plan(str(path), strict=True).meta.order  # must not raise

    assert order.requirements == []
    assert order.coverage == {}
    assert any("missing 'requirements'" in p
               for p in submission_violations(load_plan(str(path)))), "still refused"


# --- the carve-outs: nothing already accepted is affected --------------------


def test_meta_order_is_not_required_of_a_non_substantive_plan(tmp_path):
    """The grade is the substantive one, exactly as for `knowledge` and `preconditions`.
    Requiring a customer, a functional place and a coverage map of a two-stage errand
    would be the engine demanding an activity ontology of an errand."""
    plan = _write_plan(tmp_path / "small.toml", omit=("order",), final_check=False,
                       weight_class="small_change")

    assert submission_violations(load_plan(plan)) == []


def test_meta_order_the_loader_never_refuses_an_orderless_plan(tmp_path):
    """The hinge, one fixture and both directions: `load_plan(strict=True)` accepts a plan
    with no order and `submission_violations` refuses the same document. A later tidy that
    moves this check into `parse_plan` — where the rest of the meta validation already
    lives, so it will look right — turns this red instead of silently making the
    requirement retroactive over every plan a live session re-reads."""
    plan = _write_plan(tmp_path / "hinge.toml", omit=("order",))

    doc = load_plan(plan, strict=True)  # must not raise

    assert doc.meta.order is None
    assert [p for p in submission_violations(doc) if "missing the 'order' table" in p]
    with pytest.raises(PlanError, match="missing the 'order' table"):
        validate_submission(doc)


def test_meta_order_every_frozen_corpus_plan_loads_unaffected(tmp_path):
    """THE NEGATIVE CLAIM, over the WHOLE corpus rather than a sample. None of the 55
    frozen plans carries an order — they all predate the table — and this change must not
    move how a single one of them loads, because nothing was migrated and nothing could
    be: three attestations are computed over a plan's bytes and cannot be re-signed on the
    plan's behalf.

    THE CLAIM IS NOT "every plan loads strict with no error", and that is a correction to
    this stage's own done criterion rather than a softening of it. 30 of the 55 corpus
    plans ALREADY raise under `strict=True` — `[stage.principle].derivation` became
    required after most of them were written — so the literal wording was false of the
    corpus before this change existed, and a test asserting it could only ever have been
    made green by weakening its domain to the 25 that happen to pass. What is asserted
    instead is the property that wording was reaching for, in its strongest true form:

      * `strict=False` — the mode every already-authored plan is actually re-read through
        — loads all 55 with no error, and none of them acquires an order;
      * `strict=True` raises nothing about the ORDER on any of them: either the plan loads
        (and its order is None) or it raises the same pre-existing PlanError it always did.

    The plan-by-plan pin on that second half is test_frozen_plan_compat.py, which compares
    every corpus plan's strict outcome — message included — against the committed
    baseline; it is the second half of this stage's verify_command for exactly this
    reason. This test carries the half that is universal without a baseline.

    Two guards keep the enumeration from going quiet. The count floor: `glob` returning
    three files reads exactly like a passing test, and a domain that quietly shrank is how
    an assertion over "the whole fixture" becomes an assertion over a sample with nobody
    editing the assertion (50 is under the 55 frozen, so an ordinary addition or removal
    does not trip it and a broken enumeration does). And the partition non-degeneracy: if
    every plan raised under strict, the strict half would be vacuously satisfied by a
    corpus that loads nothing at all."""
    plans = sorted(CORPUS_DIR.glob("*.toml"))
    assert len(plans) >= 50, f"the corpus enumeration found only {len(plans)} plans"

    strict_ok = []
    for p in plans:
        lenient = load_plan(str(p), strict=False)  # must not raise, for any plan
        assert lenient.meta.order is None, f"{p.name} unexpectedly declares [meta.order]"
        try:
            doc = load_plan(str(p), strict=True)
        except PlanError as exc:
            assert "order" not in str(exc), (
                f"{p.name} now raises about the order under strict: {exc}"
            )
            continue
        assert doc.meta.order is None, f"{p.name} unexpectedly declares [meta.order]"
        strict_ok.append(p.name)

    assert strict_ok, "no corpus plan loads strict at all — the strict half proves nothing"


# --- the meta-level change-decision obligation -------------------------------


def test_meta_order_adding_a_requirement_is_a_substantive_replan(tmp_path):
    """The meta-level half of the standing obligation that a field the engine can hold is
    known to every function deciding what counts as a change to it.

    Adding a requirement changes what the plan is FOR, so it re-arms the plan-approval
    gate rather than passing as a wording fix. That is `plan.order_scope`, spliced into
    `_structural_signature`; without it a requirement could be added to an approved plan
    and the replan would read as a refinement the user never sees."""
    old = load_plan(_write_plan(tmp_path / "old.toml"), strict=False)
    new = load_plan(
        _write_plan(tmp_path / "new.toml",
                    coverage={**_COVERAGE, "R3": ["stage 1 verify_command"]}),
        strict=False,
    )

    assert diff_plans(old, new) == "substantive"


def test_meta_order_rewording_the_order_is_a_refinement(tmp_path):
    """The other half. A re-worded functional place is a correction — the kind an
    overcome-difficulty replan makes — so it must diff as a refinement and not vanish.
    Without `plan.order_place` in the refinement tier it would read as 'no_change' and be
    silently dropped, the exact failure this key family exists to prevent."""
    old = load_plan(_write_plan(tmp_path / "old.toml"), strict=False)
    path = tmp_path / "new.toml"
    _write_plan(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "the norm that governs an act of activity in this engine",
            "the norm that governs an act of activity, its filling defective",
        ),
        encoding="utf-8",
    )
    new = load_plan(str(path), strict=False)

    assert diff_plans(old, new) == "refinement"


def test_meta_order_an_orderless_plan_classifies_exactly_as_before(tmp_path):
    """The identity that makes the two tests above safe to add: for a plan with no order
    both key contributions are the EMPTY tuple, so every plan authored before this field
    classifies byte-identically to the way it did. A `... or ""` default here would flip
    every such comparison to a spurious change."""
    from agentctl.plan import order_place, order_scope

    old = load_plan(_write_plan(tmp_path / "old.toml", omit=("order",)), strict=False)
    new = load_plan(_write_plan(tmp_path / "new.toml", omit=("order",)), strict=False)

    assert order_scope(old.meta) == () and order_place(old.meta) == ()
    assert diff_plans(old, new) == "no_change"
