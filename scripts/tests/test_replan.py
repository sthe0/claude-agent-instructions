"""Replan diff routing: refinement resumes execution; substantive re-arms the gate.
Also covers the loop guard on repeated identical stage failures, the
--coverage-waiver bypass of the replan coverage gate, the approved-plan snapshot
(#8), and PASSED carry-forward across a substantive replan (#12)."""
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli
from agentctl.plan import diff_plans, load_plan
from agentctl.state import (
    Actor, Criterion, LandedSpec, Means, Node, Stage, StageStatus, Subject, Supply,
)


def ns(**kw):
    return Namespace(**kw)


def _cover_the_order(store, sid, stage=1):
    """Cover the order through the ordinary CLI verbs. With the premise gate LIVE its
    order-coverage half fail-closes on an EMPTY order bag once a plan is submitted, so
    the three #48(b) deadlock tests below — which are about the QUESTION channel —
    must satisfy it to reach approve at all. That half's own two-directional proof
    lives in test_order_coverage.py."""
    cli.cmd_order_raise(ns(session=sid, id="O1", element="the order this plan answers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=stage, reason=""),
                          store=store)


def _read_gate_log(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def test_refinement_resumes_without_reapproval(store, fixtures_dir):
    sid = "rf"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert state.approval.passed  # gate stays passed
    assert state.stage(1).title == "Scaffold the module skeleton"  # prose applied


def test_substantive_rearms_plan_gate(store, fixtures_dir):
    sid = "sb"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=bigger), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert not state.approval.passed  # re-arm: must re-approve
    assert [s.index for s in state.stages] == [1, 2, 3]


def test_refinement_after_failure_rearms_the_stage(store, fixtures_dir):
    sid = "rfa"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)

    # stage 1 fails -> FAILED, node DIAGNOSING (the overcome-difficulty sub-spine)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert state.node == Node.DIAGNOSING.value

    # replan is blocked until the difficulty cycle is worked through
    blocked = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert blocked.ok is False
    assert "blockers" in blocked.data

    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)

    # now a refinement replan must re-arm the failed stage and point back at it
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.action == "next_stage"
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None  # cleared on exit
    assert state.stage(1).outcome.status == StageStatus.PENDING.value
    assert state.ready_stages()[0].index == 1  # the retried stage is selectable again


def test_refinement_applies_changed_means_to_state(store, fixtures_dir):
    """A means-only refinement must land the new means/method in state (not just
    title + result image) so the corrected means actually takes effect."""
    sid = "mc"
    base = str(fixtures_dir / "plan_two_stage_means.toml")
    changed = str(fixtures_dir / "plan_two_stage_means_changed.toml")
    _to_executing_stage1(store, sid, base)
    assert store.load(sid).stage(1).means.means == "blind reload"

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"  # refinement resumes execution, no re-approval
    state = store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert state.approval.passed
    assert state.stage(1).means.means == "mirror the working caller"
    assert state.stage(1).means.method == "establish the import context the working caller uses"


def test_verify_command_change_classifies_as_refinement(fixtures_dir):
    """diff_plans returns 'refinement' (not 'no_change') when only verify_command differs."""
    base = load_plan(str(fixtures_dir / "plan_two_stage_verifyfix.toml"))
    changed = load_plan(str(fixtures_dir / "plan_two_stage_verifyfix_changed.toml"))
    assert diff_plans(base, changed) == "refinement"


def test_refinement_carries_verify_command_into_state(store, fixtures_dir):
    """refinement replan must land the new verify_command in live state."""
    sid = "vc"
    base = str(fixtures_dir / "plan_two_stage_verifyfix.toml")
    changed = str(fixtures_dir / "plan_two_stage_verifyfix_changed.toml")
    _to_executing_stage1(store, sid, base)

    assert store.load(sid).stage(1).criterion.verify_command == "python -c 'import mod'"

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert state.stage(1).criterion.verify_command == "python -c 'import mod; assert True'"


def test_refinement_preserves_passed_stage_on_verify_command_change(store, fixtures_dir):
    """A verify_command-only refinement must not reset an already-PASSED stage."""
    sid = "pp"
    base = str(fixtures_dir / "plan_two_stage_verifyfix.toml")
    changed = str(fixtures_dir / "plan_two_stage_verifyfix_changed.toml")
    _to_executing_stage1(store, sid, base)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.current_stage = 2
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).criterion.verify_command == "python -c 'import mod; assert True'"


