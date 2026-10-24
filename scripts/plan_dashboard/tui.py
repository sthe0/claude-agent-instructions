"""Console view of the plan dashboard: everything the screen shows, as pure functions.

``tui_app.py`` maps these onto textual widgets; ``plan-dashboard.py tui --once`` prints
``once()`` with the standard library only. Nothing here reads a clock (``now`` is always an
argument), imports textual or writes a file, so plain pytest pins the whole layout.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

from lib import config_root
from plan_dashboard import model

SESSION_ID_WIDTH = 8
TASK_ID_WIDTH = 24
BAR_WIDTH = 8
DEFAULT_INTERVAL_S = 2.0
OVERVIEW_COLUMNS = ("session", "task", "live", "node", "stage", "diff", "replans", "age", "wall", "cost", "goal")
STATUS_MARKS = {"PASSED": "done", "ACTIVE": "active", "FAILED": "failed", "SKIPPED": "skipped", "PENDING": "pending"}
NOT_COUNTED = "not counted"
TABS = ("tasks", "detail")


class NoSuchSession(Exception):
    def __init__(self, names):
        super().__init__(", ".join(names))
        self.names = list(names)


def missing_sessions(snapshot: dict, sessions) -> list[str]:
    """The ``--session`` names that match no task of ``snapshot``."""
    known = {task["session_id"] for task in snapshot["tasks"]}
    return [s for s in sessions or () if config_root.sanitize_session_id(s) not in known]


def fmt_duration(seconds) -> str:
    """``?`` for an unknown span; a negative one (a start after ``now``, clock skew) reads as zero."""
    if seconds is None:
        return "?"
    minutes, secs = divmod(max(int(seconds), 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def fmt_value(value, estimate, partial=False) -> str:
    """Money as the screen shows it: ``≥`` a lower bound, ``~`` an estimate, ``not counted``
    for a figure that was never measured (never 0, which would read as free)."""
    if value is None:
        return NOT_COUNTED
    prefix = "≥" if partial else ("~" if estimate else "")
    return f"{prefix}${value:.2f}"


def _wall(seconds, estimate) -> str:
    return ("~" if estimate and seconds is not None else "") + fmt_duration(seconds)


def _parse(value):
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _since(now: dt.datetime, moment) -> str | None:
    start = _parse(moment)
    return None if start is None else fmt_duration((now - start).total_seconds())


def one_line(text, width=None) -> str:
    line = " ".join((text or "").split())
    return line if width is None or len(line) <= width else line[: max(width - 1, 0)] + "…"


def cost_text(totals) -> str:
    if not totals:
        return "-"
    return fmt_value(totals["cost_usd"], totals["estimate"]["cost_usd"], partial=totals["partial"])


def _stage_progress(done, total) -> str:
    if not total:
        return "no plan"
    done = done or 0
    filled = min(max(round(BAR_WIDTH * done / total), 0), BAR_WIDTH)
    return f"stage {done}/{total} [{'#' * filled}{'-' * (BAR_WIDTH - filled)}]"


def _difficulty(task) -> str:
    """The count of every difficulty the task has had, behind ``!`` while one is open."""
    counts = task["difficulties"]
    count = counts["open"] + counts["stacked"] + counts["closed"]
    return f"!{count}" if counts["open"] else str(count)


def by_recency_live_first(tasks) -> list[dict]:
    recent = sorted(tasks, key=lambda t: (t["last_activity"] or "", t["session_id"]), reverse=True)
    return sorted(recent, key=lambda t: t["liveness"] != "active")


def overview_header(snapshot: dict, now: dt.datetime) -> str:
    """The line above the table: host, time and how many listed tasks are active, idle, in all."""
    tasks = snapshot["tasks"]
    counts = {band: sum(1 for t in tasks if t["liveness"] == band) for band in ("active", "idle")}
    clock = f"{now:%Y-%m-%d %H:%M:%S %Z}".strip()
    return f"{snapshot['hostname']}  {clock}  {counts['active']} active  {counts['idle']} idle  {len(tasks)} total"


def overview_rows(snapshot: dict, now: dt.datetime) -> list[dict]:
    """One row per task, active first and then the most recent: ``session_id`` and the ``cells``
    of ``OVERVIEW_COLUMNS``."""
    rows = []
    for task in by_recency_live_first(snapshot["tasks"]):
        sid = task["session_id"]
        since = _since(now, task["last_activity"])
        age = "?" if since is None else f"{since} ago"
        if not task["readable"]:
            cells = (sid[:SESSION_ID_WIDTH], "-", task["liveness"], "?", "stage ?/?", "-", "-", age, "-", "-",
                     f"unreadable: {task['reason']}")
        else:
            totals = task["totals"]
            cells = (sid[:SESSION_ID_WIDTH], one_line(task["task_id"], TASK_ID_WIDTH) or "-", task["liveness"],
                     task["node"] or "?", _stage_progress(task["stages_done"], task["stages_total"]),
                     _difficulty(task), str(task["replans"]), age,
                     _wall(totals["wall_clock_s"], totals["estimate"]["wall_clock_s"]), cost_text(totals),
                     one_line(task["goal"]))
        rows.append({"session_id": sid, "cells": cells})
    return rows


def _node(label: str, children=(), key: str | None = None) -> dict:
    """``key`` names the node among its siblings; unlike the label it does not change as the task runs."""
    return {"label": label, "children": list(children), "key": label if key is None else key}


def _phase_node(item: dict, now: dt.datetime) -> dict:
    if item["end"] is None and item["start"] is not None:
        wall = f"open ({_since(now, item['start']) or '?'} so far)"
    else:
        wall = _wall(item["wall_clock_s"], item["estimate"]["wall_clock_s"])
    spawn = "spawn not attributed" if item["spawn_cost_usd"] is None else f"spawn {fmt_value(item['spawn_cost_usd'], False)}"
    root = fmt_value(item["root_cost_usd"], item["estimate"]["root_cost_usd"])
    attempts = f"  attempts {item['attempts']}" if "attempts" in item else ""
    label = f"{item['label']}: wall {wall}  {spawn}  root {root}{attempts}"
    return _node(label, [_phase_node(child, now) for child in item["children"]], key=f"phase:{item['label']}")


def _difficulty_node(record: dict, state: str, position: int) -> dict:
    return _node(f"{state}: {record.get('title') or '?'}", key=f"difficulty:{position}")


def detail_tree(detail: dict, now: dt.datetime) -> list[dict]:
    """The Detail tab as ``{"label", "children"}`` nodes, in display order."""
    if not detail["readable"]:
        return [_node(f"unreadable: {detail['reason']}")]
    totals = detail["totals"]
    stages = [_node(f"[{STATUS_MARKS.get(s['status'], '?')}] {s['index']}. {s['title']}"
                    f" ({s['actor'] or '?'}, {s['criterion_type'] or '?'})", key=f"stage:{s['index']}")
              for s in detail["stages"]]
    difficulties = [_difficulty_node(d, state, n) for n, (d, state) in enumerate(
        [(d, "open") for d in detail["open_difficulties"]] + [(d, "closed") for d in detail["closed_difficulties"]])]
    children = [_node(f"{c['session_id'][:SESSION_ID_WIDTH]} {c['node'] or '?'} (stage {c['stage_index']}) "
                      f"{one_line(c['goal'], 60)}", key=f"child:{c['session_id']}") for c in detail["children"]]
    history = [_node(f"{e.get('ts', '?')} {e.get('event', '?')}", key=f"event:{n}") for n, e in enumerate(detail["history"])]
    tree = [
        _node(f"{detail['session_id'][:SESSION_ID_WIDTH]} {detail['node'] or '?'}  {one_line(detail['goal'], 100)}",
              key="head"),
        _node(f"totals: wall {_wall(totals['wall_clock_s'], totals['estimate']['wall_clock_s'])}  "
              f"cost {cost_text(totals)}  root {fmt_value(totals['root_cost_usd'], totals['estimate']['root_cost_usd'])}",
              key="totals"),
        _node("stages", stages),
        _node("phases", [_phase_node(item, now) for item in detail["phases"]]),
    ]
    for title, nodes in (("difficulties", difficulties), ("child sessions", children), ("recent events", history)):
        if nodes:
            tree.append(_node(title, nodes))
    return tree


def missing_detail_tree() -> list[dict]:
    """The Detail tab for a selected task whose state file is gone."""
    return [_node("no such session")]


def render_lines(tree: list[dict], indent: int = 0) -> list[str]:
    lines = []
    for node in tree:
        lines.append(" " * indent + node["label"])
        lines += render_lines(node["children"], indent + 2)
    return lines


TREE_KEYS = frozenset({"up", "down", "j", "k", "enter", "space", "left", "right"})


def key_owner(tab: str, key: str) -> str:
    """``tree`` for a key the Detail tab's tree handles itself, ``global`` for one the view handles."""
    return "tree" if tab == "detail" and key in TREE_KEYS else "global"


