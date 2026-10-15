"""A parent stage's spend includes every linked descendant, counted exactly once.

*Difficulty removed:* a stage that dispatches a linked child (norm handoff + own contour)
sees only the cost rows keyed by its own plan_path, so the child's reviews and every
grandchild's spawns never reach the parent's `effort.divergence()` spend scale — a
delegated overrun is invisible exactly where the divergence trigger looks for it.

Every spawn-specialist cost row now carries `spawner_session`; the parent books each
linked descendant's rows against a per-descendant watermark (`effort_subtree_seen`) and
every other row by plan_path as before. The scenarios drive the real commands
(record-result, push/pop-subplan, spawn-specialist's cost-row writer). Behavioural
assertions are exact equalities whose message starts `BEHAVIOUR:`; the two mutant tests
patch the partition seams and require those same scenarios to go red.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, effort
from agentctl.config import Thresholds
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)
from conftest import STAGE_OBSERVATIONS

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SPAWN_SCRIPT = SCRIPTS_DIR / "spawn-specialist.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SPAWN_MOD = _load("spawn_specialist_parent_effort_rollup", SPAWN_SCRIPT)


@pytest.fixture
def fixtures_dir(venued_fixtures_dir):
    return venued_fixtures_dir


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


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


def _row(spawner, usd, plan, **extra):
    row = {"plan_path": plan, "stage_index": 1, "cost_usd": usd}
    if spawner is not None:
        row["spawner_session"] = spawner
    return {**row, **extra}


def _write_cost_log(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _link(store, session, parent, *, running=False):
    extra = {}
    if running:
        extra = {"node": Node.EXECUTING.value,
                 "approval": GateRecord("plan_approval", armed=True, passed=True, by="tester")}
    store.save(SessionState(
        session_id=session, task_id=f"{session}-task",
        parent_link={"parent_session": parent, "stage_index": 1, "depth": 1}, **extra,
    ))


def _spend(state):
    return state.effort_actuals.get(effort.ACTUAL_SPEND_KEY, 0.0)


def _record_stage1(store, sid, cost_log):
    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation=STAGE_OBSERVATIONS[0], cost_log=cost_log),
        store=store,
    )
    assert d.ok is True, d.detail
    return store.load(sid)


def _parent_spend(store, fixtures_dir, tmp_path, rows_for, linked=(), sid="P"):
    """Run parent `sid` through stage 1 against a cost log; return its booked spend."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    for child, parent in linked:
        _link(store, child, parent)
    _to_executing_stage1(store, sid, plan)
    log = _write_cost_log(tmp_path / "costs.jsonl", rows_for(plan))
    return _spend(_record_stage1(store, sid, log))


def _scenario_descendant_row_on_parent_plan(store, fixtures_dir, tmp_path):
    spend = _parent_spend(
        store, fixtures_dir, tmp_path,
        lambda plan: [_row("P", 1.0, plan), _row("C", 3.0, plan)],
        linked=[("C", "P")],
    )
    assert spend == 4.0, (
        f"BEHAVIOUR: a linked child's row naming the parent's own plan is booked once "
        f"(1.0 own + 3.0 child = 4.0), got {spend}"
    )


def _scenario_only_linked_descendants(store, fixtures_dir, tmp_path):
    store.save(SessionState(
        session_id="U", task_id="u-task",
        parent_link={"parent_session": "X", "stage_index": 1, "depth": 1},
    ))
    spend = _parent_spend(
        store, fixtures_dir, tmp_path,
        lambda plan: [
            _row("P", 1.5, plan),
            _row("U", 5.0, "/elsewhere/plan.toml"),
            _row("ghost", 7.0, "/elsewhere/other.toml"),
        ],
    )
    assert spend == 1.5, (
        f"BEHAVIOUR: only sessions linked below P roll up; an unlinked session's and a "
        f"session-less spawner's rows stay out (own 1.5), got {spend}"
    )


def test_grandchild_spend_rolls_up(store, fixtures_dir, tmp_path):
    spend = _parent_spend(
        store, fixtures_dir, tmp_path,
        lambda plan: [
            _row("P", 1.0, plan),
            _row("C", 2.0, "/c/plan.toml"),
            _row("G", 4.0, "/g/plan.toml"),
        ],
        linked=[("C", "P"), ("G", "C")],
    )
    assert spend == 7.0, (
        f"BEHAVIOUR: P's spend includes its child and its grandchild "
        f"(1.0 + 2.0 + 4.0 = 7.0), got {spend}"
    )


