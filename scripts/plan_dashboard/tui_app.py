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


class DashboardApp(App):
    TITLE = "plan dashboard"
    BINDINGS = [Binding(key, f"key('{key}')", label, priority=True, show=key in ("enter", "escape", "a", "r", "q"))
                for key, label in KEYS]

    def __init__(self, *, state_root, cost_log, transcripts_root=None, window_hours=24.0, include_all=False,
                 sessions=(), interval=tui.DEFAULT_INTERVAL_S, clock=utc_now):
        super().__init__()
        self.state_root, self.cost_log, self.transcripts_root = state_root, cost_log, transcripts_root
        self.window_hours, self.sessions, self.interval, self.clock = window_hours, list(sessions), interval, clock
        self.view = tui.View(include_all=include_all)
        self.last_collect: dt.datetime | None = None
        self._loading = False
        self._again = False

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
        if detail is not None:
            self._fill_tree(tui.detail_tree(detail, now))
        elif selection_gone:
            self._fill_tree(tui.missing_detail_tree())
        scope = "all tasks" if self.view.include_all else "live tasks"
        self.sub_title = f"{len(ids)} {scope}, read {now:%H:%M:%S}"
        self._finish_load()

    def _fill_tree(self, nodes: list[dict]) -> None:
        tree = self.query_one(Tree)
        tree.clear()
        tree.root.set_label(Text(nodes[0]["label"]))

        def add(parent, items):
            for item in items:
                label = Text(item["label"])
                if item["children"]:
                    add(parent.add(label, expand=True), item["children"])
                else:
                    parent.add_leaf(label)

        add(tree.root, nodes[1:])
        tree.root.expand()

    def action_key(self, key: str) -> None:
        self.dispatch_key(key)

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
