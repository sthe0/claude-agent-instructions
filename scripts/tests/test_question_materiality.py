"""Stage 4 of premise-loop-determinize: a raised question must name the control of
this plan its answer could flip.

Two halves, and the whole point of the stage is that they stay apart:

  * the RULE half — does the named control EXIST in this plan — is decidable from
    the plan document, so the engine decides it, at the WRITE seam
    (`cmd_question_raise`), by the same resolver `check-order-coverage.py` uses.
    Covered here in both directions, plus the two seams that must NOT move: a
    question persisted before the requirement existed still discharges the gate,
    and `check-order-coverage.py`'s own accepted grammars are unchanged.
  * the PERCEPTION half — could the answer really MOVE that control — is a model
    judgement, advisory, never blocking. Covered here with an INJECTED runner: no
    test in this file may reach a live model.

The auto-dismissal of an enumeration candidate addressed to no control of the plan
is the same rule applied to the other channel, and is covered here too — it must be
recorded with the one countable reason, not raised as a blocker for the coordinator.
"""
from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import advisor, cli, controls, plugins, plugins_premise, premise
from agentctl.plan import load_plan, parse_plan
from agentctl.state import SessionState

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_order_coverage_materiality", ROOT / "scripts" / "check-order-coverage.py"
)
check_order_coverage = importlib.util.module_from_spec(_SPEC)
sys.modules["check_order_coverage_materiality"] = check_order_coverage
_SPEC.loader.exec_module(check_order_coverage)


_PLAN = """\
[meta]
task_id = "materiality"
goal = "exercise the question-materiality seam"
done_criterion = "all stages PASSED"
criterion_type = "measurable"

[meta.order]
customer_id = "user"
customer = "the position that posed this fixture's task"
functional_place = "the norm governing an act of activity, in a test"

[[meta.order.requirements]]
id = "R1"
text = "the named control is the one the answer moves"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]

[[final_check]]
command = "true"
label = "the suite is green"

[[stage]]
index = 1
title = "Stage 1"
executor = "in_thread"
expected_result_image = "img"
criterion_type = "measurable"
done_criterion = "stage 1 done"
means = "Edit"
method = "do it"
verify_command = "true"
depends_on = []
output_artifacts = ["s1.py"]
"""


@pytest.fixture
def plan_path(tmp_path):
    path = tmp_path / "plan.toml"
    path.write_text(_PLAN, encoding="utf-8")
    return path


@pytest.fixture
def session(store, plan_path):
    state = SessionState(session_id="s", task_id="t")
    plugins.activate(state, "premise")
    state.plan_path = str(plan_path)
    store.save(state)
    return "s"


def _raise(store, sid, *, id="Q1", target="stage:1.done_criterion", question="?",
           control=None, plan=None, runner=None, **kw):
    ns = Namespace(session=sid, id=id, target=target, question=question, **kw)
    if control is not None:
        ns.control = control
    if plan is not None:
        ns.plan = plan
    return cli.cmd_question_raise(ns, store=store, runner=runner)


def _questions(store, sid):
    return store.load(sid).plugins["premise"]["questions"]


def _answering(text):
    """A runner that answers every judge call with `text` on its first line."""
    def run(argv, **kw):
        return SimpleNamespace(returncode=0, stdout=text, stderr="", timed_out=False)
    return run


# --- the RULE half: refused at the write seam ----------------------------------

def test_a_control_the_plan_does_not_contain_is_refused_at_raise(store, session):
    d = _raise(store, session, control="stage 9 done_criterion")
    assert d.ok is False and d.action == "noop"
    assert "no stage 9" in d.detail
    assert _questions(store, session) == []


def test_a_control_outside_every_grammar_is_refused_with_the_resolvers_own_reason(
        store, session):
    d = _raise(store, session, control="the vibe of the plan")
    assert d.ok is False
    assert "matches none of the five accepted" in d.detail


def test_a_resolvable_control_is_accepted_and_recorded(store, session):
    d = _raise(store, session, control="stage 1 done_criterion")
    assert d.ok is True
    assert _questions(store, session)[0]["control"] == "stage 1 done_criterion"


