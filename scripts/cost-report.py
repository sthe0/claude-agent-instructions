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
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home

COST_LOG = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"
PROJECTS_DIR = agent_home() / "projects"  # system root (isolated or legacy)

# USD per 1M tokens. Rates change — refresh via the `claude-api` skill.
# cache_write = 5-minute cache-creation rate (1.25x base input); cache_read = 0.1x base input.
PRICING_USD_PER_MTOK = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
}
_FALLBACK_RATES = PRICING_USD_PER_MTOK["opus"]


def _rates_for(model: str | None) -> dict:
    m = (model or "").lower()
    for key in ("opus", "sonnet", "haiku"):
        if key in m:
            return PRICING_USD_PER_MTOK[key]
    return _FALLBACK_RATES


def token_cost(usage: dict, model: str | None) -> float:
    r = _rates_for(model)
    return (
        (usage.get("input_tokens", 0) or 0) * r["input"]
        + (usage.get("output_tokens", 0) or 0) * r["output"]
        + (usage.get("cache_creation_input_tokens", 0) or 0) * r["cache_write"]
        + (usage.get("cache_read_input_tokens", 0) or 0) * r["cache_read"]
    ) / 1_000_000


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
    """Return (spawns, refused). Entries with no 'event' field are treated as spawn.

    'spawn_start' rows are dropped: each pairs with the 'spawn' row the same child
    writes on return, so counting both would double every spawn and average in a
    costless row.
    """
    spawns: list[dict] = []
    refused: list[dict] = []
    for e in entries:
        event = e.get("event", "spawn")
        if event == "spawn_start":
            continue
        if event == "refused":
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


INTERRUPT_SENTINEL = "[Request interrupted by user]"
CORRECTION_RE = re.compile(
    r"нет\b|не так|неправильн|неверн|поправ|по-русски|шире|только\b|"
    r"wrong|actually|instead|not just|don't|почему (?:только|ты)|не нужно|не надо",
    re.IGNORECASE,
)


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _is_tool_result(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in content
    )


def parse_transcripts(files: list[Path], classify: bool = False) -> dict:
    """Aggregate token usage + interaction counts from session transcript JSONL.

    Real user prompts = type:user lines whose content is text (not a tool_result).
    AskUserQuestion tool_use calls count as agent->user asks.
    """
    by_model_tokens: dict[str, Counter] = defaultdict(Counter)
    interactive_usd = 0.0
    user_prompts = interrupts = asks = corrections = 0
    timestamps: list[str] = []
    for path in files:
        for d in _iter_jsonl(path):
            ts = d.get("timestamp") or (d.get("message") or {}).get("ts")
            if isinstance(ts, str):
                timestamps.append(ts)
            typ = d.get("type")
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if typ == "assistant":
                usage = msg.get("usage")
                if usage:
                    model = msg.get("model")
                    interactive_usd += token_cost(usage, model)
                    tok = by_model_tokens[model or "?"]
                    for k in ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens", "cache_read_input_tokens"):
                        tok[k] += usage.get(k, 0) or 0
                for c in (msg.get("content") or []):
                    if (isinstance(c, dict) and c.get("type") == "tool_use"
                            and c.get("name") == "AskUserQuestion"):
                        asks += 1
            elif typ == "user":
                content = msg.get("content")
                if _is_tool_result(content):
                    continue
                text = _msg_text(content)
                if not text.strip():
                    continue
                if INTERRUPT_SENTINEL in text:
                    interrupts += 1
                else:
                    user_prompts += 1
                    if classify and CORRECTION_RE.search(text):
                        corrections += 1
    return {
        "by_model_tokens": by_model_tokens,
        "interactive_usd": interactive_usd,
        "user_prompts": user_prompts,
        "interrupts": interrupts,
        "asks": asks,
        "corrections": corrections,
        "classify": classify,
        "span": (min(timestamps), max(timestamps)) if timestamps else None,
        "n_files": len(files),
    }


