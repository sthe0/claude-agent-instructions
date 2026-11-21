"""agent-report.py host-comparison: task figures per coordination host and task weight.

Difficulty removed: tasks run on different hosts (claude, opencode, cursor) land in the same
ledgers, so a host's convergence, quality, cost and active time could not be set against
another's. A task is an engine session. Its group comes from the root sessions that worked on it,
never from its spawns or the host that finished it:

1. unknown when any root session of it is unstamped (a null host in the engine state's
   `host_binds` or in the quality row's `hosts`) or any row of it carries no host;
2. otherwise, from the engine state's `host_binds` when the state still exists, else from the
   host stamps of the task's quality rows;
3. one host gives that host's group, claude plus opencode gives `mixed` (counted for neither
   host), and any cursor puts the task in the separate `cursor` group.

A cost that is not wholly recorded stays out of every sum and mean; it is never a zero. Rows
written before the stamps existed are never back-filled: they read as unknown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from lib import config_root
from lib.agent_report.common import (
    SPAWN_COST_LOG, TASK_QUALITY_LOG, parse_ts_utc, read_jsonl_rows,
)

UNKNOWN = "unknown"
MIXED = "mixed"
CURSOR = "cursor"
ABANDON_KEY = "telemetry-abandon-window-days"
HOST_BINDS_SCHEMA = 63
CONFIG_ROW = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")


def parse_since(text: str) -> dt.datetime:
    """A bare date (read as midnight UTC) or a full ISO-8601 timestamp, with or without `Z`."""
    return parse_ts_utc(text)


def abandon_window_days(config_path: "Path | None" = None) -> float:
    """`telemetry-abandon-window-days` from config.md, the one definition of the window."""
    path = config_path or config_root.agent_home() / "config.md"
    for line in path.read_text(encoding="utf-8").splitlines():
        m = CONFIG_ROW.match(line)
        if m and m.group(1) == ABANDON_KEY:
            return float(m.group(2))
    raise ValueError(f"{path} has no {ABANDON_KEY} row")


def _number(value) -> "float | None":
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _mean(values: list) -> "float | None":
    return round(sum(values) / len(values), 4) if values else None


def _ts(row: dict) -> "dt.datetime | None":
    try:
        return parse_ts_utc(row.get("ts") or "")
    except ValueError:
        return None


def load_states(state_dir: "Path | None") -> dict:
    """Engine sessions by id: host binds, weight, whether resolved, last write time. A linked
    child's state is not a task. A state written before host_binds existed that was bound to a
    host reads as one unstamped root session, as SessionState.from_dict reads it."""
    states: dict = {}
    if state_dir is None or not state_dir.is_dir():
        return states
    for path in sorted(state_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            touched = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("parent_link"):
            continue
        binds = data.get("host_binds")
        if not isinstance(binds, list) or not binds:
            stale = int(data.get("schema_version") or 0) < HOST_BINDS_SCHEMA
            binds = [{"host": None}] if data.get("runtime_host") and stale else None
        session = data.get("session_id") or path.stem
        states[session] = {
            "binds": [b.get("host") for b in binds if isinstance(b, dict)] if binds else None,
            "weight_class": data.get("weight_class"),
            "resolved": any(h.get("event") == "resolve" for h in data.get("history") or [] if isinstance(h, dict)),
            "touched": touched,
        }
    return states


def group_of(hosts: "list | None", unstamped_row: bool) -> str:
    """The task's group from the hosts of its root sessions (None entries are unstamped)."""
    if unstamped_row or not hosts or any(not h for h in hosts):
        return UNKNOWN
    distinct = set(hosts)
    if CURSOR in distinct:
        return CURSOR
    return hosts[0] if len(distinct) == 1 else MIXED


def _row_hosts(quality: list[dict]) -> "list | None":
    hosts: list = []
    for row in quality:
        listed = row.get("hosts")
        hosts += listed if isinstance(listed, list) and listed else [row.get("host")]
    return hosts or None


