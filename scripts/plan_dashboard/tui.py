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
from plan_dashboard import attention, model

SESSION_ID_WIDTH = 8
TASK_ID_WIDTH = 24
BAR_WIDTH = 8
DEFAULT_INTERVAL_S = 2.0
OVERVIEW_COLUMNS = ("wait", "session", "task", "live", "node", "stage", "diff", "replans", "age", "wall", "cost",
                    "goal")
STATUS_MARKS = {"PASSED": "done", "ACTIVE": "active", "FAILED": "failed", "SKIPPED": "skipped", "PENDING": "pending"}
NOT_COUNTED = "not counted"
TABS = ("tasks", "detail", "chat")
NO_VALUE = "—"
WAIT_MARKS = {"choice": "?", "permission": "!", "idle": "…"}
WAIT_STYLES = {"choice": "bold yellow", "permission": "bold red", "idle": "cyan"}
NEEDS_YOU = ("choice", "permission")
ROLE_LABELS = {"user": "you", "assistant": "assistant"}


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


def wait_mark(waiting) -> str:
    """The one-character mark of a waiting value; blank for ``none`` and for ``None`` (no transcript)."""
    return WAIT_MARKS.get(waiting, "")


def row_style(waiting, liveness) -> str:
    """A rich style for a row: a session that needs the user stands out whatever its liveness, an idle
    one is quieter, and anything not active fades."""
    style = WAIT_STYLES.get(waiting, "")
    if liveness in ("idle", "stale") and waiting not in NEEDS_YOU:
        style = f"{style} dim".strip()
    return style


def _waiting_of(attention_payload, task_id) -> str | None:
    overlay = ((attention_payload or {}).get("tasks") or {}).get(task_id)
    return overlay.get("waiting") if isinstance(overlay, dict) else None


def _session_rows(attention_payload) -> list[dict]:
    return list((attention_payload or {}).get("sessions") or [])


def waiting_count(snapshot: dict, attention_payload) -> int:
    """How many rows of the table need the user: tasks and session rows whose waiting is a choice or a
    permission prompt. An idle session, a working one and one without a transcript are not counted."""
    tasks = sum(1 for t in snapshot["tasks"] if _waiting_of(attention_payload, t["session_id"]) in NEEDS_YOU)
    return tasks + sum(1 for row in _session_rows(attention_payload) if row.get("waiting") in NEEDS_YOU)


def overview_header(snapshot: dict, now: dt.datetime, attention_payload=None) -> str:
    """The line above the table: host, time, how many listed tasks are active, idle, in all, and
    how many rows wait for the user."""
    tasks = snapshot["tasks"]
    counts = {band: sum(1 for t in tasks if t["liveness"] == band) for band in ("active", "idle")}
    clock = f"{now:%Y-%m-%d %H:%M:%S %Z}".strip()
    return (f"{snapshot['hostname']}  {clock}  {counts['active']} active  {counts['idle']} idle  {len(tasks)} total"
            f"  waiting: {waiting_count(snapshot, attention_payload)}")


def overview_rows(snapshot: dict, now: dt.datetime, attention_payload=None) -> list[dict]:
    """One row per task, active first and then the most recent, then one per session the locator knows
    that no task owns. A row is its ``session_id`` (a session row's harness id), the ``cells`` of
    ``OVERVIEW_COLUMNS``, its ``kind`` (``task`` or ``session``), ``waiting`` and ``style``."""
    rows = []
    for task in by_recency_live_first(snapshot["tasks"]):
        sid = task["session_id"]
        waiting = _waiting_of(attention_payload, sid)
        mark = wait_mark(waiting)
        since = _since(now, task["last_activity"])
        age = "?" if since is None else f"{since} ago"
        if not task["readable"]:
            cells = (mark, sid[:SESSION_ID_WIDTH], "-", task["liveness"], "?", "stage ?/?", "-", "-", age, "-", "-",
                     f"unreadable: {task['reason']}")
        else:
            totals = task["totals"]
            cells = (mark, sid[:SESSION_ID_WIDTH], one_line(task["task_id"], TASK_ID_WIDTH) or "-", task["liveness"],
                     task["node"] or "?", _stage_progress(task["stages_done"], task["stages_total"]),
                     _difficulty(task), str(task["replans"]), age,
                     _wall(totals["wall_clock_s"], totals["estimate"]["wall_clock_s"]), cost_text(totals),
                     one_line(task["goal"]))
        rows.append({"session_id": sid, "cells": cells, "kind": "task", "waiting": waiting,
                     "style": row_style(waiting, task["liveness"])})
    for entry in _session_rows(attention_payload):
        harness, waiting = entry["harness_session"], entry.get("waiting")
        since = _since(now, entry.get("since"))
        cells = (wait_mark(waiting), entry.get("name") or harness, NO_VALUE, NO_VALUE, NO_VALUE, NO_VALUE, NO_VALUE,
                 NO_VALUE, NO_VALUE if since is None else f"{since} ago", NO_VALUE, NO_VALUE,
                 one_line(entry.get("cwd")))
        rows.append({"session_id": harness, "cells": cells, "kind": "session", "waiting": waiting,
                     "style": row_style(waiting, None)})
    return rows


