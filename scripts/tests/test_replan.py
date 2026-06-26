"""Replan diff routing: refinement resumes execution; substantive re-arms the gate.
Also covers the loop guard on repeated identical stage failures."""
from argparse import Namespace

from agentctl import cli
from agentctl.state import Node, StageStatus


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