def _cost(rows: list[dict], spawns: list[dict]) -> "float | None":
    """The task's cost when every part of it is priced, else None: never a partial sum."""
    row = rows[-1]
    total = _number(row.get("total_cost_usd"))
    attributed = row.get("attributed_stages")
    if total is None or any(_number(s.get("cost_usd")) is None for s in spawns):
        return None
    if isinstance(attributed, int) and attributed != (row.get("spawn_count") or 0):
        return None
    return total


def _figures(tasks: list[dict]) -> dict:
    resolved = [t for t in tasks if t["row"] is not None]
    abandoned = len(tasks) - len(resolved)

    def over(key):
        return [v for v in (_number(t["row"].get(key)) for t in resolved) if v is not None]

    costs = [t["cost"] for t in resolved if t["cost"] is not None]
    fires = [float(len(t["row"].get("effort_fires") or [])) for t in resolved]
    active = [v for v in (_number((t["row"].get("effort_actual") or {}).get("wall_clock")) for t in resolved)
              if v is not None]
    quality, replans, rounds, difficulties = over("quality"), over("n_replans"), over("n_review_rounds"), \
        over("n_difficulty_records")
    return {
        "tasks": len(tasks),
        "resolved_tasks": len(resolved),
        "abandoned_tasks": abandoned,
        "share_resolved": round(len(resolved) / len(tasks), 4) if tasks else None,
        "total_cost_usd": round(sum(costs), 4) if costs else None,
        "mean_cost_usd": _mean(costs),
        "cost_tasks": len(costs),
        "cost_unknown_tasks": len(resolved) - len(costs),
        "mean_quality": _mean(quality),
        "quality_tasks": len(quality),
        "mean_replans": _mean(replans),
        "replans_tasks": len(replans),
        "mean_review_rounds": _mean(rounds),
        "review_rounds_tasks": len(rounds),
        "mean_difficulties": _mean(difficulties),
        "difficulties_tasks": len(difficulties),
        "mean_effort_fires": _mean(fires),
        "effort_fires_tasks": len(fires),
        "mean_active_minutes": _mean(active),
        "active_time_tasks": len(active),
    }