def test_only_linked_descendants_roll_up(store, fixtures_dir, tmp_path):
    _scenario_only_linked_descendants(store, fixtures_dir, tmp_path)


def test_descendant_row_on_parent_plan_counted_once(store, fixtures_dir, tmp_path):
    _scenario_descendant_row_on_parent_plan(store, fixtures_dir, tmp_path)


def test_subplan_custody_counts_child_once(store, fixtures_dir, tmp_path):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _link(store, "C", "P")
    _to_executing_stage1(store, "P", plan)
    log = _write_cost_log(tmp_path / "costs.jsonl", [_row("C", 3.0, "/c/plan.toml")])
    _record_stage1(store, "P", log)
    cli.cmd_next_stage(ns(session="P"), store=store)

    pushed = cli.cmd_push_subplan(
        ns(session="P", plan=plan, task="svc", originating_stage=2), store=store)
    assert pushed.ok is True, pushed.detail
    cli.cmd_classify(ns(session="P", chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session="P"), store=store)
    cli.cmd_submit_plan(ns(session="P", plan=plan, cost_log=log), store=store)

    inner = store.load("P")
    inner.stages = [Stage(
        index=1, title="child stage", subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"), actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="done"),
        outcome=Outcome(status=StageStatus.PASSED.value),
    )]
    inner.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    inner.node = Node.RESOLVED.value
    inner.partition = Partition(verdict="single")
    inner.current_stage = None
    store.save(inner)
    popped = cli.cmd_pop_subplan(ns(session="P"), store=store)
    assert popped.ok is True, popped.detail

    spend = _spend(store.load("P"))
    assert spend == 3.0, (
        f"BEHAVIOUR: a linked child's 3.0 booked before a sub-plan push and re-read "
        f"inside it is counted once after pop, got {spend}"
    )


def test_running_child_finished_descendants_count(store, fixtures_dir, tmp_path):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _link(store, "C", "P", running=True)
    _link(store, "G", "C")
    _to_executing_stage1(store, "P", plan)
    log = _write_cost_log(tmp_path / "costs.jsonl", [
        _row("C", 2.0, "/c/plan.toml"),
        _row("G", 1.5, "/g/plan.toml"),
    ])
    running = _spend(_record_stage1(store, "P", log))
    assert running == 3.5, (
        f"BEHAVIOUR: while C is still running, its own finished spawns and its finished "
        f"grandchild already count (2.0 + 1.5 = 3.5), got {running}"
    )

    state = store.load("P")
    rows = [_row("C", 2.0, "/c/plan.toml"), _row("G", 1.5, "/g/plan.toml"),
            _row("P", 6.0, plan)]
    cli._refresh_spend(state, store, rows, plan)
    assert _spend(state) == 9.5, "BEHAVIOUR: C's own finished row adds once on top (3.5 + 6.0)"


def _shared_order_parent_and_child(store, fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "shared-order")
    plan_p = str(fixtures_dir / "plan_two_stage.toml")
    plan_c = str(tmp_path / "child_plan.toml")
    shutil.copy(plan_p, plan_c)
    _to_executing_stage1(store, "C", plan_c)
    child = store.load("C")
    child.parent_link = {"parent_session": "P", "stage_index": 1, "depth": 1}
    store.save(child)
    _to_executing_stage1(store, "P", plan_p)
    return plan_c


def _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch):
    """P -> C -> G, all on one ledgered order; returns the plan paths of C and G."""
    plan_c = _shared_order_parent_and_child(store, fixtures_dir, tmp_path, monkeypatch)
    plan_g = str(tmp_path / "grandchild_plan.toml")
    shutil.copy(plan_c, plan_g)
    _to_executing_stage1(store, "G", plan_g)
    grandchild = store.load("G")
    grandchild.parent_link = {"parent_session": "C", "stage_index": 1, "depth": 2}
    store.save(grandchild)
    return plan_c, plan_g


def _order_window_spend():
    return cli.order_approvals.get("shared-order")["effort_since_user_approval"]["spend"]


