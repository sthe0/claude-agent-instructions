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
import pathlib
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.config import Thresholds
from agentctl.plan import (
    _COST_TIERS,
    _structural_signature,
    PlanError,
    diff_plans,
    parse_plan,
    stage_carry_key,
    stage_question_key,
)
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


# --- (viii) the cost_tier vocabulary is closed, and rejected at submission ------
# Not a style check: an unrecognized tier is accepted by parse_plan, survives
# submit and approve, and then surfaces either as an argparse usage error inside
# the spawn (spawn-specialist.py's --budget choices) or as a KeyError raised from
# config.md lookup inside cmd_approve's arming. Both are three layers from the typo.

def test_unknown_cost_tier_is_rejected_under_strict():
    with pytest.raises(PlanError) as exc:
        _doc([_stage_dict(cost_tier="Large")])
    assert "cost_tier" in str(exc.value) and "vocabulary" in str(exc.value)


@pytest.mark.parametrize("tier", ["small", "medium", "large"])
def test_every_vocabulary_tier_is_accepted(tier):
    assert _doc([_stage_dict(cost_tier=tier)]).stages[0].actor.cost_tier == tier


def test_absent_cost_tier_is_not_rejected():
    assert _doc([_stage_dict()]).stages[0].actor.cost_tier is None


def test_cost_tier_vocabulary_matches_the_spawn_budget_choices():
    """The parser's vocabulary and spawn-specialist.py's --budget choices are two
    copies of one list; a tier accepted here but unknown there dies in the spawn."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "spawn-specialist.py").read_text()
    for tier in _COST_TIERS:
        assert f'"{tier}"' in src or f"'{tier}'" in src


# --- (ix) a cost_tier-only edit is a refinement, not a no-op -------------------
# cost_tier is engine-consumed but absent from _structural_signature and
# stage_carry_key, which is the exact shape diff_plans' own comment records as the
# venue defect. Without cost_tier in _prose the edit diffs as 'no_change', whose
# branch does NOT rewrite state.plan_path — so the tier applies while the engine
# keeps summing the cost ledger under the previous plan file.

def test_cost_tier_only_edit_classifies_as_refinement():
    old = _doc([_stage_dict(cost_tier="small")])
    new = _doc([_stage_dict(cost_tier="large")])
    assert diff_plans(old, new) == "refinement"


def test_adding_a_cost_tier_to_an_undeclared_stage_is_a_refinement():
    assert diff_plans(_doc([_stage_dict()]), _doc([_stage_dict(cost_tier="medium")])) == "refinement"


def test_a_plan_without_cost_tier_still_diffs_as_no_change_against_itself():
    """The conditional join must not perturb plans that omit the field."""
    assert diff_plans(_doc([_stage_dict()]), _doc([_stage_dict()])) == "no_change"


# --- (x) each accessor is bound to the config.md row it names -----------------
# The stage's verify_command only greps config.md for the key literals, so nothing
# otherwise fails when a row is renamed or an accessor's f-string drifts: the break
# surfaces at runtime as a KeyError inside cmd_approve's arming.

def test_every_effort_accessor_reads_a_row_that_exists_in_config_md():
    thr = Thresholds()
    assert thr.effort_divergence_multiple() > 0
    assert thr.effort_replan_absolute() > 0
    assert thr.effort_absolute_interactions() == 0  # ships accounting-only
    for tier in _COST_TIERS:
        assert thr.effort_stage_minutes(tier) > 0
        assert thr.budget_usd_float(tier) > 0


def test_effort_accessors_raise_a_named_keyerror_on_a_missing_row():
    thr = Thresholds({})
    for call in (thr.effort_divergence_multiple, thr.effort_replan_absolute,
                 thr.effort_absolute_interactions):
        with pytest.raises(KeyError, match="config.md"):
            call()
    with pytest.raises(KeyError, match="config.md"):
        thr.effort_stage_minutes("medium")


def test_the_three_wall_clock_rows_are_ordered_by_tier():
    thr = Thresholds()
    assert (thr.effort_stage_minutes("small") < thr.effort_stage_minutes("medium")
            < thr.effort_stage_minutes("large"))
    assert thr.effort_stage_minutes("small") * 5 >= thr.substantive_wall_clock_min


# --- (xi) the approve-time refresh is the only path a tier reaches state by ----
# A REVISE verdict is answered by editing plan_path IN PLACE at plan-mutable
# PLAN_READY, so a cost_tier corrected during plan review reaches state.stages
# only through _refresh_caches_from_plan_path. Arming reads state.stages, so a
# tier that stopped at the file would be estimated against the stale value.

def test_refresh_from_plan_path_picks_up_a_tier_edited_at_plan_ready(tmp_path):
    plan = tmp_path / "p.toml"

    def write(tier_line):
        plan.write_text(
            '[meta]\ntask_id = "t"\n\n[[stage]]\nindex = 1\ntitle = "s"\n'
            'executor = "spawn:developer"\nexpected_result_image = "img"\n'
            'done_criterion = "dc"\nmeans = "Edit"\nmethod = "do"\n' + tier_line
        )

    write('cost_tier = "small"\n')
    state = SessionState(session_id="s1", task_id="t", plan_path=str(plan))
    state.stages = [s for s in cli.load_plan(str(plan)).stages]
    assert state.stage(1).actor.cost_tier == "small"

    write('cost_tier = "large"\n')          # the plan-review REVISE edit
    cli._refresh_caches_from_plan_path(state)
    assert state.stage(1).actor.cost_tier == "large"


def test_refresh_from_plan_path_propagates_a_cleared_tier(tmp_path):
    plan = tmp_path / "p.toml"
    head = ('[meta]\ntask_id = "t"\n\n[[stage]]\nindex = 1\ntitle = "s"\n'
            'executor = "spawn:developer"\nexpected_result_image = "img"\n'
            'done_criterion = "dc"\nmeans = "Edit"\nmethod = "do"\n')
    plan.write_text(head + 'cost_tier = "large"\n')
    state = SessionState(session_id="s1", task_id="t", plan_path=str(plan))
    state.stages = [s for s in cli.load_plan(str(plan)).stages]
    plan.write_text(head)
    cli._refresh_caches_from_plan_path(state)
    assert state.stage(1).actor.cost_tier is None
