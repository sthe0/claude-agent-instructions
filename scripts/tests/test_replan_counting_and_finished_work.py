"""Replan counting by change class, and effort fires on finished work.

Two difficulties, one seam. (1) The effort REPLANS scale counted every applied replan, so a
loop of replans that changed nothing the work runs under (a no_change replan, a `cost_tier`
bookkeeping edit, a check-only edit) spent the budget meant to catch a planning approach that
is not converging; each applied replan now records its class from `change_tier` and the scale
counts only operative ones. (2) A spend or wall-clock fire raised when every stage was already
PASSED or SKIPPED routed the session into DIAGNOSING to correct a plan with nothing left to
run; it is now recorded as a `finished_work` fire with one retier datum per window stage and
the session proceeds.

Everything the stage changes is reached through the public command surface, so a test fails
on a tree without the stage instead of failing the module at collection.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agentctl import cli, effort, gates, task_accumulator
from agentctl.state import Node
from conftest import STAGE_OBSERVATIONS

_STAGE1_LAST_LINE = 'output_artifacts = ["mod.py"]'
_STAGE2_LAST_LINE = 'output_artifacts = ["tests/test_mod.py"]'
_NO_CLASSES = {"no_change": 0, "bookkeeping": 0, "check_only": 0, "operative": 0}


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    """The replan-authorization gate has its own suite; here an applied non-substantive
    replan must not wait on a diff presentation."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def ns(**kw):
    return Namespace(**kw)


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _plan(fixtures_dir):
    return str(fixtures_dir / "plan_two_stage.toml")


def _edited(fixtures_dir, tmp_path, name, *, stage1=(), stage2=()):
    """The two-stage fixture with extra lines appended to a stage's table."""
    text = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    for last, lines in ((_STAGE1_LAST_LINE, stage1), (_STAGE2_LAST_LINE, stage2)):
        assert text.count(last) == 1, "fixture no longer ends the stage as expected"
        text = text.replace(last, "\n".join((last, *lines)))
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_cost_log(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
                    encoding="utf-8")


def _pass(store, sid, index, cost_log=None):
    return cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation=STAGE_OBSERVATIONS[index - 1],
           cost_log=str(cost_log) if cost_log else None), store=store)


def _fail(store, sid, cost_log=None):
    return cli.cmd_record_result(
        ns(session=sid, status="failed", actual="boom", control="", observation="",
           cost_log=str(cost_log) if cost_log else None), store=store)


def _critique(store, sid, *, differences=(), invariants=()):
    """Work the open difficulty through to a normalized critique that names a resource
    failure (so no norm edit is owed)."""
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        invariants_to_preserve=list(invariants),
                        differences_to_remove=list(differences),
                        failure_address="resource", cause_category="implementation_defect",
                        machinery_induced="false", mechanism="none",
                        functional_place="verification"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def _failed_and_critiqued(store, sid, plan, **critique):
    _to_executing_stage1(store, sid, plan)
    assert _fail(store, sid).node == Node.DIAGNOSING.value
    _critique(store, sid, **critique)


def _replan(store, sid, plan, **kw):
    return cli.cmd_replan(ns(session=sid, plan=plan, **kw), store=store)


def _accumulated_replans(store, sid):
    return task_accumulator.get(store.load(sid).task_id)["per_axis_totals"]["replan_count"]


# --- #200: counting by change class ---------------------------------------------------

def test_operative_replan_still_counts(store, venued_fixtures_dir, tmp_path):
    sid = "operative"
    plan = _plan(venued_fixtures_dir)
    edited = _edited(venued_fixtures_dir, tmp_path, "operative.toml",
                     stage1=('method = "build it in two passes"',))
    _failed_and_critiqued(store, sid, plan)

    d = _replan(store, sid, edited)

    assert d.ok, d.detail
    state = store.load(sid)
    assert effort.replan_count(state) == 1
    assert effort.replan_class_counts(state) == {**_NO_CLASSES, "operative": 1}
    assert _accumulated_replans(store, sid) == 1


def test_check_only_replan_does_not_count_on_the_replans_scale(store, venued_fixtures_dir, tmp_path):
    sid = "check-only"
    plan = _plan(venued_fixtures_dir)
    edited = _edited(venued_fixtures_dir, tmp_path, "check-only.toml", stage1=("expected_exit = 1",))
    _failed_and_critiqued(store, sid, plan)

    d = _replan(store, sid, edited)

    assert d.ok, d.detail
    state = store.load(sid)
    assert effort.replan_count(state) == 0
    assert effort.replan_class_counts(state) == {**_NO_CLASSES, "check_only": 1}
    assert effort.actual(state)[effort.SCALE_REPLANS] == 0
    assert _accumulated_replans(store, sid) == 0


