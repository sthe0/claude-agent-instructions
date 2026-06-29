"""End-to-end cost attribution test: synthetic cost-log row -> record-result -> resolve surfacing.

Drives a full session (start -> ... -> resolve) with a fixture cost log and asserts
that the surfaced plan total equals the sum of the per-stage attributions.
"""
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


def _full_session_to_resolved(store, sid, plan_path, cost_log_path, tmp_path):
    """Drive a two-stage spawn session to RESOLVED with the given cost log.

    Returns the resolve Directive.
    """
    # Start + classify (substantive)
    cli.cmd_start(ns(session=sid, task="e2e-cost", goal="g",
                     done_criterion="dc", criterion_type="measurable",
                     recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan_path)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                          m3_severe=False, m4_severe=False), store=store)

    # Stage 1
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(cost_log_path)),
        store=store,
    )

    # Stage 2
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(cost_log_path)),
        store=store,
    )

    # Final verification (arms resolution gate + sets state.cost)
    d_vf = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d_vf.ok is True, f"verify-final failed: {d_vf.detail}"

    # Satisfy experience plugin gate
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)

    # Resolve
    return cli.cmd_resolve(ns(session=sid, by="user"), store=store)


def test_resolve_surfaces_plan_total_equal_to_stage_sum(store, fixtures_dir, tmp_path):
    """Surfaced plan total == sum of per-stage attributions."""
    plan = fixtures_dir / "plan_two_stage.toml"

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        json.dumps({"plan_path": str(plan), "stage_index": 1,
                    "cost_usd": 0.40, "duration_ms": 1000}) + "\n" +
        json.dumps({"plan_path": str(plan), "stage_index": 2,
                    "cost_usd": 0.30, "duration_ms": 800}) + "\n",
        encoding="utf-8",
    )

    d = _full_session_to_resolved(store, "e2e-s1", plan, cost_log, tmp_path)

    assert d.ok is True
    assert d.node == Node.RESOLVED.value
    assert d.marker == "COMPLETED"
    assert "cost" in d.data

    cost = d.data["cost"]
    assert cost["total_cost_usd"] == pytest.approx(0.70)
    assert cost["total_duration_ms"] == 1800
    assert cost["spawn_count"] == 2
    assert cost["attributed_stages"] == 2
    assert cost["note"] != ""


def test_state_cost_consistent_with_directive(store, fixtures_dir, tmp_path):
    """state.cost after resolve equals what was surfaced in the Directive."""
    plan = fixtures_dir / "plan_two_stage.toml"

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        json.dumps({"plan_path": str(plan), "stage_index": 1,
                    "cost_usd": 1.20, "duration_ms": 3000}) + "\n" +
        json.dumps({"plan_path": str(plan), "stage_index": 2,
                    "cost_usd": 0.80, "duration_ms": 2000}) + "\n",
        encoding="utf-8",
    )

    sid = "e2e-s2"
    d = _full_session_to_resolved(store, sid, plan, cost_log, tmp_path)

    state = store.load(sid)
    assert state.cost is not None
    assert state.cost.total_cost_usd == pytest.approx(d.data["cost"]["total_cost_usd"])
    assert state.cost.total_duration_ms == d.data["cost"]["total_duration_ms"]
    assert state.cost.spawn_count == d.data["cost"]["spawn_count"]


def test_cost_history_event_logged_at_resolve(store, fixtures_dir, tmp_path):
    """A 'cost' event is logged to history when state.cost is non-None at resolve."""
    plan = fixtures_dir / "plan_two_stage.toml"

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        json.dumps({"plan_path": str(plan), "stage_index": 1,
                    "cost_usd": 0.50, "duration_ms": 1200}) + "\n",
        encoding="utf-8",
    )

    sid = "e2e-s3"
    _full_session_to_resolved(store, sid, plan, cost_log, tmp_path)

    state = store.load(sid)
    cost_events = [e for e in state.history if e.get("event") == "cost"]
    assert len(cost_events) == 1
    ev = cost_events[0]
    assert ev["total_cost_usd"] == pytest.approx(0.50)
    assert ev["attributed_stages"] == 1


def test_e2e_no_cost_log_resolves_cleanly(store, fixtures_dir, tmp_path):
    """A session with no cost log still resolves; plan total is None/zero."""
    plan = fixtures_dir / "plan_two_stage.toml"
    empty_log = tmp_path / "empty.jsonl"
    empty_log.write_text("", encoding="utf-8")

    d = _full_session_to_resolved(store, "e2e-s4", plan, empty_log, tmp_path)

    assert d.ok is True
    cost = d.data["cost"]
    assert cost["total_cost_usd"] is None
    assert cost["spawn_count"] == 0
    assert cost["attributed_stages"] == 0
