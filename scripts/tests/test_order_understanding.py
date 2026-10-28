"""The agent's understanding of an order — activity, difficulty, proposals, constraints —
as part of `[meta.order]`: typed, digestible per part, well-formed at the submission seam,
and invisible to every order that does not state it.

Four claims, each pinned in the direction that could fail:

* the fields parse leniently and default empty (a stored plan loads as before);
* each part has its own digest, so the user can agree them one at a time;
* an ill-formed table is refused at the seam, an incomplete understanding is graded only by
  `order_understanding_completeness_violations`;
* an order stating none of it keeps the exact keys the BASE commit computes for it. The
  golden is not a literal copied out of the new code: the base tree is extracted with
  `git archive` and asked, so "unchanged" is a re-runnable comparison.

A test earns trust only when it goes red on a real mutation, so `order_agreement_mutants`
plants four defects and `test_every_mutant_is_killed` re-runs this file under each and
asserts pytest exit 1 and the intended test among the failures.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from agentctl import plan as plan_mod
from agentctl import submission
from agentctl.plan import (
    UNDERSTANDING_PARTS,
    PlanError,
    diff_plans,
    load_order_draft,
    load_plan,
    order_place,
    order_scope,
    understanding_digest,
    understanding_parts,
)
from agentctl.state import PROPOSAL_VERDICTS, Constraint, Order, Proposal
from agentctl.submission import (
    _ORDER_ABSENT,
    _order_understanding_violations,
    submission_violations,
    validate_submission,
)
from order_agreement_mutants import ENV, MUTANTS, USAGE_ERROR, apply_active_mutant
from test_meta_order import _write_plan

REPO_ROOT = Path(__file__).resolve().parents[2]

# The commit the understanding fields were added on top of. Pinned, not read from the
# environment: a comparison whose base can be redirected is a comparison that can be made
# to pass.
ORDER_AGREEMENT_BASE = "7390425c831837295c9294c30eed7942f3b1a490"

_GOOD = '''activity = "release engineering"
difficulty = "a release takes a day of hand-work"
proposals = [
  { id = "P1", idea = "tag releases automatically", verdict = "adopt", rationale = "cheap, reversible" },
  { id = "P2", idea = "rewrite the packager", verdict = "reject", rationale = "no gain for the risk" },
]
constraints = [
  { id = "C1", text = "no new dependency", source = "the user" },
]
'''


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


def _good_order() -> Order:
    return Order(
        activity="release engineering",
        difficulty="a release takes a day",
        proposals=[Proposal("P1", "tag automatically", "adopt", "cheap"),
                   Proposal("P2", "rewrite it", "reject", "no gain")],
        constraints=[Constraint("C1", "no new dependency", "the user")],
    )


# --- the fields: lenient parse, empty defaults -------------------------------


def test_the_understanding_defaults_empty():
    order = Order.from_dict({})

    assert (order.activity, order.difficulty, order.proposals, order.constraints) == ("", "", [], [])
    assert order.malformed == ()
    assert PROPOSAL_VERDICTS == ("adopt", "adapt", "reject")


def test_the_understanding_parses_into_typed_entries():
    order = Order.from_dict({
        "activity": "a", "difficulty": "d",
        "proposals": [{"id": "P1", "idea": "i", "verdict": "adapt", "rationale": "r"}],
        "constraints": [{"id": "C1", "text": "t", "source": "s"}],
    })

    assert (order.activity, order.difficulty) == ("a", "d")
    assert order.proposals == [Proposal("P1", "i", "adapt", "r")]
    assert order.constraints == [Constraint("C1", "t", "s")]
    assert order.malformed == ()


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


# --- the digest: one hash per agreed part ------------------------------------


def test_the_parts_are_named_and_a_missing_order_reads_as_an_empty_one():
    assert tuple(understanding_parts(_good_order())) == UNDERSTANDING_PARTS
    assert understanding_parts(None) == understanding_parts(Order())
    assert understanding_digest(None) == understanding_digest(Order())


def _edited(part: str) -> Order:
    order = _good_order()
    if part == "activity":
        order.activity += " (changed)"
    elif part == "difficulty":
        order.difficulty += " (changed)"
    elif part == "proposals":
        order.proposals[0].verdict = "adapt"
    else:
        order.constraints[0].text += " (changed)"
    return order


@pytest.mark.parametrize("part", UNDERSTANDING_PARTS)
def test_digest_covers_every_part(part):
    """Editing one part moves that part's digest, the whole digest, and nothing else."""
    before, after = understanding_parts(_good_order()), understanding_parts(_edited(part))

    assert set(before) == set(UNDERSTANDING_PARTS)
    assert {k for k in before if before[k] != after[k]} == {part}
    assert understanding_digest(_good_order()) != understanding_digest(_edited(part))


def test_reordering_proposals_and_constraints_changes_no_digest():
    a = _good_order()
    b = _good_order()
    b.proposals.reverse()
    b.constraints.append(Constraint("C0", "other", ""))
    b.constraints.reverse()
    a.constraints.append(Constraint("C0", "other", ""))

    assert understanding_parts(a) == understanding_parts(b)
    assert understanding_digest(a) == understanding_digest(b)


def test_the_parts_do_not_collide_when_a_text_moves_between_them():
    one, two = Order(activity="x"), Order(difficulty="x")

    assert understanding_digest(one) != understanding_digest(two)


