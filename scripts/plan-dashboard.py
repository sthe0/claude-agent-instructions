#!/usr/bin/env python3
"""Plan dashboard command line: read-only views of the live agentctl tasks.

Subcommand ``snapshot`` prints every selected task with its totals as JSON or text;
``--detail`` adds each task's stages, difficulties, history tail and phase tree; ``--attention``
adds each task's waiting state and a top-level ``sessions`` list; ``--chat ID`` prints the chat
tail of a task or of a listed session. Subcommand ``locate`` selects the machine-local
session-locator plugin (``--plugin-dir``, ``--locator``, also accepted by ``snapshot``, ``tui`` and
``web``): ``--health`` runs its local health check (exit 0 healthy, 1 unhealthy, 3 none or several
without ``--locator``), ``--session ID`` reports what it knows of that task's session. Subcommand
``tui`` is the console view: interactive through ``uv run`` (exit 4 without uv), or plain
text with ``--once``. Subcommand ``web`` serves the read-only page on the loopback address
(``--port``; exit 5 when it cannot listen; ``--self-test`` probes a throwaway server).
Subcommand ``publish`` keeps one status message per machine up to date through the
machine-local publisher plugin: ``--dry-run`` prints the digest, ``--check-publisher`` runs the
plugin's local health check, ``--once`` and ``--watch`` send (exit 6: no usable plugin; exit 1:
a failed send, repeated failures in ``--watch``, a busy lock or an unsavable state). Subcommand
``bench`` times the console's reads on the live state against budgets (``--budget-live-table`` and
the like; exit 1 when any read is over, 2 for a ``--session`` it cannot find). The shared
options (state root, window, selection, ledger, transcripts) come from one parent parser so
later views reuse them. Exit code 3: a ``--session`` that names no state file, in ``snapshot``,
``tui --once`` and ``publish`` (the interactive ``tui`` opens with an empty list instead).
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
from plan_dashboard import attention, bench, locate, model, publish, tui, web  # noqa: E402

EXIT_NO_SUCH_SESSION = 3
EXIT_NO_LOCATOR = 3
EXIT_NEEDS_UV = 4
EXIT_CANNOT_LISTEN = 5
EXIT_PUBLISHER = 6
DEFAULT_PORT = 8765
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


def locator_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plugin-dir", type=Path, default=None)
    parser.add_argument("--locator", default=None)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plan-dashboard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot", parents=[shared_parser(), locator_parser()])
    snapshot.add_argument("--format", choices=("json", "text"), default="json")
    snapshot.add_argument("--detail", action="store_true")
    snapshot.add_argument("--attention", action="store_true")
    snapshot.add_argument("--chat", metavar="ID", default=None)
    console = sub.add_parser("tui", parents=[shared_parser(), locator_parser()])
    console.add_argument("--once", action="store_true")
    console.add_argument("--interval", type=float, default=tui.DEFAULT_INTERVAL_S)
    page = sub.add_parser("web", parents=[shared_parser(), locator_parser()])
    page.add_argument("--port", type=int, default=DEFAULT_PORT)
    page.add_argument("--interval", type=float, default=web.DEFAULT_INTERVAL_S)
    page.add_argument("--self-test", action="store_true")
    where = sub.add_parser("locate", parents=[shared_parser(), locator_parser()])
    where.add_argument("--health", action="store_true")
    pub = sub.add_parser("publish", parents=[shared_parser()])
    mode = pub.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check-publisher", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    pub.add_argument("--plugin-dir", type=Path, default=None)
    pub.add_argument("--publish-state", type=Path, default=None)
    pub.add_argument("--interval", type=float, default=publish.DEFAULT_INTERVAL_S)
    pub.add_argument("--min-interval", type=float, default=publish.MIN_INTERVAL_S)
    timing = sub.add_parser("bench", parents=[shared_parser(), locator_parser()])
    for name, budget in bench.BUDGETS.items():
        timing.add_argument(bench.budget_flag(name), type=float, default=budget, dest=bench.budget_dest(name))
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
                locator=locate.select_or_none(args.plugin_dir, args.locator),
            )
        except tui.NoSuchSession as exc:
            return no_such_session(exc.names)
        print(text)
        return 0
    if shutil.which("uv") is None:
        print("interactive console needs uv; use 'plan-dashboard.py tui --once'", file=sys.stderr)
        return EXIT_NEEDS_UV
    return subprocess.call(tui.launch_argv(args))


def run_web(args) -> int:
    options = dict(window_hours=args.window_hours, include_all=args.include_all, sessions=args.sessions,
                   cost_log=args.cost_log, transcripts_root=args.transcripts_root,
                   locator=locate.select_or_none(args.plugin_dir, args.locator))

    def announce(server):
        print(f"plan dashboard on http://{web.LOOPBACK}:{server.port}/ (loopback only; "
              f"from another machine: ssh -L {server.port}:{web.LOOPBACK}:{server.port} <host>)", flush=True)

    if args.self_test:
        failures = web.self_test(args.state_root, **options)
        print("web self-test: " + ("ok" if not failures else "failed: " + ", ".join(failures)))
        return 1 if failures else 0
    try:
        web.serve(args.state_root, web.LOOPBACK, args.port, interval=args.interval, ready=announce, **options)
    except OSError as exc:
        print(f"plan-dashboard: cannot listen on {web.LOOPBACK}:{args.port}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_LISTEN
    except KeyboardInterrupt:
        pass
    return 0


def run_locate(args) -> int:
    try:
        locator = locate.select(args.plugin_dir, args.locator)
    except locate.LocatorError as exc:
        print(f"plan-dashboard: {exc}", file=sys.stderr)
        return EXIT_NO_LOCATOR
    name = locate.name_of(locator)
    if args.health:
        ok, detail = locate.check_health(locator)
        print(f"locator {name}: {'ok' if ok else 'unhealthy'}{': ' + detail if detail else ''}")
        return 0 if ok else 1
    print(f"locator: {name}")
    print("hooks: " + ", ".join(locate.defined_hooks(locator)))
    for session in args.sessions:
        harness, path, _ = attention.bind_session(session, locator, args.transcripts_root)
        waiting = attention.task_overlay(session, locator, args.transcripts_root)["waiting"]
        target = locate.jump_target(locator, harness) if harness else None
        print(f"session {session}")
        print(f"  harness session: {harness or 'none'}")
        print(f"  transcript: {'found' if path else 'not found'}")
        print(f"  waiting: {waiting or 'unknown'}")
        print(f"  jump: {target['label'] + ' (return: ' + target['return_hint'] + ')' if target else 'no jump target'}")
    return 0


def add_attention(snapshot: dict, args) -> None:
    locator = locate.select_or_none(args.plugin_dir, args.locator)
    overlays, rows = attention.collect([t["session_id"] for t in snapshot["tasks"]], locator, args.transcripts_root)
    for task in snapshot["tasks"]:
        task["attention"] = overlays[task["session_id"]]
    snapshot["sessions"] = rows


def run_chat(snapshot: dict, args) -> int:
    locator = locate.select_or_none(args.plugin_dir, args.locator)
    if any(t["session_id"] == args.chat for t in snapshot["tasks"]):
        print(model.to_json(attention.task_chat(args.chat, locator, args.transcripts_root)))
        return 0
    _, rows = attention.collect([t["session_id"] for t in snapshot["tasks"]], locator, args.transcripts_root)
    if any(row["harness_session"] == args.chat for row in rows):
        print(model.to_json(attention.chat_tail(args.chat, locator, args.transcripts_root)))
        return 0
    return no_such_session([args.chat])


def run_publish(args) -> int:
    options = dict(window_hours=args.window_hours, include_all=args.include_all, sessions=args.sessions,
                   cost_log=args.cost_log)
    if args.dry_run or args.once or args.watch:
        snapshot = model.collect(args.state_root, now=dt.datetime.now(dt.timezone.utc), **options)
        missing = tui.missing_sessions(snapshot, args.sessions)
        if missing:
            return no_such_session(missing)
        if args.dry_run:
            print(publish.digest(snapshot))
            return 0
    try:
        plugin = publish.select_publisher(args.plugin_dir)
    except publish.PublisherError as exc:
        print(f"plan-dashboard: {exc}", file=sys.stderr)
        return EXIT_PUBLISHER
    name = getattr(plugin, "NAME", "?")
    if args.check_publisher:
        ok, detail = publish.check_health(plugin)
        print(f"publisher {name}: {'ok' if ok else 'unhealthy'}{': ' + detail if detail else ''}")
        return 0 if ok else 1
    state_path = args.publish_state or publish.default_state_path()
    common = dict(min_interval_s=args.min_interval, **options)
    if args.once:
        try:
            print(publish.run_once(args.state_root, plugin, state_path, now=dt.datetime.now(dt.timezone.utc), **common))
        except Exception as exc:
            print(f"plan-dashboard: publish failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        publish.watch(args.state_root, plugin, state_path, interval=args.interval,
                      report=lambda outcome: print(outcome, flush=True), **common)
    except (publish.PublishAborted, publish.StatePersistError) as exc:
        print(f"plan-dashboard: publish stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def run_bench(args) -> int:
    return bench.run(
        args.state_root, session=args.sessions[-1] if args.sessions else None, window_hours=args.window_hours,
        cost_log=args.cost_log, transcripts_root=args.transcripts_root,
        locator=locate.select_or_none(args.plugin_dir, args.locator),
        budgets={name: getattr(args, bench.budget_dest(name)) for name in bench.BUDGETS},
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "bench":
        return run_bench(args)
    if args.command == "tui":
        return run_tui(args)
    if args.command == "web":
        return run_web(args)
    if args.command == "publish":
        return run_publish(args)
    if args.command == "locate":
        return run_locate(args)
    now = dt.datetime.now(dt.timezone.utc)
    snapshot = model.collect(
        args.state_root, now=now, window_hours=args.window_hours, include_all=args.include_all,
        sessions=args.sessions, cost_log=args.cost_log,
    )
    missing = tui.missing_sessions(snapshot, args.sessions)
    if missing:
        return no_such_session(missing)
    if args.chat is not None:
        return run_chat(snapshot, args)
    if args.attention:
        add_attention(snapshot, args)
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