def test_repo_root_change_classifies_as_refinement_and_updates_state(store, fixtures_dir):
    """A meta.repo_root-only change classifies as refinement and updates state.repo_root."""
    base = load_plan(str(fixtures_dir / "plan_two_stage_verifyfix.toml"))
    with_root = load_plan(str(fixtures_dir / "plan_two_stage_verifyfix_reporoot.toml"))
    assert diff_plans(base, with_root) == "refinement"

    sid = "rr"
    _to_executing_stage1(store, sid, str(fixtures_dir / "plan_two_stage_verifyfix.toml"))
    assert store.load(sid).repo_root is None

    d = cli.cmd_replan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage_verifyfix_reporoot.toml")), store=store)
    assert d.action == "continue"
    assert store.load(sid).repo_root == "/tmp/test-repo-root"


def test_final_check_change_classifies_as_refinement(fixtures_dir):
    """diff_plans returns 'refinement' (not 'no_change') when only final_check differs."""
    base = load_plan(str(fixtures_dir / "plan_two_stage_finalcheck.toml"))
    changed = load_plan(str(fixtures_dir / "plan_two_stage_finalcheck_changed.toml"))
    assert diff_plans(base, changed) == "refinement"


def test_refinement_carries_final_check_into_state(store, fixtures_dir):
    """refinement replan must land the new final_check in live state."""
    sid = "fc"
    base = str(fixtures_dir / "plan_two_stage_finalcheck.toml")
    changed = str(fixtures_dir / "plan_two_stage_finalcheck_changed.toml")
    _to_executing_stage1(store, sid, base)

    assert store.load(sid).final_check[0].command == "true"

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert state.final_check[0].command == "echo changed"


def test_refinement_preserves_passed_stage_on_final_check_change(store, fixtures_dir):
    """A final_check-only refinement must not reset an already-PASSED stage."""
    sid = "fcp"
    base = str(fixtures_dir / "plan_two_stage_finalcheck.toml")
    changed = str(fixtures_dir / "plan_two_stage_finalcheck_changed.toml")
    _to_executing_stage1(store, sid, base)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.current_stage = 2
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.final_check[0].command == "echo changed"


def test_loop_guard_escalates_on_repeated_failure(store, fixtures_dir):
    sid = "lg"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_record_result(ns(session=sid, status="failed", actual="same error"), store=store)
    assert d.action == "declare"  # first failure enters DIAGNOSING

    # restart the same stage, fail with the identical digest -> escalate
    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.ACTIVE.value
    state.current_stage = 1
    state.node = Node.EXECUTING.value
    store.save(state)

    d = cli.cmd_record_result(ns(session=sid, status="failed", actual="same error"), store=store)
    assert d.marker == "ESCALATE"


# --- --coverage-waiver: bypass of the replan coverage gate -------------------

def _to_diagnosing_with_critique(store, sid, plan, *, invariants_to_preserve=None):
    """Drive a session into DIAGNOSING with a critique that declares a similarity
    neither fixture plan carries as a stage condition/invariant, guaranteeing the
    coverage gate blocks."""
    _to_executing_stage1(store, sid, plan)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        invariants_to_preserve=invariants_to_preserve or ["keep idempotency"],
                        differences_to_remove=[], failure_address="нормативное"), store=store)
    # re-norm the difficulty so replan clears the normalization gate and the coverage
    # gate (checked after it) is what these tests exercise.
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def test_coverage_waiver_bypasses_block_and_is_recorded(store, monkeypatch, tmp_path, fixtures_dir):
    log_path = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log_path)
    sid = "cw1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_diagnosing_with_critique(store, sid, plan)

    blocked = cli.cmd_replan(ns(session=sid, plan=refined, coverage_waiver=None), store=store)
    assert blocked.ok is False
    assert "coverage_blockers" in blocked.data

    d = cli.cmd_replan(
        ns(session=sid, plan=refined, coverage_waiver="accepted risk, tracked in ABC-1"),
        store=store,
    )
    assert d.ok is True

    state = store.load(sid)
    waived = [h for h in state.history if h.get("event") == "replan_coverage_waived"]
    assert len(waived) == 1
    assert waived[0]["reason"] == "accepted risk, tracked in ABC-1"

    rows = _read_gate_log(log_path)
    assert "replan_coverage_waiver" in [r["gate"] for r in rows]


