"""#14: `reject` is the resolution gate's negative exit — re-opens the difficulty
cycle and marks stage(s) FAILED so a reject is never a structural no-op.
#15: a corrected plan may be resubmitted at PLAN_READY (pre-approval) via the
`revise_plan` edge, without `reset --force`, and unconditionally invalidates any
recorded thinker review so the plan-review gate re-arms."""
from argparse import Namespace

from agentctl import cli
from agentctl.state import Node, StageStatus


def ns(**kw):
    return Namespace(**kw)


def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _to_resolution(store, sid, plan):
    _to_plan_ready(store, sid, plan)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                  control="reviewed: ok"), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)


# --- #14: reject -------------------------------------------------------------

def test_reject_reopens_difficulty_cycle_and_fails_default_stage(store, fixtures_dir):
    sid = "rej1"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))

    d = cli.cmd_reject(ns(session=sid, reason="output doesn't match what was asked", stage=None), store=store)
    assert d.marker == "OVERCOME-DIFFICULTY"
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.stage(2).outcome.status == StageStatus.FAILED.value  # default: final stage
    assert state.stage(1).outcome.status == StageStatus.PASSED.value  # untouched
    assert state.current_stage is None
    assert state.difficulty is not None
    assert state.difficulty.declaration.actual == "output doesn't match what was asked"


def test_reject_named_stage(store, fixtures_dir):
    sid = "rej2"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))

    d = cli.cmd_reject(ns(session=sid, reason="stage 1's output is wrong", stage=[1]), store=store)
    assert d.ok is False
    assert d.data["rejected_stages"] == [1]
    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert state.stage(2).outcome.status == StageStatus.PASSED.value  # not touched


def test_reject_empty_reason_refused(store, fixtures_dir):
    sid = "rej3"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))

    d = cli.cmd_reject(ns(session=sid, reason="   ", stage=None), store=store)
    assert d.ok is False
    state = store.load(sid)
    assert state.node == Node.RESOLUTION.value  # gate held
    assert all(s.outcome.status == StageStatus.PASSED.value for s in state.stages)


def test_reject_refused_outside_resolution(store, fixtures_dir):
    sid = "rej4"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))

    d = cli.cmd_reject(ns(session=sid, reason="too early", stage=None), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.PLAN_READY.value


def test_reject_unknown_stage_index_refused(store, fixtures_dir):
    sid = "rej5"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))

    d = cli.cmd_reject(ns(session=sid, reason="bad index", stage=[99]), store=store)
    assert d.ok is False
    state = store.load(sid)
    assert state.node == Node.RESOLUTION.value  # gate held, no stage touched
    assert all(s.outcome.status == StageStatus.PASSED.value for s in state.stages)


# --- #15: pre-approval plan resubmission (revise_plan) -----------------------

def test_resubmit_at_plan_ready_uses_revise_plan_edge(store, fixtures_dir):
    sid = "rev1"
    original = str(fixtures_dir / "plan_two_stage.toml")
    corrected = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, original)
    assert store.load(sid).node == Node.PLAN_READY.value

    d = cli.cmd_submit_plan(ns(session=sid, plan=corrected), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value  # stays at PLAN_READY, no reset needed
    assert state.plan_path == corrected
    assert state.stage(1).title == "Scaffold the module skeleton"  # refined content applied
    assert not state.approval.passed


def test_resubmit_clears_recorded_plan_review(store, fixtures_dir):
    """A resubmission invalidates any recorded thinker-review verdict so the
    plan-review gate re-arms for the newly submitted plan version."""
    sid = "rev2"
    original = str(fixtures_dir / "plan_two_stage.toml")
    corrected = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, original)

    cli.cmd_plan_review(ns(session=sid, target=None, verdict="pass",
                           reviewer="thinker", concerns=[], note=""), store=store)
    assert store.load(sid).plan_review is not None

    cli.cmd_submit_plan(ns(session=sid, plan=corrected), store=store)
    state = store.load(sid)
    assert state.plan_review is None


def test_first_submission_at_planning_uses_submit_plan_not_revise(store, fixtures_dir):
    """The ordinary first submission (from PLANNING) is unaffected by #15 — it must
    still route through the plain `submit_plan` edge, not `revise_plan`."""
    sid = "rev3"
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    state = store.load(sid)
    assert state.node == Node.PLANNING.value

    d = cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    for entry in state.history:
        if entry.get("event") == "submit_plan":
            assert entry.get("revised") is False
