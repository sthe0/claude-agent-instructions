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

from agentctl import cli, method_statement, plugins, render
from agentctl.plan import load_plan
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_meta_order import _write_plan
from test_order_phase import (  # noqa: F401  (home and ostore are fixtures)
    BUDGET, SESSION, SIDES, _draft, _start, _submit, home, ostore,
)
from test_order_two_sides import BESIDE

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
        store=store, runner=runner)


def _submitted(store, tmp_path, **draft_kw):
    _start(store)
    assert _submit(store, _draft(tmp_path, **draft_kw)).ok


def _plan_file(tmp_path, *, two_sided=True, beside=""):
    directory = tmp_path / "plan"
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
        ns(session=SESSION, id="Q1", target="goal", question=question, kind=kind), store=store, runner=runner)


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


def test_past_order_how_built_is_customer_side(ostore, tmp_path):
    _submitted(ostore, tmp_path)
    judge = Judge(answering(M.HOW_BUILT, containing="cutting a release"))

    refused = _review(ostore, judge)

    assert not refused.ok and "customer side step" in refused.detail
    assert (M.CUSTOMER_SIDE, "cutting a release") in judge.calls


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


# --- the brief site, stage descriptions and plan review ---------------------------------------------


def test_brief_site_reports_method(tmp_path):
    doc = load_plan(_plan_file(tmp_path))
    judge = Judge(answering(M.METHOD, place=M.BRIEF))

    lines = render.brief_site_defects(doc, 1, runner=judge)

    assert lines and judge.places == {M.BRIEF}
    assert any("stage 1 title (brief)" in line and "states a method" in line for line in lines)
    assert len(judge.calls) >= 1


def test_how_built_in_brief_is_defect(tmp_path):
    doc = load_plan(_plan_file(tmp_path))

    lines = render.brief_site_defects(doc, 1, runner=Judge(answering(M.HOW_BUILT, place=M.BRIEF)))

    assert lines and all("states how the product is built" in line for line in lines)


def test_brief_binding_conditions_not_judged(tmp_path):
    doc = load_plan(_plan_file(tmp_path))
    doc.stages[0].binding_conditions = [SimpleNamespace(id="B1", text="the build host runs Linux")]
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
    old_doc = load_plan(_plan_file(tmp_path / "old", two_sided=False)) if (tmp_path / "old").mkdir() is None else None

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


def test_type_entry_reading_in_order_prompt(tmp_path):
    doc = load_plan(_plan_file(tmp_path))

    unit = render.render_unit_review_bundle(doc, "unit:base", plan_sha256="0" * 64)
    pair = render.render_pair_review_bundle(doc, "base-1", plan_sha256="0" * 64, view_dir=tmp_path)
    stage_pair = render.render_pair_review_bundle(doc, "1-base", plan_sha256="0" * 64, view_dir=tmp_path) \
        if "1-base" in __import__("agentctl.plan", fromlist=["plan"]).review_pairs(doc) else ""

    assert M.TYPE_ENTRY_READING in unit and M.TYPE_ENTRY_READING in pair
    assert M.TYPE_ENTRY_READING not in stage_pair


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


def _labelled(counts: dict[str, tuple[int, int]], *, runs=3) -> dict:
    """Results with `counts[label] = (items, found per item out of `runs`)`: that many items of the
    label, each found in the given number of runs and reported `neither` in the rest."""
    rows = []
    for label, (items, found) in counts.items():
        for i in range(items):
            rows.append({"id": f"{label}{i}", "label": label,
                         "reported": [label] * found + [M.NEITHER] * (runs - found)})
    return {"results": rows}


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
])
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
    data = _labelled({M.METHOD: (5, 3), M.HOW_BUILT: (2, 1), M.NEITHER: (3, 3)})
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