@pytest.mark.parametrize("control", [
    "stage 1 verify_command",
    "stage 1 done_criterion",
    "final_check 1",
    "order requirement R1",
])
def test_every_materiality_grammar_resolves_against_this_plan(store, session, control):
    assert _raise(store, session, control=control).ok is True


def test_an_empty_control_is_refused_rather_than_treated_as_unnamed(store, session):
    # Otherwise `--control ""` is a one-flag bypass of a required flag, and the
    # question lands looking exactly like a legacy one.
    d = _raise(store, session, control="   ")
    assert d.ok is False and "empty name" in d.detail


def test_the_parser_will_not_let_the_production_path_omit_a_control():
    # cmd_question_raise reads --control with getattr, so a programmatic Namespace
    # without one still works (that is the legacy seam). The ONE production path
    # runs through this parser, and it has no such latitude.
    parser = cli.build_parser()
    parser.parse_args(["question-raise", "--session", "s", "--id", "Q1",
                       "--target", "plan.goal", "--control", "stage 1 done_criterion"])
    with pytest.raises(SystemExit):
        parser.parse_args(["question-raise", "--session", "s", "--id", "Q1",
                           "--target", "plan.goal"])


def test_a_control_is_not_resolved_before_a_plan_exists(store):
    state = SessionState(session_id="p", task_id="t")
    plugins.activate(state, "premise")
    store.save(state)
    d = _raise(store, "p", control="stage 9 done_criterion")
    assert d.ok is True, "undecidable without a plan — raising must stay open"
    assert _questions(store, "p")[0]["control"] == "stage 9 done_criterion"


def test_a_named_plan_resolves_the_control_instead_of_the_sessions_own(
        store, session, tmp_path):
    other = tmp_path / "corrected.toml"
    other.write_text(_PLAN.replace("index = 1", "index = 4")
                          .replace("stage 1 verify_command", "stage 4 verify_command")
                          .replace("stage 1 done", "stage 4 done")
                          .replace('title = "Stage 1"', 'title = "Stage 4"'),
                     encoding="utf-8")
    assert _raise(store, session, control="stage 4 done_criterion").ok is False
    assert _raise(store, session, control="stage 4 done_criterion",
                  plan=str(other)).ok is True


def test_an_unloadable_named_plan_refuses_rather_than_skipping_the_check(store, session):
    d = _raise(store, session, control="stage 9 done_criterion", plan="/no/such/plan.toml")
    assert d.ok is False and "cannot load the plan named by --plan" in d.detail


def test_an_empty_named_plan_is_refused(store, session):
    d = _raise(store, session, control="stage 1 done_criterion", plan="   ")
    assert d.ok is False and "empty path" in d.detail


# --- the MIGRATION seam: a legacy question still discharges the gate -----------

def test_a_question_carrying_no_control_still_discharges_the_gate(store, session):
    state = store.load(session)
    state.plugins["premise"]["questions"] = [{
        "id": "Q_LEGACY", "target": "plan.goal", "question": "raised before the flag",
        "disposition": "assumed", "basis": "the reporter confirmed it",
        "risk": "the reporter may be wrong",
        "own_research": "read the tracker thread and the two prior runs",
    }]
    store.save(state)
    state = store.load(session)
    blockers = plugins_premise.premise_blockers(state, state.plugins["premise"])
    assert not any("Q_LEGACY" in b for b in blockers), blockers
    assert not any("control" in b for b in blockers), blockers


def test_the_round_trip_defaults_a_missing_control_to_empty():
    [q] = premise.questions_from_dicts([{"id": "Q1", "target": "plan.goal"}])
    assert q.control == ""
    assert premise.questions_to_dicts([q])[0]["control"] == ""


# --- the enumeration channel: dismissed, not raised ----------------------------

def _enumerate(store, session, plan_path, pairs):
    state = store.load(session)
    bag = state.plugins["premise"]
    cli._apply_enumeration_result(
        bag, load_plan(plan_path), plan_path, pairs, True)
    return bag["candidates"]


def test_a_candidate_addressed_to_no_control_is_dismissed_with_a_countable_reason(
        store, session, plan_path):
    candidates = _enumerate(store, session, plan_path, [
        ("stage:9.method", "what does the stage that does not exist do?"),
    ])
    assert [c["disposition"] for c in candidates] == ["dismissed"]
    assert candidates[0]["reason"] == premise.CANDIDATE_IMMATERIAL


