#!/usr/bin/env python3
"""Calibrate the spawn budget tiers from recorded spend.

Under flat billing the per-tier `budget-*-usd` values are no longer kill-caps
(P1 decoupled the runaway kill into `spawn-runaway-ceiling-usd`); they are
expected-size TELEMETRY LABELS. This tool closes the calibration loop the flat
framing needs: it reads the two ledgers written by the coordination machinery
and reports whether those tier labels still match reality, grouping realized
spend two ways:

  A. (spawn `kind` x `budget_tier`) from claude-spawn-costs.jsonl — the axis
     that maps directly to the config.md knobs being calibrated.
  B. (task `weight_class` x `deliverable_kind`) from claude-task-quality.jsonl —
     the user's "budgets of different task types" view.

The Flags section drives raise/lower recommendations on the (kind x tier) axis
(there is no per-task-type config knob, so group B is informational). A group's
realized p90 above its configured tier value ⇒ the tier is under-set (the label
under-estimates real work, and pre-P1 would have been a false kill); a median far
below the tier ⇒ over-provisioned.

Sibling of cost-report.py / policy-scorecard.py; reuses cost-report.py's ledger
parsing rather than duplicating it. Read-only over both ledgers.

Usage:
  budget-calibration.py [--last N | --all] [--min-samples K]
  budget-calibration.py --check      # one flag line + exit 1 if miscalibrated (for the hook)
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import config as _agent_config  # noqa: E402

# Reuse cost-report.py's ledger parsing (hyphenated filename ⇒ importlib).
_CR_SPEC = importlib.util.spec_from_file_location(
    "cost_report", SCRIPTS_DIR / "cost-report.py"
)
_cost_report = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(_cost_report)
parse_entries = _cost_report.parse_entries
parse_ts = _cost_report.parse_ts
split_events = _cost_report.split_events
fmt_usd = _cost_report.fmt_usd

SPAWN_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"
QUALITY_LOG = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"

# The tiers that have a config.md knob to calibrate against.
KNOWN_TIERS = ("small", "medium", "large")

# Flag thresholds (mirror policy-scorecard's ratio-style flags). Review-flagged
# defaults — re-tune once real grouped history exists.
UNDERSET_P90_MULT = 1.0     # p90 > tier value ⇒ tier under-set
OVERPROV_MEDIAN_FRAC = 0.3  # median < 0.3 x tier value ⇒ over-provisioned
MIN_SAMPLES_DEFAULT = 3     # don't flag a group with fewer rows than this

DEFAULT_LAST = 50


def _sort_by_ts(rows: list[dict]) -> list[dict]:
    """Ascending by ts; rows without a parseable ts sort first (treated as oldest)."""
    def key(r: dict):
        ts = r.get("ts")
        if not ts:
            return (0, "")
        try:
            return (1, parse_ts(ts).isoformat())
        except (ValueError, TypeError):
            return (0, "")
    return sorted(rows, key=key)


def _scope(rows: list[dict], last: int | None) -> list[dict]:
    """Apply --last N (most recent N by ts) or --all (last is None)."""
    ordered = _sort_by_ts(rows)
    if last is None:
        return ordered
    return ordered[-last:]


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile; robust for any n >= 1 (statistics.quantiles
    requires n >= 2)."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _stats(costs: list[float]) -> dict:
    return {
        "n": len(costs),
        "median": statistics.median(costs) if costs else None,
        "p90": _percentile(costs, 90),
    }


def _tier_value(thresholds, tier: str) -> float | None:
    if tier not in KNOWN_TIERS:
        return None
    try:
        return float(thresholds.budget_usd(tier))
    except (KeyError, ValueError):
        return None


def group_by_kind_tier(spawn_rows: list[dict]) -> dict:
    """(kind, tier) -> [cost_usd, ...] for spawn rows carrying a numeric cost."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in spawn_rows:
        cost = r.get("cost_usd")
        if cost is None:
            continue
        kind = r.get("kind") or "?"
        tier = r.get("budget_tier") or "?"
        buckets[(kind, tier)].append(float(cost))
    return buckets


def group_by_task_type(quality_rows: list[dict]) -> dict:
    """(weight_class, deliverable_kind) -> [total_cost_usd, ...].

    Excludes rows with spawn_count == 0: per the P2 writer contract a null/absent
    total_cost_usd on such a row means "no spawn spend recorded" (an in-thread
    task), NOT a $0 task — including them would understate real per-type spend.
    """
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in quality_rows:
        if (r.get("spawn_count") or 0) == 0:
            continue
        cost = r.get("total_cost_usd")
        if cost is None:
            continue
        wc = r.get("weight_class") or "?"
        dk = r.get("deliverable_kind") or "?"
        buckets[(wc, dk)].append(float(cost))
    return buckets