def test_coverage_waiver_empty_reason_refused(store, fixtures_dir):
    sid = "cw2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_diagnosing_with_critique(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=refined, coverage_waiver="   "), store=store)
    assert d.ok is False
    assert "coverage_blockers" in d.data
    state = store.load(sid)
    assert not any(h.get("event") == "replan_coverage_waived" for h in state.history)



# --- #8: cmd_approve snapshots the approved plan; cmd_replan diffs the snapshot ---

def test_approve_snapshots_the_approved_plan(store, fixtures_dir):
    sid = "snapA"
    plan_file = fixtures_dir / "plan_two_stage.toml"
    _to_executing_stage1(store, sid, str(plan_file))
    state = store.load(sid)
    assert state.plan_snapshot_path
    assert state.plan_snapshot_hash
    snap_path = Path(state.plan_snapshot_path)
    assert snap_path.exists()
    assert snap_path.read_bytes() == plan_file.read_bytes()


def test_replan_uses_snapshot_when_plan_path_edited_in_place(store, fixtures_dir, tmp_path):
    """Regression for #8: editing the tracked plan file IN PLACE after approval used
    to self-diff to 'no_change' (old and new both read from the mutated plan_path),
    silently dropping the correction. cmd_replan must diff against the immutable
    approved-plan snapshot instead."""
    sid = "snap1"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage.toml").read_text())
    _to_executing_stage1(store, sid, str(plan_path))

    # simulate the bug scenario: the SAME path is overwritten with the substantive
    # plan, so a plan_path-vs-plan_path diff would self-compare and see no_change.
    plan_path.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())

    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert [s.index for s in state.stages] == [1, 2, 3]


def test_replan_lenient_snapshot_missing_derivation_does_not_brick_replan(store, fixtures_dir, tmp_path):
    """Regression: a substantive snapshot frozen before [stage.principle].derivation
    became a required subfield must not permanently block replan. Approval-time
    validation is strict today and cannot itself produce such a file, so the stale
    snapshot is written directly (simulating one frozen by an older trunk) and set
    as the session's approved-plan snapshot before replanning against a normal new
    plan — cmd_replan must complete the diff instead of raising."""
    sid = "snapLenient"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    stale_snapshot = tmp_path / "stale_snapshot.toml"
    stale_snapshot.write_text("""
[meta]
task_id = "old-task"
weight_class = "substantive"
external_research = "checked wiki; none applies"

[[stage]]
index = 1
title = "Old stage"
executor = "spawn:developer"
expected_result_image = "old result"
done_criterion = "old check passes"
material = "existing code"
means = "Edit tool"
method = "old method"
conditions = "EXECUTING node"
invariants = "legacy plans unchanged"
capability_required = "Python"
verify_command = "pytest -q"

[stage.principle]
statement = "old statement"
source = "old source"
confidence = "high"
refutation = "old refutation"
""")
    state = store.load(sid)
    state.plan_snapshot_path = str(stale_snapshot)
    store.save(state)

    new_plan = str(fixtures_dir / "plan_two_stage_substantive.toml")
    d = cli.cmd_replan(ns(session=sid, plan=new_plan), store=store)
    assert d.marker == "PLAN-READY"


# --- #12: PASSED carry-forward across a substantive replan -----------------

def test_substantive_replan_carries_forward_passed_unchanged_stage(store, fixtures_dir):
    sid = "carry1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_executing_stage1(store, sid, plan)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.stage(1).outcome.actual = "done"
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=bigger), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).outcome.actual == "done"
    assert state.stage(2).outcome.status == StageStatus.PENDING.value
    assert state.stage(3).outcome.status == StageStatus.PENDING.value


def test_substantive_replan_resets_passed_stage_whose_definition_changed(store, fixtures_dir):
    """A stage's title changed -> its carry key no longer matches -> PASSED does
    NOT carry forward, even though the stage survives at the same index."""
    sid = "carry2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    retitled = str(fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml")
    _to_executing_stage1(store, sid, plan)

    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=retitled), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PENDING.value
    assert state.stage(1).title == "Scaffold module (revised)"


def test_coverage_waiver_does_not_bypass_difficulty_completeness(store, fixtures_dir):
    """A waiver only lifts the coverage gate — it must not let a replan through
    while the difficulty record (declare/investigate/critique) is incomplete."""
    sid = "cw3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    # difficulty record left incomplete: no declare/investigate/critique yet

    d = cli.cmd_replan(ns(session=sid, plan=refined, coverage_waiver="whatever reason"), store=store)
    assert d.ok is False
    assert "blockers" in d.data
    assert d.action == "declare"


