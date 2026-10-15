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
from agentctl.config import Thresholds
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
    """Applies to every test in this module: the replan-authorization gate is not under test
    here, and the sub-plan approvals driven below would otherwise stop at it."""
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

#: Below the hook's per-turn active-minutes cap (substantive-wall-clock-min, 30 in config.md),
#: so that an EXECUTING gap is booked whole and the pin can compare it exactly.
GAP_MINUTES = 10.0
BOOKED_BEFORE = 5.0


def _state_file(home: Path, session: str) -> Path:
    return home / ".claude" / "agentctl" / "state" / f"{session}.json"


def _prompt_after_gap(home: Path, session: str, node: str) -> dict:
    """Write a state at `node` whose previous prompt was GAP_MINUTES ago, run the prompt hook
    as the harness does, and return the state it leaves."""
    assert GAP_MINUTES < Thresholds().substantive_wall_clock_min, "setup: the gap exceeds the hook's cap"
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


#: A pop satisfies the originating stage, re-derives the pointer from stage status, and adds the
#: child's actuals onto the parent's (rebuilding the vector with both keys), so these legitimately
#: differ from the parent's pre-push value; test_subplan.py and test_parent_effort_rollup.py pin them.
POP_REWRITTEN_FIELDS = frozenset({"stages", "current_stage", "effort_actuals"})


def test_sessionstate_fields_classified(store, venued_fixtures_dir, tmp_path):
    import dataclasses

    from agentctl import state as state_module

    declared_plan_bound = set(state_module.PLAN_BOUND_FIELDS)
    declared_session_scoped = set(state_module.SESSION_SCOPED_FIELDS)
    actual = {f.name for f in dataclasses.fields(state_module.SessionState)}
    both = sorted(declared_plan_bound & declared_session_scoped)
    unclassified = sorted(actual - declared_plan_bound - declared_session_scoped)
    stale = sorted((declared_plan_bound | declared_session_scoped) - actual)
    frame_fields = {f.name for f in dataclasses.fields(state_module.PlanFrame)}
    uncarried = sorted(declared_plan_bound - frame_fields)

    assert not both, f"BEHAVIOUR: fields classified as both plan-bound and session-scoped: {both}"
    assert not unclassified, (
        f"BEHAVIOUR: SessionState fields with no plan-bound/session-scoped classification: {unclassified}"
    )
    assert not stale, f"BEHAVIOUR: classified names that are not SessionState fields: {stale}"
    assert not uncarried, (
        f"BEHAVIOUR: plan-bound fields the PlanFrame has no field to carry: {uncarried}"
    )

    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = _plan_with_grant(venued_fixtures_dir / "plan_two_stage.toml",
                                  tmp_path / "subplan.toml", "Bash(git status:*)")
    _to_executing_stage1(store, "P", parent_plan)
    cli.cmd_next_stage(ns(session="P"), store=store)
    before = dataclasses.asdict(store.load("P"))
    assert before["approved_grants_sha256"], "setup: the approved parent plan carries no binding"

    pushed = cli.cmd_push_subplan(
        ns(session="P", plan=child_plan, task="svc", originating_stage=2), store=store)
    assert pushed.ok is True, f"setup: push-subplan failed: {pushed.detail}"
    cli.cmd_classify(ns(session="P", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="P"), store=store)
    assert cli.cmd_submit_plan(ns(session="P", plan=child_plan), store=store).ok is True, "setup: submit"
    assert cli.cmd_approve(ns(session="P", by="user"), store=store).ok is True, "setup: approve"
    _resolve_child_in_place(store, "P")
    popped = cli.cmd_pop_subplan(ns(session="P"), store=store)
    assert popped.ok is True, f"setup: pop-subplan failed: {popped.detail}"
    after = dataclasses.asdict(store.load("P"))

    drifted = sorted(name for name in declared_plan_bound - POP_REWRITTEN_FIELDS
                     if after[name] != before[name])
    assert not drifted, f"BEHAVIOUR: push/pop did not round-trip the plan-bound fields {drifted}"


# --- sub-plan custody: runtime grants, the wall-clock axis, nesting, another order ---------------

def _classify_args(sid: str) -> Namespace:
    return ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
              tracker_key=None, architectural=True, external_effect=False,
              new_dependency=False, public_api_change=False)


def _copy_plan(src: str, dest: Path, retitle: dict[str, str] | None = None) -> str:
    text = Path(src).read_text(encoding="utf-8")
    for old, new in (retitle or {}).items():
        assert f'title = "{old}"' in text, f"setup: the fixture has no stage titled {old!r}"
        text = text.replace(f'title = "{old}"', f'title = "{new}"')
    dest.write_text(text, encoding="utf-8")
    return str(dest)


def _push_and_approve(store, sid: str, plan: str, *, originating: int = 1, execute: bool = False,
                      after_push=None) -> None:
    pushed = cli.cmd_push_subplan(
        ns(session=sid, plan=plan, task=f"sub-{Path(plan).stem}", originating_stage=originating), store=store)
    assert pushed.ok is True, f"setup: push-subplan failed: {pushed.detail}"
    if after_push is not None:
        after_push(store.load(sid))
    cli.cmd_classify(_classify_args(sid), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    submitted = cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert submitted.ok is True, f"setup: the sub-plan was not submitted: {submitted.detail}"
    approved = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert approved.ok is True, f"setup: the sub-plan was not approved: {approved.detail}"
    if execute:
        cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                             m3_severe=False, m4_severe=False), store=store)
        cli.cmd_next_stage(ns(session=sid), store=store)
        assert store.load(sid).node == Node.EXECUTING.value, "setup: the sub-plan is not executing"


