"""The method-statement rule: one judge for every place a statement of the work stands, and the harness
that measures it.

Each wired site (order review, question raising, the brief site, plan review) is shown to reach the
judge through a counting stub runner, never a live model. The place table, the exempt records (binding
conditions, the records kept beside an order, an order in the old form) and the could-not-read defect are
pinned at the site that decides them; `ORDER_AGREEMENT_MUTANT=binding_conditions_judged` plants the defect
of handing binding conditions to the judge and must turn the exemption tests red. The eval harness
(`scripts/method-statement-eval.py`) is driven in-process over the fixtures in
`fixtures/method_statement/`.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli, cli_order, cli_review, method_statement, plugins, render
from agentctl.plan import load_plan, review_pairs
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_meta_order import _write_plan
from test_order_phase import (  # noqa: F401  (home and ostore are fixtures)
    BUDGET, SESSION, SIDES, _draft, _start, _submit, home, ostore,
)
from test_dispatch_host import _capture_runner, _to_executing
from test_order_two_sides import BESIDE, _agree_two_sided, _replacing, _sides

SCRIPTS = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "method_statement"
M = method_statement

METHOD_TEXT = "tag releases automatically"
BINDING_TEXT = "no change to the release branch"
IDEA_TEXT = "tag from a cron job"
MEANS_TEXT = "the command runs on the build host"
BUDGET_TEXT = "one reviewed plan of a few stages"


class Judge:
    """A stand-in for the host-model runner: records every (place, text) it is shown and answers with
    the verdict `verdict_for(place, text)` (`neither` by default), or fails when `fail`."""

    def __init__(self, verdict_for=None, *, fail=False):
        self.verdict_for = verdict_for or (lambda place, text: M.NEITHER)
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def __call__(self, argv, timeout=None, stdin=None):
        place = re.search(r"stands in the place `(\w+)`", stdin).group(1)
        text = stdin.split("Statement:\n", 1)[1].rstrip("\n")
        self.calls.append((place, text))
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="unavailable", timed_out=False)
        reply = {"verdict": self.verdict_for(place, text), "reason": "stub"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="", timed_out=False)

    @property
    def places(self) -> set[str]:
        return {place for place, _ in self.calls}

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.calls]


def answering(verdict, *, place=None, containing=""):
    """A verdict function: `verdict` for a statement in `place` (any when None) containing `containing`."""
    return lambda p, text: verdict if (place is None or p == place) and containing in text else M.NEITHER


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _review(store, runner=None, verdict="pass", target=None):
    return cli.cmd_plan_review(
        ns(session=SESSION, scope="order", target=target, verdict=verdict, reviewer="thinker", concerns=None,
           note="", plan_digest=None, customer_questions=None, transcript=None),
        store=store, method_runner=runner)


def _submitted(store, tmp_path, **draft_kw):
    _start(store)
    assert _submit(store, _draft(tmp_path, **draft_kw)).ok


def _plan_file(tmp_path, *, two_sided=True, beside="", name="plan"):
    directory = tmp_path / name
    directory.mkdir()
    path = Path(_write_plan(directory / "plan.toml"))
    if two_sided:
        path.write_text(path.read_text(encoding="utf-8") + SIDES + BUDGET + beside, encoding="utf-8")
    return path


def _topo():
    spec = importlib.util.spec_from_file_location("plan_review_topological", SCRIPTS / "plan-review-topological.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["plan_review_topological"] = module
    spec.loader.exec_module(module)
    return module


def _raise_question(store, runner, *, question="Which tool should we use?", kind=None):
    return cli.cmd_question_raise(
        ns(session=SESSION, id="Q1", target="goal", question=question, kind=kind), store=store,
        method_runner=runner)


def _premise_session(store, tmp_path, **draft_kw):
    _submitted(store, tmp_path, **draft_kw)
    state = store.load(SESSION)
    plugins.activate(state, "premise")
    store.save(state)


def _stored_questions(store):
    return store.load(SESSION).plugins["premise"]["questions"]


# --- the place table --------------------------------------------------------------------------------


def test_executor_side_method_defect_how_built_in_place(ostore, tmp_path):
    assert M.is_defect(M.METHOD, M.EXECUTOR_SIDE) and not M.is_defect(M.HOW_BUILT, M.EXECUTOR_SIDE)
    assert M.is_defect(M.HOW_BUILT, M.CUSTOMER_SIDE) and M.is_defect(M.METHOD, M.SUB_PLAN) is False
    assert M.is_defect(M.COULD_NOT_READ, M.EXECUTOR_SIDE) and not M.is_defect(M.COULD_NOT_READ, M.SUB_PLAN)
    _submitted(ostore, tmp_path)
    judge = Judge(answering(M.METHOD, place=M.EXECUTOR_SIDE, containing=METHOD_TEXT))

    refused = _review(ostore, judge)

    assert not refused.ok and "proposal P1" in refused.detail and "states a method" in refused.detail


def test_how_built_on_customer_side_reported(ostore, tmp_path):
    _submitted(ostore, tmp_path)
    judge = Judge(answering(M.HOW_BUILT, place=M.CUSTOMER_SIDE, containing="the release command"))

    refused = _review(ostore, judge)

    assert not refused.ok and "customer side product" in refused.detail
    assert "states how the product is built" in refused.detail


def test_how_built_on_executor_side_in_place(ostore, tmp_path):
    _submitted(ostore, tmp_path)
    judge = Judge(answering(M.HOW_BUILT, place=M.EXECUTOR_SIDE))

    assert _review(ostore, judge).ok
    assert M.EXECUTOR_SIDE in judge.places


def test_past_order_how_built_is_customer_side(ostore, ev, tmp_path):
    _submitted(ostore, tmp_path)
    judge = Judge(answering(M.HOW_BUILT, containing="cutting a release"))

    refused = _review(ostore, judge)

    assert not refused.ok and "customer side step" in refused.detail
    assert (M.CUSTOMER_SIDE, "cutting a release") in judge.calls

    def past_order_judge(how_built_place):
        def verdict(place, text):
            if "queue" in text or "migration" in text:
                return M.METHOD
            if ("Python module" in text or "Postgres" in text) and place == how_built_place:
                return M.HOW_BUILT
            return M.NEITHER
        return Judge(verdict)

    def evaluated(how_built_place):
        out = tmp_path / f"result-{how_built_place}.json"
        run = ["run", "--kind", "orders", "--set", _fx("labelled-orders.json"), "--runs", "3", "--out", str(out),
               "--commit", "abc1234"]
        assert ev.main(run, runner=past_order_judge(how_built_place), out=_Sink()) == 0
        sink = _Sink()
        code = ev.main(["check", "--kind", "orders", "--results", str(out), *ORDERS_ARGS], out=sink)
        return code, "".join(sink.lines)

    in_place_code, in_place = evaluated(M.EXECUTOR_SIDE)
    defect_code, defect = evaluated(M.CUSTOMER_SIDE)

    assert in_place_code == 1 and "found share of how_built 0.000" in in_place
    assert defect_code == 0 and "found how_built: 1.000" in defect


# --- order review -----------------------------------------------------------------------------------


def test_order_review_site_reports_method(ostore, tmp_path):
    _submitted(ostore, tmp_path)
    judge = Judge(answering(M.METHOD, place=M.CUSTOMER_SIDE, containing="a command that tags"))

    refused = _review(ostore, judge)

    assert not refused.ok and refused.action == "fix_order"
    assert "customer side missing" in refused.detail and "method-statement rule" in refused.detail
    assert len(judge.calls) >= 1
    assert ostore.load(SESSION).order_reviews == []


def test_customer_side_method_blocks(ostore, tmp_path):
    _submitted(ostore, tmp_path)

    refused = _review(ostore, Judge(answering(M.METHOD, place=M.CUSTOMER_SIDE)))

    assert not refused.ok and "customer side step" in refused.detail


def test_could_not_read_blocks(ostore, tmp_path):
    _submitted(ostore, tmp_path)
    for judge in (Judge(fail=True), Judge(answering(M.COULD_NOT_READ))):
        refused = _review(ostore, judge)
        assert not refused.ok and "could not read" in refused.detail
    assert ostore.load(SESSION).order_reviews == []


def test_executor_side_how_built_passes(ostore, tmp_path):
    _submitted(ostore, tmp_path)

    review = _review(ostore, Judge(answering(M.HOW_BUILT, place=M.EXECUTOR_SIDE)))

    assert review.ok
    assert ostore.load(SESSION).order_reviews[-1].verdict == "pass"


def test_neither_stub_default_passes(ostore, tmp_path):
    _submitted(ostore, tmp_path)

    assert _review(ostore).ok


def test_method_idea_verdict_refused_at_order_review(ostore, tmp_path):
    _submitted(ostore, tmp_path, sides=SIDES + BESIDE)
    judge = Judge(answering(M.METHOD, place=M.EXECUTOR_SIDE, containing=METHOD_TEXT))

    refused = _review(ostore, judge)

    assert not refused.ok and "proposal P1" in refused.detail
    assert not any(IDEA_TEXT in text for text in judge.texts)


def test_order_review_judges_text_only(ostore, tmp_path):
    _submitted(ostore, tmp_path, sides=SIDES + BESIDE)
    judge = Judge()

    assert _review(ostore, judge).ok

    assert len(judge.calls) >= 1
    for beside in (BINDING_TEXT, IDEA_TEXT, MEANS_TEXT, BUDGET_TEXT):
        assert not any(beside in text for text in judge.texts), beside


def test_old_form_order_not_judged(ostore, tmp_path):
    judge = Judge(answering(M.METHOD))
    _start(ostore)
    old_form = _draft(tmp_path, omit=("customer_side",))

    assert _review(ostore, judge, target=str(old_form)).ok

    assert judge.calls == []
    plan_path = _plan_file(tmp_path, two_sided=False)
    doc = load_plan(plan_path)
    unloadable = tmp_path / "not-a-plan.toml"
    unloadable.write_text("not a plan\n", encoding="utf-8")
    assert _topo().field_place_concerns(str(unloadable), "unit:1", runner=judge) == []
    assert render.brief_site_defects(doc, 1, runner=judge) == []
    assert render._method_rule_section(doc, order_node=True) == []
    assert _topo().field_place_concerns(str(plan_path), "unit:1", runner=judge) == []
    assert judge.calls == []


# --- question raising -------------------------------------------------------------------------------


def test_question_about_method_is_defect(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    judge = Judge(answering(M.METHOD, place=M.QUESTION))

    refused = _raise_question(ostore, judge, question="Should we use a queue with two workers?")

    assert not refused.ok and "question refused before it reaches the user" in refused.detail
    assert judge.calls and judge.places == {M.QUESTION}
    assert _stored_questions(ostore) == []


def test_question_about_how_built_is_defect(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    judge = Judge(answering(M.HOW_BUILT, place=M.QUESTION))

    refused = _raise_question(ostore, judge, question="Is the report a Python module?")

    assert not refused.ok and "states how the product is built" in refused.detail
    assert _stored_questions(ostore) == []


def test_question_in_the_terms_of_the_activity_is_raised(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    judge = Judge()

    raised = _raise_question(ostore, judge, question="Who cuts the release today?")

    assert raised.ok and [q["id"] for q in _stored_questions(ostore)] == ["Q1"]
    assert len(judge.calls) == 1


def test_binding_conditions_question_not_judged(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    judge = Judge(answering(M.METHOD))

    raised = _raise_question(ostore, judge, kind=M.BINDING_CONDITIONS_KIND)

    assert raised.ok and judge.calls == []


def test_question_on_an_old_form_order_not_judged(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    state = ostore.load(SESSION)
    state.order_draft_path = str(_draft(tmp_path, name="old.toml", omit=("customer_side",)))
    ostore.save(state)
    judge = Judge(answering(M.METHOD))

    assert _raise_question(ostore, judge).ok and judge.calls == []


def test_question_naming_an_old_form_plan_is_judged_by_the_sessions_own_order(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    old_plan = _plan_file(tmp_path, two_sided=False, name="old")
    judge = Judge(answering(M.METHOD, place=M.QUESTION))

    refused = cli.cmd_question_raise(
        ns(session=SESSION, id="Q1", target="goal", question="Should we use a queue with two workers?",
           kind=None, plan=str(old_plan)),
        store=ostore, method_runner=judge)

    assert not refused.ok and "states a method" in refused.detail and judge.places == {M.QUESTION}
    assert _stored_questions(ostore) == []


def _plan_only_session(store, plan_path):
    _start(store)
    state = store.load(SESSION)
    plugins.activate(state, "premise")
    state.plan_path = str(plan_path)
    store.save(state)
    assert not state.order_draft_path


def test_question_in_a_session_whose_plan_is_old_form_not_judged(ostore, tmp_path):
    _plan_only_session(ostore, _plan_file(tmp_path, two_sided=False, name="old"))
    judge = Judge(answering(M.METHOD))

    assert _raise_question(ostore, judge).ok and judge.calls == []


def test_question_in_a_session_whose_plan_states_no_order_not_judged(ostore, tmp_path):
    _plan_only_session(ostore, _write_plan(tmp_path / "plan.toml", omit=("order",)))
    judge = Judge(answering(M.METHOD))

    assert _raise_question(ostore, judge).ok and judge.calls == []


def test_question_in_a_session_whose_plan_is_new_form_is_judged(ostore, tmp_path):
    _plan_only_session(ostore, _plan_file(tmp_path, name="new"))
    judge = Judge(answering(M.METHOD, place=M.QUESTION))

    refused = _raise_question(ostore, judge, question="Should we use a queue with two workers?")

    assert not refused.ok and judge.places == {M.QUESTION}


def test_question_before_any_order_draft_is_judged(ostore):
    _start(ostore)
    state = ostore.load(SESSION)
    plugins.activate(state, "premise")
    ostore.save(state)
    assert not state.order_draft_path and not state.plan_path
    judge = Judge(answering(M.METHOD, place=M.QUESTION))

    refused = _raise_question(ostore, judge, question="Should we use a queue with two workers?")

    assert not refused.ok and judge.places == {M.QUESTION}
    assert _stored_questions(ostore) == []


def test_method_judge_has_its_own_runner(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    other = []

    def other_judge(argv, timeout=None, stdin=None):
        other.append(stdin)
        return SimpleNamespace(returncode=1, stdout="", stderr="unavailable", timed_out=False)

    method_judge = Judge()
    raised = cli.cmd_question_raise(
        ns(session=SESSION, id="Q1", target="goal", question="Who cuts the release today?", kind=None),
        store=ostore, runner=other_judge, method_runner=method_judge)
    reviewed = cli.cmd_plan_review(
        ns(session=SESSION, scope="order", target=None, verdict="pass", reviewer="thinker", concerns=None,
           note="", plan_digest=None, customer_questions=None, transcript=None),
        store=ostore, runner=other_judge, method_runner=method_judge)

    assert raised.ok and reviewed.ok and len(method_judge.calls) >= 2
    assert not any("stands in the place" in (stdin or "") for stdin in other)


def test_executor_revision_after_a_review_pass_is_judged(ostore, tmp_path):
    _agree_two_sided(ostore, tmp_path)
    assert ostore.load(SESSION).order_reviews[-1].verdict == "pass"
    revised = _sides(tmp_path, "revised.toml",
                     replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))
    judge = Judge(answering(M.METHOD, place=M.EXECUTOR_SIDE, containing="tag on every merge"))

    def revise(runner):
        return cli_order.cmd_order_executor_revise(
            ns(session=SESSION, plan=str(revised), why="the way it is built changed", by="agent"),
            store=ostore, method_runner=runner)

    refused = revise(judge)

    assert not refused.ok and "proposal P1" in refused.detail and "states a method" in refused.detail
    assert "sub-plan" in refused.detail and ostore.load(SESSION).executor_side_revisions == []
    revised = _sides(tmp_path, "revised-again.toml",
                     replace=_replacing("proposals", "tag releases automatically", "tag on every push"))
    assert revise(Judge()).ok and len(ostore.load(SESSION).executor_side_revisions) == 1


# --- the brief site, stage descriptions and plan review ---------------------------------------------


def _dispatch_on_new_form_plan(store, venued_fixtures_dir, tmp_path, method_runner, *, two_sided=True,
                               dry_run=False, before=None, **extra):
    """Dispatch the active stage of a session whose plan is in the new form (or the old one); `(directive,
    spawns)`. `before(plan_path)` runs first (a reading the plan review made); `extra` are more arguments."""
    sid = "brief-site"
    _to_executing(store, sid, venued_fixtures_dir)
    state = store.load(sid)
    plan_path = _plan_file(tmp_path, two_sided=two_sided)
    state.plan_path = str(plan_path)
    store.save(state)
    if before is not None:
        before(plan_path)
    spawned = []
    directive = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium", dry_run=dry_run, **extra),
                                 store=store, runner=_capture_runner(spawned), method_runner=method_runner)
    return directive, spawned


def test_dispatch_refuses_a_brief_that_states_a_method(store, venued_fixtures_dir, tmp_path):
    judge = Judge(answering(M.METHOD, place=M.BRIEF))

    refused, spawned = _dispatch_on_new_form_plan(store, venued_fixtures_dir, tmp_path, judge)

    assert not refused.ok and refused.action == "fix_brief" and spawned == []
    assert "stage 1" in refused.detail and "states a method" in refused.detail
    assert refused.data["problems"] and judge.places == {M.BRIEF}
    assert "through a replan" in refused.detail


def test_dispatch_spawns_a_brief_that_states_the_result(store, venued_fixtures_dir, tmp_path):
    judge = Judge()

    directive, spawned = _dispatch_on_new_form_plan(store, venued_fixtures_dir, tmp_path, judge)

    assert directive.ok and len(spawned) == 1 and judge.places == {M.BRIEF}


def test_dispatch_where_the_judge_does_not_answer_is_its_own_refusal(store, venued_fixtures_dir, tmp_path):
    judge = Judge(fail=True)

    refused, spawned = _dispatch_on_new_form_plan(store, venued_fixtures_dir, tmp_path, judge)

    assert not refused.ok and refused.action == "judge_unavailable" and spawned == []
    assert "Nothing in the brief needs fixing: repeat the dispatch" in refused.detail
    assert "replan" not in refused.detail and refused.data["problems"]
    assert len(judge.calls) == 1


def test_dispatch_stops_asking_after_a_defect_is_found_and_the_judge_goes_quiet(
        store, venued_fixtures_dir, tmp_path):
    answers = iter([M.METHOD])
    calls = []

    def runner(argv, timeout=None, stdin=None):
        calls.append(stdin)
        verdict = next(answers, None)
        if verdict is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="unavailable", timed_out=False)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"verdict": verdict, "reason": "stub"}),
                               stderr="", timed_out=False)

    refused, spawned = _dispatch_on_new_form_plan(store, venued_fixtures_dir, tmp_path, runner)

    assert refused.action == "fix_brief" and spawned == [] and len(calls) == 2
    assert all("could not read" not in line for line in refused.data["problems"])


@pytest.mark.parametrize("by, outcome", [("user", "spawns"), ("agent", "refused"), ("Agent", "refused"),
                                         ("alice", "refused")])
def test_only_the_user_lets_an_unread_brief_through(store, venued_fixtures_dir, tmp_path, by, outcome):
    judge = Judge(fail=True)

    directive, spawned = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, unread_brief_by=by)

    if outcome == "spawns":
        assert directive.ok and len(spawned) == 1
    else:
        assert not directive.ok and directive.action == "judge_unavailable" and spawned == []


@pytest.mark.parametrize("by, customer, let_through", [
    ("agent", "", False), ("AGENT", "", False), ("", "", False), ("alice", "", True),
    ("alice", "user", False), ("user", "user", True),
])
def test_unread_brief_override_is_the_customers_and_never_the_agents(by, customer, let_through):
    from agentctl import cli_dispatch

    doc = SimpleNamespace(meta=SimpleNamespace(order=SimpleNamespace(customer_id=customer)))
    unread = M.Finding("stage 1 title", M.BRIEF, M.COULD_NOT_READ, "no answer")

    refusal = cli_dispatch._unread_brief_refusal(
        SimpleNamespace(node="EXECUTING"), SimpleNamespace(index=1), doc, unread, by)

    assert (refusal is None) == let_through
    assert refusal is None or refusal.action == "judge_unavailable"


def test_a_user_cannot_let_a_defective_brief_through(store, venued_fixtures_dir, tmp_path):
    judge = Judge(answering(M.METHOD, place=M.BRIEF))

    refused, spawned = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, unread_brief_by="user")

    assert refused.action == "fix_brief" and spawned == []


def test_dispatch_serves_a_reading_the_plan_review_made(store, venued_fixtures_dir, tmp_path):
    def read_at_plan_review(plan_path):
        M.site_defects(M.plan_statements(load_plan(plan_path)), runner=Judge())

    judge = Judge(answering(M.METHOD))

    directive, spawned = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, before=read_at_plan_review)

    assert directive.ok and len(spawned) == 1 and judge.calls == []


def test_dispatch_refuses_on_a_defect_the_plan_review_read(store, venued_fixtures_dir, tmp_path):
    def read_at_plan_review(plan_path):
        M.site_defects(M.plan_statements(load_plan(plan_path)),
                       runner=Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION)))

    judge = Judge()

    refused, spawned = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, before=read_at_plan_review)

    assert refused.action == "fix_brief" and spawned == [] and judge.calls == []
    assert "through a replan" in refused.detail


def test_dispatch_on_an_old_form_plan_makes_no_judge_call(store, venued_fixtures_dir, tmp_path):
    judge = Judge(answering(M.METHOD))

    directive, spawned = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, two_sided=False)

    assert directive.ok and len(spawned) == 1 and judge.calls == []


def test_dispatch_dry_run_makes_no_judge_call_and_writes_no_cache(store, venued_fixtures_dir, tmp_path):
    judge = Judge(answering(M.METHOD))

    directive, spawned = _dispatch_on_new_form_plan(
        store, venued_fixtures_dir, tmp_path, judge, dry_run=True)

    assert directive.ok and judge.calls == [] and not M.cache_dir().exists()


def test_brief_site_reports_method(tmp_path):
    doc = load_plan(_plan_file(tmp_path))
    judge = Judge(answering(M.METHOD, place=M.BRIEF))

    lines = render.brief_site_defects(doc, 1, runner=judge)

    assert lines and judge.places == {M.BRIEF}
    assert any("stage 1 title (brief)" in line and "states a method" in line for line in lines)
    assert len(judge.calls) >= 1


def test_how_built_in_brief_is_defect(tmp_path):
    requirement = "the release command is a shell script"
    doc = load_plan(_plan_file(tmp_path))
    doc.stages[0].title = requirement
    judge = Judge(answering(M.HOW_BUILT, place=M.BRIEF, containing=requirement))

    lines = render.brief_site_defects(doc, 1, runner=judge)

    assert len(lines) == 1 and "stage 1 title (brief)" in lines[0] and "states how the product is built" in lines[0]
    assert judge.texts.count(requirement) == 1

    carried = load_plan(_plan_file(tmp_path, name="carried"))
    carried.stages[0].binding_conditions = [{"id": "B1", "text": requirement, "set_by": "user"}]
    beside = Judge(answering(M.HOW_BUILT, place=M.BRIEF, containing=requirement))

    assert render.brief_site_defects(carried, 1, runner=beside) == []
    assert requirement not in beside.texts


def test_brief_binding_conditions_not_judged(tmp_path):
    doc = load_plan(_plan_file(tmp_path))
    doc.stages[0].binding_conditions = [
        {"id": "B1", "text": "the build host runs Linux", "set_by": "user"}]
    judge = Judge(answering(M.METHOD))

    render.brief_site_defects(doc, 1, runner=judge)

    assert judge.calls and not any("build host runs Linux" in text for text in judge.texts)


def test_sub_plan_fields_are_not_judged(tmp_path):
    path = _plan_file(tmp_path)
    doc = load_plan(path)
    judge = Judge(answering(M.METHOD))

    render.brief_site_defects(doc, 1, runner=judge)
    _topo().field_place_concerns(str(path), "unit:1", runner=judge)

    means = doc.stages[0].means
    sub_plan = [text for text in (means.method, means.procedure) if isinstance(text, str) and text.strip()]
    assert sub_plan and judge.calls
    for text in sub_plan:
        assert not any(text in judged for judged in judge.texts)


def test_plan_review_prompt_carries_rule(tmp_path):
    path = _plan_file(tmp_path)
    doc = load_plan(path)
    old_doc = load_plan(_plan_file(tmp_path, two_sided=False, name="old"))

    base = render.render_unit_review_bundle(doc, "unit:base", plan_sha256="0" * 64)
    stage = render.render_unit_review_bundle(doc, "unit:1", plan_sha256="0" * 64)
    old_base = render.render_unit_review_bundle(old_doc, "unit:base", plan_sha256="0" * 64)

    assert M.RULE_TEXT in base and M.TYPE_ENTRY_READING in base
    assert M.RULE_TEXT in stage and M.TYPE_ENTRY_READING not in stage
    assert M.RULE_TEXT not in old_base

    judge = Judge(answering(M.METHOD, place=M.STAGE_DESCRIPTION))
    concerns = _topo().field_place_concerns(str(path), "unit:1", runner=judge)

    assert judge.calls and judge.places == {M.STAGE_DESCRIPTION}
    assert concerns and all(c.startswith("blocking: stage:1 ") for c in concerns)


def test_how_built_in_a_result_reaches_the_recorded_review(tmp_path):
    topo = _topo()
    result_line = 'expected_result_image = "The seam requires a typed order of a substantive plan."'
    method_line = 'method = "run"'
    path = _plan_file(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert result_line in text and method_line in text
    path.write_text(text.replace(result_line, 'expected_result_image = "the release command is a shell script"')
                    .replace(method_line, 'method = "write the release command as a shell script"'), encoding="utf-8")
    judge = Judge(answering(M.HOW_BUILT, place=M.STAGE_DESCRIPTION, containing="shell script"))

    concerns = topo.field_place_concerns(str(path), "unit:1", runner=judge)

    assert len(concerns) == 1 and concerns[0].startswith("blocking: stage:1 stage 1 result (stage description)")
    assert "states how the product is built" in concerns[0]
    assert not any("write the release command" in judged for judged in judge.texts)
    argv = topo.record_argv(None, str(path), "unit:1", "revise", "0" * 64, concerns)
    recorded = [argv[i + 1] for i, token in enumerate(argv) if token == "--concern"]
    parsed, refusal = cli_review._parse_review_concerns(SimpleNamespace(concern_ledger={}), recorded, "revise")
    assert refusal == "" and [p.severity for p in parsed] == ["blocking"]


def test_binding_conditions_a_stage_carries_raise_no_plan_review_concern(tmp_path, monkeypatch):
    path = _plan_file(tmp_path)
    doc = load_plan(path)
    doc.stages[0].binding_conditions = [{"id": "B1", "text": BINDING_TEXT, "set_by": "user"}]
    topo = _topo()
    monkeypatch.setattr(topo.plan, "load_plan", lambda _path: doc)
    judge = Judge(answering(M.METHOD, containing=BINDING_TEXT))

    assert topo.field_place_concerns(str(path), "unit:1", runner=judge) == []
    assert judge.calls and not any(BINDING_TEXT in text for text in judge.texts)


def test_whole_plan_review_prompt_carries_rule(tmp_path):
    new_form = _plan_file(tmp_path)
    old_form = _plan_file(tmp_path, two_sided=False, name="old")

    new_site = render.build_site_prompt("whole-plan-review", new_form, view_root=tmp_path / "views")
    old_site = render.build_site_prompt("whole-plan-review", old_form, view_root=tmp_path / "views")

    assert M.RULE_TEXT in new_site.text and M.TYPE_ENTRY_READING in new_site.text
    assert M.RULE_TEXT not in old_site.text and M.TYPE_ENTRY_READING not in old_site.text


def _two_stage_plan(tmp_path, name="two"):
    """A new-form plan whose stage 2 relies on stage 1: review pairs `base-1` and `2-1`."""
    path = _plan_file(tmp_path, name=name)
    text = path.read_text(encoding="utf-8")
    start, end = text.index("[[stage]]"), text.index("[meta.order.customer_side]")
    second = text[start:end].replace("index = 1\n", "index = 2\ndepends_on = [1]\n", 1)
    path.write_text(text[:end] + second + text[end:], encoding="utf-8")
    return path


def test_type_entry_reading_in_order_prompt(tmp_path):
    doc = load_plan(_two_stage_plan(tmp_path))
    pair_ids = review_pairs(doc)
    stage_pair = "2-1"

    unit = render.render_unit_review_bundle(doc, "unit:base", plan_sha256="0" * 64)
    order_pair = render.render_pair_review_bundle(doc, "base-1", plan_sha256="0" * 64, view_dir=tmp_path)
    other_pair = render.render_pair_review_bundle(doc, stage_pair, plan_sha256="0" * 64, view_dir=tmp_path)

    assert "base-1" in pair_ids and stage_pair in pair_ids
    assert M.TYPE_ENTRY_READING in unit and M.TYPE_ENTRY_READING in order_pair
    assert M.TYPE_ENTRY_READING not in other_pair


# --- the judge --------------------------------------------------------------------------------------


def test_judge_caches_a_read_statement_and_not_an_unread_one(tmp_path):
    cache = tmp_path / "cache"
    judge = Judge(answering(M.METHOD))

    first = M.judge("use a queue", M.QUESTION, runner=judge, cache=cache)
    second = M.judge("use a queue", M.QUESTION, runner=Judge(fail=True), cache=cache)
    unread = M.judge("another", M.QUESTION, runner=Judge(fail=True), cache=cache)
    again = M.judge("another", M.QUESTION, runner=Judge(), cache=cache)

    assert first["verdict"] == second["verdict"] == M.METHOD
    assert unread["verdict"] == M.COULD_NOT_READ and again["verdict"] == M.NEITHER


def test_judge_refuses_a_reply_outside_the_verdicts():
    bad = Judge(lambda place, text: "maybe")

    assert M.judge("x", M.QUESTION, runner=bad)["verdict"] == M.COULD_NOT_READ


def test_blank_statements_are_not_judged():
    judge = Judge()

    assert M.findings([M.Statement("blank", M.QUESTION, "  "), M.Statement("sub", M.SUB_PLAN, "text")],
                      runner=judge) == [] and judge.calls == []


def test_the_judge_is_told_the_customers_labelling_criterion():
    prompts = []

    def runner(argv, timeout=None, stdin=None):
        prompts.append(stdin)
        return SimpleNamespace(returncode=0, stdout='{"verdict": "neither", "reason": "stub"}', stderr="",
                               timed_out=False)

    M.judge("the monthly totals of each account", M.CUSTOMER_SIDE, runner=runner)

    [prompt] = prompts
    for criterion in (
        "labelled by the terms it is stated in, never by who said it",
        "stated in terms of the customer's own activity, is external to the executor's planned activity, "
        "and stays valid whatever the executor chooses",
        "Stated in terms of the product's internals",
        "it is a HOW-BUILT statement",
        "Stated in terms of the executor's process or means",
        "it is a METHOD",
        "an acceptance check with its reason",
    ):
        assert criterion in prompt, criterion


def test_production_sites_judge_a_statement_once(ostore, tmp_path):
    _premise_session(ostore, tmp_path)
    question = "Should we use a queue with two workers?"

    first = _raise_question(ostore, Judge(answering(M.METHOD, place=M.QUESTION)), question=question)
    again = _raise_question(ostore, Judge(fail=True), question=question)

    assert not first.ok and not again.ok
    assert "states a method" in again.detail and "could not read" not in again.detail
    assert M.cache_dir().parent == M.GJ.agentctl_dir()
    unread = Judge(fail=True)
    assert _raise_question(ostore, unread, question="Who cuts the release today?").ok is False
    assert _raise_question(ostore, Judge(), question="Who cuts the release today?").ok


@pytest.mark.parametrize("verdict, place, advice", [
    (M.COULD_NOT_READ, M.CUSTOMER_SIDE, "repeat the order review"),
    (M.COULD_NOT_READ, M.EXECUTOR_SIDE, "repeat the order review or the executor revision"),
    (M.COULD_NOT_READ, M.QUESTION, "raise the question again"),
    (M.COULD_NOT_READ, M.BRIEF, "repeat the dispatch"),
    (M.COULD_NOT_READ, M.STAGE_DESCRIPTION, "repeat the plan review"),
    (M.METHOD, M.CUSTOMER_SIDE, "how the work is done is the executor's to decide"),
    (M.METHOD, M.QUESTION, "how the work is done is the executor's to decide"),
    (M.METHOD, M.EXECUTOR_SIDE, "a method belongs to the executor's sub-plan"),
    (M.METHOD, M.BRIEF, "a method belongs to the stage's sub-plan"),
    (M.METHOD, M.STAGE_DESCRIPTION, "a method belongs to the stage's sub-plan"),
    (M.HOW_BUILT, M.CUSTOMER_SIDE, "put a requirement on the build in a binding condition"),
    (M.HOW_BUILT, M.QUESTION, "put a requirement on the build in a binding condition"),
    (M.HOW_BUILT, M.BRIEF, "put a requirement on the build in a binding condition"),
    (M.HOW_BUILT, M.STAGE_DESCRIPTION, "put a requirement on the build in a binding condition"),
])
def test_defect_advice_follows_the_verdict_and_the_place(verdict, place, advice):
    line = M.defect_line(M.Finding("a statement", place, verdict, "the reason"))

    assert advice in line
    assert ("binding condition" in line) == (verdict == M.HOW_BUILT)
    assert ("the judge did not answer" in line) == (verdict == M.COULD_NOT_READ)


def test_the_judge_stops_at_its_first_unanswered_statement_when_asked():
    statements = [M.Statement(f"s{i}", M.BRIEF, f"text {i}") for i in range(4)]
    judge = Judge(fail=True)

    found = M.findings(statements, runner=judge, stop_on_unread=True)

    assert [f.verdict for f in found] == [M.COULD_NOT_READ] and len(judge.calls) == 1
    assert len(M.findings(statements, runner=Judge(fail=True))) == 4


def test_a_verdict_serves_every_place_with_the_same_defect_row(tmp_path):
    cache = tmp_path / "cache"
    first = M.judge("use a queue", M.STAGE_DESCRIPTION, runner=Judge(answering(M.METHOD)), cache=cache)
    judge = Judge()

    served = M.judge("use a queue", M.BRIEF, runner=judge, cache=cache)
    other_row = M.judge("use a queue", M.EXECUTOR_SIDE, runner=judge, cache=cache)

    assert first["verdict"] == served["verdict"] == M.METHOD
    assert other_row["verdict"] == M.NEITHER and judge.places == {M.EXECUTOR_SIDE}


def test_a_changed_prompt_does_not_read_an_old_verdict(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    M.judge("use a queue", M.QUESTION, runner=Judge(answering(M.METHOD)), cache=cache)
    assert M.judge("use a queue", M.QUESTION, runner=Judge(fail=True), cache=cache)["verdict"] == M.METHOD

    monkeypatch.setattr(M, "_PROMPT", M._PROMPT + "\nAnswer briefly.\n")

    assert M.judge("use a queue", M.QUESTION, runner=Judge(fail=True), cache=cache)["verdict"] == M.COULD_NOT_READ


def test_a_changed_judge_model_does_not_read_an_old_verdict(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    M.judge("use a queue", M.QUESTION, runner=Judge(answering(M.METHOD)), cache=cache)

    monkeypatch.setattr(M.GJ, "_JUDGE_MODEL", "another-model")

    assert M.judge("use a queue", M.QUESTION, runner=Judge(fail=True), cache=cache)["verdict"] == M.COULD_NOT_READ


# --- a whole-plan pass -----------------------------------------------------------------------------


def _whole_plan_pass(store, path, method_runner, *, scope=""):
    return cli.cmd_plan_review(
        ns(session=SESSION, scope=scope, target=str(path), verdict="pass", reviewer="thinker", concerns=None,
           note="", plan_digest=None, customer_questions=None, transcript=None),
        store=store, method_runner=method_runner)


@pytest.mark.parametrize("scope", ["", "stage:1"])
def test_a_pass_on_a_new_form_plan_is_refused_on_a_defect(ostore, tmp_path, scope):
    _start(ostore)
    path = _plan_file(tmp_path)

    unread = _whole_plan_pass(ostore, path, Judge(fail=True), scope=scope)
    method = _whole_plan_pass(ostore, path, Judge(answering(M.METHOD, place=M.STAGE_DESCRIPTION)), scope=scope)

    assert not method.ok and method.action == "fix_plan" and "states a method" in method.detail
    assert "stage 1" in method.detail and not unread.ok and unread.action == "fix_plan"
    assert "could not read" in unread.detail


def test_a_pass_on_a_new_form_plan_reads_the_stage_descriptions(ostore, tmp_path):
    _start(ostore)
    judge = Judge()

    passed = _whole_plan_pass(ostore, _plan_file(tmp_path), judge)

    assert passed.action != "fix_plan" and judge.places == {M.STAGE_DESCRIPTION}


def test_a_pass_on_an_old_form_plan_is_not_judged(ostore, tmp_path):
    _start(ostore)
    judge = Judge(answering(M.METHOD))

    passed = _whole_plan_pass(ostore, _plan_file(tmp_path, two_sided=False, name="old"), judge)

    assert passed.action != "fix_plan" and judge.calls == []


def test_a_revise_is_not_refused_on_a_defect(ostore, tmp_path):
    _start(ostore)
    judge = Judge(answering(M.METHOD))

    revised = cli.cmd_plan_review(
        ns(session=SESSION, scope="", target=str(_plan_file(tmp_path)), verdict="revise", reviewer="thinker",
           concerns=["blocking: stage:1 a concern"], note="", plan_digest=None, customer_questions=None,
           transcript=None), store=ostore, method_runner=judge)

    assert revised.action != "fix_plan" and judge.calls == []


# --- the eval harness -------------------------------------------------------------------------------


@pytest.fixture
def ev():
    spec = importlib.util.spec_from_file_location("method_statement_eval", SCRIPTS / "method-statement-eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fx(name: str) -> str:
    return str(FIXTURES / name)


def _check(ev, kind, results, *extra) -> int:
    return ev.main(["check", "--kind", kind, "--results", results, *extra], out=_Sink())


class _Sink:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


ORDERS_ARGS = ("--min-found", "0.90", "--max-flagged", "0.05", "--min-runs", "3")
LENGTH_ARGS = ("--set", _fx("typical-orders.json"), "--set-digest-file", _fx("typical-orders.sha256"),
               "--tolerance-chars", "0", "--excess-readings", _fx("excess-readings.json"),
               "--min-orders", "3", "--min-runs", "3")
SMALL_ARGS = ("--config-root", _fx("config-root"), "--min-share", "0.90", "--min-items", "10")


def _edited(tmp_path, name, change) -> str:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    change(data)
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def _labelled(counts: dict[str, tuple[int, int]], *, runs=3, kind="orders") -> dict:
    """Results with `counts[label] = (items, found per item out of `runs`)`: that many items of the
    label, each found in the given number of runs and reported `neither` in the rest."""
    rows = []
    for label, (items, found) in counts.items():
        for i in range(items):
            rows.append({"id": f"{label}{i}", "label": label,
                         "reported": [label] * found + [M.NEITHER] * (runs - found)})
    return {"kind": kind, "results": rows}


@pytest.mark.parametrize("kind,results,args", [
    ("orders", "passing-results.json", ORDERS_ARGS),
    ("stage-briefs", "passing-stage-briefs.json", ORDERS_ARGS),
    ("plan-reviews", "passing-plan-reviews.json", ("--min-found", "0.85", "--max-flagged", "0.10", "--min-runs", "3")),
    ("small-tasks", "passing-small-tasks.json", SMALL_ARGS),
    ("link-reading", "passing-link-reading.json", ()),
    ("length", "passing-length.json", LENGTH_ARGS),
])
def test_check_passes_each_fixture(ev, kind, results, args):
    assert _check(ev, kind, _fx(results), *args) == 0


def test_check_fails_the_failing_fixture(ev):
    assert _check(ev, "orders", _fx("failing-results.json"), *ORDERS_ARGS) == 1


def _degrade_orders(data):
    data["results"][0]["reported"] = [M.NEITHER] * 3
    data["results"][1]["reported"] = [M.NEITHER] * 3


def _degrade_small_tasks(data):
    data["results"][0]["reported"] = [M.SUBSTANTIVE if hasattr(M, "SUBSTANTIVE") else "substantive"]
    data["results"][1]["reported"] = ["substantive"]


def _degrade_link(data):
    data["results"][0]["read"] = False


def _degrade_length(data):
    for run in data["runs"]:
        run["orders"][0]["new_chars"] = run["orders"][0]["old_chars"] + 5


@pytest.mark.parametrize("kind,results,change,args", [
    ("orders", "passing-results.json", _degrade_orders, ORDERS_ARGS),
    ("length", "passing-length.json", _degrade_length, LENGTH_ARGS),
    ("stage-briefs", "passing-stage-briefs.json", _degrade_orders, ORDERS_ARGS),
    ("plan-reviews", "passing-plan-reviews.json", _degrade_orders,
     ("--min-found", "0.85", "--max-flagged", "0.10", "--min-runs", "3")),
    ("small-tasks", "passing-small-tasks.json", _degrade_small_tasks, SMALL_ARGS),
    ("link-reading", "passing-link-reading.json", _degrade_link, ()),
], ids=["orders", "length", "stage-briefs", "plan-reviews", "small-tasks", "link-reading"])
def test_check_below_threshold(ev, tmp_path, kind, results, change, args):
    assert _check(ev, kind, _edited(tmp_path, results, change), *args) == 1


def test_orders_check_per_label(ev, tmp_path):
    pooled_pass = _labelled({M.METHOD: (5, 3), M.HOW_BUILT: (2, 2), M.NEITHER: (5, 3)})
    pooled_pass["results"][5]["reported"] = [M.HOW_BUILT, M.NEITHER, M.NEITHER]
    pooled_pass["results"][6]["reported"] = [M.HOW_BUILT, M.HOW_BUILT, M.NEITHER]
    out = tmp_path / "per-label.json"
    out.write_text(json.dumps(pooled_pass), encoding="utf-8")
    sink = _Sink()

    code = ev.main(["check", "--kind", "orders", "--results", str(out), *ORDERS_ARGS], out=sink)

    text = "".join(sink.lines)
    assert code == 1 and "found share of how_built" in text and "found share of method" not in text


def test_trial_found_only_under_its_own_label(ev, tmp_path):
    data = _labelled({M.METHOD: (2, 0), M.HOW_BUILT: (2, 3), M.NEITHER: (2, 3)})
    for row in data["results"][:2]:
        row["reported"] = [M.HOW_BUILT] * 3
    out = tmp_path / "mislabelled.json"
    out.write_text(json.dumps(data), encoding="utf-8")

    assert _check(ev, "orders", str(out), *ORDERS_ARGS) == 1


def test_stage_briefs_check_per_label(ev, tmp_path):
    data = _labelled({M.METHOD: (5, 3), M.HOW_BUILT: (2, 1), M.NEITHER: (3, 3)}, kind="stage-briefs")
    out = tmp_path / "briefs.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    sink = _Sink()

    code = ev.main(["check", "--kind", "stage-briefs", "--results", str(out), *ORDERS_ARGS], out=sink)

    assert code == 1 and "found share of how_built" in "".join(sink.lines)


def test_check_refuses_changed_set_digest(ev, tmp_path):
    changed = tmp_path / "labelled-orders.json"
    changed.write_text((FIXTURES / "labelled-orders.json").read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert _check(ev, "orders", _fx("passing-results.json"), *ORDERS_ARGS, "--set", _fx("labelled-orders.json")) == 0
    assert _check(ev, "orders", _fx("passing-results.json"), *ORDERS_ARGS, "--set", str(changed)) == 1
    assert _check(ev, "orders", _fx("passing-results.json"), *ORDERS_ARGS, "--set", _fx("labelled-orders.json"),
                  "--set-digest-file", _fx("sets.sha256")) == 0
    stale = _edited(tmp_path, "passing-results.json", lambda d: d.update(set_sha256="0" * 64))
    assert _check(ev, "orders", stale, *ORDERS_ARGS, "--set-digest-file", _fx("sets.sha256")) == 1


def test_check_refuses_other_commit(ev):
    args = (*ORDERS_ARGS,)
    assert _check(ev, "orders", _fx("passing-results.json"), *args, "--commit", "0123456789abcdef0123456789abcdef01234567") == 0
    assert _check(ev, "orders", _fx("passing-results.json"), *args, "--commit", "0123456") == 0
    assert _check(ev, "orders", _fx("passing-results.json"), *args, "--commit", "f" * 40) == 1


def test_check_refuses_other_config_root(ev, tmp_path):
    other = tmp_path / "config-root"
    shutil.copytree(FIXTURES / "config-root", other)
    args = ("--config-root", str(other), "--min-share", "0.90", "--min-items", "10")

    assert _check(ev, "small-tasks", _fx("passing-small-tasks.json"), *args) == 1


def test_check_refuses_changed_instruction_digests(ev, tmp_path):
    root = tmp_path / "config-root"
    shutil.copytree(FIXTURES / "config-root", root)
    results = _edited(tmp_path, "passing-small-tasks.json", lambda d: d.update(config_root=str(root)))
    args = ("--config-root", str(root), "--min-share", "0.90", "--min-items", "10")
    assert _check(ev, "small-tasks", results, *args) == 0

    (root / "CLAUDE.md").write_text("changed instructions\n", encoding="utf-8")

    assert _check(ev, "small-tasks", results, *args) == 1


def test_check_refuses_a_result_of_another_kind(ev):
    assert _check(ev, "orders", _fx("passing-results.json"), *ORDERS_ARGS) == 0
    assert _check(ev, "stage-briefs", _fx("passing-results.json"), *ORDERS_ARGS) == 1
    assert _check(ev, "plan-reviews", _fx("passing-results.json"), "--min-found", "0.85", "--max-flagged", "0.10",
                  "--min-runs", "3") == 1


def test_check_with_a_set_refuses_rows_that_are_not_the_sets(ev, tmp_path):
    args = (*ORDERS_ARGS, "--set", _fx("labelled-orders.json"))
    assert _check(ev, "orders", _fx("passing-results.json"), *args) == 0

    def relabel(data):
        row = data["results"][0]
        row["label"] = M.HOW_BUILT if row["label"] == M.METHOD else M.METHOD

    assert _check(ev, "orders", _edited(tmp_path, "passing-results.json", lambda d: d["results"].pop(0)), *args) == 1
    assert _check(ev, "orders", _edited(tmp_path, "passing-results.json", relabel), *args) == 1
    assert _check(ev, "orders", _edited(tmp_path, "passing-results.json",
                                        lambda d: d["results"].append(dict(d["results"][0], id="extra"))), *args) == 1


def test_check_refuses_a_changed_imported_instruction_file(ev, tmp_path):
    root = tmp_path / "config-root"
    shutil.copytree(FIXTURES / "config-root", root)
    claude = root / "CLAUDE.md"
    claude.write_text(claude.read_text(encoding="utf-8") + "\n@~/.claude-agent/config.md\n", encoding="utf-8")
    (root / "config.md").write_text("small-change-max-lines 200\n", encoding="utf-8")
    digests = ev.instruction_digests(root)
    assert sorted(digests) == ["CLAUDE.md", "config.md"]
    results = _edited(tmp_path, "passing-small-tasks.json",
                      lambda d: d.update(config_root=str(root), instruction_digests=digests))
    args = ("--config-root", str(root), "--min-share", "0.90", "--min-items", "10")
    assert _check(ev, "small-tasks", results, *args) == 0

    (root / "config.md").write_text("small-change-max-lines 20\n", encoding="utf-8")

    assert _check(ev, "small-tasks", results, *args) == 1


def _readings(tmp_path, readings) -> str:
    out = tmp_path / "readings.json"
    out.write_text(json.dumps({"readings": readings}), encoding="utf-8")
    return str(out)


def _length_args(readings: str, *, min_orders="3") -> tuple:
    return ("--tolerance-chars", "0", "--excess-readings", readings, "--min-orders", min_orders, "--min-runs", "3")


def test_check_refuses_excess_without_reading(ev, tmp_path):
    assert _check(ev, "length", _fx("passing-length.json"), *_length_args(_readings(tmp_path, []))) == 1


def test_check_refuses_excess_read_removable(ev, tmp_path):
    readings = _readings(tmp_path, [{"id": "o3", "reading": "removable", "why": "a sentence that can go"}])

    assert _check(ev, "length", _fx("passing-length.json"), *_length_args(readings)) == 1


def test_check_accepts_excess_read_irreducible(ev):
    assert _check(ev, "length", _fx("passing-length.json"), *_length_args(_fx("excess-readings.json"))) == 0


def test_check_refuses_short_set(ev):
    assert _check(ev, "small-tasks", _fx("passing-small-tasks.json"), "--config-root", _fx("config-root"),
                  "--min-share", "0.90", "--min-items", "11") == 1
    assert _check(ev, "orders", _fx("passing-results.json"), "--min-found", "0.9", "--max-flagged", "0.05",
                  "--min-runs", "4") == 1


def test_length_min_orders_floor(ev):
    assert _check(ev, "length", _fx("passing-length.json"), *_length_args(_fx("excess-readings.json"))) == 0
    assert _check(ev, "length", _fx("passing-length.json"),
                  *_length_args(_fx("excess-readings.json"), min_orders="4")) == 1


def test_check_unreadable_input_is_a_usage_error(ev, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    assert _check(ev, "orders", str(broken), *ORDERS_ARGS) == 2
    assert _check(ev, "orders", str(tmp_path / "missing.json"), *ORDERS_ARGS) == 2
    assert _check(ev, "orders", _fx("passing-results.json")) == 2


def test_run_records_config_root(ev, tmp_path):
    out = tmp_path / "result.json"
    judge = Judge(lambda place, text: M.METHOD if "queue" in text else M.NEITHER)

    code = ev.main(["run", "--kind", "orders", "--set", _fx("labelled-orders.json"), "--runs", "2", "--out",
                    str(out), "--config-root", _fx("config-root"), "--commit", "abc1234"],
                   runner=judge, out=_Sink())

    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and len(judge.calls) == 7 * 2 and judge.places == {M.CUSTOMER_SIDE}
    assert data["config_root"] == _fx("config-root") and data["commit"] == "abc1234"
    assert data["instruction_digests"] == {"CLAUDE.md": ev.sha256_file(FIXTURES / "config-root" / "CLAUDE.md")}
    assert data["set_sha256"] == ev.sha256_file(FIXTURES / "labelled-orders.json")
    assert all(len(row["reported"]) == 2 for row in data["results"])
    assert _check(ev, "orders", str(out), "--min-found", "0.0", "--max-flagged", "1.0", "--min-runs", "2",
                  "--set", _fx("labelled-orders.json"), "--commit", "abc1234") == 0


def test_run_small_tasks_reads_the_instructions_under_the_root(ev, tmp_path):
    out = tmp_path / "small.json"
    items = tmp_path / "small-set.json"
    items.write_text(json.dumps({"items": [{"id": "t1", "text": "rename a variable", "expected": "small"}]}),
                     encoding="utf-8")
    prompts = []

    def runner(argv, timeout=None, stdin=None):
        prompts.append(stdin)
        return SimpleNamespace(returncode=0, stdout='{"class": "small"}', stderr="", timed_out=False)

    code = ev.main(["run", "--kind", "small-tasks", "--set", str(items), "--runs", "1", "--out", str(out),
                    "--config-root", _fx("config-root"), "--commit", "abc1234"], runner=runner, out=_Sink())

    assert code == 0 and "Fixture instructions" in prompts[0]
    assert json.loads(out.read_text(encoding="utf-8"))["results"][0]["reported"] == ["small"]
