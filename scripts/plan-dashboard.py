#!/usr/bin/env python3
"""Plan dashboard command line: read-only views of the live agentctl tasks.

Subcommand ``snapshot`` prints every selected task with its totals as JSON or text;
``--detail`` adds each task's stages, difficulties, history tail and phase tree. Subcommand
``tui`` is the console view: interactive through ``uv run`` (exit 4 without uv), or plain
text with ``--once``. The shared
options (state root, window, selection, ledger, transcripts) come from one parent parser so
later views reuse them. Exit code 3: a ``--session`` that names no state file, in ``snapshot``
and ``tui --once`` (the interactive ``tui`` opens with an empty list instead).
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import cost  # noqa: E402
from lib import config_root  # noqa: E402
from plan_dashboard import model, tui  # noqa: E402

EXIT_NO_SUCH_SESSION = 3
EXIT_NEEDS_UV = 4
GOAL_WIDTH = 100


def shared_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-root", type=Path, default=config_root.agentctl_state_dir())
    parser.add_argument("--window-hours", type=float, default=24)
    parser.add_argument("--all", action="store_true", dest="include_all")
    parser.add_argument("--session", action="append", default=[], dest="sessions")
    parser.add_argument("--cost-log", type=Path, default=cost.COST_LOG)
    parser.add_argument("--transcripts-root", type=Path, default=None)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plan-dashboard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot", parents=[shared_parser()])
    snapshot.add_argument("--format", choices=("json", "text"), default="json")
    snapshot.add_argument("--detail", action="store_true")
    console = sub.add_parser("tui", parents=[shared_parser()])
    console.add_argument("--once", action="store_true")
    console.add_argument("--interval", type=float, default=tui.DEFAULT_INTERVAL_S)
    return parser


def _duration(seconds) -> str:
    if seconds is None:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def _money(value, *, estimate=False, partial=False) -> str:
    prefix = "≥" if partial else ("~" if estimate else "")
    return f"{prefix}${value:.2f}"


def _totals_text(totals: dict) -> str:
    wall = ("~" if totals["estimate"]["wall_clock_s"] else "") + _duration(totals["wall_clock_s"])
    parts = [f"wall {wall}",
             f"cost {_money(totals['cost_usd'], estimate=totals['estimate']['cost_usd'], partial=totals['partial'])}"]
    if totals["root_counted"]:
        parts.append(f"root {_money(totals['root_cost_usd'], estimate=True)}")
    else:
        parts.append("root not counted")
    return "  ".join(parts)


def _goal_text(goal) -> str:
    one_line = " ".join((goal or "").split())
    return one_line if len(one_line) <= GOAL_WIDTH else one_line[: GOAL_WIDTH - 1] + "…"


def _phase_lines(items: list[dict], indent: int) -> list[str]:
    lines = []
    for item in items:
        wall = ("~" if item["estimate"]["wall_clock_s"] else "") + _duration(item["wall_clock_s"])
        root = "root not counted" if item["root_cost_usd"] is None else f"root {_money(item['root_cost_usd'], estimate=True)}"
        spawn = "spawn not attributed" if item["spawn_cost_usd"] is None else f"spawn {_money(item['spawn_cost_usd'])}"
        lines.append(f"{' ' * indent}{item['label']}: wall {wall}  {spawn}  {root}")
        lines += _phase_lines(item["children"], indent + 2)
    return lines


def render_text(snapshot: dict) -> str:
    lines = []
    for task in snapshot["tasks"]:
        head = f"{task['session_id'][:36]}  {task['liveness']:<8}"
        if not task["readable"]:
            lines.append(f"{head}  stage ?/?  unreadable: {task['reason']}")
            continue
        stage = f"stage {task['stages_done']}/{task['stages_total']}"
        node = task["node"] or "?"
        lines.append(f"{head}  {node:<10}  {stage}  {_totals_text(task['totals'])}  {_goal_text(task['goal'])}".rstrip())
        detail = task.get("detail")
        if detail and detail["readable"]:
            lines += _phase_lines(detail["phases"], 4)
    return "\n".join(lines)


def no_such_session(names) -> int:
    print(f"plan-dashboard: no such session: {', '.join(names)}", file=sys.stderr)
    return EXIT_NO_SUCH_SESSION


def run_tui(args) -> int:
    if args.once:
        try:
            text = tui.once(
                args.state_root, window_hours=args.window_hours, include_all=args.include_all,
                sessions=args.sessions, cost_log=args.cost_log, transcripts_root=args.transcripts_root,
                now=dt.datetime.now(dt.timezone.utc), width=shutil.get_terminal_size().columns,
            )
        except tui.NoSuchSession as exc:
            return no_such_session(exc.names)
        print(text)
        return 0
    if shutil.which("uv") is None:
        print("interactive console needs uv; use 'plan-dashboard.py tui --once'", file=sys.stderr)
        return EXIT_NEEDS_UV
    return subprocess.call(tui.launch_argv(args))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tui":
        return run_tui(args)
    now = dt.datetime.now(dt.timezone.utc)
    snapshot = model.collect(
        args.state_root, now=now, window_hours=args.window_hours, include_all=args.include_all,
        sessions=args.sessions, cost_log=args.cost_log,
    )
    missing = tui.missing_sessions(snapshot, args.sessions)
    if missing:
        return no_such_session(missing)
    if args.detail:
        children = model.child_index(args.state_root)
        for task in snapshot["tasks"]:
            task["detail"] = model.detail(
                args.state_root, task["session_id"], cost_log=args.cost_log,
                transcripts_root=args.transcripts_root, children=children,
            )
            if task["detail"]["readable"]:
                task["totals"] = task["detail"].pop("totals")
    print(model.to_json(snapshot) if args.format == "json" else render_text(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