def chat_lines(chat: dict | None) -> list[str]:
    """The Chat tab as plain lines: a title, then each entry as ``you`` / ``assistant`` (with ``[tool]``
    for a tool call) over its text indented two spaces. An ``AskUserQuestion`` entry carries its
    question and option labels as that text."""
    if chat is None:
        return ["no chat selected"]
    lines = [f"chat: {chat.get('harness_session') or 'no session'}"]
    for entry in chat["entries"]:
        tool = f" [{entry['tool']}]" if entry.get("tool") else ""
        lines.append(f"{ROLE_LABELS.get(entry['role'], entry['role'])}{tool}")
        lines += [f"  {line}" for line in entry["text"].splitlines()]
    if not chat["entries"]:
        lines.append("no messages yet")
    return lines


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
CHAT_KEYS = frozenset({"up", "down", "j", "k"})


def key_owner(tab: str, key: str) -> str:
    """``tree`` for a key the Detail tab's tree handles itself, ``chat`` for one the Chat tab scrolls
    with, ``global`` for one the view handles."""
    if tab == "detail" and key in TREE_KEYS:
        return "tree"
    if tab == "chat" and key in CHAT_KEYS:
        return "chat"
    return "global"


def scroll_step(key: str) -> int:
    """Lines a Chat key scrolls: down for ``down`` and ``j``, up for ``up`` and ``k``, nothing else."""
    return {"down": 1, "j": 1, "up": -1, "k": -1}.get(key, 0)


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
    """What the screen is showing; ``session_ids`` are the rows of the Tasks tab in order, ``harness_ids``
    those of them that are session rows (no task). ``chat`` is what the Chat tab shows, ``("task", id)``
    or ``("session", harness id)``. ``refresh`` and ``quit`` are requests the last key made, for the
    app to act on."""
    tab: str = "tasks"
    cursor: int = 0
    session_ids: tuple[str, ...] = ()
    selected: str | None = None
    include_all: bool = False
    refresh: bool = False
    quit: bool = False
    harness_ids: frozenset[str] = frozenset()
    chat: tuple[str, str] | None = None


def open_row(view: View, session_id: str) -> View:
    """``view`` with the row ``session_id`` opened: a task on the Detail tab, a session row on its Chat."""
    if session_id in view.harness_ids:
        return dataclasses.replace(view, tab="chat", chat=("session", session_id), refresh=True)
    return dataclasses.replace(view, tab="detail", selected=session_id, chat=("task", session_id), refresh=True)


def handle_key(view: View, key: str) -> View:
    view = dataclasses.replace(view, refresh=False, quit=False)
    last = len(view.session_ids) - 1
    if key in ("down", "j") and view.tab == "tasks":
        return dataclasses.replace(view, cursor=min(view.cursor + 1, max(last, 0)))
    if key in ("up", "k") and view.tab == "tasks":
        return dataclasses.replace(view, cursor=max(view.cursor - 1, 0))
    if key == "enter" and view.tab == "tasks" and 0 <= view.cursor <= last:
        return open_row(view, view.session_ids[view.cursor])
    if key in ("escape", "backspace"):
        return dataclasses.replace(view, tab="tasks")
    if key == "tab":
        other = TABS[(TABS.index(view.tab) + 1) % len(TABS)]
        wanted = view.selected if other == "detail" else view.chat if other == "chat" else None
        return dataclasses.replace(view, tab=other, refresh=wanted is not None)
    if key == "a":
        return dataclasses.replace(view, include_all=not view.include_all, refresh=True)
    if key == "r":
        return dataclasses.replace(view, refresh=True)
    if key == "q":
        return dataclasses.replace(view, quit=True)
    return view


def reconcile(view: View, session_ids, harness_ids=()) -> View:
    """``view`` over a re-read row list: the cursor stays on the row it was on, wherever the
    re-sort moved it, and is clamped to the last row only when that row is gone."""
    ids = tuple(session_ids)
    on = view.session_ids[view.cursor] if 0 <= view.cursor < len(view.session_ids) else None
    cursor = ids.index(on) if on in ids else min(view.cursor, max(len(ids) - 1, 0))
    return dataclasses.replace(view, session_ids=ids, cursor=cursor, harness_ids=frozenset(harness_ids))


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


def read_attention(snapshot: dict, locator, transcripts_root) -> dict:
    """The attention overlay payload for the listed tasks: what the Tasks tab marks and counts."""
    return attention.event_payload(*attention.collect([t["session_id"] for t in snapshot["tasks"]], locator,
                                                      transcripts_root))


def read_chat(target, locator, transcripts_root) -> dict | None:
    """The chat tail the Chat tab shows for ``target`` (see ``View.chat``); None when nothing is selected."""
    if target is None:
        return None
    kind, ident = target
    if kind == "task":
        return attention.task_chat(ident, locator, transcripts_root)
    return attention.chat_tail(ident, locator, transcripts_root)


def once(state_root, *, window_hours, include_all, sessions, cost_log, transcripts_root, now, width,
         locator=None) -> str:
    """The plain-text console view: the task table, each task's mark, the session rows and the
    ``waiting: N`` count, then each ``--session``'s detail tree.
    Raises ``NoSuchSession`` for a ``--session`` that names no task."""
    snapshot = model.collect(state_root, now=now, window_hours=window_hours, include_all=include_all,
                             sessions=sessions, cost_log=cost_log)
    missing = missing_sessions(snapshot, sessions)
    if missing:
        raise NoSuchSession(missing)
    overlay = read_attention(snapshot, locator, transcripts_root)
    rows = overview_rows(snapshot, now, overlay)
    lines = [overview_header(snapshot, now, overlay), *(_table(rows, width) if rows else ["no live tasks"])]
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
    for option, value in (("--plugin-dir", args.plugin_dir), ("--locator", args.locator)):
        if value is not None:
            argv += [option, str(value)]
    return argv