def summarize(quality_rows: list[dict], spawn_rows: list[dict], since: "dt.datetime | None" = None,
              *, states: "dict | None" = None, abandon_days: float = 7.0,
              now: "dt.datetime | None" = None) -> dict:
    states = states or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    by_session: dict = defaultdict(list)
    for row in quality_rows:
        if isinstance(row, dict) and row.get("session"):
            by_session[row["session"]].append(row)
    spawns_of: dict = defaultdict(list)
    orphans: list = []
    for row in spawn_rows:
        if not isinstance(row, dict) or row.get("event", "spawn") != "spawn":
            continue
        (spawns_of[row["spawner_session"]] if row.get("spawner_session") else orphans).append(row)

    models: dict = defaultdict(list)

    def note_models(group, *rows):
        for row in rows:
            for m in row.get("models") or []:
                if m not in models[group]:
                    models[group].append(m)

    grouped: dict = defaultdict(lambda: defaultdict(list))
    task_group: dict = {}
    for session in sorted(set(by_session) | set(states)):
        rows = sorted(by_session.get(session, []), key=lambda r: r.get("ts") or "")
        state = states.get(session)
        last = _ts(rows[-1]) if rows else (state["touched"] if state else None)
        if rows and since is not None and (last is None or last < since):
            continue
        if not rows:
            if state is None or state["resolved"] or not state["weight_class"] or last is None:
                continue
            if (since is not None and last < since) or (now - last).days < abandon_days:
                continue
        spawns = spawns_of.get(session, [])
        binds = state["binds"] if state and state["binds"] else _row_hosts(rows)
        unstamped = any(not r.get("host") for r in [*rows, *spawns])
        group = group_of(binds, unstamped)
        task_group[session] = group
        weight = (rows[-1].get("weight_class") if rows else state["weight_class"]) or UNKNOWN
        grouped[group][weight].append({"row": rows[-1] if rows else None,
                                       "cost": _cost(rows, spawns) if rows else None})
        note_models(group, *rows, *spawns)
    spawn_groups: dict = defaultdict(list)
    for session, spawns in spawns_of.items():
        for row in spawns:
            if _ts(row) is not None and (since is None or _ts(row) >= since):
                spawn_groups[task_group.get(session) or row.get("host") or UNKNOWN].append(row)
    for row in orphans:
        if _ts(row) is not None and (since is None or _ts(row) >= since):
            spawn_groups[row.get("host") or UNKNOWN].append(row)
    groups = {}
    for group, by_weight in sorted(grouped.items()):
        groups[group] = {
            "tasks": sum(len(t) for t in by_weight.values()),
            "models": models.get(group, []),
            "by_weight": {w: _figures(t) for w, t in sorted(by_weight.items())},
        }
    spawn_figures = {}
    for group, rows in sorted(spawn_groups.items()):
        costs = [c for c in (_number(r.get("cost_usd")) for r in rows) if c is not None]
        spawn_figures[group] = {
            "spawns": len(rows),
            "cost_known_spawns": len(costs),
            "total_cost_usd": round(sum(costs), 4) if costs else None,
        }
    return {"groups": groups, "spawns": spawn_figures}


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def render_text(report: dict) -> str:
    lines = ["# Host comparison", "", "## Tasks by host group and weight", "",
             "| group | weight | tasks | resolved share | cost USD (mean) | cost tasks | quality | replans | "
             "review rounds | difficulties | effort fires | active min |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for group, data in report["groups"].items():
        for weight, f in data["by_weight"].items():
            lines.append(
                f"| {group} | {weight} | {f['tasks']} | {_cell(f['share_resolved'])} | "
                f"{_cell(f['mean_cost_usd'])} | {f['cost_tasks']} | {_cell(f['mean_quality'])} | "
                f"{_cell(f['mean_replans'])} | {_cell(f['mean_review_rounds'])} | "
                f"{_cell(f['mean_difficulties'])} | {_cell(f['mean_effort_fires'])} | "
                f"{_cell(f['mean_active_minutes'])} |")
    lines += ["", "## Models seen by group", ""]
    lines += [f"- {group}: {', '.join(data['models']) or 'n/a'}" for group, data in report["groups"].items()]
    lines += ["", "## Spawns by group", "", "| group | spawns | cost known | total cost USD |", "|---|---|---|---|"]
    for group, f in report["spawns"].items():
        lines.append(f"| {group} | {f['spawns']} | {f['cost_known_spawns']} | {_cell(f['total_cost_usd'])} |")
    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-report.py host-comparison",
        description="Convergence, quality, cost and active time of tasks per coordination host and "
                    "task weight, with the number of tasks behind each figure. A task with an "
                    "unstamped root session is `unknown`; a task on both claude and opencode is "
                    "`mixed`; cursor is kept apart. An unrecorded cost is n/a (null in --json), never 0.")
    parser.add_argument("--json", action="store_true", help="print the figures as JSON")
    parser.add_argument("--since", metavar="D", help="only rows at or after D: a date (YYYY-MM-DD) "
                        "or a full timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--quality-log", type=Path, default=TASK_QUALITY_LOG, help=argparse.SUPPRESS)
    parser.add_argument("--spawn-log", type=Path, default=SPAWN_COST_LOG, help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        since = parse_since(args.since) if args.since else None
    except ValueError:
        print(f"host-comparison: --since {args.since!r} is neither a date nor a timestamp", file=sys.stderr)
        return 2
    try:
        window = abandon_window_days(args.config)
    except (OSError, ValueError) as exc:
        print(f"host-comparison: {exc}", file=sys.stderr)
        return 2
    state_dir = args.state_dir if args.state_dir is not None else config_root.agentctl_state_dir()
    report = summarize(
        read_jsonl_rows(args.quality_log, warn=False), read_jsonl_rows(args.spawn_log, warn=False), since,
        states=load_states(state_dir), abandon_days=window,
        now=parse_ts_utc(args.now) if args.now else None)
    sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else render_text(report))
    return 0
