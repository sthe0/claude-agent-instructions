"""Stage 2 of the effort-divergence-trigger plan: an optional Actor.cost_tier
declaration on a stage, wired to dispatch's tier resolution (flag > declared
tier > "medium" default) and surviving a state reload and a refinement replan.

cost_tier is deliberately excluded from stage_carry_key / stage_question_key /
_structural_signature — those three answer "did the stage's bytes change?" for
carry-forward, question invalidation, and substantive-replan classification
respectively, and a dispatch-budget label is none of those questions.
"""
from __future__ import annotations

import argparse
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import _structural_signature, parse_plan, stage_carry_key, stage_question_key
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


def _stage_dict(index=1, **overrides):
    base = {
        "index": index, "title": "s", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": "do",
    }
    base.update(overrides)
    return base


def _doc(stages):
    return parse_plan({"meta": {"task_id": "t"}, "stage": stages})


# --- (i) undeclared cost_tier keys identically to a declared one ---------------

def test_undeclared_cost_tier_matches_declared_on_carry_and_question_keys():
    bare = _doc([_stage_dict()]).stages[0]
    declared = _doc([_stage_dict(cost_tier="large")]).stages[0]

    assert bare.actor.cost_tier is None
    assert declared.actor.cost_tier == "large"
    assert stage_carry_key(bare) == stage_carry_key(declared)
    assert stage_question_key(bare) == stage_question_key(declared)


def test_undeclared_cost_tier_matches_declared_on_structural_signature():
    assert _structural_signature(_doc([_stage_dict()])) == _structural_signature(
        _doc([_stage_dict(cost_tier="large")])
    )


# --- (ii) round-trips through state persistence ---------------------------------

def test_cost_tier_round_trips_through_state_json():
    s = SessionState(
        session_id="sess", task_id="task",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        stages=[
            Stage(
                index=1, title="s1",
                subject=Subject(material="m", result="img"),
                means=Means(means="Edit", method="do it"),
                actor=Actor(executor="spawn:developer", cost_tier="large"),
                criterion=Criterion(criterion_type="measurable", done_criterion="crit"),
            ),
        ],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.stage(1).actor.cost_tier == "large"


def test_legacy_actor_without_cost_tier_loads_with_default():
    flat = {
        "index": 1, "title": "legacy", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "crit",
        "criterion_type": "measurable",
    }
    stage = Stage.from_dict(flat)
    assert stage.actor.cost_tier is None


# --- (iii) a refinement replan carries a changed declared tier ------------------

def _plain_stage(cost_tier):
    return Stage(
        index=1, title="s",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="in_thread", cost_tier=cost_tier),
        criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
    )


def test_apply_refined_stage_fields_carries_changed_cost_tier():
    cur = _plain_stage("small")
    refined = _plain_stage("large")
    cli._apply_refined_stage_fields(cur, refined)
    assert cur.actor.cost_tier == "large"


def test_apply_refined_stage_fields_carries_cleared_cost_tier():
    cur = _plain_stage("small")
    refined = _plain_stage(None)
    cli._apply_refined_stage_fields(cur, refined)
    assert cur.actor.cost_tier is None


# --- (iv) dispatch resolves flag > declaration > default, on both call paths ----

def _spawn_stage(cost_tier=None):
    return Stage(
        index=1, title="s",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor="spawn:developer", cost_tier=cost_tier),
        criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _executing(sid, stage):
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = 1
    return s


def _budget_arg(argv):
    return argv[argv.index("--budget") + 1]


def test_dispatch_uses_declared_tier_when_flag_omitted(store):
    """In-process Namespace path: args.budget is None (flag omitted), the stage
    declares a tier -> the declaration wins over the "medium" default."""
    sid = "eff-declared"
    store.save(_executing(sid, _spawn_stage(cost_tier="large")))
    seen = []

    def runner(argv):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")

    cli.cmd_dispatch(ns(session=sid, budget=None, complexity="medium", dry_run=False),
                     store=store, runner=runner)
    assert _budget_arg(seen[0]) == "large"


def test_dispatch_flag_overrides_declared_tier(store):
    """An explicit --budget flag wins over the stage's own declaration."""
    sid = "eff-flag-wins"
    store.save(_executing(sid, _spawn_stage(cost_tier="large")))
    seen = []

    def runner(argv):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")

    cli.cmd_dispatch(ns(session=sid, budget="small", complexity="medium", dry_run=False),
                     store=store, runner=runner)
    assert _budget_arg(seen[0]) == "small"


def test_dispatch_defaults_to_medium_without_flag_or_declaration(store):
    sid = "eff-default"
    store.save(_executing(sid, _spawn_stage(cost_tier=None)))
    seen = []

    def runner(argv):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")

    cli.cmd_dispatch(ns(session=sid, budget=None, complexity="medium", dry_run=False),
                     store=store, runner=runner)
    assert _budget_arg(seen[0]) == "medium"


def _dispatch_budget_action() -> argparse.Action:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            dispatch_parser = action.choices["dispatch"]
            for sub in dispatch_parser._actions:
                if sub.dest == "budget":
                    return sub
    raise AssertionError("dispatch subparser has no --budget action")


def test_argparse_budget_default_is_none():
    """The OS-argv path: --budget's argparse default must be None (not "medium"),
    so an omitted flag is indistinguishable, at cmd_dispatch, from the in-process
    Namespace path — both fall through to the stage's declared cost_tier."""
    assert _dispatch_budget_action().default is None
