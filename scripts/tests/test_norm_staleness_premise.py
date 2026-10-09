"""Stage 2 of norm-staleness: one norm delta read by every consumer of "what changed".

The difficulty: a plan edit that left a stage's deliverable alone still re-opened work
keyed on the whole stage definition -- the enumeration re-read every question target of a
moved stage, a covered order element went stale on a change to HOW the stage proceeds, a
verify_command rewrite that kept its program and script was judged substantive, and a
refinement that changed a command the engine cannot resolve passed as inside the
autonomy boundary. Each case is exercised here against the real `plan`/`cli`/`premise`
code; the pure `plan.norm_delta` API is called at test time (not imported by name), so
this module collects on a tree that predates it."""
from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

# pytest trims a failure's message from the short summary at the terminal width; the stage's
# negative control reads that message to tell an assertion from an error.
os.environ.setdefault("COLUMNS", "200")

from agentctl import cli, grants, order_approvals, plan, plan_resources, plugins, plugins_premise, premise, tool_contracts  # noqa: E402
from agentctl.plan import diff_plans, load_plan  # noqa: E402
from agentctl.state import SessionState  # noqa: E402
from agentctl.text_shape import WHOLE_STAGE_ELEMENT  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _runner(stdout):
    prompts: list[str] = []

    def run(argv, **kw):
        prompts.append(kw.get("stdin", ""))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    run.prompts = prompts
    return run


_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
means = "tool"
method = "{method}"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
{verify}"""


def _write_plan(path, stages, *, verify=None, final_checks=(), functional_place=None, repo_root=None):
    """`stages`: (index, result image, method) triples. `verify` maps a stage index to its
    verify_command; `final_checks` are commands; `functional_place` declares an order."""
    body = [
        "[meta]",
        'task_id = "demo-norm-staleness"',
        'goal = "exercise the norm delta"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
    ]
    if repo_root is not None:
        body.append(f"repo_root = {json.dumps(str(repo_root))}")
    if functional_place is not None:
        body += ["", "[meta.order]", 'customer_id = "acme"', 'customer = "the asker"',
                 f"functional_place = {json.dumps(functional_place)}"]
    body.append("")
    prev = None
    for i, img, method in stages:
        command = (verify or {}).get(i)
        extra = f"verify_command = {json.dumps(command)}\nexpected_exit = 0\n" if command else ""
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, method=method, deps=deps, verify=extra))
        prev = i
    for n, command in enumerate(final_checks, 1):
        body.append(f'[[final_check]]\nlabel = "check {n}"\ncommand = {json.dumps(command)}\nexpected_exit = 0\n')
    path.write_text("\n".join(body), encoding="utf-8")
    return path


BASE = [(1, "img-one", "do it by hand"), (2, "img-two", "do it by hand")]
EDITED_RESULT = [(1, "img-one-EDITED", "do it by hand"), BASE[1]]
EDITED_METHOD = [(1, "img-one", "do it differently"), BASE[1]]


def _state(store, plan_path, sid="s"):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    state.plan_path = str(plan_path)
    store.save(state)
    return state


def _enumerate(store, run, sid="s"):
    return cli.cmd_question_enumerate(
        Namespace(session=sid, reopen_dismissed=False), store=store, runner=run)


def _bag(store, sid="s"):
    return store.load(sid).plugins["premise"]


def _first_pass(store, tmp_path, stages=None):
    plan_path = _write_plan(tmp_path / "plan.toml", stages or BASE)
    _state(store, plan_path)
    _enumerate(store, _runner(""))
    return plan_path


def _candidate_targets(store):
    return {c["target"] for c in _bag(store)["candidates"]}


# --- enumeration narrowed to the moved elements of moved stages -------------------

def test_unmoved_element_of_a_moved_stage_is_out_of_scope(store, tmp_path):
    """Only the result image of stage 1 changed: a question addressed to its means is
    listed as out of scope, a question addressed to its result is raised."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, EDITED_RESULT)
    d = _enumerate(store, _runner(
        "stage:1.means\tis the tool right?\nstage:1.result\tis the image checkable?"))

    assert _candidate_targets(store) == {"stage:1.result"}
    assert d.data["out_of_scope"] == [
        {"target": "stage:1.means", "question": "is the tool right?",
         "reason": premise.CANDIDATE_UNMOVED_ELEMENT},
    ]


def test_moved_element_pair_survives(store, tmp_path):
    """The pair addressed to the element that moved is an ordinary candidate."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, EDITED_RESULT)
    d = _enumerate(store, _runner("stage:1.result\tis the image checkable?"))

    assert d.data["out_of_scope"] == []
    assert _candidate_targets(store) == {"stage:1.result"}


def test_whole_stage_mapped_element_survives_when_the_stage_moved(store, tmp_path):
    """`order` shares the whole-stage key, so any move of the stage moves it."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, EDITED_RESULT)
    _enumerate(store, _runner("stage:1.order\tis the order of this stage right?"))

    assert _candidate_targets(store) == {"stage:1.order"}