def test_child_flush_does_not_double_parent_order_window(store, fixtures_dir, tmp_path, monkeypatch):
    plan_c = _shared_order_parent_and_child(store, fixtures_dir, tmp_path, monkeypatch)
    log = _write_cost_log(tmp_path / "costs.jsonl", [_row("C", 2.0, plan_c)])
    _record_stage1(store, "C", log)
    window = cli.order_approvals.get("shared-order")["effort_since_user_approval"]
    assert window["spend"] == 2.0

    parent = _record_stage1(store, "P", log)
    assert _spend(parent) == 2.0, (
        f"BEHAVIOUR: P's spend counts C's 2.0, got {_spend(parent)}"
    )
    window = cli.order_approvals.get("shared-order")["effort_since_user_approval"]
    assert window["spend"] == 2.0, (
        f"BEHAVIOUR: C already flushed its 2.0 into the shared order window, so P "
        f"booking it must not add it again, got {window['spend']}"
    )
    totals = cli._effort_cross_totals(parent)
    assert totals[effort.ORDER_SPEND_KEY] == 2.0, (
        f"BEHAVIOUR: the order total P is judged on stays 2.0, got {totals[effort.ORDER_SPEND_KEY]}"
    )


def test_parent_books_first_child_never_flushes_window_not_short(
        store, fixtures_dir, tmp_path, monkeypatch):
    plan_c = _shared_order_parent_and_child(store, fixtures_dir, tmp_path, monkeypatch)
    log = _write_cost_log(tmp_path / "costs.jsonl", [_row("C", 2.0, plan_c)])
    _record_stage1(store, "P", log)
    assert _order_window_spend() == 2.0, (
        f"BEHAVIOUR: C never flushed its own 2.0, so P's flush carries it into the shared "
        f"order window, got {_order_window_spend()}"
    )


def test_child_rows_its_own_refresh_never_books_still_reach_window(
        store, fixtures_dir, tmp_path, monkeypatch):
    plan_c = _shared_order_parent_and_child(store, fixtures_dir, tmp_path, monkeypatch)
    _record_stage1(store, "C", _write_cost_log(tmp_path / "c.jsonl", [_row("C", 1.0, plan_c)]))
    assert _order_window_spend() == 1.0
    rows = [_row("C", 1.0, plan_c), _row("C", 2.0, "/elsewhere/plan.toml")]
    parent = _record_stage1(store, "P", _write_cost_log(tmp_path / "p.jsonl", rows))
    assert _spend(parent) == 3.0, f"BEHAVIOUR: P books both of C's rows (3.0), got {_spend(parent)}"
    assert _order_window_spend() == 3.0, (
        f"BEHAVIOUR: C flushed only the 1.0 its own refresh books; the 2.0 on a plan path "
        f"C never books reaches the window through P, got {_order_window_spend()}"
    )


def test_grandchild_flushed_and_child_unbooked_row_reach_window_once(
        store, fixtures_dir, tmp_path, monkeypatch):
    plan_c, plan_g = _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch)
    g_rows = [_row("G", 4.0, plan_g)]
    _record_stage1(store, "G", _write_cost_log(tmp_path / "g.jsonl", g_rows))
    c_rows = g_rows + [_row("C", 1.0, plan_c), _row("C", 2.0, "/elsewhere/plan.toml")]
    _record_stage1(store, "C", _write_cost_log(tmp_path / "c.jsonl", c_rows))
    assert _order_window_spend() == 5.0
    parent = _record_stage1(store, "P", _write_cost_log(tmp_path / "p.jsonl", c_rows))
    assert _spend(parent) == 7.0, f"BEHAVIOUR: P books C's 3.0 and G's 4.0, got {_spend(parent)}"
    assert _order_window_spend() == 7.0, (
        f"BEHAVIOUR: G's 4.0 and C's booked 1.0 are in the window (5.0); only C's 2.0 on a "
        f"path C never books is missing, got {_order_window_spend()}"
    )


def test_grandchild_never_flushed_carried_by_child_reaches_window_once(
        store, fixtures_dir, tmp_path, monkeypatch):
    plan_c, plan_g = _shared_order_with_grandchild(store, fixtures_dir, tmp_path, monkeypatch)
    rows = [_row("C", 1.0, plan_c), _row("G", 4.0, plan_g)]
    _record_stage1(store, "C", _write_cost_log(tmp_path / "c.jsonl", rows))
    assert _order_window_spend() == 5.0
    parent = _record_stage1(store, "P", _write_cost_log(tmp_path / "p.jsonl", rows))
    assert _spend(parent) == 5.0, f"BEHAVIOUR: P books C's 1.0 and G's 4.0, got {_spend(parent)}"
    assert _order_window_spend() == 5.0, (
        f"BEHAVIOUR: G never flushed but C carried G's 4.0 into the window, so P's flush "
        f"must not add it again, got {_order_window_spend()}"
    )