# --- the draft loader --------------------------------------------------------


def test_load_order_draft_reads_a_stageless_order(tmp_path):
    draft = tmp_path / "draft.toml"
    draft.write_text("[meta.order]\n" + _GOOD, encoding="utf-8")

    order = load_order_draft(draft)

    assert order is not None and order.activity == "release engineering"
    assert [p.id for p in order.proposals] == ["P1", "P2"]
    assert [c.id for c in order.constraints] == ["C1"]


def test_load_order_draft_without_an_order_is_none(tmp_path):
    draft = tmp_path / "draft.toml"
    draft.write_text('[meta]\ntask_id = "x"\n', encoding="utf-8")

    assert load_order_draft(draft) is None


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

    order = load_order_draft(draft)

    assert order is not None and "order" in order.malformed


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
        "proposals = [", 'proposals = [\n  { id = "P3", idea = "i", verdict = "adopt", rationale = "r" },'),
        name="more.toml")
    flipped = _doc(tmp_path, _GOOD.replace('verdict = "adopt"', 'verdict = "adapt"'), name="flip.toml")

    assert diff_plans(old, more) == "substantive"
    assert diff_plans(old, flipped) == "substantive"


def test_adding_a_constraint_is_substantive(tmp_path):
    old = _doc(tmp_path, _GOOD, name="old.toml")
    new = _doc(tmp_path, _GOOD.replace(
        "constraints = [", 'constraints = [\n  { id = "C2", text = "t", source = "s" },'), name="new.toml")

    assert diff_plans(old, new) == "substantive"


@pytest.mark.parametrize("old,new", [
    ("release engineering", "release engineering, rewritten"),
    ("tag releases automatically", "tag every release automatically"),
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


@pytest.mark.parametrize("what,extra,expected", [
    ("proposal without id",
     'proposals = [{ idea = "i", verdict = "adopt", rationale = "r" }]\n', "carry no 'id'"),
    ("proposal without idea",
     'proposals = [{ id = "P1", verdict = "adopt", rationale = "r" }]\n', "no 'idea'"),
    ("proposal without rationale",
     'proposals = [{ id = "P1", idea = "i", verdict = "adopt" }]\n', "no 'rationale'"),
    ("duplicate proposal id",
     'proposals = [{ id = "P1", idea = "i", verdict = "adopt", rationale = "r" },'
     ' { id = "P1", idea = "j", verdict = "reject", rationale = "r" }]\n', "declared more than once"),
    ("constraint without id", 'constraints = [{ text = "t" }]\n', "carry no 'id'"),
    ("constraint without text", 'constraints = [{ id = "C1" }]\n', "no 'text'"),
    ("duplicate constraint id",
     'constraints = [{ id = "C1", text = "t" }, { id = "C1", text = "u" }]\n', "declared more than once"),
    ("proposals not a list", 'proposals = "one long sentence"\n', "proposals"),
    ("bare sentence among constraints",
     'constraints = ["a sentence", { id = "C1", text = "t" }]\n', "constraints"),
])
def test_seam_refuses_an_ill_formed_table(tmp_path, what, extra, expected):
    problems = _seam(tmp_path, extra)

    assert any(expected in p for p in problems), (what, problems)


def test_the_loader_reads_an_ill_formed_understanding_and_validation_refuses_it(tmp_path):
    """Well-formedness binds at the seam, not in the loader: a stored plan re-reads."""
    doc = _doc(tmp_path, _GOOD.replace('verdict = "reject"', 'verdict = "maybe"'))

    assert doc.meta.order.proposals[1].verdict == "maybe"
    with pytest.raises(PlanError, match="verdict outside"):
        validate_submission(doc)


def test_no_order_means_nothing_to_grade_here(tmp_path):
    assert _order_understanding_violations(_doc(tmp_path, omit=("order",)).meta) == []


# --- the completeness grade (stage 2's order-submit asks for it) -------------


def order_understanding_completeness_violations(meta):
    # Called through the module so a mutant that replaces the function is seen by the tests.
    return submission.order_understanding_completeness_violations(meta)


def test_a_complete_understanding_has_no_completeness_violation(tmp_path):
    assert order_understanding_completeness_violations(_doc(tmp_path, _GOOD).meta) == []


def test_completeness_reports_an_absent_activity_and_difficulty(tmp_path):
    problems = order_understanding_completeness_violations(_doc(tmp_path).meta)

    assert any("missing 'activity'" in p for p in problems), problems
    assert any("missing 'difficulty'" in p for p in problems), problems


def test_completeness_reports_each_missing_part_on_its_own(tmp_path):
    only_activity = order_understanding_completeness_violations(_doc(tmp_path, 'activity = "a"\n').meta)

    assert not any("'activity'" in p for p in only_activity)
    assert any("missing 'difficulty'" in p for p in only_activity)


def test_completeness_without_an_order_names_the_absence(tmp_path):
    assert order_understanding_completeness_violations(_doc(tmp_path, omit=("order",)).meta) == [_ORDER_ABSENT]


def test_complete_implies_well_formed(tmp_path):
    bad = _GOOD.replace('verdict = "reject"', 'verdict = "maybe"')

    problems = order_understanding_completeness_violations(_doc(tmp_path, bad).meta)

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
