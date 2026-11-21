"""agent-report.py host-comparison: resolved-task and spawn figures per coordination host.

Difficulty removed: tasks run on different hosts (claude, cursor, opencode) land in the same
ledgers, so a host's quality, cost and effort could not be set against another's. Each task is
attributed to one host from the stamp on its quality row, and the figures are split by weight
class so a host that ran only heavy tasks is not read as costlier than one that ran light ones.

A task whose host is not known — a row from before stamps existed, or a task whose first stretch
was bound before they did — is counted under `unknown`, never under the host that finished it.
A cost that is not recorded stays null and is left out of every sum and mean; it is not a zero.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

from lib.agent_report.common import (
    SPAWN_COST_LOG, TASK_QUALITY_LOG, parse_ts_utc, read_jsonl_rows,
)

UNKNOWN = "unknown"
MIXED = "mixed"


def parse_since(text: str) -> dt.datetime:
    """A bare date (read as midnight UTC) or a full ISO-8601 timestamp, with or without `Z`."""
    return parse_ts_utc(text)


def task_host(row: dict) -> str:
    """The one host a task ran on; `unknown` if any stretch of it has no stamp, `mixed` if the
    stamped stretches name different hosts."""
    hosts = row.get("hosts")
    if isinstance(hosts, list) and hosts:
        if any(not h for h in hosts):
            return UNKNOWN
        distinct = set(hosts)
        return hosts[0] if len(distinct) == 1 else MIXED
    return row.get("host") or UNKNOWN


def _in_window(row: dict, since: "dt.datetime | None") -> bool:
    if since is None:
        return True
    try:
        return parse_ts_utc(row.get("ts") or "") >= since
    except ValueError:
        return False


def _mean(values: list) -> "float | None":
    return round(sum(values) / len(values), 4) if values else None


def _number(value) -> "float | None":
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def summarize(quality_rows: list[dict], spawn_rows: list[dict], since: "dt.datetime | None" = None) -> dict:
    tasks: dict = defaultdict(list)
    for row in quality_rows:
        if isinstance(row, dict) and _in_window(row, since):
            tasks[(task_host(row), row.get("weight_class") or UNKNOWN)].append(row)
    task_figures = []
    for (host, weight), rows in sorted(tasks.items()):
        costs = [c for c in (_number(r.get("total_cost_usd")) for r in rows) if c is not None]
        quality = [q for q in (_number(r.get("quality")) for r in rows) if q is not None]
        replans = [n for n in (_number(r.get("n_replans")) for r in rows) if n is not None]
        task_figures.append({
            "host": host,
            "weight_class": weight,
            "tasks": len(rows),
            "cost_known_tasks": len(costs),
            "total_cost_usd": round(sum(costs), 4) if costs else None,
            "mean_cost_usd": _mean(costs),
            "rated_tasks": len(quality),
            "mean_quality": _mean(quality),
            "mean_replans": _mean(replans),
        })
    spawns: dict = defaultdict(list)
    for row in spawn_rows:
        if isinstance(row, dict) and row.get("event", "spawn") == "spawn" and _in_window(row, since):
            spawns[row.get("host") or UNKNOWN].append(row)
    spawn_figures = []
    for host, rows in sorted(spawns.items()):
        costs = [c for c in (_number(r.get("cost_usd")) for r in rows) if c is not None]
        spawn_figures.append({
            "host": host,
            "spawns": len(rows),
            "cost_known_spawns": len(costs),
            "total_cost_usd": round(sum(costs), 4) if costs else None,
            "mean_cost_usd": _mean(costs),
        })
    return {"tasks": task_figures, "spawns": spawn_figures}


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def render_text(report: dict) -> str:
    lines = ["# Host comparison", "", "## Resolved tasks by host and weight class", "",
             "| host | weight | tasks | cost known | total cost USD | mean cost USD | rated | mean quality | mean replans |",
             "|---|---|---|---|---|---|---|---|---|"]
    for f in report["tasks"]:
        lines.append(
            f"| {f['host']} | {f['weight_class']} | {f['tasks']} | {f['cost_known_tasks']} | "
            f"{_cell(f['total_cost_usd'])} | {_cell(f['mean_cost_usd'])} | {f['rated_tasks']} | "
            f"{_cell(f['mean_quality'])} | {_cell(f['mean_replans'])} |")
    lines += ["", "## Spawns by host", "",
              "| host | spawns | cost known | total cost USD | mean cost USD |", "|---|---|---|---|---|"]
    for f in report["spawns"]:
        lines.append(f"| {f['host']} | {f['spawns']} | {f['cost_known_spawns']} | "
                     f"{_cell(f['total_cost_usd'])} | {_cell(f['mean_cost_usd'])} |")
    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-report.py host-comparison",
        description="Resolved-task quality, cost and effort, and spawn cost, per coordination host "
                    "and weight class. A row with no host stamp counts as `unknown`; an unrecorded "
                    "cost is n/a (null in --json), never 0.")
    parser.add_argument("--json", action="store_true", help="print the figures as JSON")
    parser.add_argument("--since", metavar="D", help="only rows at or after D: a date (YYYY-MM-DD) "
                        "or a full timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--quality-log", type=Path, default=TASK_QUALITY_LOG, help=argparse.SUPPRESS)
    parser.add_argument("--spawn-log", type=Path, default=SPAWN_COST_LOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        since = parse_since(args.since) if args.since else None
    except ValueError:
        print(f"host-comparison: --since {args.since!r} is neither a date nor a timestamp", file=sys.stderr)
        return 2
    report = summarize(read_jsonl_rows(args.quality_log, warn=False),
                       read_jsonl_rows(args.spawn_log, warn=False), since)
    sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else render_text(report))
    return 0
