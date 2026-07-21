"""agentctl.config.Thresholds.runaway_ceiling_usd — the typed accessor for the
single global runaway backstop (spawn-runaway-ceiling-usd), decoupled from the
per-tier budget-*-usd telemetry labels. Fail-safe to the large tier if absent,
mirroring spawn-specialist.runaway_ceiling (never unbounded)."""
from __future__ import annotations

from agentctl.config import Thresholds


def test_runaway_ceiling_usd_returns_config_key_when_present():
    thr = Thresholds({"spawn-runaway-ceiling-usd": "25.0", "budget-large-usd": "8.00"})
    assert thr.runaway_ceiling_usd() == "25.0"


def test_runaway_ceiling_usd_falls_back_to_large_tier_when_absent():
    thr = Thresholds({"budget-large-usd": "8.00"})
    assert thr.runaway_ceiling_usd() == "8.00"


def test_budget_usd_still_returns_the_tier_label():
    thr = Thresholds({"budget-small-usd": "1.00", "budget-large-usd": "8.00"})
    assert thr.budget_usd("small") == "1.00"


def test_live_config_ceiling_above_large_tier():
    thr = Thresholds()
    assert float(thr.runaway_ceiling_usd()) > float(thr.budget_usd("large"))
