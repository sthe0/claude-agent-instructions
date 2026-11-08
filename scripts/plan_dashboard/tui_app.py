# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Interactive console view of the plan dashboard: textual widgets over ``tui``'s pure layout.

Run with ``plan-dashboard.py tui`` (which calls ``uv run`` on this file). This is the only
Core file that imports textual; every row, label and key decision comes from ``tui``.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import importlib.util
import signal
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane, Tree  # noqa: E402

from plan_dashboard import events, locate, model, tui  # noqa: E402
from plan_dashboard import snapshot as snapshot_store  # noqa: E402

TICK_S = 0.5
KEPT_CYCLES = 4
CHAT_LOADING = "loading chat…"
TREE_LOADING = "loading…"
KEYS = (
    ("up", "Up"), ("down", "Down"), ("j", "Down"), ("k", "Up"), ("enter", "Open"), ("escape", "Back"),
    ("backspace", "Back"), ("tab", "Tab"), ("a", "All/live"), ("r", "Refresh"), (tui.JUMP_KEY, "Jump"),
    ("q", "Quit"),
)
KEY_ACTION_OWNERS = {"key": "global", "tree_key": "tree", "chat_key": "chat"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def note(app: "DashboardApp", ev: str, **payload) -> None:
    """Stamp ``ev`` when the screen next repaints, so the stream and the screen agree to within a frame."""
    if events.enabled():
        app.call_after_refresh(lambda: events.emit(ev, **payload))


def cycle_of(app: "DashboardApp", snapshot: dict) -> int | None:
    return next((number for held, number in app.cycles if held is snapshot), None)


def note_marks(app: "DashboardApp", overlay: dict | None, seen) -> None:
    """Each task's mark is painted and, the same moment, confirmed: the read that produced it has read every
    source of the task that changed (``seen`` is ``snapshot.observation()``, and ``applied`` the task's share)."""
    if overlay is None or not events.enabled():
        return
    changed, kinds, driven = seen
    tasks = [task for task, mark in (overlay.get("tasks") or {}).items()
             if isinstance(mark, dict) and task not in app.confirmed]
    applied = snapshot_store.applied_by_task(changed, kinds, driven, tasks)
    for task in tasks:
        app.confirmed.add(task)
        note(app, "mark_painted", task=task)
        note(app, "mark_confirmed", task=task, applied=applied[task])


def unconfirmed(app: "DashboardApp", ids) -> list[str]:
    """The rows among ``ids`` whose wait cell the live check has not confirmed: all of them before the first
    overlay read; after it the rows still behind ``~``, and in all mode the task rows no mark was read for."""
    if app.overlay is None:
        return list(ids)
    live = app.overlay.get("tasks") or {}
    return [sid for sid in ids if sid in app.old_ids
            or (app.view.include_all and sid not in live and sid not in app.marked
                and sid not in app.view.harness_ids)]


def note_confirmed(app: "DashboardApp") -> None:
    """Say once that the first screen, then once that the whole table, shows confirmed values."""
    if app.old_cleared:
        return
    table = app.query_one(DataTable)
    first = tui.rows_in_view(app.view.session_ids, round(table.scroll_offset.y), table.size.height, 0)
    if not app.first_confirmed and (first or not app.view.session_ids) and not unconfirmed(app, first):
        app.first_confirmed = True
        note(app, "first_screen_confirmed")
    if app.first_confirmed and not unconfirmed(app, visible_ids(app)):
        app.old_cleared = True
        note(app, "last_old_cleared")


def read_in_flight(app: "DashboardApp", group: str) -> bool:
    """Whether a read of kind ``group`` is still running: the tick leaves such a read alone rather than
    start, restart or cancel it, or a read slower than the interval would never land."""
    if group == "collect":
        return app._loading
    return any(worker.group == group and not worker.is_finished for worker in app.workers)


def refresh_if_due(app: "DashboardApp") -> None:
    if not app.started:
        return
    if tui.due(app.clock(), app.last_collect, app.interval) and not read_in_flight(app, "collect"):
        app.reload()
    if app.view.tab == "detail" and tui.due(app.clock(), app.last_detail_read, app.interval) \
            and not read_in_flight(app, "detail"):
        start_detail_read(app)
    if app.view.tab == "chat" and tui.due(app.clock(), app.last_chat_read, app.interval) \
            and not read_in_flight(app, "chat"):
        start_chat_read(app)
    start_marks_read(app)


def route_key(app: "DashboardApp", action: str, parameters) -> bool:
    """Whether a key binding is live: the priority ones step aside for the keys the Detail tab's tree
    and the Chat tab's scroll own, and each of those two is live only on its tab."""
    return tui.key_owner(app.view.tab, parameters[0] if parameters else "") == KEY_ACTION_OWNERS[action]


def focus_for(app: "DashboardApp", tab: str) -> None:
    widget = {"detail": Tree, "chat": VerticalScroll}.get(tab, DataTable)
    app.query_one(widget).focus()


def row_cells(row: dict) -> list[Text]:
    return [Text(cell, style=row["style"]) for cell in row["cells"]]


def attempt(read) -> tuple[dict | None, Exception | None]:
    try:
        return read(), None
    except Exception as exc:
        return None, exc


def read_overlay(app: "DashboardApp", snapshot: dict) -> tuple[dict | None, Exception | None]:
    """The attention overlay of the live set, None when its read fails, with the error: the Tasks table
    does not depend on it, so a failure here must not blank it."""
    return attempt(lambda: tui.read_attention(tui.live_snapshot(snapshot, app.sessions), app.locator,
                                              app.transcripts_root))


def counted_marks(app: "DashboardApp") -> dict | None:
    """What the header's waiting counter counts: the live overlay, in all mode too; until it is read, the
    marks stored in the snapshot."""
    return app.overlay if app.overlay is not None else app.stored


def header_old(app: "DashboardApp", shown: dict) -> frozenset[str]:
    """The header figures still taken from the snapshot: the counts while the rows drawn are the stored ones,
    the waiting figure until the live overlay is read."""
    if app.stored is None:
        return frozenset()
    return frozenset(name for name, stored in (("waiting", app.overlay is None), ("counts", shown is not app.snapshot))
                     if stored)


def stored_marks(app: "DashboardApp") -> dict | None:
    """The snapshot's marks of the rows still behind ``~``; None when the start had no snapshot."""
    if app.stored is None:
        return None
    return {"tasks": {task: mark for task, mark in (app.stored.get("tasks") or {}).items() if task in app.old_ids},
            "sessions": [] if app.overlay is not None else list(app.stored.get("sessions") or ())}


def shown_marks(app: "DashboardApp") -> dict | None:
    """The marks the rows show: the live overlay in live mode; in all mode the marks read for rows in view
    with the live overlay winning for the live set. A row not yet read again shows the snapshot's mark."""
    stored = stored_marks(app)
    if not app.view.include_all:
        if stored is None:
            return app.overlay
        live = app.overlay or {}
        return {"tasks": {**stored["tasks"], **(live.get("tasks") or {})},
                "sessions": list(live["sessions"]) if app.overlay is not None else stored["sessions"]}
    represented = app.order.represented if app.order is not None else frozenset()
    computed = {**((stored or {}).get("tasks") or {}), **app.marks}
    live = app.overlay if app.overlay is not None else {"sessions": (stored or {}).get("sessions") or []}
    return tui.shown_payload(live, computed, represented)


def read_chat_of(app: "DashboardApp", target) -> tuple[dict | None, Exception | None]:
    return attempt(lambda: tui.read_chat(target, app.locator, app.transcripts_root))


def deliver(loop: asyncio.AbstractEventLoop, future: asyncio.Future, outcome: tuple[bool, object]) -> None:
    """Hand a thread's outcome to the loop; once the loop is closed (the app has exited) nobody is waiting."""
    ok, value = outcome

    def settle() -> None:
        if not future.done():
            future.set_result(value) if ok else future.set_exception(value)

    try:
        loop.call_soon_threadsafe(settle)
    except RuntimeError:
        if not loop.is_closed():
            raise


async def in_daemon_thread(work):
    """Run ``work()`` in a daemon thread and await its result. Not the loop's default executor: asyncio
    joins that on shutdown, so a read still running would hold the shell back after ``q``."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def run() -> None:
        try:
            outcome = (True, work())
        except Exception as exc:
            outcome = (False, exc)
        deliver(loop, future, outcome)

    threading.Thread(target=run, daemon=True, name="plan-dashboard-read").start()
    return await future


def scroll_chat(scroll: VerticalScroll, key: str) -> None:
    scroll.scroll_relative(y=tui.scroll_step(key), animate=False)


def show_chat(scroll: VerticalScroll, body: Static, chat: dict | None) -> None:
    """Put the chat lines in ``body``; a reader who was at the end keeps following the tail, one who
    scrolled up stays where they are."""
    at_end = scroll.scroll_y >= scroll.max_scroll_y
    body.update(Text("\n".join(tui.chat_lines(chat))))
    if at_end:
        scroll.call_after_refresh(scroll.scroll_end, animate=False)


def clear_chat(body: Static, target) -> None:
    """Empty the Chat tab for a new ``target`` so the old one's lines are not shown under it while it loads."""
    body.update(Text(CHAT_LOADING if target is not None else "\n".join(tui.chat_lines(None))))


def chat_is_current(target, read_for) -> bool:
    """Whether a chat read for ``read_for`` still belongs on the tab now showing ``target``."""
    return target == read_for


def show_status(app: "DashboardApp", text: str) -> None:
    app.query_one("#status", Static).update(Text(text))


def mode_title(include_all: bool, count: int) -> str:
    """The persistent mode and row count of the sub-title, e.g. ``all: 12 tasks``."""
    return f"{'all' if include_all else 'live'}: {count} tasks"


def announce_loading(app: "DashboardApp", include_all: bool) -> None:
    """Say at once that the list is being read again: listing every task takes seconds."""
    for kind in ("overlay", "order", "marks"):
        note_read(app, kind, None)
    app.sub_title = f"loading {'all' if include_all else 'live'} tasks…"


def start_chat_read(app: "DashboardApp") -> None:
    """Read the open chat in its own exclusive worker group, apart from the reload whose cost grows with
    the number of tasks; a newer chat read cancels the older one, so an older read never lands after it."""
    view = app.view
    if view.tab != "chat" or view.chat is None:
        return
    app.last_chat_read = app.clock()
    app.run_worker(read_chat_for(app, view.chat), group="chat", exclusive=True)


async def read_chat_for(app: "DashboardApp", target) -> None:
    chat, error = await in_daemon_thread(lambda: read_chat_of(app, target))
    chat_ready(app, target, chat, error)


def chat_ready(app: "DashboardApp", target, chat: dict | None, error: Exception | None) -> None:
    if not chat_is_current(app.view.chat, target) or app.view.tab != "chat":
        return
    show_read_error(app, "chat", error)
    if chat is not None:
        show_chat(app.query_one("#chat-scroll", VerticalScroll), app.query_one("#chat-body", Static), chat)


def start_overlay_read(app: "DashboardApp", snapshot: dict, issued_all: bool) -> None:
    """Read the waiting marks after the table is shown; while one such read runs, a newer table waits for
    the next one instead of cancelling it."""
    if read_in_flight(app, "overlay"):
        return
    app.run_worker(read_overlay_for(app, snapshot, issued_all), group="overlay", exclusive=True)


async def read_overlay_for(app: "DashboardApp", snapshot: dict, issued_all: bool) -> None:
    def read():
        overlay, error = read_overlay(app, snapshot)
        sessions = tui.session_activity(overlay, tui.attention.transcript_index(app.transcripts_root))
        return overlay, error, sessions, snapshot_store.observation()

    overlay, error, sessions, seen = await in_daemon_thread(read)
    app.session_activity = sessions
    app.activity.update(sessions)
    overlay_ready(app, overlay, error, issued_all, seen)
    if (cycle := cycle_of(app, snapshot)) is not None:
        note(app, "cycle_end", cycle=cycle, applied=seen[0])


def overlay_ready(app: "DashboardApp", overlay: dict | None, error: Exception | None, issued_all: bool,
                  seen) -> None:
    if title_is_current(app, issued_all):
        note_read(app, "overlay", error)
    if overlay is not None:
        app.overlay = overlay
        app.old_ids = app.old_ids - confirmed_by(app, overlay)
    if app.snapshot is not None:
        show_table(app, app.snapshot, app.read_at, issued_all)
    if overlay is not None:
        note_marks(app, overlay, seen)
        if not app.live_check_ended:
            app.live_check_ended = True
            note(app, "live_check_end", applied=seen[0])
        note_confirmed(app)


def confirmed_by(app: "DashboardApp", overlay: dict) -> set[str]:
    """The old rows an overlay read replaces: in live mode every row, since the table lists the live set and
    a row the overlay has no mark for shows ``?``; in all mode the live set's tasks and session rows."""
    if not app.view.include_all:
        return set(app.old_ids)
    return set(overlay.get("tasks") or {}) | {row["harness_session"] for row in overlay.get("sessions") or ()}


def note_read(app: "DashboardApp", kind: str, error: Exception | None) -> None:
    """Keep the failure of the last ``kind`` read, or drop it when that read succeeded: every table that is
    shown rebuilds the sub-title, so a failure lives here, not in the title text, until a read of its own
    kind succeeds."""
    app.read_errors.pop(kind, None)
    if error is not None:
        app.read_errors[kind] = error


def read_failure_suffix(app: "DashboardApp") -> str:
    """The sub-title's ``; read failed: …`` part: the failure noted last."""
    return f"; read failed: {next(reversed(app.read_errors.values()))}" if app.read_errors else ""


def show_read_error(app: "DashboardApp", kind: str, error: Exception | None) -> None:
    """Note the outcome of a ``kind`` read (``error`` None: it succeeded) and show it in the sub-title now."""
    note_read(app, kind, error)
    app.sub_title = app.sub_title.split("; read failed")[0] + read_failure_suffix(app)


def start_order_read(app: "DashboardApp", snapshot: dict, issued_all: bool) -> None:
    """After the table is shown: the index read and stat behind the ordering key, then the re-sort. A newer
    table's read replaces an older one's, whose order would sort rows that are gone. The marks of the live set
    are read on their own, not after this: a pending question must show whatever the index read costs."""
    app.run_worker(read_order_for(app, snapshot, issued_all), group="order", exclusive=True)
    app.run_worker(in_daemon_thread(lambda: warm_child_index(app, snapshot)), group="children", exclusive=True)


def warm_child_index(app: "DashboardApp", snapshot: dict) -> None:
    """Cache the child index back to the oldest listed task's first activity, so a Detail read after the
    reload stats the state files instead of reading them."""
    model.child_index(app.state_root, since=tui.oldest_first_activity(snapshot["tasks"]))


async def read_order_for(app: "DashboardApp", snapshot: dict, issued_all: bool) -> None:
    order, error = await in_daemon_thread(lambda: attempt(lambda: tui.read_order(snapshot, app.transcripts_root)))
    order_ready(app, snapshot, order, error, issued_all)


def order_ready(app: "DashboardApp", snapshot: dict, order, error: Exception | None, issued_all: bool) -> None:
    if snapshot is not app.snapshot:
        return
    if order is not None:
        app.order, app.order_all = order, issued_all
        app.activity, app.marked = {**order.activity, **app.session_activity}, set()
        if not app.cursor_chosen:
            app.view = tui.unanchored(app.view)
        note_read(app, "order", None)
        show_table(app, snapshot, app.read_at, issued_all)
    elif title_is_current(app, issued_all):
        show_read_error(app, "order", error)
    start_marks_read(app)


def visible_ids(app: "DashboardApp") -> list[str]:
    """The rows of the table's viewport plus ``tui.MARK_MARGIN`` each side."""
    table = app.query_one(DataTable)
    return tui.rows_in_view(app.view.session_ids, round(table.scroll_offset.y), table.size.height)


def marks_due(app: "DashboardApp") -> list[str]:
    """The task rows in view whose marks this reload has not read yet; only all mode reads marks by row,
    and a live-set row has its mark from the live overlay."""
    if not app.view.include_all or app.order is None or not app.order_all:
        return []
    live = (app.overlay or {}).get("tasks") or {}
    return [sid for sid in visible_ids(app)
            if sid not in app.marked and sid not in app.view.harness_ids and sid not in live]


def start_marks_read(app: "DashboardApp") -> None:
    """Read the marks of the rows scrolling or a reload brought into view, on their own exclusive worker;
    rows read since the last reload are not read again until the next one."""
    if read_in_flight(app, "marks"):
        return
    ids = marks_due(app)
    if not ids:
        return
    app.run_worker(read_marks_for(app, ids, app.order), group="marks", exclusive=True)


def read_marks_of(app: "DashboardApp", ids: list[str], order) -> dict:
    return tui.read_marks(ids, order, app.locator, app.transcripts_root)


async def read_marks_for(app: "DashboardApp", ids: list[str], order) -> None:
    while True:
        computed, error, seen = await in_daemon_thread(
            lambda: (*attempt(lambda: read_marks_of(app, ids, order)), snapshot_store.observation(rotate=False)))
        if order is app.order:
            break
        # A newer order's own start of this group was refused while this read ran, so the rows it brought
        # into view are read here, by the same worker, instead of staying unread until the next scroll.
        order, ids = app.order, marks_due(app)
        if not ids:
            return
    app.marked.update(ids)
    if computed is not None:
        app.marks.update(computed)
        app.old_ids = app.old_ids - set(ids)
        show_marks(app, ids)
        show_read_error(app, "marks", None)
        note_marks(app, {"tasks": computed}, seen)
        note_confirmed(app)
    elif title_is_current(app, app.view.include_all):
        show_read_error(app, "marks", error)


def show_marks(app: "DashboardApp", ids) -> None:
    """Restyle the rows of ``ids`` with their new marks in place, without rebuilding the table."""
    table, wanted = app.query_one(DataTable), set(ids)
    present = {key.value for key in table.rows}
    marks = shown_marks(app)
    for task in app.snapshot["tasks"]:
        if task["session_id"] in wanted and task["session_id"] in present:
            row = tui.task_row(task, app.read_at, marks, app.old_ids)
            for column, cell in zip(tui.OVERVIEW_COLUMNS, row_cells(row)):
                table.update_cell(row["session_id"], column, cell)


def show_table(app: "DashboardApp", snapshot: dict, now: dt.datetime, issued_all: bool) -> None:
    """Rows, header and sub-title of ``snapshot`` with the marks as last read, sorted by the activity as
    last read: the table waits for neither."""
    marks = shown_marks(app)
    rows = tui.overview_rows(snapshot, now, marks, app.activity, app.old_ids)
    app.query_one("#overview-head", Static).update(Text(tui.overview_header(
        snapshot, now, marks, counted_marks(app), header_old(app, snapshot))))
    ids = tuple(row["session_id"] for row in rows)
    app.view = tui.reconcile(app.view, ids, (row["session_id"] for row in rows if row["kind"] == "session"))
    table = app.query_one(DataTable)
    table.clear()
    for row in rows:
        table.add_row(*row_cells(row), key=row["session_id"])
    if ids:
        table.move_cursor(row=app.view.cursor)
    if title_is_current(app, issued_all):
        app.sub_title = f"{mode_title(app.view.include_all, len(ids))}, read {now:%H:%M:%S}{read_failure_suffix(app)}"


def paint_stored(app: "DashboardApp", section: dict) -> None:
    """Paint the table the snapshot stored, every wait cell behind ``~`` until the live check replaces it."""
    stored, now = section["snapshot"], app.clock()
    app.stored, app.activity = section["marks"], dict(section["activity"])
    app.old_ids = frozenset(tui.stored_ids(stored, app.stored))
    show_table(app, stored, now, app.view.include_all)
    app.rows_noted = True
    note(app, "rows_painted", from_snapshot=True, rows=len(app.view.session_ids))


def forget_snapshot(app: "DashboardApp") -> None:
    """Treat a snapshot that failed to restore or paint as absent: what it painted and what it seeded go, and
    the start reads everything live."""
    app.stored, app.activity, app.old_ids, app.rows_noted = None, {}, frozenset(), False
    app.query_one(DataTable).clear()
    app.query_one("#overview-head", Static).update("")
    tui.attention.clear_cache()
    model.clear_cache()


def show_tree_loading(app: "DashboardApp") -> None:
    """Say at once that another task's tree is being read, so the previous task's tree is not shown under it."""
    tree = app.query_one(Tree)
    tree.clear()
    tree.root.set_label(Text(TREE_LOADING))
    app.tree_task = app.tree_read_for = None


def start_detail_read(app: "DashboardApp") -> None:
    """Read the selected task's Detail tree in its own exclusive worker group, apart from the reload: a
    newer read cancels the older one, so an older read never lands after it."""
    view = app.view
    if view.tab != "detail" or not view.selected:
        return
    if view.selected != app.tree_read_for:
        show_tree_loading(app)
    app.last_detail_read = app.clock()
    app.run_worker(read_detail_for(app, view.selected, app.clock()), group="detail", exclusive=True)


async def read_detail_for(app: "DashboardApp", task: str, now: dt.datetime) -> None:
    detail, error = await in_daemon_thread(lambda: attempt(lambda: read_detail(app, task)))
    detail_ready(app, task, detail, error, now)


def read_detail(app: "DashboardApp", task: str) -> dict | None:
    """The Detail tree's data; its children come from the child index bounded by the task's first activity,
    since a linked child's state file is created after its parent starts."""
    row = next((t for t in (app.snapshot or {}).get("tasks", ()) if t["session_id"] == task), None)
    try:
        return model.detail(app.state_root, task, cost_log=app.cost_log, transcripts_root=app.transcripts_root,
                            children=model.child_index(app.state_root, since=tui.first_activity(row)))
    except model.UnknownSession:
        return None


def detail_ready(app: "DashboardApp", task: str, detail: dict | None, error: Exception | None,
                 now: dt.datetime) -> None:
    if app.view.selected != task:
        return
    show_read_error(app, "detail", error)
    if error is not None:
        return
    nodes = tui.detail_tree(detail, now) if detail is not None else tui.missing_detail_tree()
    shown = task if detail is not None else None
    tree = app.query_one(Tree)
    state = tree_state_for(remember(tree, app.tree_task), shown)
    fill_tree(tree, nodes, state.expanded, state.cursor)
    app.tree_task, app.tree_read_for = shown, task


def jump_from(app: "DashboardApp") -> None:
    """Switch to the window of the session ``g`` points at; the locator's argv runs in a worker thread
    (it is bounded to 5 s) and its outcome lands in the status line."""
    subject, overlay = tui.jump_subject(app.view), shown_marks(app)
    app.run_worker(jump_task(app, subject, overlay), group="jump")


async def jump_task(app: "DashboardApp", subject, overlay) -> None:
    show_status(app, await in_daemon_thread(lambda: tui.jump_to(subject, overlay, app.locator)))


def title_is_current(app: "DashboardApp", issued_all: bool) -> bool:
    """Whether a reload issued under ``issued_all`` may still set the sub-title: after a toggle the older
    reload's mode and count would replace the loading text of the newer one."""
    return issued_all == app.view.include_all


def tree_state_for(state: tui.TreeState, task: str | None) -> tui.TreeState:
    return tui.state_for_task(state, task)


def remember(tree: Tree, task: str | None) -> tui.TreeState:
    """The nodes the user has open and the cursor, as key paths, read off the tree before it is rebuilt."""
    expanded = set()

    def walk(node):
        for child in node.children:
            if child.data is not None and child.is_expanded:
                expanded.add(child.data)
            walk(child)

    walk(tree.root)
    cursor = tree.cursor_node.data if tree.cursor_node is not None and tree.cursor_node.data is not None else ()
    return tui.TreeState(task=task, expanded=frozenset(expanded), cursor=cursor)


def fill_tree(tree: Tree, nodes: list[dict], expanded, cursor: tuple[str, ...]) -> None:
    """Rebuild ``tree`` collapsed below its top level, reopening the nodes in ``expanded`` (key paths)
    and putting the cursor back on ``cursor``, or on its nearest surviving ancestor."""
    tree.clear()
    tree.root.data = ()
    tree.root.set_label(Text(nodes[0]["label"]))
    by_path = {(): tree.root}

    def add(parent, items, prefix):
        for item in items:
            path = prefix + (item["key"],)
            label = Text(item["label"])
            if item["children"]:
                by_path[path] = add(parent.add(label, data=path, expand=path in expanded), item["children"], path)
            else:
                by_path[path] = parent.add_leaf(label, data=path)
        return parent

    add(tree.root, nodes[1:], ())
    tree.root.expand()
    tree.call_after_refresh(tree.move_cursor, by_path[tui.surviving_path(nodes[1:], tuple(cursor))])


def apply_tree_key(tree: Tree, key: str) -> None:
    node = tree.cursor_node
    if node is None:
        return
    action = tui.tree_action(key, expandable=node.allow_expand, expanded=node.is_expanded,
                             has_parent=node.parent is not None)
    if action == "down":
        tree.action_cursor_down()
    elif action == "up":
        tree.action_cursor_up()
    elif action == "toggle":
        node.toggle()
    elif action == "expand":
        node.expand()
    elif action == "collapse":
        node.collapse()
    elif action == "parent":
        tree.move_cursor(node.parent)


class DashboardApp(App):
    TITLE = "plan dashboard"
    CSS = """
    TabbedContent, TabPane { height: 1fr; }
    Tree, #chat-scroll { height: 1fr; }
    """
    BINDINGS = [Binding(key, f"key('{key}')", label, priority=True, show=key in ("enter", "escape", "a", "r", "g", "q"))
                for key, label in KEYS] + [Binding(key, f"tree_key('{key}')", show=False)
                                           for key in ("j", "k", "space", "left", "right")] + [
        Binding(key, f"chat_key('{key}')", priority=True, show=False) for key in sorted(tui.CHAT_KEYS)]

    def __init__(self, *, state_root, cost_log, transcripts_root=None, window_hours=24.0, include_all=False,
                 sessions=(), interval=tui.DEFAULT_INTERVAL_S, clock=utc_now, locator=None, snapshot_path=None):
        """``snapshot_path`` names the snapshot file the start paints from and the exit writes; None keeps
        the dashboard from reading or writing any."""
        super().__init__()
        self.snapshot_path = snapshot_path
        self.replaced_signals: dict = {}
        self.started = snapshot_path is None
        self.stored: dict | None = None
        self.old_ids: frozenset = frozenset()
        self.session_activity: dict = {}
        self.snapshot_all = include_all
        self.first_confirmed = False
        self.old_cleared = False
        self.state_root, self.cost_log, self.transcripts_root = state_root, cost_log, transcripts_root
        self.window_hours, self.sessions, self.interval, self.clock = window_hours, list(sessions), interval, clock
        self.locator = locator
        self.view = tui.View(include_all=include_all)
        self.last_collect: dt.datetime | None = None
        self._loading = False
        self._again = False
        self.tree_task: str | None = None
        self.overlay: dict | None = None
        self.last_chat_read: dt.datetime | None = None
        self.read_errors: dict[str, Exception] = {}
        self.snapshot: dict | None = None
        self.read_at: dt.datetime | None = None
        self.last_detail_read: dt.datetime | None = None
        self.tree_read_for: str | None = None
        self.order: tui.Order | None = None
        self.order_all = False
        self.activity: dict = {}
        self.marks: dict = {}
        self.marked: set = set()
        self.cursor_chosen = False
        self.cycle = 0
        self.cycles: list[tuple[dict, int]] = []
        self.confirmed: set = set()
        self.live_check_ended = False
        self.rows_noted = False

    def check_action(self, action: str, parameters) -> bool:
        if action in KEY_ACTION_OWNERS:
            return route_key(self, action, parameters)
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tasks"):
            with TabPane("Tasks", id="tasks"):
                yield Static("", id="overview-head")
                yield DataTable(cursor_type="row", zebra_stripes=True)
            with TabPane("Detail", id="detail"):
                yield Tree("select a task on the Tasks tab")
            with TabPane("Chat", id="chat"):
                with VerticalScroll(id="chat-scroll"):
                    yield Static(Text("\n".join(tui.chat_lines(None))), id="chat-body")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for name in tui.OVERVIEW_COLUMNS:
            table.add_column(name, key=name)
        if self.snapshot_path is None:
            self.reload()
        else:
            self.install_sigterm()
            self.run_worker(self._start(), group="start")
        self.set_interval(max(min(self.interval, TICK_S), 0.05), self._tick)

    def install_sigterm(self) -> None:
        """SIGTERM, SIGHUP and SIGINT end the app like the quit key, so the snapshot is written on the way out.

        A plain handler, not the loop's: it stays in place after the loop is gone, so a repeated request
        while the snapshot is written is absorbed instead of killing the process by the default action.
        """
        try:
            loop = asyncio.get_running_loop()

            def ask_to_exit(signum, frame) -> None:
                try:
                    loop.call_soon_threadsafe(self.exit)
                except RuntimeError:
                    pass

            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                self.replaced_signals[sig] = signal.signal(sig, ask_to_exit)
        except (RuntimeError, ValueError):
            pass

    async def _start(self) -> None:
        """Paint the rows the snapshot holds for this selection, behind ``~``, then read live."""
        key = snapshot_store.selection_key(self.view.include_all, self.window_hours, self.sessions)
        section, error = await in_daemon_thread(lambda: attempt(lambda: tui.restore_snapshot(self.snapshot_path, key)))
        if section is not None:
            error = attempt(lambda: paint_stored(self, section))[1]
        if error is not None:
            forget_snapshot(self)
        self.started = True
        self.reload()

    def save_snapshot(self) -> Path | None:
        """Write the snapshot of the screen last shown; nothing when no live read has landed yet, so a start
        that was cut short keeps the earlier snapshot."""
        if self.snapshot_path is None or self.snapshot is None or self.snapshot_all != self.view.include_all:
            return None
        key = snapshot_store.selection_key(self.snapshot_all, self.window_hours, self.sessions)
        section = tui.snapshot_section(self.snapshot, shown_marks(self) or {"tasks": {}, "sessions": []},
                                       self.activity, self.read_at)
        try:
            path = tui.write_snapshot(self.snapshot_path, key, section)
        except OSError:
            return None
        events.emit("snapshot_written", path=str(path))
        return path

    def _tick(self) -> None:
        refresh_if_due(self)

    def reload(self, force: bool = False) -> None:
        if self._loading:
            self._again = self._again or force
            return
        self._loading = True
        self.last_collect = self.clock()
        self.cycle += 1
        events.emit("cycle_start", cycle=self.cycle)
        self.run_worker(self._load(self.cycle), group="collect")

    async def _load(self, cycle: int) -> None:
        now, include_all = self.last_collect, self.view.include_all
        try:
            snapshot = await in_daemon_thread(lambda: model.collect(
                self.state_root, now=now, window_hours=self.window_hours, include_all=include_all,
                sessions=self.sessions, cost_log=self.cost_log, windowed=tui.attention.windowed_engines(self.locator)))
        except Exception as exc:
            self._failed(exc)
            return
        self.snapshot, self.read_at, self.snapshot_all = snapshot, now, include_all
        self.cycles = [*self.cycles[-(KEPT_CYCLES - 1):], (snapshot, cycle)]
        show_table(self, snapshot, now, include_all)
        if not self.rows_noted:
            self.rows_noted = True
            note(self, "rows_painted", from_snapshot=False, rows=len(snapshot["tasks"]))
        self._finish_load()
        start_order_read(self, snapshot, include_all)
        start_overlay_read(self, snapshot, include_all)

    def _finish_load(self) -> None:
        self._loading = False
        if self._again:
            self._again = False
            self.reload()

    def _failed(self, exc: Exception) -> None:
        self.sub_title = f"read failed: {exc}"
        self._finish_load()

    def action_key(self, key: str) -> None:
        self.dispatch_key(key)

    def action_tree_key(self, key: str) -> None:
        apply_tree_key(self.query_one(Tree), key)

    def action_chat_key(self, key: str) -> None:
        scroll_chat(self.query_one("#chat-scroll", VerticalScroll), key)

    def dispatch_key(self, key: str) -> None:
        previous = self.view
        self.view = tui.handle_key(previous, key)
        if self.view.quit:
            self.exit()
            return
        if self.view.tab != self.query_one(TabbedContent).active:
            self.query_one(TabbedContent).active = self.view.tab
        if (self.view.chat, self.view.tab) != (previous.chat, previous.tab):
            note_read(self, "chat", None)  # a failure belongs to the chat it was read for, not to later reloads
        if (self.view.selected, self.view.tab) != (previous.selected, previous.tab):
            note_read(self, "detail", None)
        if self.view.chat != previous.chat:
            clear_chat(self.query_one("#chat-body", Static), self.view.chat)
            start_chat_read(self)
        if self.view.include_all != previous.include_all:
            announce_loading(self, self.view.include_all)
        if self.view.cursor != previous.cursor and self.view.session_ids:
            self.query_one(DataTable).move_cursor(row=self.view.cursor)
            self.cursor_chosen = True
        if self.view.refresh:
            self.reload(force=True)
        if self.view.tab == "detail" and (self.view.refresh or (self.view.selected, self.view.tab) != (
                previous.selected, previous.tab)):
            start_detail_read(self)
        if self.view.jump:
            jump_from(self)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        stale = event.cursor_row != self.query_one(DataTable).cursor_row
        if not stale and event.cursor_row != self.view.cursor and event.cursor_row < len(self.view.session_ids):
            self.view = dataclasses.replace(self.view, cursor=event.cursor_row)
            self.cursor_chosen = True
        start_marks_read(self)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.view = dataclasses.replace(self.view, cursor=event.cursor_row)
        self.cursor_chosen = True
        self.dispatch_key("enter")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab = event.pane.id
        if tab in tui.TABS and tab != self.view.tab:
            self.view = dataclasses.replace(self.view, tab=tab)
            if tab == "detail":
                start_detail_read(self)
        if tab in tui.TABS:
            self.call_after_refresh(focus_for, self, tab)
        if tab == "chat":
            start_chat_read(self)


def _load_cli():
    spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    cli = _load_cli()
    parser = argparse.ArgumentParser(parents=[cli.shared_parser(), cli.locator_parser()])
    parser.add_argument("--interval", type=float, default=tui.DEFAULT_INTERVAL_S)
    parser.add_argument("--events", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.events:
        events.enable_path(args.events)
    app = DashboardApp(state_root=args.state_root, cost_log=args.cost_log, transcripts_root=args.transcripts_root,
                       window_hours=args.window_hours, include_all=args.include_all, sessions=args.sessions,
                       interval=args.interval, locator=locate.select_or_none(args.plugin_dir, args.locator),
                       snapshot_path=snapshot_store.default_path())
    run_app(app)
    return 0


def run_app(app: DashboardApp, **run_options) -> None:
    """Run ``app`` to its exit (the quit key or SIGTERM), then write its snapshot."""
    try:
        app.run(**run_options)
        app.save_snapshot()
    finally:
        for sig, handler in app.replaced_signals.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
