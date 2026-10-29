"""The agent's understanding of an order — activity, difficulty, proposals, constraints —
as part of `[meta.order]`: typed, digestible per part, well-formed at the submission seam,
and invisible to every order that does not state it.

Four claims, each pinned in the direction that could fail:

* the fields parse leniently and default empty (a stored plan loads as before);
* each thing the user can agree has its own digest, so they can agree them one at a time;
* an ill-formed table is refused at the seam, an incomplete understanding is graded only by
  `order_understanding_completeness_violations`;
* an order stating none of it keeps the exact keys the BASE commit computes for it. The
  golden is not a literal copied out of the new code: the base tree is extracted with
  `git archive` and asked, so "unchanged" is a re-runnable comparison. The base must be an
  ancestor of HEAD and must hold neither `Proposal` nor `understanding_digest`.

A test earns trust only when it goes red on a real mutation, so `order_agreement_mutants`
plants six defects against this file and `test_every_mutant_is_killed` re-runs this file under each and
asserts pytest exit 1 and the intended test among the failures. Every call into the code a
mutant replaces goes through its module (`plan_mod.`, `submission.`), never a by-name import.
"""
from __future__ import annotations

import argparse
import copy
import inspect
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from agentctl import cli
from agentctl import plan as plan_mod
from agentctl import submission
from agentctl.store import FileStateStore
from agentctl.plan import PlanError, diff_plans, load_order_draft, load_plan, order_place, order_scope
from agentctl.state import PROPOSAL_VERDICTS, Constraint, Order, Proposal
from agentctl.submission import _ORDER_ABSENT, submission_violations, validate_submission
from order_agreement_mutants import ENV, MUTANTS, USAGE_ERROR, apply_active_mutant
from test_meta_order import _write_plan
from test_order_phase import _draft as _order_draft

REPO_ROOT = Path(__file__).resolve().parents[2]

# The commit the understanding fields were added on top of. Pinned, not read from the
# environment: a comparison whose base can be redirected is a comparison that can be made
# to pass.
ORDER_AGREEMENT_BASE = "7390425c831837295c9294c30eed7942f3b1a490"

# R1 and R2 are the requirements `_write_plan` declares; `against` points at them.
_GOOD = '''activity = "release engineering"
difficulty = "a release takes a day of hand-work"
proposals = [
  { id = "P1", text = "tag releases automatically", verdict = "adopt", critique = "cheap, reversible", against = ["R1"] },
  { id = "P2", text = "rewrite the packager", verdict = "reject", critique = "no gain for the risk", against = ["R2", "R1"] },
]
constraints = [
  { id = "K1", text = "no new dependency" },
]
'''

_PARTS_OF_GOOD = {
    "customer", "activity", "difficulty", "functional_place",
    "R:R1", "R:R2", "P:P1", "P:P2", "K:K1",
}


@pytest.fixture(autouse=True)
def _planted_mutant(monkeypatch):
    apply_active_mutant(monkeypatch)


def _doc(tmp_path, extra="", *, name="p.toml", omit=()):
    """A substantive plan whose `[meta.order]` additionally holds the lines `extra`."""
    path = Path(_write_plan(tmp_path / name, omit=omit))
    if extra:
        text = path.read_text(encoding="utf-8")
        assert "[meta.order]\n" in text
        path.write_text(text.replace("[meta.order]\n", "[meta.order]\n" + extra, 1), encoding="utf-8")
    return load_plan(str(path), strict=False)


@pytest.fixture
def good(tmp_path):
    return _doc(tmp_path, _GOOD)


# --- the fields: lenient parse, empty defaults -------------------------------


def test_the_understanding_defaults_empty():
    order = Order.from_dict({})

    assert (order.activity, order.difficulty, order.proposals, order.constraints) == ("", "", [], [])
    assert order.malformed == ()
    assert PROPOSAL_VERDICTS == ("adopt", "adapt", "reject")


def test_the_understanding_parses_into_typed_entries():
    order = Order.from_dict({
        "activity": "a", "difficulty": "d",
        "proposals": [{"id": "P1", "text": "t", "verdict": "adapt", "critique": "c",
                       "against": ["R1", "R2"]}],
        "constraints": [{"id": "K1", "text": "t"}],
    })

    assert (order.activity, order.difficulty) == ("a", "d")
    assert order.proposals == [Proposal("P1", "t", "adapt", "c", ["R1", "R2"])]
    assert order.constraints == [Constraint("K1", "t")]
    assert order.malformed == ()