def calibration_flags(kind_tier: dict, thresholds, min_samples: int) -> list[str]:
    """Raise/lower recommendations on the (kind x tier) axis vs the configured
    tier value. Only groups with >= min_samples rows are judged."""
    flags: list[str] = []
    for (kind, tier), costs in sorted(kind_tier.items()):
        if len(costs) < min_samples:
            continue
        tier_val = _tier_value(thresholds, tier)
        if tier_val is None:
            continue
        st = _stats(costs)
        if st["p90"] is not None and st["p90"] > tier_val * UNDERSET_P90_MULT:
            flags.append(
                f"RAISE {tier}: {kind} p90 {fmt_usd(st['p90'])} > tier {fmt_usd(tier_val)} "
                f"(n={st['n']}) — tier under-set / pre-P1 false-kill risk"
            )
        elif st["median"] is not None and st["median"] < tier_val * OVERPROV_MEDIAN_FRAC:
            flags.append(
                f"LOWER {tier}: {kind} median {fmt_usd(st['median'])} < "
                f"{OVERPROV_MEDIAN_FRAC:g}x tier {fmt_usd(tier_val)} (n={st['n']}) — over-provisioned"
            )
    return flags


def build_report(spawn_rows, quality_rows, thresholds, *, last, min_samples) -> tuple[str, list[str]]:
    spawns, _refused = split_events(spawn_rows)
    spawns = _scope(spawns, last)
    quality = _scope(quality_rows, last)

    kind_tier = group_by_kind_tier(spawns)
    task_type = group_by_task_type(quality)
    flags = calibration_flags(kind_tier, thresholds, min_samples)

    scope_label = "all history" if last is None else f"last {last}"
    lines: list[str] = []
    lines.append(f"budget calibration — {scope_label} (min-samples {min_samples})")
    lines.append("")
    lines.append("A. realized spend by (kind x tier) vs configured tier label:")
    if kind_tier:
        for (kind, tier), costs in sorted(kind_tier.items()):
            st = _stats(costs)
            tier_val = _tier_value(thresholds, tier)
            ref = f"tier {fmt_usd(tier_val)}" if tier_val is not None else "tier ?"
            lines.append(
                f"  {kind:<22} {tier:<7} n={st['n']:<4} "
                f"median {fmt_usd(st['median'])}  p90 {fmt_usd(st['p90'])}   [{ref}]"
            )
    else:
        lines.append("  (no spawn cost rows in range)")
    lines.append("")
    lines.append("B. realized spend by task type (weight_class x deliverable_kind), "
                 "in-thread tasks excluded:")
    if task_type:
        for (wc, dk), costs in sorted(task_type.items()):
            st = _stats(costs)
            lines.append(
                f"  {wc:<14} {dk:<12} n={st['n']:<4} "
                f"median {fmt_usd(st['median'])}  p90 {fmt_usd(st['p90'])}"
            )
    else:
        lines.append("  (no task-quality rows with recorded spawn spend in range)")
    lines.append("")
    if flags:
        lines.append("Flags — tiers to review (run self-improvement to adjust config.md):")
        for f in flags:
            lines.append(f"  ⚑ {f}")
    else:
        lines.append("Flags: none — tier labels look consistent with realized spend.")
    return "\n".join(lines), flags


def _load_thresholds(config_path: Path | None):
    constants = _agent_config.parse_config_md(config_path) if config_path else _agent_config.parse_config_md()
    return _agent_config.Thresholds(constants)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Calibrate spawn budget tiers from recorded spend.")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--last", type=int, default=DEFAULT_LAST,
                       help=f"consider the most recent N rows per ledger (default {DEFAULT_LAST})")
    scope.add_argument("--all", action="store_true", help="consider all recorded history")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES_DEFAULT,
                    help=f"minimum rows in a group before it is flagged (default {MIN_SAMPLES_DEFAULT})")
    ap.add_argument("--check", action="store_true",
                    help="quiet mode for the nudge hook: print ONE flag line and exit 1 if any "
                         "tier looks miscalibrated, else print nothing and exit 0")
    ap.add_argument("--spawn-log", type=Path, default=SPAWN_LOG, help="override spawn-costs ledger (tests)")
    ap.add_argument("--quality-log", type=Path, default=QUALITY_LOG, help="override task-quality ledger (tests)")
    ap.add_argument("--config", type=Path, default=None, help="override config.md path (tests)")
    args = ap.parse_args(argv)

    last = None if args.all else args.last
    thresholds = _load_thresholds(args.config)
    spawn_rows = parse_entries(args.spawn_log)
    quality_rows = parse_entries(args.quality_log)

    report, flags = build_report(spawn_rows, quality_rows, thresholds,
                                 last=last, min_samples=args.min_samples)

    if args.check:
        if flags:
            head = flags[0]
            more = f" (+{len(flags) - 1} more)" if len(flags) > 1 else ""
            print(f"{head}{more}")
            return 1
        return 0

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