def test_the_auto_dismissals_are_countable_by_their_one_reason(
        store, session, plan_path):
    candidates = _enumerate(store, session, plan_path, [
        ("stage:9.method", "a question about a stage that is not there"),
        ("stage:8.method", "and another"),
        ("stage:1.method", "a question about a stage that is"),
    ])
    immaterial = [c for c in candidates if c["reason"] == premise.CANDIDATE_IMMATERIAL]
    assert len(immaterial) == 2
    assert all(c["disposition"] == "dismissed" for c in immaterial)


def test_a_candidate_the_plan_does_contain_a_control_for_is_still_raised(
        store, session, plan_path):
    candidates = _enumerate(store, session, plan_path, [
        ("stage:1.method", "is the method actually the cheapest one?"),
    ])
    assert candidates[0]["disposition"] == "raised" and candidates[0]["reason"] == ""


def test_a_plan_level_candidate_is_raised_not_dismissed(store, session, plan_path):
    # No control is derivable from a plan-level or unparseable address, and
    # dismissing on an address WE could not read would discard the question.
    candidates = _enumerate(store, session, plan_path, [
        ("plan.goal", "is the goal the one the order asked for?"),
        ("not-a-target-at-all", "and one nobody can parse"),
    ])
    assert [c["disposition"] for c in candidates] == ["raised", "raised"]


def test_an_auto_dismissed_candidate_does_not_block_the_gate(
        store, session, plan_path):
    state = store.load(session)
    bag = state.plugins["premise"]
    cli._apply_enumeration_result(
        bag, load_plan(plan_path), plan_path,
        [("stage:9.method", "about a stage that is not there")], True)
    store.save(state)
    state = store.load(session)
    blockers = plugins_premise.premise_blockers(state, state.plugins["premise"])
    assert not any("qenum" in b for b in blockers), blockers


# --- the PERCEPTION half: advisory, injected runner, never blocking ------------