# --- Gap 2: no_change replan refreshes stale materialization on legacy sessions ---

def test_no_change_replan_refreshes_stale_verify_command_and_backfills_snapshot(
        store, fixtures_dir, tmp_path):
    """A legacy session (plan_snapshot_path=None) whose plan file's verify_command is
    edited IN PLACE self-diffs to no_change (old==new==plan_path, for lack of a
    snapshot to diff against). The no_change branch must still refresh state's
    verify_command from the file and backfill a snapshot — else record-result keeps
    running the stale command held in state."""
    sid = "nc1"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage_verifyfix.toml").read_text())
    _to_executing_stage1(store, sid, str(plan_path))

    # make it a legacy pre-snapshot session
    state = store.load(sid)
    state.plan_snapshot_path = None
    state.plan_snapshot_hash = None
    store.save(state)
    old_cmd = state.stage(1).criterion.verify_command
    assert old_cmd == "python -c 'import mod'"

    # edit verify_command IN PLACE (same path) -> old==new -> no_change
    edited = plan_path.read_text().replace(old_cmd, "python -c 'import mod; assert True'")
    assert edited != plan_path.read_text()  # the replacement actually matched
    plan_path.write_text(edited)

    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.action == "continue"  # no_change, resumes without re-approval
    state = store.load(sid)
    # stale verify_command refreshed from the file
    assert state.stage(1).criterion.verify_command == "python -c 'import mod; assert True'"
    # snapshot backfilled so the next replan diffs against real bytes
    assert state.plan_snapshot_path
    assert Path(state.plan_snapshot_path).exists()


# --- #17: PARTITIONED -> VERIFYING when a replan carries every PASSED stage forward ---

def _to_partitioned(store, sid, plan):
    """Drive a session to PARTITIONED WITHOUT calling next-stage, so the caller
    controls exactly which stage(s) are ready."""
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


def test_donecriterion_only_change_is_substantive_but_preserves_stage_carry_keys(fixtures_dir):
    """Sanity check for the regression fixture: a [meta] done_criterion-only edit
    must classify as substantive (meta.done_criterion is in _structural_signature)
    while every stage's carry key (which excludes meta fields) stays identical."""
    from agentctl.plan import stage_carry_key

    base = load_plan(str(fixtures_dir / "plan_two_stage.toml"))
    changed = load_plan(str(fixtures_dir / "plan_two_stage_donecriterion_changed.toml"))
    assert diff_plans(base, changed) == "substantive"
    assert [stage_carry_key(s) for s in base.stages] == [stage_carry_key(s) for s in changed.stages]


def test_next_stage_finalizes_partitioned_when_replan_preserved_all_passed(store, fixtures_dir):
    """The exact deadlock scenario: both stages PASSED, a substantive replan (meta
    done_criterion only) carries both PASSED outcomes forward, re-approval lands
    back at PARTITIONED with no ready stage -- next-stage must advance straight to
    VERIFYING and verify-final must reach RESOLUTION, with zero manual state edits
    beyond the legitimate cmd_record_result PASSED calls."""
    sid = "fin1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    changed = str(fixtures_dir / "plan_two_stage_donecriterion_changed.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_record_result(ns(session=sid, status="passed", actual="mod scaffolded",
                               control="reviewed: ok"), store=store)
    assert d.action == "next_stage"
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.node == Node.EXECUTING.value
    d = cli.cmd_record_result(ns(session=sid, status="passed", actual="tests added",
                               control="reviewed: ok"), store=store)
    assert d.action == "verify_final"
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert state.all_stages_passed()

    # substantive replan (meta done_criterion only) -- re-arms the approval gate but
    # carries both PASSED outcomes forward (stage_carry_key unchanged for each stage)
    d = cli.cmd_replan(ns(session=sid, plan=changed), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert not state.approval.passed
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(2).outcome.status == StageStatus.PASSED.value

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value

    d = cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                             m3_severe=False, m4_severe=False), store=store)
    assert d.node == Node.PARTITIONED.value
    state = store.load(sid)
    assert state.ready_stages() == []  # both PASSED, nothing left to start

    # the fix: next-stage must not dead-end here -- it advances straight to
    # VERIFYING via the guarded finalize_partitioned edge
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.ok is True
    assert d.node == Node.VERIFYING.value
    assert d.action == "verify_final"
    assert store.load(sid).node == Node.VERIFYING.value

    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.node == Node.RESOLUTION.value
    assert store.load(sid).node == Node.RESOLUTION.value


