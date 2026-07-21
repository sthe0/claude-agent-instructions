"""Task-type + realized-tier enrichment of the resolve quality row (P2).

Covers: _write_quality_row / cmd_resolve additionally carrying weight_class,
deliverable_kind, route (already computed at classify, previously dropped),
and budget_tiers (the distinct budget_tier labels of this task's spawn-costs
rows, joined by plan_path). Additive fields only — the pre-existing keys
(total_cost_usd, spawn_count, n_stages, ...) are asserted unchanged.
"""
from __future__ import annotations

import json
from argparse import Namespace

from agentctl import cli


def ns(**kw):
    return Namespace(**kw)


def _read_quality_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _drive_to_resolved(store, sid, plan, *, deliverable_kind="", cost_log=None):
    """Start -> classify -> ... -> resolve, returning the resolve Directive."""
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=None, architectural=True, external_effect=False,
        new_dependency=False, public_api_change=False,
        deliverable_kind=deliverable_kind,
    ), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation="",
                                 cost_log=str(cost_log) if cost_log else None), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched",
                             note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    return cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                              quality_note=None, cost_log=str(cost_log) if cost_log else None),
                            store=store)


def test_quality_row_carries_weight_class_route_deliverable_kind(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = _drive_to_resolved(store, "tt-1", plan, deliverable_kind="code")

    assert d.ok is True
    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    row = rows[-1]
    assert row["weight_class"] == "SUBSTANTIVE"
    assert row["route"] == "SPAWN"
    assert row["deliverable_kind"] == "code"


def test_quality_row_deliverable_kind_null_when_unclassified(store, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")

    _drive_to_resolved(store, "tt-2", plan, deliverable_kind="")

    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    assert rows[-1]["deliverable_kind"] is None


def test_quality_row_budget_tiers_from_matching_spawn_rows(store, fixtures_dir, tmp_path):
    plan = fixtures_dir / "plan_two_stage.toml"
    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        json.dumps({"plan_path": str(plan), "stage_index": 1,
                    "budget_tier": "large", "cost_usd": 0.40}) + "\n" +
        json.dumps({"plan_path": str(plan), "stage_index": 2,
                    "budget_tier": "medium", "cost_usd": 0.10}) + "\n" +
        # a row for a DIFFERENT plan must not leak into this task's tiers
        json.dumps({"plan_path": "/some/other/plan.toml", "stage_index": 1,
                    "budget_tier": "small", "cost_usd": 0.05}) + "\n",
        encoding="utf-8",
    )

    d = _drive_to_resolved(store, "tt-3", str(plan), cost_log=cost_log)

    assert d.ok is True
    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    row = rows[-1]
    assert row["budget_tiers"] == ["large", "medium"]
    # pre-existing fields stay populated (additive change, not a rewrite)
    assert row["spawn_count"] == 2
    assert row["total_cost_usd"] == 0.5


def test_quality_row_budget_tiers_empty_when_no_spawn_rows(store, fixtures_dir, tmp_path):
    plan = fixtures_dir / "plan_two_stage.toml"
    empty_log = tmp_path / "empty.jsonl"
    empty_log.write_text("", encoding="utf-8")

    d = _drive_to_resolved(store, "tt-4", str(plan), cost_log=empty_log)

    assert d.ok is True
    rows = _read_quality_rows(cli.TASK_QUALITY_LOG)
    row = rows[-1]
    assert row["budget_tiers"] == []
    assert row["spawn_count"] == 0
