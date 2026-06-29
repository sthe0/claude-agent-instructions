"""Per-stage and whole-plan cost attribution from the spawn-cost log.

Reads ~/.local/log/claude-spawn-costs.jsonl (written by spawn-specialist.py) and
aggregates rows by (plan_path, stage_index) so cmd_record_result can stamp each
spawn stage's Outcome with the real tokens/dollars it consumed.

NOTE: only spawn stages carry attributed cost; main-session and in-thread tokens
are not split per stage. See scripts/cost-report.py for the whole-session estimate.
"""
from __future__ import annotations

import json
from pathlib import Path

COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"


def read_rows(log_path: Path | str = COST_LOG) -> list[dict]:
    """Read all rows from the cost log.

    Returns [] if the file is absent or unreadable. Skips individual lines that
    are not valid JSON (malformed writes, truncated flushes) without aborting.
    """
    path = Path(log_path)
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows


def attribute_stage(
    rows: list[dict],
    plan_path: str | None,
    stage_index: int | None,
) -> dict:
    """Sum cost log rows that match (plan_path, stage_index).

    Returns a dict with keys cost_usd / duration_ms / spawn_count.
    None-safe: rows whose cost_usd or duration_ms is None are counted in
    spawn_count but excluded from the respective sum (the sum itself stays
    None if no row contributes a numeric value).

    Returns zeros/None when plan_path or stage_index are None — in-thread
    stages never receive attribution.
    """
    if plan_path is None or stage_index is None:
        return {"cost_usd": None, "duration_ms": None, "spawn_count": 0}

    plan_str = str(plan_path)
    matching = [
        r for r in rows
        if r.get("plan_path") == plan_str and r.get("stage_index") == stage_index
    ]
    spawn_count = len(matching)

    cost_values = [r["cost_usd"] for r in matching if r.get("cost_usd") is not None]
    dur_values = [r["duration_ms"] for r in matching if r.get("duration_ms") is not None]

    return {
        "cost_usd": sum(cost_values) if cost_values else None,
        "duration_ms": sum(dur_values) if dur_values else None,
        "spawn_count": spawn_count,
    }


def rollup_plan(rows: list[dict], plan_path: str | None, stages: list) -> "CostRollup":
    """Aggregate per-stage outcome costs into a plan-level CostRollup.

    Folds over the stages' already-attributed Outcome fields (no second log read
    needed — record-result has already stored them on each Outcome). The `rows`
    and `plan_path` parameters are accepted for a consistent call signature but
    are not re-read here.
    """
    from .state import CostRollup

    total_cost_usd: float | None = None
    total_duration_ms: int | None = None
    attributed = 0
    spawn_count = 0

    for stage in stages:
        outcome = stage.outcome
        if outcome.cost_usd is not None:
            total_cost_usd = (total_cost_usd or 0.0) + outcome.cost_usd
            attributed += 1
        if outcome.duration_ms is not None:
            total_duration_ms = (total_duration_ms or 0) + outcome.duration_ms
        spawn_count += outcome.spawn_count

    note = (
        "spawn stage costs attributed from ~/.local/log/claude-spawn-costs.jsonl; "
        "main-session/in-thread tokens are not split per stage "
        "(see scripts/cost-report.py for the whole-session estimate)"
    )
    return CostRollup(
        total_cost_usd=total_cost_usd,
        total_duration_ms=total_duration_ms,
        spawn_count=spawn_count,
        attributed_stages=attributed,
        note=note,
    )
