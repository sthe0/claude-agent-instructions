"""The overcome-difficulty sub-spine (Variant B): a FAILED stage routes to the
DIAGNOSING node; the engine enforces declare -> investigate -> critique in order
and machine-blocks `replan` (via gates.difficulty_blockers) until the Difficulty
record is complete. The cognition stays in the overcome-difficulty skill; this
covers the deterministic SHELL the engine owns."""
from argparse import Namespace

from agentctl import cli, gates
from agentctl.state import (
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    SessionState,
    StageStatus,
)


def ns(**kw):
    return Namespace(**kw)


def _to_failed_stage1(store, sid, plan):
    """Drive a substantive session to EXECUTING stage 1, then fail it -> DIAGNOSING."""
    cli.cmd_start(ns(session=sid, task="diff-demo", goal="", done_criterion="",
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
    return cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)


def _declare(store, sid):
    return cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)


def _investigate(store, sid):
    return cli.cmd_investigate(ns(session=sid, localized_expectation="le",
                                  localized_actual="la"), store=store)


def _critique(store, sid):
    return cli.cmd_critique(ns(session=sid, functional_ground="fg",
                               replanning_task="rt"), store=store)


# --- entry: FAILED -> DIAGNOSING ---------------------------------------------

def test_failed_stage_enters_diagnosing(store, fixtures_dir):
    d = _to_failed_stage1(store, "f1", str(fixtures_dir / "plan_two_stage.toml"))
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"
    state = store.load("f1")
    assert state.difficulty is not None
    assert not state.difficulty.complete()


# --- the gate: replan blocked until complete ---------------------------------

def test_replan_blocked_while_difficulty_incomplete(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_failed_stage1(store, "f2", plan)
    d = cli.cmd_replan(ns(session="f2", plan=plan), store=store)
    assert d.ok is False
    assert d.action == "declare"
    assert d.data["blockers"]  # names the missing sections


def test_replan_allowed_after_full_cycle(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "f3", plan)
    _declare(store, "f3")
    _investigate(store, "f3")
    c = _critique(store, "f3")
    assert c.action == "replan"  # cycle complete; replan unblocked
    d = cli.cmd_replan(ns(session="f3", plan=refined), store=store)
    assert d.ok is True
    assert d.action == "next_stage"
    state = store.load("f3")
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None  # cleared on exit
    assert state.stage(1).outcome.status == StageStatus.PENDING.value  # re-armed


def test_substantive_replan_from_diagnosing_rearms_gate(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_failed_stage1(store, "f4", plan)
    _declare(store, "f4")
    _investigate(store, "f4")
    _critique(store, "f4")
    d = cli.cmd_replan(ns(session="f4", plan=bigger), store=store)
    assert d.marker == "PLAN-READY"
    state = store.load("f4")
    assert state.node == Node.PLAN_READY.value
    assert state.difficulty is None
    assert not state.approval.passed  # must re-approve


# --- ordering enforcement ----------------------------------------------------

def test_investigate_before_declare_refused(store, fixtures_dir):
    _to_failed_stage1(store, "f5", str(fixtures_dir / "plan_two_stage.toml"))
    d = _investigate(store, "f5")
    assert d.ok is False
    assert d.action == "declare"
    assert store.load("f5").difficulty.investigation is None


def test_critique_before_investigation_refused(store, fixtures_dir):
    _to_failed_stage1(store, "f6", str(fixtures_dir / "plan_two_stage.toml"))
    _declare(store, "f6")
    d = _critique(store, "f6")
    assert d.ok is False
    assert store.load("f6").difficulty.critique is None


def test_difficulty_commands_refused_outside_diagnosing(store, fixtures_dir):
    # at PLAN_READY (never failed a stage) declare must refuse
    cli.cmd_start(ns(session="f7", task="t", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session="f7", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="f7"), store=store)
    cli.cmd_submit_plan(ns(session="f7", plan=str(fixtures_dir / "plan_two_stage.toml")), store=store)
    d = _declare(store, "f7")
    assert d.ok is False
    assert d.action == "noop"


# --- guardian unit -----------------------------------------------------------

def test_difficulty_blockers_unit():
    # outside DIAGNOSING -> unconstrained
    s = SessionState(session_id="g", task_id="t", node=Node.EXECUTING.value,
                     approval=__import__("agentctl.state", fromlist=["GateRecord"]).GateRecord(
                         "plan_approval", armed=True, passed=True, by="u"))
    assert gates.difficulty_blockers(s) == []

    # in DIAGNOSING, no record -> blocked
    s2 = SessionState(session_id="g2", task_id="t", node=Node.DIAGNOSING.value)
    assert gates.difficulty_blockers(s2)

    # partial -> still blocked
    s2.difficulty = Difficulty(declaration=Declaration("e", "a", "m"))
    assert gates.difficulty_blockers(s2)

    # complete -> clear
    s2.difficulty.investigation = Investigation("le", "la")
    s2.difficulty.critique = Critique("fg", "rt")
    assert gates.difficulty_blockers(s2) == []