def test_next_stage_does_not_finalize_when_a_stage_is_not_ready_and_not_passed(store, fixtures_dir):
    """The negative guard: a non-ready, non-PASSED stage at PARTITIONED must still
    refuse -- never silently finalize just because ready_stages() is empty."""
    sid = "fin2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_partitioned(store, sid, plan)

    state = store.load(sid)
    # simulate stage 1 having failed (not PASSED); stage 2 depends on stage 1, so
    # neither stage is ready -- but the session has NOT reached all-PASSED.
    state.stage(1).outcome.status = StageStatus.FAILED.value
    store.save(state)
    assert store.load(sid).ready_stages() == []
    assert not store.load(sid).all_stages_passed()

    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.PARTITIONED.value  # unchanged


def test_no_change_replan_with_snapshot_is_idempotent(store, fixtures_dir):
    """Backward-compat: a session WITH a snapshot whose plan is genuinely unchanged
    stays a true no-op — the refresh copies identical values and the existing
    snapshot is left intact (not re-backfilled)."""
    sid = "nc2"
    plan = str(fixtures_dir / "plan_two_stage_verifyfix.toml")
    _to_executing_stage1(store, sid, plan)
    state = store.load(sid)
    snap_before = state.plan_snapshot_path
    cmd_before = state.stage(1).criterion.verify_command
    assert snap_before  # _to_executing_stage1's approve created one

    d = cli.cmd_replan(ns(session=sid, plan=plan), store=store)
    assert d.action == "continue"
    state = store.load(sid)
    assert state.plan_snapshot_path == snap_before  # untouched
    assert state.stage(1).criterion.verify_command == cmd_before
    assert state.node == Node.EXECUTING.value


# --- #48: a mid-execution correction must not re-arm reviewed stages -----------

def _to_passed_stage1_via_dispatch(store, sid, plan_path):
    """Drive stage 1 the way a real session does — dispatch the spawn, then record
    the result — rather than assigning PASSED onto state. Issue #48 names dispatch
    as the mutator, so a test that skips it cannot see the defect it reports."""
    from agentctl.dispatch import RunResult

    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False, constraints=""), store=store,
                     runner=lambda argv, **kw: RunResult(0, stdout="COMPLETED: done\n"))
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                             control="reviewed: ok", observation=""), store=store)


def _submit_edit_approve(store, sid, plan_path, edited_text):
    """Submit a plan, then edit it IN PLACE at PLAN_READY before approving — the
    edit `_refresh_caches_from_plan_path` exists to absorb (PLAN_READY is
    deliberately plan-mutable).

    This reaches `approve` only because conftest turns the plan-review and
    presentation gates off. In production those gates re-hash the file and refuse
    an edit made after the plan was presented, so the real REVISE cycle re-presents
    first — the fields exercised here are the right ones, but the sequence is the
    gates-off shape, not the production one."""
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan_path)), store=store)
    Path(plan_path).write_text(edited_text, encoding="utf-8")
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)


def test_refresh_covers_every_carry_key_field():
    """The relation, not one instance of it: `_apply_refined_stage_fields` must copy
    every field `stage_carry_key` reads. While the copied set was a strict SUBSET,
    an in-place PLAN_READY edit to any of the seven uncopied fields left the live
    stage stale, and the next substantive replan re-armed a PASSED stage whose plan
    text never changed. Adding a field to the key without adding it here fails HERE,
    at the coupling, rather than as a puzzling re-arm in some later session.

    Both stages are built with EVERY carry-key field distinct, so the assertion
    cannot pass by the two sides happening to agree on an uncopied field — which is
    exactly how a fixture-pair version of this test passed against the defect."""
    from agentctl.plan import stage_carry_key

    def _stage(tag, *, venue, kind, deps):
        return Stage(
            index=1,
            title=f"title-{tag}",
            subject=Subject(material=f"material-{tag}", result=f"result-{tag}",
                            invariants=f"invariants-{tag}"),
            means=Means(means=f"means-{tag}", method=f"method-{tag}"),
            actor=Actor(executor=f"spawn:{tag}"),
            criterion=Criterion(criterion_type=tag,
                                done_criterion=f"done-{tag}",
                                verify_command=f"verify-{tag}",
                                expected_exit=len(tag),
                                verify_venue=venue,
                                verify_kind=kind,
                                landed=LandedSpec(target=f"target-{tag}",
                                                  delivered_stage=deps)),
            conditions=f"conditions-{tag}",
            supplies=[Supply(on=deps)],
        )

    live = _stage("aaa", venue="delivery", kind="shell", deps=2)
    refined = _stage("bb", venue="repo_root", kind="landed", deps=3)

    # every element of the key differs, so nothing is compared trivially
    assert all(a != b for a, b in zip(stage_carry_key(live), stage_carry_key(refined)))

    cli._apply_refined_stage_fields(live, refined)
    assert stage_carry_key(live) == stage_carry_key(refined)