def test_credit_counts_what_parent_already_credited_not_booking_order(
        store, fixtures_dir, tmp_path, monkeypatch):
    plan_c = _shared_order_parent_and_child(store, fixtures_dir, tmp_path, monkeypatch)
    plan_p = str(fixtures_dir / "plan_two_stage.toml")
    off_path = _row("C", 1.0, "/elsewhere/plan.toml")
    _record_stage1(store, "P", _write_cost_log(tmp_path / "p.jsonl", [off_path]))
    assert _order_window_spend() == 1.0
    rows = [off_path, _row("C", 2.0, plan_c)]
    _record_stage1(store, "C", _write_cost_log(tmp_path / "c.jsonl", rows))
    assert _order_window_spend() == 3.0
    parent = store.load("P")
    cli._refresh_spend(parent, store, rows, plan_p)
    cli._flush_order_effort(parent)
    assert _spend(parent) == 3.0, f"BEHAVIOUR: P books both of C's rows (3.0), got {_spend(parent)}"
    assert _order_window_spend() == 3.0, (
        f"BEHAVIOUR: C's flushed 2.0 and P's earlier flush of the 1.0 C never flushed make "
        f"3.0; the 2.0 P books later is C's own flush, got {_order_window_spend()}"
    )


def test_never_flushed_linked_session_watch_total_counts_its_spend(monkeypatch):
    monkeypatch.setattr(cli, "_ledgered_order_key", lambda state: "shared-order")
    linked = SessionState(session_id="C", task_id="C-task",
                          parent_link={"parent_session": "P", "stage_index": 1, "depth": 1})
    plain = SessionState(session_id="U", task_id="U-task")
    for state in (linked, plain):
        state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 2.0
    assert cli._effort_cross_totals(linked)[effort.ORDER_SPEND_KEY] == 2.0, (
        "BEHAVIOUR: a linked session that never flushed has its whole spend in the order "
        "total its watch reads, as its first flush will move it"
    )
    assert cli._effort_cross_totals(plain)[effort.ORDER_SPEND_KEY] == 0.0


def _linked_session_resubmitting(store, plan, sid, parent, depth, tmp_path, rows):
    """Linked session `sid`: its first submit-plan has no plan path (no ledgered key, so
    no flush), the cost rows arrive, and the resubmit at PLAN_READY books them and makes
    the session's first ledgered flush."""
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=depth), store=store)
    state = store.load(sid)
    state.parent_link = {"parent_session": parent, "stage_index": 1, "depth": depth}
    store.save(state)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    quiet = _write_cost_log(tmp_path / f"{sid}-quiet.jsonl", [])
    cli.cmd_submit_plan(ns(session=sid, plan=plan, cost_log=quiet), store=store)
    log = _write_cost_log(tmp_path / f"{sid}-rows.jsonl", rows)
    resubmitted = cli.cmd_submit_plan(ns(session=sid, plan=plan, cost_log=log), store=store)
    assert resubmitted.ok is True, resubmitted.detail
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _ledgered_once_planned(monkeypatch):
    monkeypatch.setattr(cli, "_ledgered_order_key",
                        lambda state: "shared-order" if state.plan_path else None)


def test_child_review_spend_before_first_flush_reaches_window(
        store, fixtures_dir, tmp_path, monkeypatch):
    _ledgered_once_planned(monkeypatch)
    plan_p = str(fixtures_dir / "plan_two_stage.toml")
    plan_c = str(tmp_path / "child_plan.toml")
    shutil.copy(plan_p, plan_c)
    rows = [_row("C", 2.0, plan_c)]
    _linked_session_resubmitting(store, plan_c, "C", "P", 1, tmp_path, rows)
    assert _order_window_spend() == 2.0, (
        f"BEHAVIOUR: a linked child's first ledgered flush moves the 2.0 it spent before it "
        f"(a review round at PLAN_READY), got {_order_window_spend()}"
    )
    _to_executing_stage1(store, "P", plan_p)
    parent = _record_stage1(store, "P", _write_cost_log(tmp_path / "p.jsonl", rows))
    assert _spend(parent) == 2.0, f"BEHAVIOUR: P books C's 2.0, got {_spend(parent)}"
    assert _order_window_spend() == 2.0, (
        f"BEHAVIOUR: C's 2.0 reached the window through C's first flush; P booking it must "
        f"neither lose nor repeat it, got {_order_window_spend()}"
    )