def test_bag_without_element_baselines_keeps_whole_stage_scope(store, tmp_path):
    """A bag written before element baselines existed cannot say which element moved:
    every element of a moved stage stays in scope."""
    plan_path = _first_pass(store, tmp_path)
    state = store.load("s")
    state.plugins["premise"].pop("enumerated_stage_elements", None)
    store.save(state)
    _write_plan(plan_path, EDITED_RESULT)
    d = _enumerate(store, _runner(
        "stage:1.means\tis the tool right?\nstage:1.result\tis the image checkable?"))

    assert d.data["out_of_scope"] == []
    assert _candidate_targets(store) == {"stage:1.means", "stage:1.result"}


def test_pass_records_element_baselines_of_the_stages_it_read(store, tmp_path):
    plan_path = _first_pass(store, tmp_path)
    recorded = _bag(store).get("enumerated_stage_elements") or {}
    assert set(recorded) == {"1", "2"}
    assert recorded["1"]["result"] != recorded["2"]["result"]

    _write_plan(plan_path, EDITED_RESULT)
    _enumerate(store, _runner(""))
    after = _bag(store)["enumerated_stage_elements"]
    assert after["1"]["result"] != recorded["1"]["result"]
    assert after["2"] == recorded["2"]


def test_element_scope_leaves_out_a_stage_that_did_not_move(store, tmp_path):
    """An in-scope stage with nothing moved has no entry: it is read whole, never as an
    empty scope that would drop every question addressed to it."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, EDITED_RESULT)
    doc = load_plan(str(plan_path))

    scope = plugins_premise.enumeration_element_scope(_bag(store), doc, {1, 2})
    assert set(scope) == {1}
    assert "result" in scope[1]


def test_plan_level_question_survives_a_stage_and_final_check_move(store, tmp_path):
    """The final checks are the plan-level control: a pass narrowed to a moved stage
    still raises a pair addressed to the plan, and only the unmoved stage element drops."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, EDITED_RESULT, final_checks=["git status"])
    d = _enumerate(store, _runner(
        "plan.done_criterion\tdoes the final check bite?\nstage:1.means\tis the tool right?"))

    assert "plan.done_criterion" in _candidate_targets(store)
    assert [o["target"] for o in d.data["out_of_scope"]] == ["stage:1.means"]