@pytest.mark.parametrize("against,expected", [("R1", ["R1"]), (7, []), ({"R1": 1}, []), (None, [])])
def test_a_proposal_reads_a_bare_or_unreadable_against_leniently(against, expected):
    raw = {"id": "P1", "against": against} if against is not None else {"id": "P1"}

    assert Order.from_dict({"proposals": [raw]}).proposals[0].against == expected


@pytest.mark.parametrize("name", ["proposals", "constraints"])
def test_a_non_list_table_degrades_and_is_recorded(name):
    order = Order.from_dict({name: "one long sentence"})

    assert getattr(order, name) == []
    assert name in order.malformed


@pytest.mark.parametrize("name", ["proposals", "constraints"])
def test_a_bare_sentence_among_the_tables_is_dropped_and_recorded(name):
    order = Order.from_dict({name: ["a sentence", {"id": "X1"}]})

    assert [e.id for e in getattr(order, name)] == ["X1"]
    assert name in order.malformed


def test_a_verdict_outside_the_vocabulary_survives_the_loader():
    """Refusal belongs to the submission seam; the loader must read what a live session
    already holds."""
    order = Order.from_dict({"proposals": [{"id": "P1", "verdict": "maybe"}]})

    assert order.proposals[0].verdict == "maybe"
    assert order.malformed == ()


# --- the digest: one hash per thing the user agrees --------------------------


def test_the_parts_are_one_per_agreeable_thing(good):
    assert set(plan_mod.understanding_parts(good)) == _PARTS_OF_GOOD


def test_a_doc_without_an_order_has_no_parts(tmp_path):
    doc = _doc(tmp_path, omit=("order",))

    assert plan_mod.understanding_parts(doc) == {}
    assert plan_mod.understanding_digest(doc) == plan_mod.understanding_digest(doc)


def _edit(order: Order, part: str, how: str) -> None:
    if part in ("customer", "activity", "difficulty", "functional_place"):
        attr = "customer_id" if how == "customer_id" else part
        setattr(order, attr, getattr(order, attr) + " (changed)")
        return
    kind, ident = part.split(":")
    item = {"R": {r.id: r for r in order.requirements},
            "P": {p.id: p for p in order.proposals},
            "K": {c.id: c for c in order.constraints}}[kind][ident]
    if how == "against":
        item.against = [*item.against, "R2"] if "R2" not in item.against else item.against[:1]
    else:
        setattr(item, how, getattr(item, how) + " (changed)" if how != "verdict" else "adapt")


_EDITS = [
    ("customer", "customer"), ("customer", "customer_id"),
    ("activity", "activity"), ("difficulty", "difficulty"), ("functional_place", "functional_place"),
    ("R:R1", "text"), ("R:R1", "derivation"),
    ("P:P1", "text"), ("P:P1", "verdict"), ("P:P1", "critique"), ("P:P1", "against"),
    ("K:K1", "text"),
]


@pytest.mark.parametrize("part,how", _EDITS)
def test_digest_covers_every_part(good, part, how):
    """Editing one thing moves that part's digest, the whole digest, and nothing else."""
    edited = copy.deepcopy(good)
    _edit(edited.meta.order, part, how)
    before, after = plan_mod.understanding_parts(good), plan_mod.understanding_parts(edited)

    assert set(before) == set(after)
    assert {k for k in before if before[k] != after[k]} == {part}
    assert plan_mod.understanding_digest(good) != plan_mod.understanding_digest(edited)


def test_what_the_agent_answers_is_not_part_of_the_understanding(good):
    """goal, done_criterion, coverage and requires_traceability may be settled after the
    user has agreed, so none of them moves the digest."""
    edited = copy.deepcopy(good)
    edited.meta.goal += " (changed)"
    edited.meta.done_criterion += " (changed)"
    edited.meta.order.coverage["R1"] = ["something else"]
    edited.meta.order.requires_traceability = not edited.meta.order.requires_traceability

    assert plan_mod.understanding_parts(edited) == plan_mod.understanding_parts(good)
    assert plan_mod.understanding_digest(edited) == plan_mod.understanding_digest(good)