def test_substantive_replan_carries_forward_passed_stage_edited_at_plan_ready(
        store, fixtures_dir, tmp_path):
    """The #48 carry-forward defect. Stage 1's done_criterion is edited at PLAN_READY
    and the corrected plan carries that same edit, so stage 1's text is UNCHANGED
    between the approved plan and the corrected one — its PASSED outcome must survive.
    done_criterion is one of the seven carry-key fields the refresh used to miss."""
    sid = "carry48"
    plan_path = tmp_path / "plan.toml"
    base = (fixtures_dir / "plan_two_stage.toml").read_text()
    plan_path.write_text(base)
    edited = base.replace("python -c 'import mod' exits 0",
                          "python -c 'import mod' exits 0 cleanly")
    assert edited != base, "fixture no longer carries the string this test edits"

    _submit_edit_approve(store, sid, plan_path, edited)
    _to_passed_stage1_via_dispatch(store, sid, plan_path)
    assert store.load(sid).stage(1).outcome.status == StageStatus.PASSED.value

    corrected = tmp_path / "corrected.toml"
    corrected.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text()
                         .replace("python -c 'import mod' exits 0",
                                  "python -c 'import mod' exits 0 cleanly"))

    d = cli.cmd_replan(ns(session=sid, plan=str(corrected)), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    assert state.stage(1).outcome.actual == "ok"


def test_substantive_replan_rearms_stage_whose_done_criterion_changed(
        store, fixtures_dir, tmp_path):
    """The companion, and the half that keeps the fix honest: the refresh now copies
    done_criterion, so this test proves the copy did not turn into a blanket carry —
    a stage whose definition GENUINELY changed between the approved and corrected
    plans is still re-armed, exactly as before."""
    sid = "rearm48"
    plan_path = tmp_path / "plan.toml"
    base = (fixtures_dir / "plan_two_stage.toml").read_text()
    plan_path.write_text(base)
    edited = base.replace("python -c 'import mod' exits 0",
                          "python -c 'import mod' exits 0 cleanly")

    _submit_edit_approve(store, sid, plan_path, edited)
    _to_passed_stage1_via_dispatch(store, sid, plan_path)

    # the corrected plan changes stage 1's done_criterion AGAIN -> genuinely different
    corrected = tmp_path / "corrected.toml"
    corrected.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text()
                         .replace("python -c 'import mod' exits 0",
                                  "python -c 'import mod' exits 0 under coverage"))

    d = cli.cmd_replan(ns(session=sid, plan=str(corrected)), store=store)
    assert d.marker == "PLAN-READY"
    assert store.load(sid).stage(1).outcome.status == StageStatus.PENDING.value