def resolve_project(project: str) -> list[Path]:
    p = Path(project).expanduser()
    if not p.exists():
        p = PROJECTS_DIR / project
    if p.is_dir():
        return sorted(f for f in p.glob("*.jsonl"))
    if p.is_file():
        return [p]
    return []


def budget_report(tr: dict, spawn_cost: float, spawn_note: str) -> str:
    low, high = spawn_cost, spawn_cost + tr["interactive_usd"]
    L = ["=== Full-budget estimate (low -> high) ==="]
    L.append(f"  A. Spawns (claude -p)            {fmt_usd(spawn_cost):>10}   measured    {spawn_note}")
    L.append(f"  B. Interactive main session      {fmt_usd(tr['interactive_usd']):>10}   estimated   token x price, {tr['n_files']} transcript(s)")
    L.append(f"  C. Subagent (Agent tool)                 $?   partial     not isolated from main transcript")
    L.append(f"  D. External compute (Nirvana/Sandbox)    $?   n/a         robot compute, not captured")
    L.append(f"  -> budget ~ {fmt_usd(low)} (measured) ... {fmt_usd(high)} (spawns + interactive est.); +C/+D unmeasured on top")
    L.append("")
    L.append("=== Token usage (interactive, by model) ===")
    for model, tok in sorted(tr["by_model_tokens"].items()):
        L.append(f"  {model:<22} in={tok['input_tokens']:>9}  out={tok['output_tokens']:>8}  "
                 f"cache_w={tok['cache_creation_input_tokens']:>9}  cache_r={tok['cache_read_input_tokens']:>11}")
    L.append("")
    L.append("=== Interaction cost (agent <-> you) ===")
    L.append(f"  your prompts:                     {tr['user_prompts']}")
    L.append(f"  your interrupts:                  {tr['interrupts']}")
    L.append(f"  agent->you asks (AskUserQuestion): {tr['asks']}")
    if tr["classify"]:
        L.append(f"  likely corrections (heuristic, approximate): {tr['corrections']}")
    if tr["span"]:
        L.append(f"\n  span: {tr['span'][0]} ... {tr['span'][1]}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--log", type=Path, default=COST_LOG, help=f"cost log path (default: {COST_LOG})")
    p.add_argument("--days", type=int, default=7, help="window size in days (default: 7)")
    p.add_argument("--since", help="window start as YYYY-MM-DD (overrides --days)")
    p.add_argument("--by", choices=("kind", "tier", "day"), default="kind", help="grouping for summary")
    p.add_argument("--detail", action="store_true", help="print one row per entry instead of summary")
    p.add_argument("--csv", action="store_true", help="emit CSV (with all fields)")
    p.add_argument("--project", help="project dir or cwd-hash under ~/.claude/projects: full-budget interval + interaction cost from session transcripts")
    p.add_argument("--session", help="a single session transcript .jsonl (instead of a whole project)")
    p.add_argument("--classify-corrections", action="store_true", help="heuristically flag likely correction prompts (approximate)")
    args = p.parse_args(argv)

    entries = parse_entries(args.log)

    if args.project or args.session:
        files = resolve_project(args.session or args.project)
        if not files:
            print(f"(no transcripts found for {args.session or args.project})")
            return 0
        tr = parse_transcripts(files, classify=args.classify_corrections)
        spawn_cost, note = 0.0, "spawn log empty"
        spawns_all = split_events(entries)[0]
        if tr["span"] and spawns_all:
            try:
                lo, hi = parse_ts(tr["span"][0]), parse_ts(tr["span"][1])
                sp = [e for e in spawns_all
                      if e.get("ts") and lo <= parse_ts(e["ts"]) <= hi]
                spawn_cost = sum((e.get("cost_usd") or 0) for e in sp)
                note = f"{len(sp)} spawn(s) in transcript time-span (window-matched, not tag-isolated)"
            except ValueError:
                pass
        print(f"cost-report: project budget    transcripts: {tr['n_files']}    spawn-log: {args.log}")
        print()
        print(budget_report(tr, spawn_cost, note))
        return 0

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
