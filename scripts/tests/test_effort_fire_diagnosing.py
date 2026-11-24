"""An effort fire on understanding, planning or a stage enters DIAGNOSING through a machine edge, and the
difficulty it opens is closed by one of two returns. Who may acknowledge the fire follows the fire's line
(`effort_segments.line_acknowledged_by`), and a user's `revise` writes stage 7's correction. The
understanding half of these tests is in test_work_of_understanding_planning.py, where its harness is."""
import contextlib
import io
import json

import pytest

from agentctl import cli, cli_approval, cli_common, effort, gates
from agentctl import effort_segments as seg
from agentctl.config import Thresholds
from agentctl.directive import Directive
from agentctl.machine import TRANSITIONS
from agentctl.own_contour import OWN_CONTOUR_VERBS
from agentctl.state import EFFORT_FIRE_FINISHED_WORK
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_effort_per_stage_judging import (  # noqa: F401  (_env is autouse)
    Eng as StageBase, _env, executing_overspent, link_child, seed_replans, stage_arming,
)
from test_effort_planning_segment import Eng as PlanningBase, OVERSPENT_USD, fires_of


class _Cycle:
    """The CLI driver with list flags and the difficulty cycle a fire opens."""

    def run(self, cmd, **flags):
        argv = ["--state-root", str(self.root), cmd.replace("_", "-")]
        for name, value in flags.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    argv.extend([flag, str(item)])
            elif value is not None and value is not False:
                argv.extend([flag, str(value)])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exit_:
                return {"ok": False, "rc": exit_.code, "marker": None, "detail": err.getvalue()}
        result = json.loads(out.getvalue())
        result["rc"] = rc
        return result

    def cycle(self, sid="s", tag="a"):
        self.run("declare", session=sid, expected=f"e-{tag}", actual=f"a-{tag}", mismatch=f"m-{tag}")
        self.run("investigate", session=sid, localized_expectation=f"le-{tag}",
                 localized_actual=f"la-{tag}", hypothesis=[f"h1-{tag}", f"h2-{tag}"])
        self.run("critique", session=sid, functional_ground=f"fg-{tag}", replanning_task=f"rt-{tag}",
                 failure_address="resource", difference_to_remove=[f"d-{tag}"],
                 cause_category="implementation_defect", machinery_induced="false",
                 mechanism="none", functional_place="verification")
        self.run("normalize", session=sid, factor=f"factor-{tag}", level="note")

    def ack(self, sid="s", by="user", decision="continue", **flags):
        return self.run("fire_acknowledge", session=sid, by=by, decision=decision, **flags)


class Eng(_Cycle, PlanningBase):
    pass


class StageEng(_Cycle, StageBase):
    pass


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


@pytest.fixture
def stage(tmp_path):
    return StageEng(tmp_path)


def test_the_machine_has_the_four_effort_edges():
    assert TRANSITIONS["understanding_fire"] == ("ROUTED", "DIAGNOSING")
    assert TRANSITIONS["planning_fire"] == ("PLANNING", "DIAGNOSING")
    assert TRANSITIONS["understanding_return"] == ("DIAGNOSING", "ROUTED")
    assert TRANSITIONS["first_plan_return"] == ("DIAGNOSING", "PLAN_READY")


# --- planning ---------------------------------------------------------------------------


def test_planning_fire_enters_diagnosing(eng):
    eng.planning()
    eng.book(usd=OVERSPENT_USD)
    refused = eng.run("submit_plan", session="s", plan=eng.write())
    state = eng.state()
    assert refused["ok"] is False
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "planning"
    [fire] = fires_of(state)
    assert fire["difficulty_uid"] == state.difficulty.uid


def test_planning_difficulty_before_first_plan_closes_by_submit_plan(eng):
    eng.planning()
    eng.book(usd=OVERSPENT_USD)
    plan = eng.write()
    eng.run("submit_plan", session="s", plan=plan)
    blocked = eng.run("submit_plan", session="s", plan=plan)
    assert blocked["ok"] is False and eng.state().node == "DIAGNOSING"
    eng.cycle()
    still = eng.run("submit_plan", session="s", plan=plan)
    assert still["ok"] is False, "the fire is the user's, so the difficulty alone does not close it"
    assert eng.ack(decision="continue")["ok"]
    returned = eng.run("submit_plan", session="s", plan=plan)
    assert returned["ok"], returned
    assert eng.state().node == "PLAN_READY"