def test_reordering_tables_and_against_changes_no_digest(good):
    edited = copy.deepcopy(good)
    order = edited.meta.order
    for items in (order.requirements, order.proposals, order.constraints):
        items.reverse()
    for p in order.proposals:
        p.against.reverse()

    assert plan_mod.understanding_parts(edited) == plan_mod.understanding_parts(good)
    assert plan_mod.understanding_digest(edited) == plan_mod.understanding_digest(good)


def test_a_text_moving_between_parts_moves_the_digest(tmp_path):
    one = _doc(tmp_path, 'activity = "x"\n', name="a.toml")
    two = _doc(tmp_path, 'difficulty = "x"\n', name="b.toml")

    assert plan_mod.understanding_digest(one) != plan_mod.understanding_digest(two)


# --- the draft loader --------------------------------------------------------


def _full_plan_text(tmp_path):
    path = Path(_write_plan(tmp_path / "full.toml"))
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("[meta.order]\n", "[meta.order]\n" + _GOOD, 1), encoding="utf-8")
    return path


def test_a_stageless_draft_loads_with_no_stages(tmp_path):
    full = _full_plan_text(tmp_path)
    draft = tmp_path / "draft.toml"
    draft.write_text(full.read_text(encoding="utf-8").split("[[stage]]")[0], encoding="utf-8")

    doc, staged = load_order_draft(draft)

    assert staged is False
    assert doc.stages == []
    assert doc.meta.order.activity == "release engineering"
    assert [p.id for p in doc.meta.order.proposals] == ["P1", "P2"]
    assert [c.id for c in doc.meta.order.constraints] == ["K1"]


def test_a_staged_file_loads_as_a_plan_with_the_same_digest(tmp_path):
    full = _full_plan_text(tmp_path)

    doc, staged = load_order_draft(full)

    assert staged is True
    assert doc.stages
    assert plan_mod.understanding_digest(doc) == plan_mod.understanding_digest(load_plan(str(full), strict=False))


def test_the_draft_and_the_plan_it_becomes_agree_on_the_digest(tmp_path):
    full = _full_plan_text(tmp_path)
    draft = tmp_path / "draft.toml"
    draft.write_text(full.read_text(encoding="utf-8").split("[[stage]]")[0], encoding="utf-8")

    drafted, _ = load_order_draft(draft)

    assert plan_mod.understanding_parts(drafted) == plan_mod.understanding_parts(load_plan(str(full), strict=False))
    assert plan_mod.understanding_digest(drafted) == plan_mod.understanding_digest(load_plan(str(full), strict=False))


def test_a_draft_without_an_order_is_refused(tmp_path):
    for body in ('[meta]\ntask_id = "x"\n', 'goal = "no meta table"\n'):
        draft = tmp_path / "draft.toml"
        draft.write_text(body, encoding="utf-8")
        with pytest.raises(PlanError, match=r"no \[meta\.order\]"):
            load_order_draft(draft)


def test_a_draft_needs_neither_task_id_nor_goal(tmp_path):
    draft = tmp_path / "draft.toml"
    draft.write_text('[meta.order]\nactivity = "a"\n', encoding="utf-8")

    doc, staged = load_order_draft(draft)

    assert (staged, doc.meta.order.activity, doc.meta.task_id) == (False, "a", "")


def test_load_order_draft_refuses_a_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(PlanError, match="not found"):
        load_order_draft(tmp_path / "absent.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[meta.order\n", encoding="utf-8")
    with pytest.raises(PlanError, match="malformed TOML"):
        load_order_draft(bad)


def test_load_order_draft_degrades_a_non_table_order(tmp_path):
    draft = tmp_path / "draft.toml"
    draft.write_text('[meta]\norder = "a sentence"\n', encoding="utf-8")

    doc, _ = load_order_draft(draft)

    assert "order" in doc.meta.order.malformed


# --- the keys: nothing moves when nothing is stated --------------------------


def test_empty_understanding_adds_no_member_to_place_or_scope(tmp_path):
    meta = _doc(tmp_path).meta
    ids = tuple(r.id for r in meta.order.requirements)

    assert order_scope(meta) == ((ids, tuple(sorted(meta.order.coverage))),)
    assert len(order_place(meta)) == 1
    assert len(order_place(meta)[0]) == 8


def test_a_stated_understanding_reaches_both_keys(tmp_path):
    bare, stated = _doc(tmp_path, name="a.toml").meta, _doc(tmp_path, _GOOD, name="b.toml").meta

    assert len(order_scope(stated)) == len(order_scope(bare)) + 1
    assert len(order_place(stated)[0]) == len(order_place(bare)[0]) + 1