def _pop(store, sid: str) -> None:
    _resolve_child_in_place(store, sid)
    popped = cli.cmd_pop_subplan(ns(session=sid), store=store)
    assert popped.ok is True, f"setup: pop-subplan failed: {popped.detail}"


def _book(store, sid: str, *, spend: float = 0.0, minutes: float = 0.0) -> None:
    from agentctl import effort

    state = store.load(sid)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = (
        float(state.effort_actuals.get(effort.ACTUAL_SPEND_KEY) or 0.0) + spend)
    state.effort_actuals[effort.ACTUAL_MINUTES_KEY] = (
        float(state.effort_actuals.get(effort.ACTUAL_MINUTES_KEY) or 0.0) + minutes)
    store.save(state)


def _order_window(key: str) -> tuple[float, float]:
    window = cli.order_approvals.get(key)["effort_since_user_approval"]
    return window["spend"], window["wall_clock"]


def test_pop_restores_parent_runtime_grants_not_the_childs(store, venued_fixtures_dir, tmp_path):
    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = _copy_plan(parent_plan, tmp_path / "child.toml",
                            {"Scaffold module": "Child alpha", "Add tests": "Child beta"})
    _to_executing_stage1(store, "P", parent_plan)
    cli.cmd_next_stage(ns(session="P"), store=store)
    parent_grants = {"1": [{"rule": "Bash(git push:*)", "provenance": "runtime",
                            "stage_title": "Scaffold module"}]}
    state = store.load("P")
    state.runtime_grants = {k: [dict(e) for e in v] for k, v in parent_grants.items()}
    store.save(state)

    def child_starts_without_grants(pushed):
        assert pushed.runtime_grants == {}, (
            f"BEHAVIOUR: the sub-plan started with the parent's runtime grants {pushed.runtime_grants}"
        )

    _push_and_approve(store, "P", child_plan, after_push=child_starts_without_grants)
    state = store.load("P")
    state.runtime_grants = {"1": [{"rule": "Bash(git status:*)", "provenance": "runtime",
                                   "stage_title": "Child alpha"}]}
    store.save(state)
    _pop(store, "P")

    assert store.load("P").runtime_grants == parent_grants, (
        f"BEHAVIOUR: pop-subplan left {store.load('P').runtime_grants} instead of the parent's "
        f"runtime grants {parent_grants}"
    )


def test_subplan_wall_clock_reaches_order_window_once(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "shared-order")
    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = _copy_plan(parent_plan, tmp_path / "child.toml")
    _to_executing_stage1(store, "P", parent_plan)
    _book(store, "P", minutes=10.0)
    _flush(store, "P")
    assert _order_window("shared-order") == (0.0, 10.0), "setup: the parent's minutes are not booked"

    _push_and_approve(store, "P", child_plan)
    _book(store, "P", minutes=4.0)
    _flush(store, "P")
    _pop(store, "P")
    _flush(store, "P")

    assert _order_window("shared-order") == (0.0, 14.0), (
        f"BEHAVIOUR: the sub-plan's 4.0 minutes reached the order window as "
        f"{_order_window('shared-order')[1] - 10.0} (window 10.0 -> {_order_window('shared-order')[1]})"
    )


def test_nested_subplan_spend_reaches_order_window_once(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "shared-order")
    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = _copy_plan(parent_plan, tmp_path / "child.toml")
    grandchild_plan = _copy_plan(parent_plan, tmp_path / "grandchild.toml")
    _to_executing_stage1(store, "P", parent_plan)
    _book(store, "P", spend=3.0, minutes=6.0)
    _flush(store, "P")
    assert _order_window("shared-order") == (3.0, 6.0), "setup: the parent's units are not booked"

    _push_and_approve(store, "P", child_plan, execute=True)
    _book(store, "P", spend=2.0, minutes=4.0)
    _flush(store, "P")
    _push_and_approve(store, "P", grandchild_plan)
    assert len(store.load("P").plan_stack) == 2, "setup: the sub-plans are not nested"
    _book(store, "P", spend=1.0, minutes=2.0)
    _flush(store, "P")
    _pop(store, "P")
    _flush(store, "P")
    _pop(store, "P")
    _flush(store, "P")

    assert _order_window("shared-order") == (6.0, 12.0), (
        f"BEHAVIOUR: the child's 2.0/4.0 and the grandchild's 1.0/2.0 (spend/minutes) reached the "
        f"order window as {_order_window('shared-order')[0] - 3.0}/{_order_window('shared-order')[1] - 6.0} "
        "instead of 3.0/6.0 in total"
    )


def test_subplan_naming_another_order_books_each_side_to_its_own_window(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    parent_plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    child_plan = _copy_plan(parent_plan, tmp_path / "child.toml")
    monkeypatch.setattr(
        cli, "_ledgered_order_key",
        lambda state: "order-child" if state.plan_path == child_plan else "order-parent")
    _to_executing_stage1(store, "P", parent_plan)
    _book(store, "P", spend=3.0, minutes=7.0)

    _push_and_approve(store, "P", child_plan)
    _book(store, "P", spend=2.0, minutes=4.0)
    _flush(store, "P")
    _pop(store, "P")
    _flush(store, "P")

    assert (_order_window("order-parent"), _order_window("order-child")) == ((3.0, 7.0), (2.0, 4.0)), (
        f"BEHAVIOUR: parent window {_order_window('order-parent')}, child window "
        f"{_order_window('order-child')}; expected (3.0, 7.0) and (2.0, 4.0) as (spend, minutes)"
    )