def test_corrected_plan_is_enumerable_so_the_premise_gate_stops_deadlocking_replan(
        store, fixtures_dir, monkeypatch):
    """#48(b) end to end, with the premise gate LIVE (the suite force-off deleted).

    `cmd_replan` evaluates the plan_approval plugin gate against the CORRECTED plan,
    and premise_blockers rejects an `enumerated_at` that does not match that plan's
    content digest. The corrected plan is not `state.plan_path` yet and only becomes
    so once the replan succeeds — so before `--plan`, the enumeration could only ever
    re-stamp the OLD plan's digest and the gate blocked the very replan that would
    clear it, with no route out that did not edit session state by hand.

    Every step here is an ordinary CLI call; the point of the test is that the bag is
    never touched directly."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    from agentctl import plugins_premise

    sid = "deadlock"
    base = str(fixtures_dir / "plan_two_stage.toml")
    corrected = str(fixtures_dir / "plan_two_stage_substantive.toml")

    def _silent_advisor(argv, **kw):
        # healthy runner, no questions raised: the flag flips, and the gate is then
        # carried purely by the enumerated_at binding this test is about.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=base), store=store)
    assert "premise" in store.load(sid).plugins  # gate really is live

    cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                               runner=_silent_advisor)
    _cover_the_order(store, sid)
    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)

    # the deadlock itself: the enumeration on record is bound to the OLD plan
    blocked = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert blocked.ok is False
    assert any(plugins_premise._ENUMERATE_STALE in b        # plugin gate prefixes "[premise] "
               for b in blocked.data.get("blockers", []))
    assert store.load(sid).node == Node.EXECUTING.value  # nothing moved

    # the route out: enumerate the corrected plan by name, then replan
    d = cli.cmd_question_enumerate(ns(session=sid, plan=corrected), store=store,
                                   runner=_silent_advisor)
    assert d.ok is True

    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert d.ok is True, d.detail
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert [s.index for s in state.stages] == [1, 2, 3]


def test_corrected_plan_is_rebindable_so_the_premise_gate_stops_deadlocking_replan(
        store, fixtures_dir, monkeypatch):
    """The `question-rebind` twin of #48(b): `_bound_stage_key` previously read
    only `state.plan_path`, which is not yet the CORRECTED plan of a replan
    (`cmd_replan` evaluates the plan_approval gate against `args.plan`, and that
    only becomes `state.plan_path` once the replan succeeds). A disposed
    question's `disposed_at_key` then only ever matched the OLD plan's stage key,
    tripping premise.validate_questions rule 12 (bound-stage-definition-changed)
    against the corrected one — and `question-rebind`, the reachable route out of
    that very blocker, could only re-stamp the same stale key, blocking the
    replan that would clear it.

    Every step here is an ordinary CLI call; the bag is never touched directly."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    from agentctl import plugins_premise

    sid = "deadlock-rebind"
    base = str(fixtures_dir / "plan_two_stage.toml")
    corrected = str(fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml")

    def _silent_advisor(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=base), store=store)
    assert "premise" in store.load(sid).plugins  # gate really is live

    # a question bound to stage 1, disposed against the BASE plan's stage 1 key
    cli.cmd_question_raise(ns(session=sid, id="Q1", target="stage:1.result",
                              question="does the scaffold need a __init__.py?"),
                           store=store)
    cli.cmd_question_research(ns(session=sid, id="Q1", attempted="checked the fixture"),
                              store=store)
    cli.cmd_question_dispose(ns(session=sid, id="Q1", to="researched", answer="no",
                                source="fixture", derivation="single-module package",
                                basis="", risk="", plan=None), store=store)

    cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                               runner=_silent_advisor)
    _cover_the_order(store, sid)
    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)

    # the deadlock itself: Q1's stamp is bound to the OLD (unretitled) stage 1
    blocked = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert blocked.ok is False
    assert any("definition changed" in b for b in blocked.data.get("blockers", []))
    assert store.load(sid).node == Node.EXECUTING.value  # nothing moved

    # the route out: rebind Q1 against the corrected plan by name, and enumerate
    # it too (the retitle also moves the content digest — the enumeration
    # channel's own #48(b) fix, already landed, clears that half)
    d = cli.cmd_question_rebind(ns(session=sid, id="Q1", plan=corrected,
                                   confirm_still_valid="re-read against the "
                                   "retitled stage 1; the answer still holds"),
                               store=store)
    assert d.ok is True
    d = cli.cmd_question_enumerate(ns(session=sid, plan=corrected), store=store,
                                   runner=_silent_advisor)
    assert d.ok is True

    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert d.ok is True, d.detail
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert state.stage(1).title == "Scaffold module (revised)"