def test_fire_acknowledge_names_the_step_that_is_actually_next(eng):
    """Before the difficulty's phases are done the acknowledgment answers the missing phase, never
    `next_stage`; with the cycle done it answers the return verb."""
    eng.planning()
    eng.book(usd=OVERSPENT_USD)
    plan = eng.write()
    eng.run("submit_plan", session="s", plan=plan)
    assert eng.ack()["action"] == "declare"
    eng.cycle()
    assert eng.state().node == "DIAGNOSING"
    again = eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    assert again["action"] not in ("next_stage", "declare", "investigate", "critique")


def test_a_planning_fire_at_plan_ready_leaves_the_node_and_the_users_approval_acknowledges_it(eng):
    eng.plan_ready()
    eng.book(usd=OVERSPENT_USD)
    eng.run("approve", session="s", by="agent")
    assert eng.state().node == "PLAN_READY"
    assert gates.open_fire_record(eng.state()) is not None
    assert eng.run("approve", session="s", by="user")["ok"]


@pytest.mark.parametrize("scale", ["usd", "minutes"])
def test_every_scale_opens_a_difficulty_on_planning(eng, scale):
    eng.planning()
    eng.book(**{scale: OVERSPENT_USD * 10})
    eng.run("submit_plan", session="s", plan=eng.write())
    state = eng.state()
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "planning"


# --- a stage ----------------------------------------------------------------------------------


def test_stage_fire_enters_diagnosing(stage):
    executing_overspent(stage, usd=30.0)
    stage.failed("s")
    state = stage.state("s")
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "stage"
    assert gates.open_fire_record(state) is not None


@pytest.mark.parametrize("scale", ["usd", "minutes"])
def test_every_scale_opens_a_difficulty_on_a_stage(stage, scale):
    executing_overspent(stage, **{scale: 100000.0})
    stage.failed("s")
    state = stage.state("s")
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "stage"


def test_a_fire_the_stamp_raised_enters_diagnosing_at_the_passing_record_result(stage):
    """The prompt-boundary stamp judges without refusing anything, so its fire is open at EXECUTING;
    the stage's passing result is the next act and meets it: the stage goes back to PENDING and the
    session into DIAGNOSING rather than being refused with the node unchanged."""
    executing_overspent(stage, usd=30.0)
    first = cli_common._utcnow()
    assert cli_common.stamp_and_judge("s", first, store=stage.store) is True
    assert cli_common.stamp_and_judge("s", first + 1.0, store=stage.store) is True
    assert stage.state("s").node == "EXECUTING"
    assert gates.open_fire_record(stage.state("s")) is not None
    refused = stage.passed("s")
    state = stage.state("s")
    assert refused["ok"] is False
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "stage"
    assert state.stage(1).outcome.status != "passed"
    assert gates.open_fire_record(state)["difficulty_uid"] == state.difficulty.uid


def test_a_recorded_finished_work_fire_is_still_exempt_from_the_gate(stage):
    """No path of this tree raises a fire with the finished-work disposition any more; one an old
    state file carries stays exempt, as it was."""
    executing_overspent(stage, usd=30.0)
    stage.failed("s")
    state = stage.state("s")
    gates.open_fire_record(state)["disposition"] = EFFORT_FIRE_FINISHED_WORK
    stage.store.save(state)
    assert gates.effort_fire_blockers(stage.state("s")) == []


def test_old_session_without_segments_raises_no_stage_fire_and_a_failure_still_diagnoses(stage):
    """A session whose state has no effort segments (an old state file) is judged on no stage
    segment; the failed result takes the same way into DIAGNOSING it always took."""
    stage.executing("s")
    state = stage.state("s")
    state.effort_segments = {}
    stage.store.save(state)
    stage.failed("s")
    state = stage.state("s")
    assert [f for f in state.effort_fires if f.get("segment")] == []
    assert state.node == "DIAGNOSING"


# --- the acknowledgment rule ----------------------------------------------------------------------