def test_adding_a_proposal_or_changing_a_verdict_is_substantive(tmp_path):
    old = _doc(tmp_path, _GOOD, name="old.toml")
    more = _doc(tmp_path, _GOOD.replace(
        "proposals = [",
        'proposals = [\n  { id = "P3", text = "i", verdict = "adopt", critique = "r", against = ["R1"] },'),
        name="more.toml")
    flipped = _doc(tmp_path, _GOOD.replace('verdict = "adopt"', 'verdict = "adapt"'), name="flip.toml")

    assert diff_plans(old, more) == "substantive"
    assert diff_plans(old, flipped) == "substantive"


def test_adding_a_constraint_is_substantive(tmp_path):
    old = _doc(tmp_path, _GOOD, name="old.toml")
    new = _doc(tmp_path, _GOOD.replace(
        "constraints = [", 'constraints = [\n  { id = "K2", text = "t" },'), name="new.toml")

    assert diff_plans(old, new) == "substantive"


@pytest.mark.parametrize("old,new", [
    ("release engineering", "release engineering, rewritten"),
    ("tag releases automatically", "tag every release automatically"),
    ("cheap, reversible", "cheap and easily reversed"),
    ("no new dependency", "no new runtime dependency"),
])
def test_rewording_the_understanding_is_a_refinement(tmp_path, old, new):
    a = _doc(tmp_path, _GOOD, name="a.toml")
    b = _doc(tmp_path, _GOOD.replace(old, new), name="b.toml")

    assert diff_plans(a, b) == "refinement"


def test_stating_an_understanding_where_there_was_none_is_a_refinement_at_least(tmp_path):
    assert diff_plans(_doc(tmp_path, name="a.toml"), _doc(tmp_path, 'activity = "x"\n', name="b.toml")) != "no_change"


# --- the submission seam ------------------------------------------------------


def _seam(tmp_path, extra):
    return submission_violations(_doc(tmp_path, extra))


def test_a_well_formed_understanding_passes_the_seam(tmp_path):
    assert _seam(tmp_path, _GOOD) == []


def test_an_order_stating_no_understanding_passes_the_seam(tmp_path):
    assert _seam(tmp_path, "") == []


def test_seam_refuses_a_verdict_outside_the_vocabulary(tmp_path):
    problems = _seam(tmp_path, _GOOD.replace('verdict = "reject"', 'verdict = "maybe"'))

    assert any("P2" in p and "verdict outside adopt/adapt/reject" in p for p in problems), problems


_P = 'id = "P1", text = "t", verdict = "adopt", critique = "c", against = ["R1"]'

_ILL_FORMED = [
    ("proposal without id",
     'proposals = [{ text = "t", verdict = "adopt", critique = "c", against = ["R1"] }]\n',
     "carry no 'id'"),
    ("proposal without text",
     'proposals = [{ id = "P1", verdict = "adopt", critique = "c", against = ["R1"] }]\n',
     "no 'text'"),
    ("proposal without critique",
     'proposals = [{ id = "P1", text = "t", verdict = "adopt", against = ["R1"] }]\n',
     "no 'critique'"),
    ("proposal without against",
     'proposals = [{ id = "P1", text = "t", verdict = "adopt", critique = "c" }]\n',
     "no 'against'"),
    ("proposal against an undeclared requirement",
     'proposals = [{ id = "P1", text = "t", verdict = "adopt", critique = "c", against = ["R9"] }]\n',
     "P1->R9"),
    ("proposal against a proposal",
     f'proposals = [{{ {_P} }}, {{ id = "P2", text = "t", verdict = "adopt", critique = "c", '
     'against = ["P1"] }]\n',
     "P2->P1"),
    ("duplicate proposal id", f'proposals = [{{ {_P} }}, {{ {_P} }}]\n', "name more than one"),
    ("proposal id colliding with a requirement",
     'proposals = [{ id = "R1", text = "t", verdict = "adopt", critique = "c", against = ["R2"] }]\n',
     "name more than one"),
    ("constraint without id", 'constraints = [{ text = "t" }]\n', "carry no 'id'"),
    ("constraint without text", 'constraints = [{ id = "K1" }]\n', "no 'text'"),
    ("duplicate constraint id",
     'constraints = [{ id = "K1", text = "t" }, { id = "K1", text = "u" }]\n', "name more than one"),
    ("constraint id colliding with a requirement",
     'constraints = [{ id = "R2", text = "t" }]\n', "name more than one"),
    ("constraint id colliding with a proposal",
     f'proposals = [{{ {_P} }}]\nconstraints = [{{ id = "P1", text = "t" }}]\n', "name more than one"),
    ("proposals not a list", 'proposals = "one long sentence"\n', "proposals"),
    ("bare sentence among constraints",
     'constraints = ["a sentence", { id = "K1", text = "t" }]\n', "constraints"),
]