def test_grandchild_review_spend_before_first_flush_reaches_window(
        store, fixtures_dir, tmp_path, monkeypatch):
    _ledgered_once_planned(monkeypatch)
    plan_p = str(fixtures_dir / "plan_two_stage.toml")
    plan_c = str(tmp_path / "child_plan.toml")
    plan_g = str(tmp_path / "grandchild_plan.toml")
    shutil.copy(plan_p, plan_c)
    shutil.copy(plan_p, plan_g)
    rows = [_row("G", 4.0, plan_g)]
    _linked_session_resubmitting(store, plan_c, "C", "P", 1, tmp_path, [])
    _linked_session_resubmitting(store, plan_g, "G", "C", 2, tmp_path, rows)
    assert _order_window_spend() == 4.0, (
        f"BEHAVIOUR: G's first ledgered flush moves its 4.0, got {_order_window_spend()}"
    )
    child = _record_stage1(store, "C", _write_cost_log(tmp_path / "c.jsonl", rows))
    assert _spend(child) == 4.0, f"BEHAVIOUR: C books G's 4.0, got {_spend(child)}"
    assert _order_window_spend() == 4.0, (
        f"BEHAVIOUR: C booking G's already-flushed 4.0 must not add it again, "
        f"got {_order_window_spend()}"
    )
    _to_executing_stage1(store, "P", plan_p)
    parent = _record_stage1(store, "P", _write_cost_log(tmp_path / "p.jsonl", rows))
    assert _spend(parent) == 4.0, f"BEHAVIOUR: P books G's 4.0, got {_spend(parent)}"
    assert _order_window_spend() == 4.0, (
        f"BEHAVIOUR: the 4.0 stays in the window exactly once, got {_order_window_spend()}"
    )


def test_session_without_children_keeps_base_spend(store, fixtures_dir, tmp_path):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    rows = [_row("P", 1.0, plan), _row("P", 2.5, plan), _row(None, 0.5, plan),
            _row("P", 9.0, "/other/plan.toml")]
    booked = _parent_spend(store, fixtures_dir, tmp_path, lambda p: rows)
    reference = SessionState(session_id="ref", task_id="ref-task", plan_path=plan)
    effort.refresh_spend(reference, rows, plan)
    assert booked == 4.0 == _spend(reference), (
        f"BEHAVIOUR: a session with no linked descendants books the plan_path total "
        f"(4.0), the same as effort.refresh_spend, got {booked}"
    )


def test_spend_divergence_fires_on_subtree_overrun(store, fixtures_dir, tmp_path):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _link(store, "C", "P")
    _to_executing_stage1(store, "P", plan)
    armed_estimate = store.load("P").effort_estimate[effort.SCALE_SPEND]
    overrun = 2 * armed_estimate * Thresholds().effort_divergence_multiple()
    log = _write_cost_log(tmp_path / "costs.jsonl", [_row("C", overrun, "/c/plan.toml")])
    d = cli.cmd_record_result(
        ns(session="P", status="passed", actual="ok", control="reviewed: ok",
           observation=STAGE_OBSERVATIONS[0], cost_log=log),
        store=store,
    )
    assert d.node == Node.DIAGNOSING.value, (
        f"BEHAVIOUR: {overrun} spent by a linked child against a {armed_estimate} spend "
        f"estimate fires the spend scale on the parent, node is {d.node}"
    )
    assert d.data["effort_divergence"]["scale"] == effort.SCALE_SPEND


