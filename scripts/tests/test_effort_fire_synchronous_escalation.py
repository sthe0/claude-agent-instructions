"""Stage 7 (item C) of unify-loop-prevention: an effort-divergence fire gates the
NEXT dispatch/replan/submit_plan through a synchronous, typed escalation
(Directive.marker == "ESCALATE_TO_USER") that `agentctl fire-acknowledge` must
resolve — not merely a state flag (state.effort_fires) a still-executing session
can silently leave unread. test_effort_trigger.py pins the two existing fire sites
(record-result, verify-final) forcing DIAGNOSING; this file pins the gap those
sites left open: cmd_dispatch had zero fire-awareness at all, and a FAILING
branch's fire only rode along as a side-note on an unrelated failure Directive
with no forced decision. The ed1e2dd0 incident this closes: a session executing
at 8.93x its own wall-clock trigger with the fire already recorded but never
acted on.
"""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli, effort, gates
from agentctl.state import Node, StageStatus
from conftest import STAGE_OBSERVATIONS


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


def _write_cost_log(path, rows):
    import json
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _fire_a_session(store, fixtures_dir, tmp_path, sid):
    """Drive a session through fire site 1 (record-result overrun) so it lands in
    DIAGNOSING with exactly one unacknowledged entry in state.effort_fires."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)
    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 100.0}])
    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation=STAGE_OBSERVATIONS[0], cost_log=str(cost_log)),
        store=store,
    )
    assert d.node == Node.DIAGNOSING.value
    state = store.load(sid)
    assert len(state.effort_fires) == 1
    assert state.effort_fires[-1].get("ack") is None
    return plan


def test_dispatch_blocked_by_unacknowledged_fire(store, fixtures_dir, tmp_path):
    sid = "disp-blocked"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d = cli.cmd_dispatch(ns(session=sid), store=store)

    assert d.ok is False
    assert d.marker == "ESCALATE_TO_USER"
    assert d.action == "fire_acknowledge"
    assert d.data["effort_fire"]["scale"] == effort.SCALE_SPEND
    assert d.data["effort_fire"]["task_id"] == "demo-two-stage"
    assert d.data["effort_fire"]["session_id"] == sid


def test_submit_plan_blocked_by_unacknowledged_fire(store, fixtures_dir, tmp_path):
    sid = "submit-blocked"
    plan = _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d = cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)

    assert d.ok is False
    assert d.marker == "ESCALATE_TO_USER"
    assert d.data["effort_fire"]["scale"] == effort.SCALE_SPEND


def test_replan_blocked_by_unacknowledged_fire(store, fixtures_dir, tmp_path):
    sid = "replan-blocked"
    plan = _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d = cli.cmd_replan(ns(session=sid, plan=plan), store=store)

    assert d.ok is False
    assert d.marker == "ESCALATE_TO_USER"
    assert d.data["effort_fire"]["scale"] == effort.SCALE_SPEND
    # the difficulty-record precondition never even runs — the fire is checked
    # first, so its blockers text is exactly effort_fire_blockers' own output
    assert d.data["blockers"] == gates.effort_fire_blockers(store.load(sid))


def test_fire_acknowledge_continue_clears_the_gate_without_moving_the_node(store, fixtures_dir, tmp_path):
    sid = "ack-continue"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)
    node_before = store.load(sid).node

    d = cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="continue", note="accepted, keep going"),
        store=store,
    )

    assert d.ok is True
    state = store.load(sid)
    assert state.node == node_before  # untouched — continue does not transition
    fire = state.effort_fires[-1]
    assert fire["ack"] == {
        "by": "user", "decision": "continue",
        "ts": fire["ack"]["ts"], "note": "accepted, keep going",
    }
    assert len(state.effort_fires) == 1  # append-only: the record itself is never dropped

    # the gate is now clear: a dispatch attempt no longer carries the fire marker
    d2 = cli.cmd_dispatch(ns(session=sid), store=store)
    assert d2.marker != "ESCALATE_TO_USER"


def test_fire_acknowledge_abandon_parks_at_blocked(store, fixtures_dir, tmp_path):
    """abandon parks at BLOCKED rather than RESOLVED: a mid-execution fire fires
    with stage 2 still PENDING, and check_invariants refuses RESOLVED unless
    every stage is PASSED — RESOLVED would misreport a genuinely-abandoned,
    incomplete session as done."""
    sid = "ack-abandon"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)
    node_before = store.load(sid).node

    d = cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="abandon", note="not worth it"), store=store,
    )

    assert d.ok is True
    assert d.marker == "ESCALATE"
    state = store.load(sid)
    assert state.node == Node.BLOCKED.value
    assert state.blocked_from == node_before
    assert state.effort_fires[-1]["ack"]["decision"] == "abandon"

    # audited and reversible, same as an ordinary block
    d_unblock = cli.cmd_unblock(ns(session=sid), store=store)
    assert d_unblock.ok is True
    assert store.load(sid).node == node_before


def test_fire_acknowledge_revise_leaves_diagnosing_for_the_ordinary_cycle(store, fixtures_dir, tmp_path):
    sid = "ack-revise"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d = cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="revise", note=None), store=store,
    )

    assert d.ok is True
    assert d.action == "declare"
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value  # already there; no transition needed
    assert state.difficulty is not None

    # the ordinary declare/investigate/critique/normalize/replan cycle can now proceed
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)
    d_replan = cli.cmd_replan(
        ns(session=sid, plan=str(fixtures_dir / "plan_two_stage_refined.toml")), store=store,
    )
    assert d_replan.marker != "ESCALATE_TO_USER"
    assert d_replan.action == "next_stage"


def test_fire_acknowledge_rejects_invalid_decision(store, fixtures_dir, tmp_path):
    sid = "ack-invalid"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d = cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="bogus", note=None), store=store,
    )

    assert d.ok is False
    state = store.load(sid)
    assert state.effort_fires[-1].get("ack") is None  # refused before mutating


def test_fire_acknowledge_requires_by(store, fixtures_dir, tmp_path):
    sid = "ack-no-by"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d = cli.cmd_fire_acknowledge(
        ns(session=sid, by="", decision="continue", note=None), store=store,
    )

    assert d.ok is False
    state = store.load(sid)
    assert state.effort_fires[-1].get("ack") is None


def test_fire_acknowledge_on_a_session_with_no_fire_is_a_noop(store, fixtures_dir):
    sid = "no-fire"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="continue", note=None), store=store,
    )

    assert d.ok is False
    assert "no effort-divergence fire" in d.detail


def test_repeat_acknowledge_is_a_noop_and_does_not_re_ack(store, fixtures_dir, tmp_path):
    sid = "ack-twice"
    _fire_a_session(store, fixtures_dir, tmp_path, sid)

    d1 = cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="continue", note="first"), store=store,
    )
    assert d1.ok is True
    first_ack = store.load(sid).effort_fires[-1]["ack"]

    d2 = cli.cmd_fire_acknowledge(
        ns(session=sid, by="someone-else", decision="abandon", note="second"), store=store,
    )
    assert d2.ok is True
    assert d2.action == "noop"
    state = store.load(sid)
    assert state.effort_fires[-1]["ack"] == first_ack  # unchanged by the second call
    assert state.node != Node.RESOLVED.value  # the second (abandon) decision never applied


def test_dispatch_and_submit_plan_and_replan_all_unblocked_once_acknowledged(store, fixtures_dir, tmp_path):
    sid = "unblock-all"
    plan = _fire_a_session(store, fixtures_dir, tmp_path, sid)

    for cmd, kwargs in (
        (cli.cmd_dispatch, {}),
        (cli.cmd_submit_plan, {"plan": plan}),
        (cli.cmd_replan, {"plan": plan}),
    ):
        d = cmd(ns(session=sid, **kwargs), store=store)
        assert d.marker == "ESCALATE_TO_USER"

    cli.cmd_fire_acknowledge(
        ns(session=sid, by="user", decision="continue", note=None), store=store,
    )

    # cmd_submit_plan is dropped from this half: it requires node=PLANNING
    # regardless of the fire gate (unrelated, pre-existing TransitionError) —
    # the real "resubmit while diagnosing" path is cmd_replan, exercised below.
    for cmd, kwargs in (
        (cli.cmd_dispatch, {}),
        (cli.cmd_replan, {"plan": plan}),
    ):
        d = cmd(ns(session=sid, **kwargs), store=store)
        assert d.marker != "ESCALATE_TO_USER"