def test_final_check_only_edit_leaves_every_question_in_scope(store, tmp_path):
    """No stage moved, so there is nothing to narrow against: a question addressed to a
    stage element and one addressed to the plan are both raised."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, BASE, final_checks=["git status"])
    d = _enumerate(store, _runner(
        "plan.done_criterion\tdoes the final check bite?\nstage:1.means\tis the tool right?"))

    assert d.data["out_of_scope"] == []
    assert _candidate_targets(store) == {"plan.done_criterion", "stage:1.means"}


# --- the norm delta itself ---------------------------------------------------------

def _delta(tmp_path, new_stages, **new_kwargs):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    new = load_plan(_write_plan(tmp_path / "b.toml", new_stages, **new_kwargs))
    return plan.norm_delta(old, new)


def test_norm_delta_names_the_moved_elements_only(tmp_path):
    delta = _delta(tmp_path, EDITED_RESULT)

    assert delta.moved_stages == frozenset({1})
    assert "result" in delta.question_elements(1)
    assert "means" not in delta.question_elements(1)
    assert 1 in delta.interface_moved


def test_method_edit_moves_the_stage_but_not_its_interface(tmp_path):
    delta = _delta(tmp_path, EDITED_METHOD)

    assert delta.moved_stages == frozenset({1})
    assert delta.interface_moved == frozenset()
    assert "method" in delta.question_elements(1)


def test_identical_plans_have_no_delta(tmp_path):
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    assert not plan.norm_delta(doc, doc).any_moved


def test_order_only_edit_moves_the_order_and_no_stage(tmp_path):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE, functional_place="one place"))
    new = load_plan(_write_plan(tmp_path / "b.toml", BASE, functional_place="another place"))
    delta = plan.norm_delta(old, new)

    assert delta.order_moved and delta.any_moved
    assert not delta.moved_stages
    assert not delta.final_check_moved


def test_final_check_only_edit_moves_the_final_check_and_nothing_else(tmp_path):
    delta = _delta(tmp_path, BASE, final_checks=["git status"])

    assert delta.final_check_moved and delta.any_moved
    assert not delta.moved_stages
    assert not delta.meta_moved


# --- O-items bound to the covering stage's deliverable ---------------------------------

def _covered(stamp):
    return premise.OrderElement(id="O1", element="the ask", disposition="covered", stage=1,
                                content_digest=stamp)


def _stamp(plan_path):
    return cli._bound_order_stage_key(SimpleNamespace(), _covered(""), str(plan_path))


def _norm_keys(doc):
    build = getattr(plan, "stage_norm_keys", plan.stage_element_keys)
    return {s.index: build(s) for s in doc.stages}


def _stale_note_after_edit(tmp_path, edited_stages):
    """The stale note of an element stamped against BASE once the plan became `edited_stages`."""
    old_path = _write_plan(tmp_path / "a.toml", BASE)
    new = load_plan(_write_plan(tmp_path / "b.toml", edited_stages))
    bag = {"order_elements": premise.order_elements_to_dicts([_covered(_stamp(old_path))])}

    premise.invalidate_stale_order_dispositions(bag, _norm_keys(new))
    return premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note


def test_covered_element_is_not_stale_after_a_method_only_edit(tmp_path):
    assert _stale_note_after_edit(tmp_path, EDITED_METHOD) == ""


def test_covered_element_is_stale_after_the_result_image_moves(tmp_path):
    assert _stale_note_after_edit(tmp_path, EDITED_RESULT)


def test_order_stamp_survives_a_method_only_edit(tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", BASE)
    before = _stamp(plan_path)
    _write_plan(plan_path, EDITED_METHOD)

    assert before
    assert _stamp(plan_path) == before


def test_order_stamp_moves_with_the_result_image(tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", BASE)
    before = _stamp(plan_path)
    _write_plan(plan_path, EDITED_RESULT)

    assert _stamp(plan_path) != before


def test_legacy_whole_stage_stamp_stays_accepted(tmp_path):
    """An element stamped by the older engine carries the whole-stage key; it keeps
    discharging until that whole definition moves."""
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    keys = _norm_keys(doc)
    bag = {"order_elements": premise.order_elements_to_dicts([_covered(keys[1][WHOLE_STAGE_ELEMENT])])}

    assert not premise.invalidate_stale_order_dispositions(bag, keys)
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note == ""


def test_foreign_stamp_is_stale(tmp_path):
    keys = _norm_keys(load_plan(_write_plan(tmp_path / "a.toml", BASE)))
    bag = {"order_elements": premise.order_elements_to_dicts([_covered("iface:not-this-stage")])}

    assert premise.invalidate_stale_order_dispositions(bag, keys)
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note


# --- classification from the norm delta ------------------------------------------------

def _grantgrowth(command):
    doc = load_plan(str(FIXTURES / "plan_two_stage_verifyfix_grantgrowth.toml"))
    doc.stages[0].criterion.verify_command = command
    return doc


def _verify_kind(old, new):
    return diff_plans(_grantgrowth(old), _grantgrowth(new))


@pytest.mark.parametrize("old, new", [
    ("python3 mod.py", "python3 mod.py --strict"),
    ("python3 -m a", "python3 -m a --x"),
    ("python3 -W ignore mod.py", "python3 mod.py"),
    ("python3 mod.py", "python3 mod.py -m other"),
])
def test_same_program_and_script_with_other_arguments_is_a_refinement(old, new):
    assert _verify_kind(old, new) == "refinement"


@pytest.mark.parametrize("old, new", [
    ("python3 mod.py", "python3 other.py"),
    ("python3 -m a", "python3 -m b"),
    ("python3 mod.py", "python3 -m mod"),
    ("python3 mod.py", "python3.11 -W ignore mod.py"),
    ("python3 mod.py", "python3 -u mod.py"),
    ("python3 mod.py", "env python3 mod.py"),
    ("python3 mod.py", "timeout 5 python3 mod.py"),
    ("bash x.sh", "bash -o pipefail x.sh"),
])
def test_a_new_program_script_module_or_uncovered_flag_is_growth(old, new):
    assert _verify_kind(old, new) == "substantive"


@pytest.mark.parametrize("rule, identity", [
    ("Bash(python3 mod.py:*)", ("python3", "mod.py")),
    ("Bash(python3 mod.py --strict:*)", ("python3", "mod.py")),
    ("Bash(python3 -W ignore mod.py:*)", ("python3", "mod.py")),
    ("Bash(python3 mod.py -m a:*)", ("python3", "mod.py")),
    ("Bash(python3 -m a:*)", ("python3", "-m", "a")),
    ("Bash(python3 -m a --x:*)", ("python3", "-m", "a")),
    ("Bash(pytest -q tests/a.py:*)", ("pytest",)),
    ("Bash(python3:*)", None),
    ("Bash(python3 -u:*)", None),
    ("Bash(python3 -u mod.py:*)", None),
    ("Bash(python3 -m:*)", None),
    ("Bash(python3 -m pip install x:*)", None),
    ("Bash(python3 -c print:*)", None),
    ("Bash(python3.11 -W ignore x.py:*)", None),
    ("Bash(bash -o pipefail x.sh:*)", None),
    ("Bash(env python3 mod.py:*)", None),
    ("Bash(timeout 5 python3 mod.py:*)", None),
    ("Bash(nice python3 mod.py:*)", None),
    ("Edit(//tmp/x/**)", None),
])
def test_rule_identity_never_hands_a_new_executable_an_approved_identity(rule, identity):
    assert grants.bash_rule_identity(rule) == identity


def test_rewrite_that_also_moves_the_interface_keeps_the_plain_set_difference():
    old = _grantgrowth("python3 mod.py")
    new = _grantgrowth("python3 mod.py --strict")
    new.stages[0].subject.result = "a different deliverable"

    assert "interface" in plan.norm_delta(old, new).elements[1]
    assert diff_plans(old, new) == "substantive"


def test_rewrite_that_also_moves_the_order_keeps_the_plain_set_difference(tmp_path):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE, verify={1: "python3 mod.py"},
                                functional_place="one place"))
    new = load_plan(_write_plan(tmp_path / "b.toml", BASE, verify={1: "python3 mod.py --strict"},
                                functional_place="another place"))

    assert diff_plans(old, new) == "substantive"


# --- the autonomy boundary, through the real ledger -------------------------------------

def _approved_order(store, tmp_path, old_verify):
    """A session whose order has a user-approved version holding `old_verify`; returns the
    state and the venue the plans declare."""
    venue = tmp_path / "venue"
    venue.mkdir()
    base = _write_plan(tmp_path / "base.toml", BASE, verify={1: old_verify}, repo_root=venue)
    doc = load_plan(str(base))
    key = plan.order_digest(doc)
    view = plan_resources.compute_boundary_view(doc, key)
    order_approvals.record_approval(
        key, plan_sha256="p1", resources=view.resources,
        unresolved_identities=[item["identity"] for item in view.unresolved],
        stage_effects=[], by="acme", at="2026-10-09T00:00:00Z")
    return _state(store, base), venue


def _replan_verdict(store, tmp_path, old_verify, new_verify):
    """`(diff_plans kind, kind after the boundary)` of rewriting stage 1's verify_command."""
    state, venue = _approved_order(store, tmp_path, old_verify)
    new = load_plan(str(_write_plan(tmp_path / "new.toml", BASE, verify={1: new_verify}, repo_root=venue)))
    kind = diff_plans(load_plan(state.plan_path), new)
    return kind, cli._kind_within_boundary(state, kind, new)