def tree_action(key: str, *, expandable: bool, expanded: bool, has_parent: bool) -> str | None:
    """What a tree key does to the node under the cursor: ``down``, ``up``, ``toggle``, ``expand``,
    ``collapse`` or ``parent``; None when it does nothing there."""
    if key in ("down", "j"):
        return "down"
    if key in ("up", "k"):
        return "up"
    if key in ("enter", "space"):
        return "toggle" if expandable else None
    if key == "right":
        return "expand" if expandable and not expanded else None
    if key == "left":
        return "collapse" if expandable and expanded else "parent" if has_parent else None
    return None


@dataclasses.dataclass(frozen=True)
class TreeState:
    """What the user did to the Detail tree: the key paths of the nodes opened and the one under the
    cursor, kept for one task. Key paths survive a rebuild; labels (live times, marks) do not."""
    task: str | None = None
    expanded: frozenset[tuple[str, ...]] = frozenset()
    cursor: tuple[str, ...] = ()


def state_for_task(state: TreeState, task: str | None) -> TreeState:
    """``state`` while the same task stays open; a fresh, collapsed one once another task opens."""
    return state if state.task == task else TreeState(task=task)


def key_paths(nodes: list[dict], prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    paths = set()
    for node in nodes:
        path = prefix + (node["key"],)
        paths.add(path)
        paths |= key_paths(node["children"], path)
    return paths


def surviving_path(nodes: list[dict], path: tuple[str, ...]) -> tuple[str, ...]:
    """The longest prefix of ``path`` that is still a node of ``nodes`` (the root is the empty path)."""
    known = key_paths(nodes)
    while path and path not in known:
        path = path[:-1]
    return path


@dataclasses.dataclass(frozen=True)
class View:
    """What the screen is showing; ``session_ids`` are the rows of the Tasks tab in order.
    ``refresh`` and ``quit`` are requests the last key made, for the app to act on."""
    tab: str = "tasks"
    cursor: int = 0
    session_ids: tuple[str, ...] = ()
    selected: str | None = None
    include_all: bool = False
    refresh: bool = False
    quit: bool = False


def handle_key(view: View, key: str) -> View:
    view = dataclasses.replace(view, refresh=False, quit=False)
    last = len(view.session_ids) - 1
    if key in ("down", "j") and view.tab == "tasks":
        return dataclasses.replace(view, cursor=min(view.cursor + 1, max(last, 0)))
    if key in ("up", "k") and view.tab == "tasks":
        return dataclasses.replace(view, cursor=max(view.cursor - 1, 0))
    if key == "enter" and view.tab == "tasks" and 0 <= view.cursor <= last:
        return dataclasses.replace(view, tab="detail", selected=view.session_ids[view.cursor], refresh=True)
    if key in ("escape", "backspace"):
        return dataclasses.replace(view, tab="tasks")
    if key == "tab":
        other = TABS[(TABS.index(view.tab) + 1) % len(TABS)]
        return dataclasses.replace(view, tab=other, refresh=other == "detail" and view.selected is not None)
    if key == "a":
        return dataclasses.replace(view, include_all=not view.include_all, refresh=True)
    if key == "r":
        return dataclasses.replace(view, refresh=True)
    if key == "q":
        return dataclasses.replace(view, quit=True)
    return view


def reconcile(view: View, session_ids) -> View:
    """``view`` over a re-read task list: the cursor stays on the task it was on, wherever the
    re-sort moved it, and is clamped to the last row only when that task is gone."""
    ids = tuple(session_ids)
    on = view.session_ids[view.cursor] if 0 <= view.cursor < len(view.session_ids) else None
    cursor = ids.index(on) if on in ids else min(view.cursor, max(len(ids) - 1, 0))
    return dataclasses.replace(view, session_ids=ids, cursor=cursor)


def due(now: dt.datetime, last_collect: dt.datetime | None, interval: float) -> bool:
    return last_collect is None or (now - last_collect).total_seconds() >= interval


def _table(rows: list[dict], width: int) -> list[str]:
    widths = [max(len(OVERVIEW_COLUMNS[i]), *(len(r["cells"][i]) for r in rows)) for i in range(len(OVERVIEW_COLUMNS) - 1)]
    head = "  ".join(name.ljust(widths[i]) for i, name in enumerate(OVERVIEW_COLUMNS[:-1]))
    lines = [f"{head}  {OVERVIEW_COLUMNS[-1]}".rstrip()]
    room = max(width - len(head) - 2, 10)
    for row in rows:
        left = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row["cells"][:-1]))
        lines.append(f"{left}  {one_line(row['cells'][-1], room)}".rstrip())
    return lines


