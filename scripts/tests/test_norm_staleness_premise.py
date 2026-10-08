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

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from agentctl import cli, plan, plugins, premise, tool_contracts
from agentctl.plan import diff_plans, load_plan
from agentctl.state import SessionState

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
"""


def _write_plan(path, stages):
    """`stages`: (index, result image, method) triples."""
    body = [
        "[meta]",
        'task_id = "demo-norm-staleness"',
        'goal = "exercise the norm delta"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        "",
    ]
    prev = None
    for i, img, method in stages:
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, method=method, deps=deps))
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


BASE = [(1, "img-one", "do it by hand"), (2, "img-two", "do it by hand")]


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


def _grantgrowth(command):
    doc = load_plan(str(FIXTURES / "plan_two_stage_verifyfix_grantgrowth.toml"))
    doc.stages[0].criterion.verify_command = command
    return doc


def _first_pass(store, tmp_path, stages=None):
    plan_path = _write_plan(tmp_path / "plan.toml", stages or BASE)
    _state(store, plan_path)
    _enumerate(store, _runner(""))
    return plan_path


# --- enumeration narrowed to the moved elements of moved stages -------------------

def test_unmoved_element_of_a_moved_stage_is_out_of_scope(store, tmp_path):
    """Only the result image of stage 1 changed: a question addressed to its means is
    listed as out of scope, a question addressed to its result is raised."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, [(1, "img-one-EDITED", "do it by hand"), BASE[1]])
    d = _enumerate(store, _runner(
        "stage:1.means\tis the tool right?\nstage:1.result\tis the image checkable?"))

    assert d.data["out_of_scope"] == [
        {"target": "stage:1.means", "question": "is the tool right?",
         "reason": premise.CANDIDATE_UNMOVED_ELEMENT},
    ]
    targets = {c["target"] for c in _bag(store)["candidates"]}
    assert targets == {"stage:1.result"}


def test_moved_element_pair_survives(store, tmp_path):
    """The pair addressed to the element that moved is an ordinary candidate."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, [(1, "img-one-EDITED", "do it by hand"), BASE[1]])
    d = _enumerate(store, _runner("stage:1.result\tis the image checkable?"))

    assert d.data["out_of_scope"] == []
    assert {c["target"] for c in _bag(store)["candidates"]} == {"stage:1.result"}


def test_whole_stage_mapped_element_survives_when_the_stage_moved(store, tmp_path):
    """`order` shares the whole-stage key, so any move of the stage moves it."""
    plan_path = _first_pass(store, tmp_path)
    _write_plan(plan_path, [(1, "img-one-EDITED", "do it by hand"), BASE[1]])
    _enumerate(store, _runner("stage:1.order\tis the order of this stage right?"))

    assert {c["target"] for c in _bag(store)["candidates"]} == {"stage:1.order"}


def test_bag_without_element_baselines_keeps_whole_stage_scope(store, tmp_path):
    """A bag written before element baselines existed cannot say which element moved:
    every element of a moved stage stays in scope."""
    plan_path = _first_pass(store, tmp_path)
    state = store.load("s")
    state.plugins["premise"].pop("enumerated_stage_elements", None)
    store.save(state)
    _write_plan(plan_path, [(1, "img-one-EDITED", "do it by hand"), BASE[1]])
    d = _enumerate(store, _runner(
        "stage:1.means\tis the tool right?\nstage:1.result\tis the image checkable?"))

    assert d.data["out_of_scope"] == []
    assert {c["target"] for c in _bag(store)["candidates"]} == {"stage:1.means", "stage:1.result"}


def test_pass_records_element_baselines_of_the_stages_it_read(store, tmp_path):
    plan_path = _first_pass(store, tmp_path)
    recorded = _bag(store)["enumerated_stage_elements"]
    assert set(recorded) == {"1", "2"}
    assert recorded["1"]["result"] != recorded["2"]["result"]

    _write_plan(plan_path, [(1, "img-one-EDITED", "do it by hand"), BASE[1]])
    _enumerate(store, _runner(""))
    after = _bag(store)["enumerated_stage_elements"]
    assert after["1"]["result"] != recorded["1"]["result"]
    assert after["2"] == recorded["2"]


# --- the norm delta itself ---------------------------------------------------------

def test_norm_delta_names_the_moved_elements_only(tmp_path):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    new = load_plan(_write_plan(tmp_path / "b.toml", [(1, "img-one-EDITED", "do it by hand"), BASE[1]]))
    delta = plan.norm_delta(old, new)

    assert delta.moved_stages == frozenset({1})
    assert "result" in delta.question_elements(1)
    assert "means" not in delta.question_elements(1)
    assert 1 in delta.interface_moved


def test_method_edit_moves_the_stage_but_not_its_interface(tmp_path):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    new = load_plan(_write_plan(tmp_path / "b.toml", [(1, "img-one", "do it differently"), BASE[1]]))
    delta = plan.norm_delta(old, new)

    assert delta.moved_stages == frozenset({1})
    assert delta.interface_moved == frozenset()
    assert "method" in delta.question_elements(1)


def test_identical_plans_have_no_delta(tmp_path):
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    assert not plan.norm_delta(doc, doc).any_moved


# --- O-items bound to the covering stage's deliverable ---------------------------------

def _stamp(state, plan_path):
    element = premise.OrderElement(id="O1", element="the ask", disposition="covered", stage=1)
    return cli._bound_order_stage_key(state, element, str(plan_path))


def test_order_stamp_survives_a_method_only_edit(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", BASE)
    state = _state(store, plan_path)
    before = _stamp(state, plan_path)
    _write_plan(plan_path, [(1, "img-one", "do it differently"), BASE[1]])

    assert before and _stamp(state, plan_path) == before


def test_order_stamp_moves_with_the_result_image(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", BASE)
    state = _state(store, plan_path)
    before = _stamp(state, plan_path)
    _write_plan(plan_path, [(1, "img-one-EDITED", "do it by hand"), BASE[1]])

    assert _stamp(state, plan_path) != before


def test_covered_element_is_not_stale_after_a_method_only_edit(tmp_path):
    """Gate-level view of the same: `invalidate_stale_order_dispositions` leaves a
    covered element unmarked when only the covering stage's method moved."""
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    new = load_plan(_write_plan(tmp_path / "b.toml", [(1, "img-one", "do it differently"), BASE[1]]))
    stamp = premise.order_binding_key(plan.stage_norm_keys(old.stages[0]))
    bag = {"order_elements": premise.order_elements_to_dicts([
        premise.OrderElement(id="O1", element="the ask", disposition="covered", stage=1,
                             content_digest=stamp)])}

    premise.invalidate_stale_order_dispositions(
        bag, {s.index: plan.stage_norm_keys(s) for s in new.stages})
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note == ""