def test_a_judged_no_is_surfaced_as_an_advisory_and_never_blocks(
        store, session, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    d = _raise(store, session, control="stage 1 done_criterion",
               question="which font should the report use?",
               runner=_answering("NO"))
    assert d.ok is True
    advisories = d.data.get("advisories") or []
    assert any("never blocking" in a and "stage 1 done_criterion" in a
               for a in advisories)


def test_a_judged_yes_surfaces_nothing(store, session, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    d = _raise(store, session, control="stage 1 done_criterion",
               runner=_answering("YES"))
    assert d.ok is True and not (d.data.get("advisories") or [])


def test_a_fail_open_false_surfaces_nothing(store, session, monkeypatch):
    """The three-valued contract carries the weight here. A judge that timed out
    also returns False; surfacing it would assert the plan's controls are
    indifferent to this question on the strength of a killed subprocess."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")

    def timed_out(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="", timed_out=True)

    d = _raise(store, session, control="stage 1 done_criterion", runner=timed_out)
    assert d.ok is True and not (d.data.get("advisories") or [])


def test_a_raising_judge_never_reaches_the_caller(store, session, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")

    def explode(argv, **kw):
        raise RuntimeError("no model here")

    d = _raise(store, session, control="stage 1 done_criterion", runner=explode)
    assert d.ok is True and not (d.data.get("advisories") or [])


def test_the_judge_is_not_called_when_the_advisor_is_off(store, session, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="NO", stderr="", timed_out=False)

    d = _raise(store, session, control="stage 1 done_criterion", runner=run)
    assert d.ok is True and calls == []


def test_the_prefilter_keeps_the_judge_off_a_question_with_no_text():
    assert advisor.question_materiality_prefilter("stage 1 done_criterion", "q") is True
    assert advisor.question_materiality_prefilter("stage 1 done_criterion", "") is False
    assert advisor.question_materiality_prefilter("", "q") is False


def test_a_prefiltered_out_call_is_a_judged_false_not_a_fail_open_one():
    verdict, reason = advisor.judge_question_materiality("", "", None)
    assert (verdict, reason) == (False, "")


def test_the_judge_timeout_is_the_last_resort_ceiling():
    from lib import judge_latency
    assert advisor._QUESTION_MATERIALITY_TIMEOUT_S == judge_latency.LAST_RESORT_CEILING_S


# --- the SHARED resolver: the coverage caller's grammars are unchanged ---------

def _coverage_doc():
    return parse_plan({
        "meta": {
            "task_id": "t",
            "order": {"requirements": [{"id": "R1", "text": "R1"}],
                      "coverage": {"R1": ["stage 1 verify_command"]}},
        },
        "stage": [{
            "index": 1, "title": "s1", "executor": "in_thread",
            "expected_result_image": "img", "done_criterion": "dc",
            "means": "Edit", "method": "do", "verify_command": "true",
        }],
    })


def test_check_order_coverage_accepts_exactly_its_three_grammars():
    assert controls.COVERAGE_GRAMMARS == (
        controls.STAGE_VERIFY_COMMAND,
        controls.FINAL_CHECK,
        controls.STAGE_LANDED_ASSERTION,
    )


@pytest.mark.parametrize("control", ["stage 1 done_criterion", "order requirement R1"])
def test_the_two_materiality_grammars_stay_out_of_the_coverage_verdict(control):
    doc = _coverage_doc()
    assert controls.resolve_control(
        control, doc, grammars=controls.MATERIALITY_GRAMMARS) is None
    problem = check_order_coverage.resolve_control(control, doc)
    assert problem is not None and "matches none of the three accepted" in problem


@pytest.mark.parametrize("fixture, verdict", [
    ("plan_snapshot_smd-act-defects-8.toml", 0),
    # No [meta.order] at all, so this one has always been a FAIL — the stage's own
    # verify_command names it, and it is red before this change as well as after.
    ("plan_landed_example.toml", 1),
])
def test_the_coverage_verdict_on_a_committed_plan_is_unchanged(fixture, verdict):
    plan = ROOT / "scripts" / "tests" / "fixtures" / fixture
    assert check_order_coverage.main(["check-order-coverage.py", str(plan)]) == verdict


def test_the_coverage_callers_failure_message_still_names_three():
    problem = check_order_coverage.resolve_control("nonsense", _coverage_doc())
    assert "matches none of the three accepted" in problem
    assert "a name outside the grammar is a failure, never a skip" in problem


def test_the_materiality_caller_is_the_only_one_that_widens_the_set():
    assert set(controls.COVERAGE_GRAMMARS) < set(controls.MATERIALITY_GRAMMARS)
    assert set(controls.MATERIALITY_GRAMMARS) - set(controls.COVERAGE_GRAMMARS) == {
        controls.STAGE_DONE_CRITERION, controls.ORDER_REQUIREMENT,
    }


def test_the_resolver_has_no_default_grammar_set():
    with pytest.raises(TypeError):
        controls.resolve_control("stage 1 verify_command", _coverage_doc())


def test_control_text_renders_what_the_address_points_at(plan_path):
    doc = load_plan(plan_path)
    g = controls.MATERIALITY_GRAMMARS
    assert controls.control_text("stage 1 verify_command", doc, grammars=g) == "true"
    assert controls.control_text("stage 1 done_criterion", doc, grammars=g) == "stage 1 done"
    assert controls.control_text("final_check 1", doc, grammars=g) == "the suite is green"
    assert controls.control_text("order requirement R1", doc, grammars=g) == (
        "the named control is the one the answer moves")
    assert controls.control_text("stage 9 done_criterion", doc, grammars=g) == ""


def test_control_text_renders_a_landed_assertion_as_its_containment_claim():
    plan = ROOT / "scripts" / "tests" / "fixtures" / "plan_landed_example.toml"
    doc = load_plan(str(plan))
    landed = next(s.index for s in doc.stages if s.criterion.verify_kind == "landed")
    text = controls.control_text(f"stage {landed} landed assertion", doc,
                                 grammars=controls.MATERIALITY_GRAMMARS)
    spec = next(s for s in doc.stages if s.index == landed).criterion.landed
    assert spec.target in text and str(spec.delivered_stage) in text
