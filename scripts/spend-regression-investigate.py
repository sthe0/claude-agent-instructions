#!/usr/bin/env python3
"""Turn a policy-scorecard spend-rate flag into a ranked driver shortlist.

`policy-scorecard.py` flags a rising spend rate (`_spend_rate_flag`) but a rate
alone doesn't say WHICH policy is behind it. This script compares the current
and previous `--days`-sized window over the same session ledger the scorecard
reads, and ranks six candidate cost drivers by their MOVEMENT between the two
windows (not their absolute size) — a driver that's large but flat explains
nothing about a rise, and is omitted rather than padding the list:

  - cache-read cost              (accumulated main-thread context re-read)
  - inherit->opus spawns         (delegatable-work-patterns)
  - missed-delegation clusters   (delegatable-work-patterns)
  - rework edits                 (repeated edits to the same path)
  - REPLANs                      (a step's done-criterion/place was wrong)
  - sub-agent failure markers    (marker-word proxy, not a process-failure rate)

Five of the six are counts, not dollars, so each is priced by the slope of an
ordinary-least-squares fit of session `cost_usd` against that driver's
per-session count, over every row in the ledger (not just the two windows
being compared, for a more stable slope) -- a correlational proxy, not a
causal attribution: it blends main-thread and sub-agent cost and averages
across whatever else moved alongside the driver in this ledger. cache-read
cost is the exception -- it is already a dollar figure in the ledger row, so
its impact is the raw window-to-window delta, no regression involved. A
driver whose slope comes out <= 0 (no measurable positive cost relationship
in this data) is omitted rather than reported with a misleading figure.

Read-only over the ledger: no model calls, no writes, no state changes.

Modes:
  spend-regression-investigate.py [--days N] [--ledger PATH]
      Rank drivers over the last N days vs the N days before that (default
      N=7, matching policy-scorecard.py's own window). --ledger overrides the
      ledger path (default: the real ~/.local/log ledger) -- tests use this so
      a run never touches live state.

Suggested flow once the spend-rate flag fires:
  1. Run this script (same --days as the scorecard).
  2. Read the ranked drivers top-down as ordered hypotheses.
  3. Follow the named rule to its remediation leaf.
  4. Runbook: memory-global/leaves/policy-effectiveness-tracking.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

# --- reuse policy-scorecard.py's ledger loader + window resolver (hyphenated
# filename -> load by path, no copy-paste; same pattern policy-scorecard.py
# itself uses to reuse cost-report.py) ---
_PS_PATH = SCRIPTS_DIR / "policy-scorecard.py"
_spec = importlib.util.spec_from_file_location("policy_scorecard", _PS_PATH)
policy_scorecard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy_scorecard)


class InvestigationError(RuntimeError):
    """Raised for any recoverable failure this script should report and exit
    non-zero on -- an empty ledger or a window with no sessions to compare."""


# Each driver: `row_value` reads the raw per-session ledger row (the shape
# `policy-scorecard.py`'s `_scan_session` writes); `unit` "usd" means the
# window delta IS the dollar impact, "count" means it prices the delta via an
# OLS slope against `cost_usd`. `leaf` is a memory-global/leaves/*.md stem.
DRIVERS = [
    {
        "key": "cache-read-share",
        "label": "cache-read cost",
        "unit": "usd",
        "row_value": lambda r: float(r.get("cache_read_usd", 0.0)),
        "rule": "keep the main thread lean (CLAUDE.md § Cost discipline) -- "
                "delegate verbose/exploratory work instead of accumulating "
                "context that gets re-read every turn",
        "leaf": "token-economy-plan",
    },
    {
        "key": "inherit-opus",
        "label": "inherit→opus spawns",
        "unit": "count",
        "row_value": lambda r: float(r.get("agent_spawns", {}).get("inherit_opus", 0)),
        "rule": "set the sub-agent model explicitly -- name the tier instead of "
                "inheriting opus by default",
        "leaf": "delegatable-work-patterns",
    },
    {
        "key": "missed-delegation",
        "label": "missed-delegation clusters",
        "unit": "count",
        "row_value": lambda r: float(r.get("missed_delegation_clusters", 0)),
        "rule": "delegate a run of consecutive mechanical main-thread calls to a "
                "cheap sub-agent instead of running it inline",
        "leaf": "delegatable-work-patterns",
    },
    {
        "key": "rework-edits",
        "label": "rework edits",
        "unit": "count",
        "row_value": lambda r: float(r.get("effectiveness", {}).get("rework_edits", 0)),
        "rule": "read the target code before editing it -- repeated edits to the "
                "same path are the effectiveness proxy this policy exists to catch",
        "leaf": "quality-regression-investigation",
    },
    {
        "key": "replans",
        "label": "REPLANs",
        "unit": "count",
        "row_value": lambda r: float(r.get("effectiveness", {}).get("replans", 0)),
        "rule": "a REPLAN means a step's done-criterion or its place in the plan "
                "was wrong -- get the plan right before executing, not after",
        "leaf": "quality-regression-investigation",
    },
    {
        "key": "subagent-failures",
        "label": "sub-agent failure markers",
        "unit": "count",
        "row_value": lambda r: float(r.get("effectiveness", {}).get("subagent_failures", 0)),
        "rule": "a marker-word proxy over transcript text, not a process-failure "
                "rate (policy-scorecard.py carries the same caveat) -- a rising "
                "count still warrants a look at what sub-agents are reporting",
        "leaf": "quality-regression-investigation",
    },
]


def _ols_slope(xs: list[float], ys: list[float]) -> float | None:
    """Marginal $/unit, or None when fewer than 2 points or zero x-variance --
    a slope through one point, or through points that never move, is not a fit."""
    n = len(xs)
    if n < 2:
        return None
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    var_x = sum((x - xbar) ** 2 for x in xs)
    if var_x == 0:
        return None
    cov_xy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    return cov_xy / var_x


def investigate(ledger_rows: dict[str, dict], days: int,
                now: dt.datetime | None = None) -> dict:
    """Ranked (largest dollar impact first) driver hypotheses, plus the drivers
    left out and why. Raises InvestigationError when there isn't enough ledger
    history to compare a current window against a previous one."""
    if not ledger_rows:
        raise InvestigationError("ledger has no rows -- nothing to rank")
    now = now or dt.datetime.now(dt.timezone.utc)
    cur_lo = now - dt.timedelta(days=days)
    prev_lo = now - dt.timedelta(days=2 * days)
    cur_window = policy_scorecard._window_rows(ledger_rows, cur_lo, now)
    prev_window = policy_scorecard._window_rows(ledger_rows, prev_lo, cur_lo)
    if not cur_window or not prev_window:
        raise InvestigationError(
            f"only one of the two {days}d windows has ledger sessions "
            f"({len(prev_window)} previous / {len(cur_window)} current) -- a "
            "movement comparison needs both; widen --days or wait for more history"
        )

    all_rows = list(ledger_rows.values())
    all_cost = [r.get("cost_usd", 0.0) for r in all_rows]

    cur_cost = sum(r.get("cost_usd", 0.0) for r in cur_window)
    prev_cost = sum(r.get("cost_usd", 0.0) for r in prev_window)

    ranked = []
    omitted = []
    for d in DRIVERS:
        cur_total = sum(d["row_value"](r) for r in cur_window)
        prev_total = sum(d["row_value"](r) for r in prev_window)
        delta = cur_total - prev_total
        if delta <= 0:
            omitted.append({**d, "cur": cur_total, "prev": prev_total, "delta": delta,
                            "reason": "no movement" if delta == 0 else "improved"})
            continue
        if d["unit"] == "usd":
            slope = None
            impact = delta
        else:
            xs = [d["row_value"](r) for r in all_rows]
            slope = _ols_slope(xs, all_cost)
            if slope is None or slope <= 0:
                omitted.append({**d, "cur": cur_total, "prev": prev_total, "delta": delta,
                                "reason": "no measurable positive cost relationship "
                                          "in this ledger"})
                continue
            impact = slope * delta
        ranked.append({**d, "cur": cur_total, "prev": prev_total, "delta": delta,
                       "slope": slope, "dollar_impact": impact})
    ranked.sort(key=lambda r: r["dollar_impact"], reverse=True)
    return {
        "cur_lo": cur_lo, "now": now, "prev_lo": prev_lo,
        "n_cur": len(cur_window), "n_prev": len(prev_window),
        "cur_cost": cur_cost, "prev_cost": prev_cost,
        "ranked": ranked, "omitted": omitted,
    }


def format_report(result: dict, days: int) -> str:
    cost_delta = result["cur_cost"] - result["prev_cost"]
    delta_sign = "+" if cost_delta >= 0 else "-"
    lines = [
        f"Spend-driver movement -- last {days}d vs previous {days}d "
        f"({result['n_prev']} → {result['n_cur']} sessions)",
        f"Actual cost_usd movement: ${result['prev_cost']:,.2f} → "
        f"${result['cur_cost']:,.2f} ({delta_sign}${abs(cost_delta):,.2f})",
        "",
    ]
    if not result["ranked"]:
        lines.append("(no candidate driver rose between the two windows)")
    else:
        lines.append(
            "Ranked impacts below are independent per-driver OLS correlational "
            "estimates, not additive -- they will typically sum past the actual "
            "movement above."
        )
        lines.append("")
    for r in result["ranked"]:
        slope_note = f", slope ${r['slope']:.4f}/unit" if r["slope"] is not None else ""
        if r["unit"] == "usd":
            move = f"${r['prev']:.2f}→${r['cur']:.2f}"
        else:
            move = f"{r['prev']:,.0f}→{r['cur']:,.0f}"
        lines.append(f"  ${r['dollar_impact']:+.2f}  {r['label']}: {move}{slope_note}")
        lines.append(f"      rule: {r['rule']}")
        lines.append(f"      remediation: memory-global/leaves/{r['leaf']}.md")
    if result["omitted"]:
        lines.append("")
        lines.append(f"Not ranked ({len(result['omitted'])}): " + "; ".join(
            f"{o['label']} ({o['reason']})" for o in result["omitted"]))
    return "\n".join(lines)


# --------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=7,
                        help="window size in days (default 7, matching policy-scorecard.py)")
    parser.add_argument("--ledger", type=Path,
                        help="override the ledger path (default: the real ~/.local/log "
                             "ledger) -- tests use this so a run never touches live state")
    args = parser.parse_args(argv)

    if args.ledger:
        policy_scorecard.LEDGER = args.ledger
    rows = policy_scorecard.load_ledger()

    try:
        result = investigate(rows, args.days)
    except InvestigationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_report(result, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