def test_legacy_whole_stage_stamp_stays_accepted(tmp_path):
    """An element stamped by the older engine carries the whole-stage key; it keeps
    discharging until that whole definition moves."""
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    keys = {s.index: plan.stage_norm_keys(s) for s in doc.stages}
    legacy = keys[1][plan.WHOLE_STAGE_ELEMENT]
    element = premise.OrderElement(id="O1", element="the ask", disposition="covered", stage=1,
                                   content_digest=legacy)
    bag = {"order_elements": premise.order_elements_to_dicts([element])}

    assert not premise.invalidate_stale_order_dispositions(bag, keys)
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note == ""


def test_foreign_stamp_is_stale(tmp_path):
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    keys = {s.index: plan.stage_norm_keys(s) for s in doc.stages}
    element = premise.OrderElement(id="O1", element="the ask", disposition="covered", stage=1,
                                   content_digest="iface:not-this-stage")
    bag = {"order_elements": premise.order_elements_to_dicts([element])}

    assert premise.invalidate_stale_order_dispositions(bag, keys)
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note


# --- classification from the norm delta ------------------------------------------------

def test_verify_command_arguments_only_edit_is_a_refinement():
    assert diff_plans(_grantgrowth("python3 mod.py"), _grantgrowth("python3 mod.py --strict")) \
        == "refinement"


def test_verify_command_new_script_is_substantive():
    assert diff_plans(_grantgrowth("python3 mod.py"), _grantgrowth("python3 other.py")) \
        == "substantive"


def test_kind_within_boundary_escalates_on_unresolved_changed_commands(monkeypatch):
    verdict = {"order_changed": False, "extra_resources": [], "unresolved_changed_commands": ["x"]}
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "ledgered")
    monkeypatch.setattr(cli, "_autonomy_for", lambda state, doc: verdict)

    assert cli._kind_within_boundary(object(), "refinement", object()) == "substantive"


def test_kind_within_boundary_keeps_refinement_when_nothing_escapes(monkeypatch):
    verdict = {"order_changed": False, "extra_resources": [], "unresolved_changed_commands": []}
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "ledgered")
    monkeypatch.setattr(cli, "_autonomy_for", lambda state, doc: verdict)

    assert cli._kind_within_boundary(object(), "refinement", object()) == "refinement"


def test_command_substitution_is_unresolved(tmp_path):
    """The relaxation never reaches a rewritten command the engine cannot read: nested
    execution resolves to nothing, so the boundary escalates it."""
    resolution = tool_contracts.resolve_command("python3 $(echo mod.py)", str(tmp_path))
    assert resolution.status == "unresolved"