def test_user_line_fire_refuses_agent_acknowledgment(eng):
    eng.planning()
    eng.book(usd=OVERSPENT_USD)
    eng.run("submit_plan", session="s", plan=eng.write())
    assert seg.line_acknowledged_by(eng.state().effort_fires[-1]) == seg.AUTHORITY_USER
    eng.cycle()
    refused = eng.ack(by="agent", decision="revise")
    assert refused["ok"] is False
    assert gates.open_fire_record(eng.state()) is not None
    assert eng.ack(by="user", decision="continue")["ok"]


def test_the_acknowledging_line_is_read_from_the_fire_alone():
    assert seg.line_acknowledged_by({"line": seg.LINE_DIFFICULTY}) == seg.AUTHORITY_AGENT
    assert seg.line_acknowledged_by({"line": seg.LINE_USER}) == seg.AUTHORITY_USER
    assert seg.line_acknowledged_by({"scale": "replans"}) == seg.AUTHORITY_AGENT
    assert seg.line_acknowledged_by({"scale": "usd"}) == seg.AUTHORITY_USER


def test_a_replans_fire_on_a_stage_is_the_agents_to_acknowledge(stage):
    stage.executing("s")
    seed_replans(stage, "s", Thresholds().effort_replan_absolute())
    stage.failed("s")
    state = stage.state("s")
    fire = [f for f in state.effort_fires if f.get("scale") == effort.SCALE_REPLANS][-1]
    assert seg.line_acknowledged_by(fire) == seg.AUTHORITY_AGENT
    assert state.node == "DIAGNOSING"


def test_a_childs_user_line_fire_goes_to_its_parent_and_the_child_stays_put(stage):
    executing_overspent(stage, "c", usd=30.0)
    link_child(stage, "absent", "c")
    directive = stage.run("dispatch", session="c")
    assert directive["ok"] is False and directive["data"]["norm_mismatch"] is True
    assert stage.state("c").node == "EXECUTING"
    assert "fire-acknowledge" not in OWN_CONTOUR_VERBS, "a child's own channel does not carry the verb"
    assert gates.open_fire_record(stage.state("c")) is not None
    cleared = stage.ack("c", by="user", decision="continue")
    assert cleared["ok"], "the root session acknowledges the child's fire on the user's answer"
    assert gates.open_fire_record(stage.state("c")) is None


# --- a user's revise writes stage 7's correction, an agent's writes none ---------------------------


@pytest.fixture
def corrections(monkeypatch):
    calls = []

    def spy(args, *, store, runner=None):
        calls.append((args.stage, args.by, args.minutes, args.usd))
        return Directive(True, "DIAGNOSING", "noop", "written", data={"estimate": "agreed"})

    monkeypatch.setattr(cli_approval, "cmd_estimate_correct", spy)
    return calls


def _new_form(stage):
    """The fixture plan's stages are old-form (no knowledge place); a new-form one is what a revise
    writes a correction for."""
    state = stage.state("s")
    state.stage(1).place = "p"
    stage.store.save(state)


def test_fire_acknowledge_revise_writes_the_stage_correction(stage, corrections):
    executing_overspent(stage, usd=30.0)
    stage.failed("s")
    _new_form(stage)
    done = stage.ack(by="user", decision="revise", minutes=40, usd=12.5)
    assert done["ok"], done
    assert corrections == [(1, "user", 40.0, 12.5)]
    assert gates.open_fire_record(stage.state("s")) is None


def test_a_refused_correction_leaves_the_fire_open_and_names_the_way_on(stage, monkeypatch):
    executing_overspent(stage, usd=30.0)
    stage.failed("s")
    _new_form(stage)
    monkeypatch.setattr(cli_approval, "cmd_estimate_correct",
                        lambda args, *, store, runner=None: Directive(
                            False, "DIAGNOSING", "estimate_correct",
                            "stage 1's first figure is not agreed yet: agree it first"))
    refused = stage.ack(by="user", decision="revise")
    assert refused["ok"] is False and "not agreed yet" in refused["detail"]
    assert gates.open_fire_record(stage.state("s")) is not None
    assert stage.ack(by="user", decision="continue")["ok"], "continue is the way on when no figure is agreed"


def test_agent_revise_of_a_user_line_stage_fire_writes_no_correction(stage, corrections):
    executing_overspent(stage, usd=30.0)
    stage.failed("s")
    refused = stage.ack(by="agent", decision="revise")
    assert refused["ok"] is False
    assert corrections == []
    assert stage.ack(by="user", decision="continue")["ok"]
