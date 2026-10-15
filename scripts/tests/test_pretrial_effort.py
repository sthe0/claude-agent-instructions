"""Pre-trial defects #441, #440, #359: effort and plan-bound state across sub-plans and pauses.

#441  spend made inside a sub-plan reaches the order's effort window exactly once.
#440  a prompt gap while the session is BLOCKED is not booked as active time.
#359  pop-subplan restores the parent plan's bound state exactly.

A test asserts the repaired behaviour with a message that starts `BEHAVIOUR:`; every setup
assertion lacks that marker. Equalities are exact, so a dropped unit and a double count both fail.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    Stage,
    StageStatus,
    Subject,
)
from test_parent_effort_rollup import (
    _order_window_spend,
    _record_stage1,
    _row,
    _spend,
    _to_executing_stage1,
    _write_cost_log,
)


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROMPT_HOOK = SCRIPTS_DIR / "hook-engine-start.py"
PLAN_BOUND_FIELDS = (
    "plan_snapshot_path", "plan_snapshot_hash", "approved_grants_sha256",
    "approved_grant_entries", "accepted_plan_digest",
)


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _resolve_child_in_place(store, sid: str) -> None:
    state = store.load(sid)
    state.stages = [Stage(
        index=1, title="child stage", subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"), actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="done"),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )]
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    state.node = Node.RESOLVED.value
    state.partition = Partition(verdict="single")
    state.current_stage = None
    store.save(state)


def _flush(store, sid: str) -> None:
    state = store.load(sid)
    cli._flush_order_effort(state, store=store)
    store.save(state)


def test_i441_subplan_spend_reaches_order_window_once(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "shared-order")
    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = str(tmp_path / "subplan.toml")
    shutil.copy(parent_plan, child_plan)
    parent_spend, child_spend = 3.0, 2.0
    log = _write_cost_log(tmp_path / "costs.jsonl", [
        _row(None, parent_spend, parent_plan),
        _row(None, child_spend, child_plan),
    ])
    _to_executing_stage1(store, "P", parent_plan)
    _record_stage1(store, "P", log)
    cli.cmd_next_stage(ns(session="P"), store=store)
    _flush(store, "P")
    assert _spend(store.load("P")) == parent_spend, "setup: the parent's spend is not booked"
    window_before = _order_window_spend()
    assert window_before == parent_spend, f"setup: the parent's flush moved {window_before}"

    pushed = cli.cmd_push_subplan(
        ns(session="P", plan=child_plan, task="svc", originating_stage=2), store=store)
    assert pushed.ok is True, f"setup: push-subplan failed: {pushed.detail}"
    cli.cmd_classify(ns(session="P", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="P"), store=store)
    submitted = cli.cmd_submit_plan(ns(session="P", plan=child_plan, cost_log=log), store=store)
    assert submitted.ok is True, f"setup: the sub-plan was not submitted: {submitted.detail}"
    approved = cli.cmd_approve(ns(session="P", by="user"), store=store)
    assert approved.ok is True, f"setup: the sub-plan was not approved: {approved.detail}"
    _flush(store, "P")
    _resolve_child_in_place(store, "P")
    popped = cli.cmd_pop_subplan(ns(session="P"), store=store)
    assert popped.ok is True, f"setup: pop-subplan failed: {popped.detail}"
    _flush(store, "P")

    moved = _order_window_spend() - window_before
    assert moved == child_spend, (
        f"BEHAVIOUR: the sub-plan's {child_spend} reached the order window as {moved} "
        f"(window {window_before} -> {_order_window_spend()})"
    )


# --- #440: a prompt gap while BLOCKED is not active time ------------------------------------

GAP_MINUTES = 10.0
BOOKED_BEFORE = 5.0


def _state_file(home: Path, session: str) -> Path:
    return home / ".claude" / "agentctl" / "state" / f"{session}.json"


def _prompt_after_gap(home: Path, session: str, node: str) -> dict:
    """Write a state at `node` whose previous prompt was GAP_MINUTES ago, run the prompt hook
    as the harness does, and return the state it leaves."""
    path = _state_file(home, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": session, "task_id": "t", "node": node, "weight_class": "SUBSTANTIVE",
        "last_user_prompt_ts": time.time() - GAP_MINUTES * 60, "user_prompt_count": 2,
        "effort_actuals": {"active_minutes": BOOKED_BEFORE},
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(PROMPT_HOOK)],
        input=json.dumps({"session_id": session, "prompt": "carry on"}),
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert proc.returncode == 0, f"setup: the prompt hook failed: {proc.stderr[-300:]}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_i440_blocked_prompt_gap_not_booked(tmp_path):
    after = _prompt_after_gap(tmp_path, "blocked-gap", Node.BLOCKED.value)

    assert after["user_prompt_count"] == 3, "setup: the prompt was not counted"
    assert after["last_user_prompt_ts"] > time.time() - 60, "setup: the prompt boundary was not stamped"
    assert after["effort_actuals"]["active_minutes"] == BOOKED_BEFORE, (
        f"BEHAVIOUR: a {GAP_MINUTES:.0f}-minute prompt gap while the session is BLOCKED was booked "
        f"as active time: {BOOKED_BEFORE} -> {after['effort_actuals']['active_minutes']}"
    )


def test_executing_prompt_gap_booked_as_base(tmp_path):
    after = _prompt_after_gap(tmp_path, "executing-gap", Node.EXECUTING.value)

    booked = after["effort_actuals"]["active_minutes"] - BOOKED_BEFORE
    assert abs(booked - GAP_MINUTES) < 0.5, f"an EXECUTING gap of {GAP_MINUTES} minutes booked {booked}"


# --- #359: pop-subplan restores the parent's plan-bound state ------------------------------


def _plan_with_grant(src: Path, dest: Path, rule: str) -> str:
    text = src.read_text(encoding="utf-8")
    marker = 'output_artifacts = ["mod.py"]\n'
    assert marker in text, "setup: the fixture changed and has no stage-1 output_artifacts line"
    dest.write_text(text.replace(marker, marker + f'\n[stage.grants]\nallow = ["{rule}"]\n', 1),
                    encoding="utf-8")
    return str(dest)


def _bound(state) -> dict:
    return {name: getattr(state, name) for name in PLAN_BOUND_FIELDS}


def test_i359_pop_restores_plan_bound_state(store, venued_fixtures_dir, tmp_path):
    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = _plan_with_grant(venued_fixtures_dir / "plan_two_stage.toml",
                                  tmp_path / "subplan.toml", "Bash(git status:*)")
    _to_executing_stage1(store, "P", parent_plan)
    cli.cmd_next_stage(ns(session="P"), store=store)
    parent_bound = _bound(store.load("P"))
    assert parent_bound["approved_grants_sha256"] and parent_bound["plan_snapshot_hash"], (
        f"setup: the approved parent plan carries no binding: {parent_bound}"
    )

    pushed = cli.cmd_push_subplan(
        ns(session="P", plan=child_plan, task="svc", originating_stage=2), store=store)
    assert pushed.ok is True, f"setup: push-subplan failed: {pushed.detail}"
    cli.cmd_classify(ns(session="P", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="P"), store=store)
    submitted = cli.cmd_submit_plan(ns(session="P", plan=child_plan), store=store)
    assert submitted.ok is True, f"setup: the sub-plan was not submitted: {submitted.detail}"
    approved = cli.cmd_approve(ns(session="P", by="user"), store=store)
    assert approved.ok is True, f"setup: the sub-plan was not approved: {approved.detail}"
    child_bound = _bound(store.load("P"))
    assert child_bound["approved_grants_sha256"] != parent_bound["approved_grants_sha256"], (
        "setup: the sub-plan's approval did not rebind the grants"
    )
    _resolve_child_in_place(store, "P")
    popped = cli.cmd_pop_subplan(ns(session="P"), store=store)
    assert popped.ok is True, f"setup: pop-subplan failed: {popped.detail}"

    restored = _bound(store.load("P"))

    drifted = sorted(name for name in PLAN_BOUND_FIELDS if restored[name] != parent_bound[name])
    assert not drifted, (
        f"BEHAVIOUR: pop-subplan left the plan-bound fields {drifted} as the sub-plan had them "
        "instead of restoring the parent's"
    )
