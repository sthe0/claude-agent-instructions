"""The two hard gates: empty --by is refused at both (regression lock for feb673b),
non-empty passes, and the guardian predicates report the expected blockers."""
from argparse import Namespace

from agentctl import cli, gates
from agentctl.state import (
    GateRecord,
    Node,
    SessionState,
    Stage,
    StageStatus,
)


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
    cli.cmd_decompose(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    # pass both stages of the two-stage fixture
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok"), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)


# --- empty --by refused at the plan-approval gate ------------------------

def test_approve_empty_by_is_refused(store, fixtures_dir):
    sid = "ga"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_approve(ns(session=sid, by=""), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.PLAN_READY.value  # gate held
    assert any("empty approver" in b for b in d.data["blockers"])


def test_approve_blank_by_is_refused(store, fixtures_dir):
    sid = "gab"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_approve(ns(session=sid, by="   "), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.PLAN_READY.value


def test_approve_nonempty_by_passes(store, fixtures_dir):
    sid = "gp"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_approve(ns(session=sid, by="alice"), store=store)
    assert d.ok is True
    assert store.load(sid).node == Node.APPROVED.value


# --- empty --by refused at the resolution gate ---------------------------

def test_resolve_empty_by_is_refused(store, fixtures_dir):
    sid = "gr"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_resolve(ns(session=sid, by=""), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.RESOLUTION.value  # gate held
    assert any("empty confirmer" in b for b in d.data["blockers"])


def test_resolve_nonempty_by_passes(store, fixtures_dir):
    sid = "grp"
    _to_resolution(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.ok is True
    assert store.load(sid).node == Node.RESOLVED.value


# --- guardian predicates -------------------------------------------------

def test_plan_approval_blockers_for_missing_plan():
    s = SessionState(session_id="x", task_id="t")
    blockers = gates.plan_approval_blockers(s)
    assert any("no plan artifact" in b for b in blockers)
    assert any("not verified" in b for b in blockers)


def test_plan_approval_blockers_empty_when_plan_verified():
    s = SessionState(session_id="x", task_id="t", plan_path="/p.toml", plan_verified=True)
    assert gates.plan_approval_blockers(s) == []


def test_resolution_blockers_reports_unpassed_stages():
    s = SessionState(
        session_id="x", task_id="t",
        stages=[
            Stage(1, "a", "in_thread", "img", "measurable", "dc",
                  status=StageStatus.PASSED.value),
            Stage(2, "b", "in_thread", "img", "measurable", "dc",
                  status=StageStatus.PENDING.value),
        ],
    )
    blockers = gates.resolution_blockers(s)
    assert any("[2]" in b for b in blockers)


def test_resolution_blockers_for_no_stages():
    s = SessionState(session_id="x", task_id="t")
    assert any("no stages" in b for b in gates.resolution_blockers(s))


def test_resolution_blockers_empty_when_all_passed():
    s = SessionState(
        session_id="x", task_id="t",
        approval=GateRecord("plan_approval", armed=True, passed=True),
        stages=[Stage(1, "a", "in_thread", "img", "measurable", "dc",
                      status=StageStatus.PASSED.value)],
    )
    assert gates.resolution_blockers(s) == []


def test_blockers_unknown_gate():
    s = SessionState(session_id="x", task_id="t")
    assert gates.blockers(s, "nope") == ["unknown gate 'nope'"]