@pytest.mark.parametrize("what,extra,expected", _ILL_FORMED, ids=[w for w, _, _ in _ILL_FORMED])
def test_seam_refuses_an_ill_formed_table(tmp_path, what, extra, expected):
    problems = _seam(tmp_path, extra)

    assert any(expected in p for p in problems), (what, problems)


_PROPOSAL_LINE = (
    'proposals = [{{ id = "P1", text = "tag releases automatically", verdict = "{verdict}", '
    'critique = "cheap, reversible", against = {against} }}]'
)


def _order_submit(tmp_path, monkeypatch, **proposal):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    store = FileStateStore(tmp_path / "agentctl" / "state")
    cli.cmd_start(argparse.Namespace(session="s", task="od", goal="g", done_criterion="dc",
                                     criterion_type="measurable", recursion_depth=0), store=store)
    fields = {"verdict": "adopt", "against": '["R1"]', **proposal}
    draft = _order_draft(tmp_path, replace={"proposals": _PROPOSAL_LINE.format(**fields)})
    d = cli.cmd_order_submit(argparse.Namespace(session="s", plan=str(draft)), store=store)
    return d, store.load("s")


def test_order_submit_accepts_a_well_formed_proposal(tmp_path, monkeypatch):
    d, state = _order_submit(tmp_path, monkeypatch)

    assert d.ok, d.detail
    assert state.order_draft_path and state.order_draft_sha256


@pytest.mark.parametrize("proposal, expected", [
    ({"verdict": "maybe"}, "verdict outside adopt/adapt/reject"),
    ({"against": '["R9"]'}, "P1->R9"),
])
def test_order_submit_refuses_a_malformed_proposal_and_records_nothing(
        tmp_path, monkeypatch, proposal, expected):
    d, state = _order_submit(tmp_path, monkeypatch, **proposal)

    assert d.ok is False and any(expected in p for p in d.data["problems"]), d.detail
    assert not state.order_draft_path and not state.order_draft_sha256


def test_a_requirement_id_repeated_among_requirements_is_not_reported_twice(tmp_path):
    doc = _doc(tmp_path, 'requirements = [{ id = "R1", text = "a" }, { id = "R1", text = "b" }]\n',
               omit=("requirements",))
    problems = submission_violations(doc)

    assert sum("requirement id(s) R1" in p for p in problems) == 1
    assert not any("name more than one" in p for p in problems)


def test_the_loader_reads_an_ill_formed_understanding_and_validation_refuses_it(tmp_path):
    """Well-formedness binds at the seam, not in the loader: a stored plan re-reads."""
    doc = _doc(tmp_path, _GOOD.replace('verdict = "reject"', 'verdict = "maybe"'))

    assert doc.meta.order.proposals[1].verdict == "maybe"
    with pytest.raises(PlanError, match="verdict outside"):
        validate_submission(doc)


def test_no_order_means_nothing_to_grade_here(tmp_path):
    assert submission._order_understanding_violations(_doc(tmp_path, omit=("order",)).meta) == []


# --- the completeness grade (stage 2's order-submit asks for it) -------------


def _incomplete(tmp_path, extra="", **kw):
    # Through the module so a mutant that replaces the function is seen by the tests.
    return submission.order_understanding_completeness_violations(_doc(tmp_path, extra, **kw).meta)


def test_a_complete_understanding_has_no_completeness_violation(tmp_path):
    assert _incomplete(tmp_path, _GOOD) == []


def test_completeness_asks_for_neither_proposals_nor_constraints(tmp_path):
    assert _incomplete(tmp_path, 'activity = "a"\ndifficulty = "d"\n') == []