def test_refinement_to_a_command_the_engine_cannot_resolve_goes_to_approval(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "python3 mod.py", "python3 mod.py --strict")

    assert bounded == "substantive"
    assert kind == "refinement"


@pytest.mark.parametrize("suffix", ["$(curl x)", "`curl x`"])
def test_nested_execution_in_a_rewritten_command_is_never_inside_the_boundary(store, tmp_path, suffix):
    """Derivation drops a segment it cannot read, so the grant set does not grow and the
    plain diff calls this a refinement; the whole text is unresolved, which the boundary
    escalates."""
    kind, bounded = _replan_verdict(store, tmp_path, "python3 mod.py", f"python3 mod.py {suffix}")

    assert bounded == "substantive"
    assert kind == "refinement"


@pytest.mark.parametrize("new_verify", ["python3 -u", "python3", "python3 -m pip"])
def test_interpreter_without_a_script_or_with_a_launcher_module_goes_to_approval(
        store, tmp_path, new_verify):
    """Derivation grants no rule to these, so the plain diff sees no growth; the boundary
    still reads the command as a changed unresolved one."""
    _kind, bounded = _replan_verdict(store, tmp_path, "python3 mod.py", new_verify)

    assert bounded == "substantive"


def test_refinement_to_a_resolved_command_inside_the_set_stays_a_refinement(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "pytest -q tests/a.py", "pytest -q tests/a.py -x")

    assert kind == "refinement"
    assert bounded == "refinement"


def test_rewrite_to_a_new_program_is_substantive_before_the_boundary(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "pytest -q tests/a.py", "ruff check tests/a.py")

    assert kind == "substantive"
    assert bounded == "substantive"


def test_command_substitution_is_unresolved(tmp_path):
    """The relaxation never reaches a rewritten command the engine cannot read: nested
    execution resolves to nothing, so the boundary escalates it."""
    resolution = tool_contracts.resolve_command("python3 $(echo mod.py)", str(tmp_path))
    assert resolution.status == "unresolved"
