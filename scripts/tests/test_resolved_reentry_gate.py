"""`cmd_reset` re-entering a RESOLVED task: typed, counted, and escapable.

RESOLVED has no outgoing edge but `pop_subplan`, so `reset` is the only way back
into a closed order — and the fresh SessionState it builds zeroes the effort
baseline, the replan count and every round-release counter. Before this gate, that
made an unbounded reopen loop free: each lap discarded the very budgets that would
have bounded it, and nobody was ever asked whether the order still stood.

The count therefore lives in the cross-session task accumulator, not on
SessionState — a counter kept on the state `reset` replaces would be zeroed by the
act it exists to count, which is the bug wearing the fix's name. These tests pin
both halves: the refusal shape, and where the number survives.
"""
import json
from argparse import Namespace

from agentctl import cli, gates, task_accumulator
from agentctl.config import Thresholds
from agentctl.state import Node
from conftest import STAGE_OBSERVATIONS


def ns(**kw):
    return Namespace(**kw)


def _reset_ns(sid, **kw):
    base = dict(session=sid, task="demo", goal="g2", done_criterion="dc2",
                criterion_type="measurable", recursion_depth=0, force=False,
                reopen_reason="", reopen_user_decision="")
    base.update(kw)
    return ns(**base)


def _to_resolved(store, fixtures_dir, sid):
    """Drive a substantive session on task 'demo' all the way to RESOLVED."""
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")),
                        store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation),
                              store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)
    cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                       quality_note=None), store=store)
    assert store.load(sid).node == Node.RESOLVED.value


def _reentry_count(task_id="demo"):
    return task_accumulator.get(task_id)["per_axis_totals"]["resolved_reentry"]


# --- the gate itself, as pure data --------------------------------------------

def test_gate_is_silent_off_the_resolved_reentry_path():
    for node in (Node.ROUTED.value, Node.BLOCKED.value, Node.EXECUTING.value, None):
        assert gates.resolved_reentry_blockers(
            node, task_id="demo", same_task=True, reopen_count=0) == []
    # RESOLVED but a DIFFERENT task: the ordinary "one task ~ one session" re-arm
    assert gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo2", same_task=False, reopen_count=0) == []


def test_gate_demands_a_reason_then_passes():
    blockers = gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo", same_task=True, reopen_count=0)
    assert len(blockers) == 1
    assert "--reopen-reason" in blockers[0]
    assert gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo", same_task=True, reopen_count=0,
        reason="the fix missed the cron path") == []
    # whitespace is not a reason
    assert gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo", same_task=True, reopen_count=0,
        reason="   ") != []


def test_gate_escalates_at_the_threshold():
    thr = Thresholds().effort_replan_absolute()
    at_ceiling = gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo", same_task=True, reopen_count=thr,
        reason="still not fixed")
    assert len(at_ceiling) == 1
    assert "--reopen-user-decision" in at_ceiling[0]
    assert "AskUserQuestion" in at_ceiling[0]
    # and the decision discharges it
    assert gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo", same_task=True, reopen_count=thr,
        reason="still not fixed", user_decision="user said keep going") == []
    # one below the threshold a reason is still enough
    assert gates.resolved_reentry_blockers(
        Node.RESOLVED.value, task_id="demo", same_task=True, reopen_count=thr - 1,
        reason="still not fixed") == []


# --- wired into cmd_reset ------------------------------------------------------

def test_reopen_without_reason_is_refused_and_changes_nothing(store, fixtures_dir):
    sid = "rr1"
    _to_resolved(store, fixtures_dir, sid)
    d = cli.cmd_reset(_reset_ns(sid), store=store)
    assert d.ok is False
    assert d.action == "noop"
    assert "--reopen-reason" in d.detail
    after = store.load(sid)
    assert after.node == Node.RESOLVED.value          # not re-armed
    assert after.task_id == "demo"
    assert _reentry_count() == 0                      # and not counted


def test_reopen_with_reason_succeeds_and_increments_the_axis(store, fixtures_dir):
    sid = "rr2"
    _to_resolved(store, fixtures_dir, sid)
    d = cli.cmd_reset(_reset_ns(sid, reopen_reason="the confirmed fix missed the cron path"),
                      store=store)
    assert d.ok is True
    assert d.action == "classify"
    fresh = store.load(sid)
    assert fresh.node == Node.CLASSIFIED.value
    assert fresh.task_id == "demo"
    assert _reentry_count() == 1
    # the reason is on the record a reader of THIS task's history will open
    reopens = [h for h in fresh.history if h.get("event") == "resolved_reentry"]
    assert len(reopens) == 1
    assert reopens[0]["reason"] == "the confirmed fix missed the cron path"


