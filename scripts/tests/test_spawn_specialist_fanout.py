"""spawn-specialist.py fan-out / SUM-bound refusal: bounds the SUM of committed
spend across ALL children on one instance's cost ledger, distinct from the
existing per-spawn --max-budget-usd tier cap. Opt-in via two env vars
(AGENT_BENCH_MAX_CHILDREN, AGENT_BENCH_SPAWN_BUDGET_USD) so every existing
caller — and the default (non-spawn-permitting) benchmark profile — is
unaffected when they are unset.

Tests cover:
- read_ledger_rows / committed_spawn_usd in isolation (pure functions)
- main() refuses a spawn that would exceed AGENT_BENCH_MAX_CHILDREN
- main() refuses a spawn that would push the ledger's SUM past
  AGENT_BENCH_SPAWN_BUDGET_USD, using the actual cost of completed children and
  the budget cap of still in-flight ones
- both checks are a no-op (spawn proceeds to the next stage) when their env
  var is unset
- a refusal is itself logged to the ledger as an `event: refused` row
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_fanout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ---------------------------------------------------------------------------
# read_ledger_rows
# ---------------------------------------------------------------------------

def test_read_ledger_rows_missing_file_returns_empty(tmp_path):
    assert MOD.read_ledger_rows(tmp_path / "nope.jsonl") == []


def test_read_ledger_rows_skips_blank_and_malformed_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('\n{"event": "spawn_start", "spawn_id": "a"}\nnot json\n')
    rows = MOD.read_ledger_rows(path)
    assert rows == [{"event": "spawn_start", "spawn_id": "a"}]


# ---------------------------------------------------------------------------
# committed_spawn_usd
# ---------------------------------------------------------------------------

def test_committed_spawn_usd_uses_actual_cost_for_completed_child():
    rows = [
        {"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"},
        {"event": "spawn", "spawn_id": "a", "cost_usd": 1.23},
    ]
    count, total = MOD.committed_spawn_usd(rows)
    assert count == 1
    assert total == pytest.approx(1.23)


def test_committed_spawn_usd_uses_budget_cap_for_in_flight_child():
    rows = [{"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"}]
    count, total = MOD.committed_spawn_usd(rows)
    assert count == 1
    assert total == pytest.approx(3.00)


def test_committed_spawn_usd_sums_across_multiple_children():
    rows = [
        {"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"},
        {"event": "spawn", "spawn_id": "a", "cost_usd": 2.5},
        {"event": "spawn_start", "spawn_id": "b", "budget_usd_cap": "1.00"},
    ]
    count, total = MOD.committed_spawn_usd(rows)
    assert count == 2
    assert total == pytest.approx(3.5)  # 2.5 actual + 1.00 in-flight cap


def test_committed_spawn_usd_ignores_rows_with_no_spawn_id():
    rows = [{"event": "spawn_start", "budget_usd_cap": "3.00"}]
    count, total = MOD.committed_spawn_usd(rows)
    assert count == 0
    assert total == 0.0


def test_committed_spawn_usd_empty_ledger():
    assert MOD.committed_spawn_usd([]) == (0, 0.0)


# ---------------------------------------------------------------------------
# main(): fan-out cap
# ---------------------------------------------------------------------------

def _plan(tmp_path) -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n## Stage 1 **<<this step>>**\n\ndo the thing\n")
    return plan


def _base_argv(tmp_path) -> list[str]:
    return [
        "--kind", "developer",
        "--plan", str(_plan(tmp_path)),
        "--done-criterion", "tests green",
        "--criterion-type", "measurable",
        "--dry-run",
    ]


def test_main_refuses_third_child_when_max_children_is_two(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"}) + "\n"
        + json.dumps({"event": "spawn_start", "spawn_id": "b", "budget_usd_cap": "3.00"}) + "\n"
    )
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(ledger))
    monkeypatch.setenv("AGENT_BENCH_MAX_CHILDREN", "2")
    monkeypatch.delenv("AGENT_BENCH_SPAWN_BUDGET_USD", raising=False)

    code = MOD.main(_base_argv(tmp_path))

    assert code == 5
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    refusals = [r for r in rows if r.get("event") == "refused"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "fanout-cap"
    assert refusals[0]["children_before"] == 2


def test_main_allows_second_child_when_max_children_is_two(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"}) + "\n")
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(ledger))
    monkeypatch.setenv("AGENT_BENCH_MAX_CHILDREN", "2")
    monkeypatch.delenv("AGENT_BENCH_SPAWN_BUDGET_USD", raising=False)

    code = MOD.main(_base_argv(tmp_path))

    assert code == 0  # --dry-run: falls through to printing, never spawns


def test_main_is_unaffected_when_max_children_unset(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps({"event": "spawn_start", "spawn_id": str(i), "budget_usd_cap": "3.00"})
            for i in range(10)
        )
        + "\n"
    )
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(ledger))
    monkeypatch.delenv("AGENT_BENCH_MAX_CHILDREN", raising=False)
    monkeypatch.delenv("AGENT_BENCH_SPAWN_BUDGET_USD", raising=False)

    assert MOD.main(_base_argv(tmp_path)) == 0


# ---------------------------------------------------------------------------
# main(): SUM-bound
# ---------------------------------------------------------------------------

def test_main_refuses_spawn_that_would_exceed_the_sum_bound(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"}) + "\n"
        + json.dumps({"event": "spawn", "spawn_id": "a", "cost_usd": 3.00}) + "\n"
    )
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(ledger))
    monkeypatch.delenv("AGENT_BENCH_MAX_CHILDREN", raising=False)
    monkeypatch.setenv("AGENT_BENCH_SPAWN_BUDGET_USD", "6.00")

    # kind=developer floors to the medium tier ($3.00 in this repo's config.md),
    # so 3.00 (already spent) + 3.00 (this spawn's own cap) == 6.00: right at
    # the boundary, not over it, so this one is allowed.
    code = MOD.main(_base_argv(tmp_path))
    assert code == 0

    # A second attempt on the same ledger (still 3.00 committed, but now the
    # wrapper would be adding a THIRD 3.00 on top since --dry-run never wrote
    # its own start row) simulates the over-the-cap case directly instead.
    ledger.write_text(
        json.dumps({"event": "spawn_start", "spawn_id": "a", "budget_usd_cap": "3.00"}) + "\n"
        + json.dumps({"event": "spawn", "spawn_id": "a", "cost_usd": 5.00}) + "\n"
    )
    code = MOD.main(_base_argv(tmp_path))
    assert code == 6
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    refusals = [r for r in rows if r.get("event") == "refused"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "spawn-budget-cap"
    assert refusals[0]["committed_usd_before"] == pytest.approx(5.00)
    assert refusals[0]["cap_usd"] == pytest.approx(6.00)


def test_main_is_unaffected_when_spawn_budget_unset(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"event": "spawn", "spawn_id": "a", "cost_usd": 999.0}) + "\n")
    monkeypatch.setenv("CLAUDE_SPAWN_COST_LOG", str(ledger))
    monkeypatch.delenv("AGENT_BENCH_MAX_CHILDREN", raising=False)
    monkeypatch.delenv("AGENT_BENCH_SPAWN_BUDGET_USD", raising=False)

    assert MOD.main(_base_argv(tmp_path)) == 0
