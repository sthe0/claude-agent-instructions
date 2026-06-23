"""Directive contract for the key commands: every command returns a Directive with
ok/node/action set, and the gate commands carry their return markers (submit-plan ->
PLAN-READY hard gate; resolve -> COMPLETED; loop guard -> ESCALATE)."""
from argparse import Namespace

from agentctl import cli
from agentctl.directive import Directive
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


def _start(store, sid):
    return cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)


def _to_plan_ready(store, sid, plan):
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    return cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def test_start_directive_shape(store):
    d = _start(store, "d1")
    assert isinstance(d, Directive)
    assert d.ok is True
    assert d.node == Node.CLASSIFIED.value
    assert d.action == "classify"
    assert d.marker is None


def test_classify_substantive_directive(store):
    _start(store, "d2")
    d = cli.cmd_classify(ns(session="d2", chat=False, changed_lines=200, files=5,
                            wall_clock_min=60, tracker_key=None, architectural=True,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
    assert d.ok is True
    assert d.node == Node.ROUTED.value
    assert d.action == "plan"
    assert "reasons" in d.data


def test_submit_plan_is_hard_gate_directive(store, fixtures_dir):
    d = _to_plan_ready(store, "d3", str(fixtures_dir / "plan_two_stage.toml"))
    assert d.ok is True
    assert d.node == Node.PLAN_READY.value
    assert d.action == "await_user_approval"
    assert d.marker == "PLAN-READY"


def test_resolve_completed_marker(store, fixtures_dir):
    sid = "d4"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok"), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.ok is True
    assert d.node == Node.RESOLVED.value
    assert d.marker == "COMPLETED"


def test_record_result_loop_guard_escalate_marker(store, fixtures_dir):
    sid = "d5"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    d = cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert d.ok is False
    assert d.action == "replan"
    assert d.marker is None

    # restart the same stage, fail identically -> ESCALATE marker
    state = store.load(sid)
    from agentctl.state import StageStatus
    state.stage(1).status = StageStatus.ACTIVE.value
    state.current_stage = 1
    state.node = Node.EXECUTING.value
    store.save(state)
    d = cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert d.ok is False
    assert d.marker == "ESCALATE"


def test_status_directive_on_empty_session(store):
    d = cli.cmd_status(ns(session="nope"), store=store)
    assert d.ok is True
    assert d.node == "(none)"
    assert d.action == "start"


def test_directive_to_dict_is_json_safe(store):
    d = _start(store, "d6")
    payload = d.to_dict()
    assert set(payload) >= {"ok", "node", "action", "detail", "marker", "data"}
