# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Interactive console view of the plan dashboard: textual widgets over ``tui``'s pure layout.

Run with ``plan-dashboard.py tui`` (which calls ``uv run`` on this file). This is the only
Core file that imports textual; every row, label and key decision comes from ``tui``.
"""
from __future__ import annotations

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
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane, Tree  # noqa: E402

from plan_dashboard import model, tui  # noqa: E402

TICK_S = 0.5
KEYS = (
    ("up", "Up"), ("down", "Down"), ("j", "Down"), ("k", "Up"), ("enter", "Open"), ("escape", "Back"),
    ("backspace", "Back"), ("tab", "Tab"), ("a", "All/live"), ("r", "Refresh"), ("q", "Quit"),
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def refresh_if_due(app: "DashboardApp") -> None:
    if tui.due(app.clock(), app.last_collect, app.interval):
        app.reload()


def route_key(app: "DashboardApp", action: str, parameters) -> bool:
    """Whether a key binding is live: the priority ones step aside for the keys the Detail tab's tree
    owns, and the tree's own bindings are live only there."""
    owner = tui.key_owner(app.view.tab, parameters[0] if parameters else "")
    return owner == ("tree" if action == "tree_key" else "global")


def focus_for(app: "DashboardApp", tab: str) -> None:
    (app.query_one(Tree) if tab == "detail" else app.query_one(DataTable)).focus()


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
    BINDINGS = [Binding(key, f"key('{key}')", label, priority=True, show=key in ("enter", "escape", "a", "r", "q"))
                for key, label in KEYS] + [Binding(key, f"tree_key('{key}')", show=False)
                                           for key in ("j", "k", "space", "left", "right")]

    def __init__(self, *, state_root, cost_log, transcripts_root=None, window_hours=24.0, include_all=False,
                 sessions=(), interval=tui.DEFAULT_INTERVAL_S, clock=utc_now):
        super().__init__()
        self.state_root, self.cost_log, self.transcripts_root = state_root, cost_log, transcripts_root
        self.window_hours, self.sessions, self.interval, self.clock = window_hours, list(sessions), interval, clock
        self.view = tui.View(include_all=include_all)
        self.last_collect: dt.datetime | None = None
        self._loading = False
        self._again = False
        self.tree_task: str | None = None

    def check_action(self, action: str, parameters) -> bool:
        if action in ("key", "tree_key"):
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
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for name in tui.OVERVIEW_COLUMNS:
            table.add_column(name, key=name)
        self.reload()
        self.set_interval(max(min(self.interval, TICK_S), 0.05), self._tick)

    def _tick(self) -> None:
        refresh_if_due(self)

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
        self.call_from_thread(self._apply, snapshot, detail, now, bool(view.selected) and detail is None)

    def _finish_load(self) -> None:
        self._loading = False
        if self._again:
            self._again = False
            self.reload()

    def _failed(self, exc: Exception) -> None:
        self.sub_title = f"read failed: {exc}"
        self._finish_load()

    def _apply(self, snapshot: dict, detail: dict | None, now: dt.datetime, selection_gone: bool = False) -> None:
        rows = tui.overview_rows(snapshot, now)
        self.query_one("#overview-head", Static).update(Text(tui.overview_header(snapshot, now)))
        ids = tuple(row["session_id"] for row in rows)
        self.view = tui.reconcile(self.view, ids)
        table = self.query_one(DataTable)
        table.clear()
        for row in rows:
            table.add_row(*(Text(cell) for cell in row["cells"]), key=row["session_id"])
        if ids:
            table.move_cursor(row=self.view.cursor)
        if detail is not None or selection_gone:
            nodes = tui.detail_tree(detail, now) if detail is not None else tui.missing_detail_tree()
            task = detail["session_id"] if detail is not None else None
            tree = self.query_one(Tree)
            state = tree_state_for(remember(tree, self.tree_task), task)
            fill_tree(tree, nodes, state.expanded, state.cursor)
            self.tree_task = task
        scope = "all tasks" if self.view.include_all else "live tasks"
        self.sub_title = f"{len(ids)} {scope}, read {now:%H:%M:%S}"
        self._finish_load()

    def action_key(self, key: str) -> None:
        self.dispatch_key(key)

    def action_tree_key(self, key: str) -> None:
        apply_tree_key(self.query_one(Tree), key)

    def dispatch_key(self, key: str) -> None:
        previous = self.view
        self.view = tui.handle_key(previous, key)
        if self.view.quit:
            self.exit()
            return
        if self.view.tab != self.query_one(TabbedContent).active:
            self.query_one(TabbedContent).active = self.view.tab
        if self.view.cursor != previous.cursor and self.view.session_ids:
            self.query_one(DataTable).move_cursor(row=self.view.cursor)
        if self.view.refresh:
            self.reload(force=True)

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


def _load_cli():
    spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = _load_cli().shared_parser()
    parser.add_argument("--interval", type=float, default=tui.DEFAULT_INTERVAL_S)
    args = parser.parse_args(argv)
    DashboardApp(state_root=args.state_root, cost_log=args.cost_log, transcripts_root=args.transcripts_root,
                 window_hours=args.window_hours, include_all=args.include_all, sessions=args.sessions,
                 interval=args.interval).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