def test_spawn_specialist_writes_spawner_session(monkeypatch, tmp_path):
    cost_log = tmp_path / "spawn-costs.jsonl"
    monkeypatch.setattr(SPAWN_MOD, "COST_LOG", cost_log)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    class _Proc:
        returncode = 0
        pid = 999999

        def communicate(self, input=None):
            return json.dumps({"session_id": "child-id", "result": "COMPLETED: ok", "cost_usd": 0.0}), ""

    monkeypatch.setattr(SPAWN_MOD.proc_tree, "launch_supervised", lambda *a, **k: _Proc())
    monkeypatch.setattr(SPAWN_MOD.proc_tree, "install_teardown", lambda *a, **k: None)
    monkeypatch.setattr(SPAWN_MOD.proc_tree, "kill_tree", lambda *a, **k: None)
    monkeypatch.setattr(SPAWN_MOD, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(SPAWN_MOD, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/bin/claude")
    prompt = tmp_path / "system-prompt.txt"
    prompt.write_text("system prompt\n")
    monkeypatch.setattr(SPAWN_MOD, "composed_system_prompt_file", lambda *a, **k: prompt)
    monkeypatch.setattr(SPAWN_MOD, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(SPAWN_MOD, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(SPAWN_MOD, "projects_roots", lambda: [tmp_path / "projects"])
    argv = [
        "--kind", "developer",
        "--plan", str(SCRIPTS_DIR / "tests" / "fixtures" / "plan_two_stage.toml"),
        "--done-criterion", "tests green", "--criterion-type", "measurable",
        "--complexity", "medium", "--effort", "medium", "--workdir", str(workdir),
    ]

    def spawn_row(extra_argv=(), **env):
        monkeypatch.delenv(SPAWN_MOD.OWN_SESSION_ENV, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        cost_log.unlink(missing_ok=True)
        assert SPAWN_MOD.main([*argv, *extra_argv]) == 0
        return json.loads(cost_log.read_text().splitlines()[0])

    own = spawn_row(AGENTCTL_OWN_SESSION="own-contour-1")
    assert own.get("spawner_session") == "own-contour-1", (
        f"BEHAVIOUR: a spawn made from a linked child's own contour records that "
        f"session, row has {own.get('spawner_session')!r}"
    )
    explicit = spawn_row(["--session", "explicit-session"], AGENTCTL_OWN_SESSION="own-contour-1")
    assert explicit.get("spawner_session") == "explicit-session", (
        f"BEHAVIOUR: --session wins over the spawner's own contour, row has "
        f"{explicit.get('spawner_session')!r}"
    )
    bare = spawn_row()
    assert "spawner_session" not in bare
    base_fields = {
        "ts", "event", "kind", "budget_tier", "budget_usd_cap", "effort", "depth", "cost_usd",
        "duration_ms", "return_marker", "exit_code", "malformed", "stage_index", "plan_path",
        "extractor_invoked", "extractor_model", "extractor_degraded", "extraction_reason",
        "outcome_class", "child_session_id", "transcript_path", "review_pair", "plan_sha256",
        "session_id", "ticket",
    }
    assert set(own) - {"spawner_session"} == base_fields == set(bare)


def test_base_state_and_legacy_rows_keep_base_spend(store, fixtures_dir, tmp_path):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, "L", plan)
    legacy = json.loads(store.load("L").to_json())
    legacy.pop("effort_subtree_seen", None)
    store.path("L").write_text(json.dumps(legacy), encoding="utf-8")
    assert getattr(store.load("L"), "effort_subtree_seen", {}) == {}

    log = _write_cost_log(tmp_path / "costs.jsonl", [
        {"plan_path": plan, "stage_index": 1, "cost_usd": 6.5},
        {"plan_path": "/other/plan.toml", "stage_index": 1, "cost_usd": 40.0},
    ])
    spend = _spend(_record_stage1(store, "L", log))
    assert spend == 6.5, (
        f"BEHAVIOUR: a legacy state and rows without spawner_session keep the plan_path "
        f"total (6.5), got {spend}"
    )


def test_parent_link_cycle_terminates(store):
    for session, parent in (("A", "B"), ("B", "A"), ("P", "C"), ("C", "P")):
        _link(store, session, parent)
    assert list(cli._descendant_sessions(store, "Z", ["A", "B"])) == []
    assert list(cli._descendant_sessions(store, "P", ["P", "C", "A"])) == ["C"]

    bound = Thresholds().max_recursion_depth
    names = [f"D{n}" for n in range(1, bound + 3)]
    previous = "ROOT"
    for name in names:
        _link(store, name, previous)
        previous = name
    reached = cli._descendant_sessions(store, "ROOT", names)
    assert set(reached) == set(names[:bound])


def test_descendant_scan_returns_each_candidates_link_chain(store):
    _link(store, "C", "P")
    _link(store, "G", "C")
    chains = cli._descendant_sessions(store, "P", ["C", "G", "X"])
    assert {s: [m.session_id for m in chain] for s, chain in chains.items()} == {
        "C": ["C"], "G": ["G", "C"],
    }


def test_mutant_double_count_is_caught(store, fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(effort, "own_rows", lambda rows, descendants: list(rows))
    with pytest.raises(AssertionError, match="BEHAVIOUR:"):
        _scenario_descendant_row_on_parent_plan(store, fixtures_dir, tmp_path)


def test_mutant_unlinked_rollup_is_caught(store, fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "_descendant_sessions",
        lambda store, parent, candidates: {
            s: [SessionState(session_id=s, task_id=f"{s}-task")] for s in sorted(candidates)
        })
    with pytest.raises(AssertionError, match="BEHAVIOUR:"):
        _scenario_only_linked_descendants(store, fixtures_dir, tmp_path)