def once(state_root, *, window_hours, include_all, sessions, cost_log, transcripts_root, now, width) -> str:
    """The plain-text console view: the task table, then each ``--session``'s detail tree.
    Raises ``NoSuchSession`` for a ``--session`` that names no task."""
    snapshot = model.collect(state_root, now=now, window_hours=window_hours, include_all=include_all,
                             sessions=sessions, cost_log=cost_log)
    missing = missing_sessions(snapshot, sessions)
    if missing:
        raise NoSuchSession(missing)
    rows = overview_rows(snapshot, now)
    lines = [overview_header(snapshot, now), *(_table(rows, width) if rows else ["no live tasks"])]
    pinned = {config_root.sanitize_session_id(s) for s in sessions or ()}
    shown = [task for task in snapshot["tasks"] if task["session_id"] in pinned]
    children = model.child_index(state_root) if shown else None
    for task in shown:
        detail = model.detail(state_root, task["session_id"], cost_log=cost_log,
                              transcripts_root=transcripts_root, children=children)
        lines += ["", *render_lines(detail_tree(detail, now))]
    return "\n".join(lines)


def launch_argv(args) -> list[str]:
    """The ``uv run`` command line that starts the textual app with the same selection."""
    app = Path(__file__).resolve().with_name("tui_app.py")
    argv = ["uv", "run", str(app), "--state-root", str(args.state_root), "--window-hours", str(args.window_hours),
            "--interval", str(args.interval), "--cost-log", str(args.cost_log)]
    if args.include_all:
        argv.append("--all")
    for session in args.sessions:
        argv += ["--session", session]
    if args.transcripts_root is not None:
        argv += ["--transcripts-root", str(args.transcripts_root)]
    return argv
