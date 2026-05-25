#!/usr/bin/env python3
"""Aggregate the spawn-specialist cost log.

Reads ~/.local/log/claude-spawn-costs.jsonl (one JSON object per line, written
by `scripts/spawn-specialist.py`) and prints a summary.

Two kinds of entries:
  - event == "spawn" (or missing event field for backward compat): actual
    `claude -p` invocation. Has cost_usd, duration_ms, return_marker, depth.
  - event == "refused": spawn was refused before reaching `claude -p`
    (recursion-cap, unknown-kind, plan-not-found). Has reason and minimal fields.

Default output: a summary for the last 7 days, grouped by kind.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"


def parse_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"warning: skipping malformed jsonl line: {line[:120]}", file=sys.stderr)
    return entries


def parse_ts(s: str) -> dt.datetime:
    # Handles "...+00:00" and "Z".
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


def filter_by_time(entries: list[dict], since: dt.datetime) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        ts = e.get("ts")
        if not ts:
            continue
        try:
            if parse_ts(ts) >= since:
                out.append(e)
        except ValueError:
            continue
    return out


def split_events(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (spawns, refused). Entries with no 'event' field are treated as spawn."""
    spawns: list[dict] = []
    refused: list[dict] = []
    for e in entries:
        if e.get("event", "spawn") == "refused":
            refused.append(e)
        else:
            spawns.append(e)
    return spawns, refused


def fmt_usd(x: float | None) -> str:
    return f"${x:.4f}" if x is not None else "$?"


def summary(spawns: list[dict], refused: list[dict], group_by: str) -> str:
    lines: list[str] = []
    total_cost = sum((e.get("cost_usd") or 0) for e in spawns)
    durations = [e.get("duration_ms") for e in spawns if e.get("duration_ms") is not None]
    costs = [e.get("cost_usd") for e in spawns if e.get("cost_usd") is not None]
    malformed = sum(1 for e in spawns if e.get("malformed"))

    lines.append(f"spawns: {len(spawns)}    total_cost: {fmt_usd(total_cost)}    malformed: {malformed}")
    if costs:
        lines.append(
            f"  cost/spawn: avg {fmt_usd(statistics.fmean(costs))}    "
            f"median {fmt_usd(statistics.median(costs))}    "
            f"max {fmt_usd(max(costs))}"
        )
    if durations:
        lines.append(
            f"  duration_ms: avg {int(statistics.fmean(durations))}    "
            f"median {int(statistics.median(durations))}    "
            f"max {max(durations)}"
        )

    if group_by == "kind":
        buckets: dict[str, list[dict]] = defaultdict(list)
        for e in spawns:
            buckets[e.get("kind", "?")].append(e)
        lines.append("\nby kind:")
        for kind in sorted(buckets):
            es = buckets[kind]
            c = sum((e.get("cost_usd") or 0) for e in es)
            lines.append(f"  {kind:<24} {len(es):>4} spawns    {fmt_usd(c)}")
    elif group_by == "tier":
        buckets = defaultdict(list)
        for e in spawns:
            buckets[e.get("budget_tier", "?")].append(e)
        lines.append("\nby tier:")
        for tier in sorted(buckets):
            es = buckets[tier]
            c = sum((e.get("cost_usd") or 0) for e in es)
            lines.append(f"  {tier:<10} {len(es):>4} spawns    {fmt_usd(c)}")
    elif group_by == "day":
        buckets = defaultdict(list)
        for e in spawns:
            try:
                day = parse_ts(e["ts"]).date().isoformat()
            except (KeyError, ValueError):
                continue
            buckets[day].append(e)
        lines.append("\nby day:")
        for day in sorted(buckets):
            es = buckets[day]
            c = sum((e.get("cost_usd") or 0) for e in es)
            lines.append(f"  {day}    {len(es):>4} spawns    {fmt_usd(c)}")

    depth_counter = Counter(e.get("depth") for e in spawns if e.get("depth") is not None)
    if depth_counter:
        depth_repr = "  ".join(f"d{d}={depth_counter[d]}" for d in sorted(depth_counter))
        lines.append(f"\nby depth: {depth_repr}")

    marker_counter = Counter(e.get("return_marker") for e in spawns if e.get("return_marker"))
    if marker_counter:
        marker_repr = "  ".join(f"{m}={marker_counter[m]}" for m in sorted(marker_counter))
        lines.append(f"by marker: {marker_repr}")

    if refused:
        reason_counter = Counter(e.get("reason", "?") for e in refused)
        lines.append(f"\nrefused: {len(refused)} total")
        for reason in sorted(reason_counter):
            lines.append(f"  {reason:<20} {reason_counter[reason]}")

    return "\n".join(lines)


def detail(spawns: list[dict], refused: list[dict]) -> str:
    rows: list[str] = []
    for e in spawns:
        rows.append(
            f"{e.get('ts','?'):<26} spawn   {e.get('kind','?'):<24} "
            f"{e.get('budget_tier','?'):<7} d={e.get('depth','?')}   "
            f"{fmt_usd(e.get('cost_usd'))}   marker={e.get('return_marker','?')}"
            f"{'  MALFORMED' if e.get('malformed') else ''}"
        )
    for e in refused:
        rows.append(
            f"{e.get('ts','?'):<26} refused {e.get('kind','?'):<24} "
            f"reason={e.get('reason','?')}"
        )
    return "\n".join(rows) if rows else "(no entries in selected range)"


def csv_out(spawns: list[dict], refused: list[dict]) -> str:
    import io
    buf = io.StringIO()
    fields = ["ts", "event", "kind", "budget_tier", "depth", "cost_usd",
              "duration_ms", "return_marker", "malformed", "exit_code", "reason"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for e in spawns:
        row = dict(e)
        row.setdefault("event", "spawn")
        w.writerow(row)
    for e in refused:
        w.writerow(e)
    return buf.getvalue().rstrip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--log", type=Path, default=COST_LOG, help=f"cost log path (default: {COST_LOG})")
    p.add_argument("--days", type=int, default=7, help="window size in days (default: 7)")
    p.add_argument("--since", help="window start as YYYY-MM-DD (overrides --days)")
    p.add_argument("--by", choices=("kind", "tier", "day"), default="kind", help="grouping for summary")
    p.add_argument("--detail", action="store_true", help="print one row per entry instead of summary")
    p.add_argument("--csv", action="store_true", help="emit CSV (with all fields)")
    args = p.parse_args(argv)

    entries = parse_entries(args.log)
    if not entries:
        print(f"(no entries in {args.log})")
        return 0

    if args.since:
        since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
        window_label = f"since {args.since}"
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
        window_label = f"last {args.days} days"

    in_window = filter_by_time(entries, since)
    spawns, refused = split_events(in_window)

    if args.csv:
        print(csv_out(spawns, refused))
        return 0

    print(f"cost-report: {window_label}    file: {args.log}")
    print()
    if args.detail:
        print(detail(spawns, refused))
    else:
        print(summary(spawns, refused, args.by))
    return 0


if __name__ == "__main__":
    sys.exit(main())