def test_corrected_plan_is_redisposable_so_the_premise_gate_stops_deadlocking_replan(
        store, fixtures_dir, monkeypatch):
    """The `question-dispose` twin of #48(b): a question whose ANSWER genuinely
    changes under the corrected plan is re-dispositioned (not merely rebound —
    rebind is for 're-read; still holds', dispose is for 'the answer changed'),
    and re-disposition is the OTHER writer of `disposed_at_key` that could
    previously only stamp against `state.plan_path`, the stale OLD plan."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)

    sid = "deadlock-dispose"
    base = str(fixtures_dir / "plan_two_stage.toml")
    corrected = str(fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml")

    def _silent_advisor(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=base), store=store)
    assert "premise" in store.load(sid).plugins  # gate really is live

    cli.cmd_question_raise(ns(session=sid, id="Q1", target="stage:1.result",
                              question="does the scaffold need a __init__.py?"),
                           store=store)
    cli.cmd_question_research(ns(session=sid, id="Q1", attempted="checked the fixture"),
                              store=store)
    cli.cmd_question_dispose(ns(session=sid, id="Q1", to="researched", answer="no",
                                source="fixture", derivation="single-module package",
                                basis="", risk="", plan=None), store=store)

    cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                               runner=_silent_advisor)
    _cover_the_order(store, sid)
    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).ok is True
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)

    blocked = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert blocked.ok is False
    assert any("definition changed" in b for b in blocked.data.get("blockers", []))
    assert store.load(sid).node == Node.EXECUTING.value

    # the route out: the retitle prompted a genuinely different answer, so this is
    # a fresh disposition rather than a mere rebind — and it must stamp against
    # the corrected plan, named directly, in the same act
    d = cli.cmd_question_dispose(ns(session=sid, id="Q1", to="researched",
                                    answer="yes — the revised scaffold packages "
                                    "as a namespace package", source="fixture",
                                    derivation="split-module layout needs it",
                                    basis="", risk="", plan=corrected), store=store)
    assert d.ok is True
    d = cli.cmd_question_enumerate(ns(session=sid, plan=corrected), store=store,
                                   runner=_silent_advisor)
    assert d.ok is True

    d = cli.cmd_replan(ns(session=sid, plan=corrected), store=store)
    assert d.ok is True, d.detail
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    live = store.load(sid)
    q1 = next(q for q in live.plugins["premise"]["questions"] if q["id"] == "Q1")
    assert q1["answer"].startswith("yes")


# --- venue re-derivation: [meta] delivery_worktree must reach session state -----

def _plan_with_venue(fixtures_dir, name, *, repo_root=None, delivery_worktree=None):
    """Fixture text with the [meta] venue keys injected (omitted when None)."""
    inject = ""
    if repo_root:
        inject += f'repo_root = "{repo_root}"\n'
    if delivery_worktree:
        inject += f'delivery_worktree = "{delivery_worktree}"\n'
    return (fixtures_dir / name).read_text().replace("[meta]\n", "[meta]\n" + inject, 1)


# kind: (base fixture, base worktree, new fixture, new worktree, expected)
# delivery_worktree is absent from diff_plans' comparison keys, so a venue-ONLY
# edit self-diffs to no_change — the branch that today refreshes stage fields
# and final_check but no venue field at all.
_VENUE_REPLAN_CASES = {
    "no_change": ("plan_two_stage.toml", None,
                  "plan_two_stage.toml", "/tmp/wt-one", "/tmp/wt-one"),
    "refinement": ("plan_two_stage.toml", "/tmp/wt-one",
                   "plan_two_stage_refined.toml", "/tmp/wt-two", "/tmp/wt-two"),
    "substantive": ("plan_two_stage.toml", "/tmp/wt-one",
                    "plan_two_stage_substantive.toml", None, None),
}


@pytest.mark.parametrize("kind", sorted(_VENUE_REPLAN_CASES))
def test_replan_refresh_delivery_worktree(store, fixtures_dir, tmp_path, kind):
    """A replan that INTRODUCES, CHANGES or REMOVES [meta] delivery_worktree must
    land that value in session state on every diff kind, exactly as repo_root
    already does. Losing it silently re-arms the venue asymmetry that
    resolve_check_venue exists to remove: dispatch then sends the executor into
    the canonical checkout while the plan declares a delivery worktree."""
    repo = "/tmp/canon"
    base_name, base_wt, new_name, new_wt, expected = _VENUE_REPLAN_CASES[kind]

    sid = f"rvw-{kind}"
    base = tmp_path / f"{kind}-base.toml"
    base.write_text(_plan_with_venue(fixtures_dir, base_name,
                                     repo_root=repo, delivery_worktree=base_wt))
    _to_executing_stage1(store, sid, str(base))
    assert store.load(sid).delivery_worktree == base_wt

    new = tmp_path / f"{kind}-new.toml"
    new.write_text(_plan_with_venue(fixtures_dir, new_name,
                                    repo_root=repo, delivery_worktree=new_wt))
    # Pin the branch each case claims to exercise: without this, a change to
    # diff_plans' comparison keys would silently reclassify a case and drop that
    # branch's coverage while the test stayed green.
    assert diff_plans(load_plan(base, strict=False), load_plan(new)) == kind
    d = cli.cmd_replan(ns(session=sid, plan=str(new)), store=store)
    assert d.ok is True, d.detail

    state = store.load(sid)
    assert state.delivery_worktree == expected
    assert state.repo_root == repo