def test_completeness_reports_an_absent_activity_and_difficulty(tmp_path):
    problems = _incomplete(tmp_path)

    assert any("missing 'activity'" in p for p in problems), problems
    assert any("missing 'difficulty'" in p for p in problems), problems


def test_completeness_reports_each_missing_part_on_its_own(tmp_path):
    only_activity = _incomplete(tmp_path, 'activity = "a"\n')

    assert not any("'activity'" in p for p in only_activity)
    assert any("missing 'difficulty'" in p for p in only_activity)


@pytest.mark.parametrize("name", ["customer_id", "customer", "functional_place", "requirements"])
def test_completeness_reports_a_missing_order_part(tmp_path, name):
    problems = _incomplete(tmp_path, 'activity = "a"\ndifficulty = "d"\n', omit=(name,))

    assert any(f"missing {name!r}" in p for p in problems), (name, problems)


@pytest.mark.parametrize("name", ["activity", "difficulty"])
def test_a_blank_activity_or_difficulty_is_missing(tmp_path, name):
    other = "difficulty" if name == "activity" else "activity"

    problems = _incomplete(tmp_path, f'{name} = "   "\n{other} = "x"\n')

    assert any(f"missing {name!r}" in p for p in problems), problems


def test_completeness_reports_a_malformed_requirements_table_as_malformed_not_missing(tmp_path):
    problems = _incomplete(tmp_path, 'activity = "a"\ndifficulty = "d"\nrequirements = "one sentence"\n',
                           omit=("requirements",))

    assert not any("missing 'requirements'" in p for p in problems), problems
    assert any("requirements" in p for p in problems), problems


def test_completeness_grades_the_requirements_themselves(tmp_path):
    problems = _incomplete(
        tmp_path,
        'activity = "a"\ndifficulty = "d"\n'
        'requirements = [{ text = "no id" }, { id = "R2" }]\n',
        omit=("requirements",),
    )

    assert any("carry no 'id'" in p for p in problems), problems
    assert any("R2" in p and "no 'text'" in p for p in problems), problems


def test_completeness_asks_for_a_derivation_on_every_requirement(tmp_path):
    problems = _incomplete(
        tmp_path,
        'activity = "a"\ndifficulty = "d"\n'
        'requirements = [{ id = "R1", text = "t" }, { id = "R2", text = "u", derivation = "v" }]\n',
        omit=("requirements",),
    )

    assert any("R1" in p and "no derivation" in p for p in problems), problems
    assert not any("R2" in p and "no derivation" in p for p in problems), problems


def test_completeness_asks_for_traceability_and_still_for_derivations(tmp_path):
    problems = _incomplete(
        tmp_path,
        'activity = "a"\ndifficulty = "d"\nrequirements = [{ id = "R1", text = "t" }]\n',
        omit=("requirements", "requires_traceability"),
    )

    assert any("requires_traceability" in p for p in problems), problems
    assert any("R1" in p and "no derivation" in p for p in problems), problems


def test_completeness_does_not_ask_for_coverage(tmp_path):
    assert _incomplete(tmp_path, _GOOD, omit=("coverage",)) == []


def test_completeness_without_an_order_names_the_absence(tmp_path):
    assert _incomplete(tmp_path, omit=("order",)) == [_ORDER_ABSENT]


def test_completeness_of_a_non_table_order_names_only_that(tmp_path):
    meta = _doc(tmp_path).meta
    meta.order = Order(malformed=("order",))

    problems = submission.order_understanding_completeness_violations(meta)

    assert len(problems) == 1 and "order" in problems[0]


def test_complete_implies_well_formed(tmp_path):
    problems = _incomplete(tmp_path, _GOOD.replace('verdict = "reject"', 'verdict = "maybe"'))

    assert any("verdict outside" in p for p in problems), problems


# --- every stored order keeps the keys the BASE commit computes ---------------


def _keys(plan, path):
    """Every digest/key the understanding must not move, computed by whichever `plan`
    module it is handed. Self-contained on purpose: its source runs in the base tree."""
    doc = plan.load_plan(path, strict=False)
    return {
        "place": repr(plan.order_place(doc.meta)),
        "scope": repr(plan.order_scope(doc.meta)),
        "order_digest": plan.order_digest(doc),
        "meta_digest": plan.plan_meta_digest(doc),
        "parts": plan.part_digest_map(doc),
        "content": plan.plan_content_digest(doc),
    }


