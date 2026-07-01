"""Replan diff routing: refinement resumes execution; substantive re-arms the gate.
Also covers the loop guard on repeated identical stage failures, and the
--coverage-waiver bypass of the replan coverage gate."""
import json
from argparse import Namespace

from agentctl import cli
from agentctl.plan import diff_plans, load_plan
from agentctl.state import Node, StageStatus


def ns(**kw):
    return Namespace(**kw)


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
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt"), store=store)

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
                        differences_to_remove=[]), store=store)


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
