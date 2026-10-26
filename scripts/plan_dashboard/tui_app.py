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
import dataclasses
import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane, Tree  # noqa: E402

from plan_dashboard import locate, model, tui  # noqa: E402

TICK_S = 0.5
CHAT_LOADING = "loading chat…"
KEYS = (
    ("up", "Up"), ("down", "Down"), ("j", "Down"), ("k", "Up"), ("enter", "Open"), ("escape", "Back"),
    ("backspace", "Back"), ("tab", "Tab"), ("a", "All/live"), ("r", "Refresh"), (tui.JUMP_KEY, "Jump"),
    ("q", "Quit"),
)
KEY_ACTION_OWNERS = {"key": "global", "tree_key": "tree", "chat_key": "chat"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def refresh_if_due(app: "DashboardApp") -> None:
    if tui.due(app.clock(), app.last_collect, app.interval):
        app.reload()


def route_key(app: "DashboardApp", action: str, parameters) -> bool:
    """Whether a key binding is live: the priority ones step aside for the keys the Detail tab's tree
    and the Chat tab's scroll own, and each of those two is live only on its tab."""
    return tui.key_owner(app.view.tab, parameters[0] if parameters else "") == KEY_ACTION_OWNERS[action]


def focus_for(app: "DashboardApp", tab: str) -> None:
    widget = {"detail": Tree, "chat": VerticalScroll}.get(tab, DataTable)
    app.query_one(widget).focus()


def row_cells(row: dict) -> list[Text]:
    return [Text(cell, style=row["style"]) for cell in row["cells"]]


def chat_if_open(app: "DashboardApp", view: tui.View) -> dict | None:
    """The chat tail to show, read only while the Chat tab is open on something."""
    if view.tab != "chat" or view.chat is None:
        return None
    return tui.read_chat(view.chat, app.locator, app.transcripts_root)


def attempt(read) -> tuple[dict | None, Exception | None]:
    try:
        return read(), None
    except Exception as exc:
        return None, exc


def read_overlay_and_chat(app: "DashboardApp", snapshot: dict,
                          view: tui.View) -> tuple[dict | None, dict | None, Exception | None]:
    """The attention overlay and the open chat, each None when its read fails, with the first error: the
    Tasks table does not depend on either, so a failure here must not blank it."""
    overlay, overlay_error = attempt(lambda: tui.read_attention(snapshot, app.locator, app.transcripts_root))
    chat, chat_error = attempt(lambda: chat_if_open(app, view))
    return overlay, chat, overlay_error if overlay_error is not None else chat_error


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
    app.sub_title = f"loading {'all' if include_all else 'live'} tasks…"


def start_chat_read(app: "DashboardApp") -> None:
    """Read the open chat in its own exclusive worker group, apart from the reload whose cost grows with
    the number of tasks; a newer chat read cancels only an older chat read."""
    view = app.view
    if view.tab != "chat" or view.chat is None:
        return
    app.last_chat_read = app.clock()
    app.run_worker(lambda: read_chat_for(app, view.chat), thread=True, group="chat", exclusive=True)


def read_chat_for(app: "DashboardApp", target) -> None:
    chat, error = attempt(lambda: tui.read_chat(target, app.locator, app.transcripts_root))
    app.call_from_thread(chat_ready, app, target, chat, error)


def chat_ready(app: "DashboardApp", target, chat: dict | None, error: Exception | None) -> None:
    if not chat_is_current(app.view.chat, target) or app.view.tab != "chat":
        return
    if chat is not None:
        show_chat(app.query_one("#chat-scroll", VerticalScroll), app.query_one("#chat-body", Static), chat)
    elif error is not None:
        app.sub_title = f"{app.sub_title.split('; read failed')[0]}; read failed: {error}"


def jump_from(app: "DashboardApp") -> None:
    """Switch to the window of the session ``g`` points at; the locator's argv runs in a worker thread
    (it is bounded to 5 s) and its outcome lands in the status line."""
    subject, overlay = tui.jump_subject(app.view), app.overlay
    app.run_worker(lambda: app.call_from_thread(show_status, app, tui.jump_to(subject, overlay, app.locator)),
                   thread=True, group="jump")


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
                 sessions=(), interval=tui.DEFAULT_INTERVAL_S, clock=utc_now, locator=None):
        super().__init__()
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
        self.reload()
        self.set_interval(max(min(self.interval, TICK_S), 0.05), self._tick)

    def _tick(self) -> None:
        refresh_if_due(self)
        if self.view.tab == "chat" and tui.due(self.clock(), self.last_chat_read, self.interval):
            start_chat_read(self)

    def reload(self, force: bool = False) -> None:
        if self._loading:
            self._again = self._again or force
            return
        self._loading = True
        self.last_collect = self.clock()
        self.run_worker(self._load, thread=True, group="collect")

    def _load(self) -> None:
        now, view = self.last_collect, self.view
        try:
            snapshot = model.collect(self.state_root, now=now, window_hours=self.window_hours,
                                     include_all=view.include_all, sessions=self.sessions, cost_log=self.cost_log)
            detail = None
            if view.selected:
                children = model.child_index(self.state_root)
                detail = model.detail(self.state_root, view.selected, cost_log=self.cost_log,
                                      transcripts_root=self.transcripts_root, children=children)
        except model.UnknownSession:
            detail = None
        except Exception as exc:
            self.call_from_thread(self._failed, exc)
            return
        overlay, chat, error = read_overlay_and_chat(self, snapshot, view)
        self.call_from_thread(self._apply, snapshot, detail, now, bool(view.selected) and detail is None, overlay, chat,
                              view.chat, error)

    def _finish_load(self) -> None:
        self._loading = False
        if self._again:
            self._again = False
            self.reload()

    def _failed(self, exc: Exception) -> None:
        self.sub_title = f"read failed: {exc}"
        self._finish_load()

    def _apply(self, snapshot: dict, detail: dict | None, now: dt.datetime, selection_gone: bool = False,
               overlay: dict | None = None, chat: dict | None = None, chat_target=None,
               error: Exception | None = None) -> None:
        if overlay is not None:
            self.overlay = overlay
        rows = tui.overview_rows(snapshot, now, overlay)
        self.query_one("#overview-head", Static).update(Text(tui.overview_header(snapshot, now, overlay)))
        ids = tuple(row["session_id"] for row in rows)
        self.view = tui.reconcile(self.view, ids, (row["session_id"] for row in rows if row["kind"] == "session"))
        table = self.query_one(DataTable)
        table.clear()
        for row in rows:
            table.add_row(*row_cells(row), key=row["session_id"])
        if ids:
            table.move_cursor(row=self.view.cursor)
        if chat_is_current(self.view.chat, chat_target) and (chat is not None or self.view.chat is None):
            show_chat(self.query_one("#chat-scroll", VerticalScroll), self.query_one("#chat-body", Static), chat)
        if detail is not None or selection_gone:
            nodes = tui.detail_tree(detail, now) if detail is not None else tui.missing_detail_tree()
            task = detail["session_id"] if detail is not None else None
            tree = self.query_one(Tree)
            state = tree_state_for(remember(tree, self.tree_task), task)
            fill_tree(tree, nodes, state.expanded, state.cursor)
            self.tree_task = task
        self.sub_title = f"{mode_title(self.view.include_all, len(ids))}, read {now:%H:%M:%S}"
        if error is not None:
            self.sub_title += f"; read failed: {error}"
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
        if self.view.chat != previous.chat:
            clear_chat(self.query_one("#chat-body", Static), self.view.chat)
            start_chat_read(self)
        if self.view.include_all != previous.include_all:
            announce_loading(self, self.view.include_all)
        if self.view.cursor != previous.cursor and self.view.session_ids:
            self.query_one(DataTable).move_cursor(row=self.view.cursor)
        if self.view.refresh:
            self.reload(force=True)
        if self.view.jump:
            jump_from(self)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row != self.view.cursor and event.cursor_row < len(self.view.session_ids):
            self.view = dataclasses.replace(self.view, cursor=event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.view = dataclasses.replace(self.view, cursor=event.cursor_row)
        self.dispatch_key("enter")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab = event.pane.id
        if tab in tui.TABS and tab != self.view.tab:
            self.view = dataclasses.replace(self.view, tab=tab)
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
    args = parser.parse_args(argv)
    DashboardApp(state_root=args.state_root, cost_log=args.cost_log, transcripts_root=args.transcripts_root,
                 window_hours=args.window_hours, include_all=args.include_all, sessions=args.sessions,
                 interval=args.interval, locator=locate.select_or_none(args.plugin_dir, args.locator)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