def test_the_count_survives_the_reset_that_zeroes_the_session(store, fixtures_dir):
    """The whole reason the count lives in the accumulator: reopen twice, and the
    second reset must SEE the first — though the SessionState it read is gone."""
    sid = "rr3"
    _to_resolved(store, fixtures_dir, sid)
    cli.cmd_reset(_reset_ns(sid, reopen_reason="first miss"), store=store)
    assert _reentry_count() == 1
    _to_resolved(store, fixtures_dir, sid)
    d = cli.cmd_reset(_reset_ns(sid, reopen_reason="second miss"), store=store)
    assert d.ok is True
    assert _reentry_count() == 2
    assert d.data["resolved_reentry_count"] == 2


def test_reopen_at_the_ceiling_demands_a_user_decision(store, fixtures_dir):
    sid = "rr4"
    task_accumulator.add("demo", "resolved_reentry", Thresholds().effort_replan_absolute(),
                         session_id=sid, now=None)
    _to_resolved(store, fixtures_dir, sid)
    d = cli.cmd_reset(_reset_ns(sid, reopen_reason="still not fixed"), store=store)
    assert d.ok is False
    assert "--reopen-user-decision" in d.detail
    assert store.load(sid).node == Node.RESOLVED.value

    d2 = cli.cmd_reset(
        _reset_ns(sid, reopen_reason="still not fixed",
                  reopen_user_decision="user chose to continue the order"),
        store=store)
    assert d2.ok is True
    assert store.load(sid).node == Node.CLASSIFIED.value


def test_reset_onto_a_different_task_after_resolution_is_untouched(store, fixtures_dir):
    """Regression guard for the ordinary path: a resolved session re-armed for a NEW
    task must not be asked for a reopen reason."""
    sid = "rr5"
    _to_resolved(store, fixtures_dir, sid)
    d = cli.cmd_reset(_reset_ns(sid, task="demo2"), store=store)
    assert d.ok is True
    assert store.load(sid).task_id == "demo2"
    assert _reentry_count() == 0


def test_routed_prior_node_is_unaffected(store):
    sid = "rr6"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=True, changed_lines=0, files=1,
                        wall_clock_min=0, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    assert store.load(sid).node == Node.ROUTED.value
    d = cli.cmd_reset(_reset_ns(sid), store=store)   # same task, no reason
    assert d.ok is True
    assert _reentry_count() == 0


def test_blocked_prior_node_is_unaffected(store, fixtures_dir):
    sid = "rr7"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_block(ns(session=sid, reason="waiting on an external answer"), store=store)
    assert store.load(sid).node == Node.BLOCKED.value
    d = cli.cmd_reset(_reset_ns(sid), store=store)   # same task, no reason
    assert d.ok is True
    assert _reentry_count() == 0


def test_a_schema_v1_accumulator_file_still_loads(tmp_path, monkeypatch):
    """A file written before `resolved_reentry` existed must be READ, not discarded:
    treating v1 as foreign would zero every accumulator on disk the moment this
    version shipped, handing every stuck task a fresh Rule-of-Three budget."""
    monkeypatch.setenv("AGENTCTL_TASK_ACCUMULATOR_DIR", str(tmp_path))
    path = tmp_path / f"{task_accumulator._hash_task_id('legacy')}.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "task_id": "legacy",
        "per_axis_totals": {
            "replan_count": 2, "plan_review_rounds": 1,
            "plan_enumerate_rounds": 0, "code_review_rounds": 3,
        },
        "session_ids_contributing": ["old"],
        "last_updated": "2026-01-01T00:00:00+00:00",
    }), encoding="utf-8")

    totals = task_accumulator.get("legacy")["per_axis_totals"]
    assert totals["replan_count"] == 2            # NOT zeroed
    assert totals["code_review_rounds"] == 3
    assert totals["resolved_reentry"] == 0        # zero-filled by name

    # and an add() against it upgrades the file in place without losing the rest
    task_accumulator.add("legacy", "resolved_reentry", 1, session_id="new", now=None)
    reread = task_accumulator.get("legacy")
    assert reread["schema_version"] == task_accumulator.SCHEMA_VERSION
    assert reread["per_axis_totals"]["replan_count"] == 2
    assert reread["per_axis_totals"]["resolved_reentry"] == 1