_BASE_SCRIPT = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from agentctl import plan\n"
    + inspect.getsource(_keys)
    + "print(json.dumps(_keys(plan, sys.argv[2])))\n"
)


@pytest.fixture(scope="session")
def base_scripts_dir(tmp_path_factory):
    """`scripts/` of ORDER_AGREEMENT_BASE, extracted from git. A missing history is a
    failure, never a skip: a skipped comparison reads as a passed one."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", ORDER_AGREEMENT_BASE, "scripts"],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"cannot read {ORDER_AGREEMENT_BASE} from git ({proc.stderr.decode(errors='replace')[:200]}); "
            "the base-key comparison needs the history that holds it"
        )
    root = tmp_path_factory.mktemp("order_agreement_base")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(root, filter="data")
    return root / "scripts"


def test_the_base_commit_is_an_ancestor_of_head():
    """A base that is not in this branch's history compares against nothing the branch grew from."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", ORDER_AGREEMENT_BASE, "HEAD"],
        capture_output=True,
    )

    assert proc.returncode == 0, (proc.returncode, proc.stderr.decode(errors="replace")[:200])


def test_the_base_tree_predates_the_understanding(base_scripts_dir):
    """The golden is only a golden if the base holds no trace of what is being compared."""
    sources = [p for p in (base_scripts_dir / "agentctl").rglob("*.py")]
    assert sources, "the base archive holds no engine"

    for needle in ("Proposal", "understanding_digest"):
        holders = [str(p.relative_to(base_scripts_dir)) for p in sources
                   if needle in p.read_text(encoding="utf-8", errors="replace")]
        assert holders == [], (needle, holders[:5])


@pytest.mark.parametrize("omit", [(), ("order",)], ids=["with-order", "order-less"])
def test_an_order_without_the_understanding_keeps_its_base_keys(tmp_path, base_scripts_dir, omit):
    path = _write_plan(tmp_path / "golden.toml", omit=omit)

    base = subprocess.run(
        [sys.executable, "-I", "-c", _BASE_SCRIPT, str(base_scripts_dir), path],
        capture_output=True, text=True,
    )
    assert base.returncode == 0, base.stderr[-500:]

    assert _keys(plan_mod, path) == json.loads(base.stdout)


# --- the negative control: each planted defect is caught --------------------

_KILLERS = {
    "verdict_unchecked": "test_seam_refuses_a_verdict_outside_the_vocabulary",
    "digest_ignores_constraints": "test_digest_covers_every_part",
    "completeness_unchecked": "test_completeness_reports_an_absent_activity_and_difficulty",
    "place_contributes_when_empty": "test_empty_understanding_adds_no_member_to_place_or_scope",
    "digest_proposal_partial": "test_digest_covers_every_part",
    "seam_unwired": "test_seam_refuses_a_verdict_outside_the_vocabulary",
}


def _run_this_file(mutant: str, *flags: str):
    env = {**os.environ, ENV: mutant}
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-q", "-p", "no:cacheprovider",
         "--tb=no", "-k", "not golden and not base_keys", *flags],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
@pytest.mark.parametrize("mutant", sorted(_KILLERS))
def test_every_mutant_is_killed(mutant):
    assert mutant in MUTANTS, f"{mutant} is not in the mutant table"

    result = _run_this_file(mutant, "-rf")

    assert result.returncode == 1, (mutant, result.returncode, result.stdout[-800:])
    assert f"::{_KILLERS[mutant]}" in result.stdout, (mutant, result.stdout[-800:])


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
def test_an_unknown_mutant_fails_closed():
    result = _run_this_file("no_such_mutant")

    assert result.returncode == USAGE_ERROR, (result.returncode, result.stderr[-400:])


_MISSING_TARGET_SCRIPT = """
import os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import pytest
import order_agreement_mutants as m
m.MUTANTS["gone"] = lambda mp: mp.setattr("agentctl.submission._no_such_function", None, raising=True)
os.environ[m.ENV] = "gone"
with pytest.MonkeyPatch.context() as mp:
    m.apply_active_mutant(mp)
sys.exit(0)
"""


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
def test_a_mutant_whose_target_is_gone_fails_closed():
    tests_dir = Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, "-c", _MISSING_TARGET_SCRIPT, str(tests_dir.parent), str(tests_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == USAGE_ERROR, (result.returncode, result.stderr[-400:])
    assert "no target to patch" in result.stderr, result.stderr[-400:]