def test_no_change_replan_rearms_verify_final_without_counting(store, venued_fixtures_dir):
    sid = "no-change"
    plan = _plan(venued_fixtures_dir)
    _failed_and_critiqued(store, sid, plan)

    refused = cli.cmd_verify_final(ns(session=sid, cost_log=None), store=store)
    assert not refused.ok and "no_change" in refused.detail, refused.detail

    d = _replan(store, sid, plan)

    assert d.ok, d.detail
    assert d.data["replan_classes"] == {**_NO_CLASSES, "no_change": 1}
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert effort.replan_count(state) == 0
    assert _accumulated_replans(store, sid) == 0

    cli.cmd_next_stage(ns(session=sid), store=store)
    _pass(store, sid, 1)
    cli.cmd_next_stage(ns(session=sid), store=store)
    _pass(store, sid, 2)
    final = cli.cmd_verify_final(ns(session=sid, cost_log=None), store=store)
    assert final.ok, final.detail


def test_bookkeeping_and_check_only_replans_are_counted_apart_and_shown(
        store, venued_fixtures_dir, tmp_path):
    sid = "apart"
    plan = _plan(venued_fixtures_dir)
    _to_executing_stage1(store, sid, plan)
    bookkeeping = _edited(venued_fixtures_dir, tmp_path, "bookkeeping.toml",
                          stage1=('cost_tier = "large"',))
    check_only = _edited(venued_fixtures_dir, tmp_path, "check-only.toml",
                         stage1=('cost_tier = "large"', "expected_exit = 1"))

    d1 = _replan(store, sid, bookkeeping)
    assert d1.ok, d1.detail
    assert d1.data["replan_classes"] == {**_NO_CLASSES, "bookkeeping": 1}
    d2 = _replan(store, sid, check_only)
    assert d2.ok, d2.detail
    assert d2.data["replan_classes"] == {**_NO_CLASSES, "bookkeeping": 1, "check_only": 1}

    state = store.load(sid)
    assert effort.replan_count(state) == 0
    status = cli.cmd_status(ns(session=sid), store=store)
    assert status.data["replan_classes"] == {**_NO_CLASSES, "bookkeeping": 1, "check_only": 1}
    assert [h["change_class"] for h in state.history if h["event"] == "replan"] == [
        "bookkeeping", "check_only"]


# --- #360: coverage for an unchanged plan and for a cost-tier replan -------------------

def test_unchanged_plan_replan_needs_no_carry_into_invariant(store, venued_fixtures_dir):
    sid = "unchanged"
    plan = _plan(venued_fixtures_dir)
    _failed_and_critiqued(store, sid, plan, differences=["a difference to remove"],
                          invariants=["keep idempotency"])

    d = _replan(store, sid, plan)

    assert d.ok, d.detail
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert not [h for h in state.history if h.get("event") == "replan_coverage_waived"]


def test_coverage_blocker_names_the_waiver(store, venued_fixtures_dir, tmp_path):
    sid = "names-waiver"
    plan = _plan(venued_fixtures_dir)
    edited = _edited(venued_fixtures_dir, tmp_path, "operative.toml",
                     stage1=('method = "build it in two passes"',))
    _failed_and_critiqued(store, sid, plan, invariants=["keep idempotency"])

    d = _replan(store, sid, edited, coverage_waiver=None)

    assert not d.ok
    assert d.marker is None, "a plain refusal, not an acknowledged fire's escalation"
    assert "--coverage-waiver" in d.detail
    assert d.data["coverage_blockers"]
    assert all("--coverage-waiver" in blocker for blocker in d.data["coverage_blockers"])


# --- finished-work fires ---------------------------------------------------------------

def test_non_operative_replans_still_owe_a_review_in_the_spend_estimate(
    store, venued_fixtures_dir, tmp_path
):
    sid = "mandated-reviews"
    plan = _plan(venued_fixtures_dir)
    _to_executing_stage1(store, sid, plan)
    state = store.load(sid)
    before = effort._mandated_reviews(state)
    state.log("replan", kind="refinement", change_class="bookkeeping")
    state.log("replan", kind="refinement", change_class="check_only")
    store.save(state)

    state = store.load(sid)

    assert effort.replan_count(state) == 0
    assert effort._mandated_reviews(state) == before + 2


# --- what stays as it was --------------------------------------------------------------

def test_fire_on_unfinished_work_still_diagnoses(store, venued_fixtures_dir, tmp_path):
    sid = "unfinished"
    plan = _plan(venued_fixtures_dir)
    _to_executing_stage1(store, sid, plan)
    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 0.5}])
    _pass(store, sid, 1, cost_log)
    cli.cmd_next_stage(ns(session=sid), store=store)
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 0.5},
                               {"plan_path": plan, "stage_index": 2, "cost_usd": 200.0}])

    d = _fail(store, sid, cost_log)

    assert d.node == Node.DIAGNOSING.value
    state = store.load(sid)
    assert "disposition" not in state.effort_fires[-1]
    assert state.effort_retiers == []
    assert gates.effort_fire_blockers(state)
