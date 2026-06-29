"""agentctl.cost: tolerant reader, per-stage attribution, plan-level rollup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl import cost
from agentctl.state import Actor, CostRollup, Criterion, Means, Outcome, Stage, Subject


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_log(tmp_path, rows):
    path = tmp_path / "costs.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _make_spawn_stage(index: int, cost_usd=None, duration_ms=None, spawn_count=0):
    s = Stage(
        index=index,
        title=f"stage {index}",
        subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="tests green"),
    )
    s.outcome.cost_usd = cost_usd
    s.outcome.duration_ms = duration_ms
    s.outcome.spawn_count = spawn_count
    return s


# ---------------------------------------------------------------------------
# read_rows
# ---------------------------------------------------------------------------

def test_read_rows_missing_file_returns_empty(tmp_path):
    assert cost.read_rows(tmp_path / "nonexistent.jsonl") == []


def test_read_rows_empty_file_returns_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert cost.read_rows(path) == []


def test_read_rows_returns_all_valid_rows(tmp_path):
    rows = [
        {"ts": "2026-01-01", "cost_usd": 0.5, "stage_index": 1, "plan_path": "/p.toml"},
        {"ts": "2026-01-02", "cost_usd": 0.3, "stage_index": 2, "plan_path": "/p.toml"},
    ]
    path = _write_log(tmp_path, rows)
    result = cost.read_rows(path)
    assert len(result) == 2
    assert result[0]["cost_usd"] == 0.5
    assert result[1]["cost_usd"] == 0.3


def test_read_rows_skips_malformed_lines(tmp_path):
    path = tmp_path / "costs.jsonl"
    path.write_text('{"ok": true}\nNOT_JSON\n{"also": "ok"}\n', encoding="utf-8")
    result = cost.read_rows(path)
    assert len(result) == 2
    assert result[0]["ok"] is True
    assert result[1]["also"] == "ok"


def test_read_rows_skips_blank_lines(tmp_path):
    path = tmp_path / "costs.jsonl"
    path.write_text('{"a": 1}\n\n\n{"b": 2}\n', encoding="utf-8")
    result = cost.read_rows(path)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# attribute_stage
# ---------------------------------------------------------------------------

def test_attribute_stage_sums_matching_rows(tmp_path):
    rows = [
        {"plan_path": "/p.toml", "stage_index": 1, "cost_usd": 1.0, "duration_ms": 1000},
        {"plan_path": "/p.toml", "stage_index": 1, "cost_usd": 0.5, "duration_ms": 500},
        {"plan_path": "/p.toml", "stage_index": 2, "cost_usd": 2.0, "duration_ms": 2000},
    ]
    attr = cost.attribute_stage(rows, "/p.toml", 1)
    assert attr["cost_usd"] == pytest.approx(1.5)
    assert attr["duration_ms"] == 1500
    assert attr["spawn_count"] == 2


def test_attribute_stage_no_matching_rows():
    rows = [{"plan_path": "/other.toml", "stage_index": 1, "cost_usd": 1.0, "duration_ms": 1000}]
    attr = cost.attribute_stage(rows, "/p.toml", 1)
    assert attr["cost_usd"] is None
    assert attr["duration_ms"] is None
    assert attr["spawn_count"] == 0


def test_attribute_stage_none_cost_rows_excluded_from_sum():
    rows = [
        {"plan_path": "/p.toml", "stage_index": 1, "cost_usd": None, "duration_ms": 1000},
        {"plan_path": "/p.toml", "stage_index": 1, "cost_usd": 0.5, "duration_ms": None},
    ]
    attr = cost.attribute_stage(rows, "/p.toml", 1)
    assert attr["cost_usd"] == pytest.approx(0.5)
    assert attr["duration_ms"] == 1000
    assert attr["spawn_count"] == 2


def test_attribute_stage_all_null_cost_returns_none():
    rows = [{"plan_path": "/p.toml", "stage_index": 1, "cost_usd": None, "duration_ms": None}]
    attr = cost.attribute_stage(rows, "/p.toml", 1)
    assert attr["cost_usd"] is None
    assert attr["duration_ms"] is None
    assert attr["spawn_count"] == 1


def test_attribute_stage_none_plan_path_returns_zero_attribution():
    attr = cost.attribute_stage([], None, 1)
    assert attr["cost_usd"] is None
    assert attr["duration_ms"] is None
    assert attr["spawn_count"] == 0


def test_attribute_stage_none_stage_index_returns_zero_attribution():
    attr = cost.attribute_stage([], "/p.toml", None)
    assert attr["cost_usd"] is None
    assert attr["duration_ms"] is None
    assert attr["spawn_count"] == 0


def test_attribute_stage_empty_rows():
    attr = cost.attribute_stage([], "/p.toml", 3)
    assert attr["cost_usd"] is None
    assert attr["duration_ms"] is None
    assert attr["spawn_count"] == 0


# ---------------------------------------------------------------------------
# rollup_plan
# ---------------------------------------------------------------------------

def test_rollup_plan_sums_stage_outcomes():
    stages = [
        _make_spawn_stage(1, cost_usd=1.0, duration_ms=1000, spawn_count=1),
        _make_spawn_stage(2, cost_usd=2.0, duration_ms=2000, spawn_count=2),
    ]
    rollup = cost.rollup_plan([], "/p.toml", stages)
    assert isinstance(rollup, CostRollup)
    assert rollup.total_cost_usd == pytest.approx(3.0)
    assert rollup.total_duration_ms == 3000
    assert rollup.spawn_count == 3
    assert rollup.attributed_stages == 2


def test_rollup_plan_none_cost_stages_not_counted():
    stages = [
        _make_spawn_stage(1, cost_usd=None, duration_ms=None, spawn_count=0),
        _make_spawn_stage(2, cost_usd=1.0, duration_ms=500, spawn_count=1),
    ]
    rollup = cost.rollup_plan([], "/p.toml", stages)
    assert rollup.total_cost_usd == pytest.approx(1.0)
    assert rollup.total_duration_ms == 500
    assert rollup.attributed_stages == 1
    assert rollup.spawn_count == 1


def test_rollup_plan_empty_stages():
    rollup = cost.rollup_plan([], "/p.toml", [])
    assert rollup.total_cost_usd is None
    assert rollup.total_duration_ms is None
    assert rollup.spawn_count == 0
    assert rollup.attributed_stages == 0


def test_rollup_plan_note_mentions_in_thread_limitation():
    rollup = cost.rollup_plan([], "/p.toml", [])
    assert "in-thread" in rollup.note or "spawn" in rollup.note
