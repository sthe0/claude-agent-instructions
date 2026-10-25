"""The textual layer of the console view, driven headless through textual's pilot.

The only test file besides ``tui_app.py`` that imports textual. Run with
``PLAN_DASHBOARD_REQUIRE_TEXTUAL=1`` a missing textual fails the run; without it the file is skipped.
Layout and key decisions are pinned in ``test_plan_dashboard_tui.py``; here the widgets must show them.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

if os.environ.get("PLAN_DASHBOARD_REQUIRE_TEXTUAL") == "1":
    import textual  # noqa: F401
else:
    pytest.importorskip("textual", exc_type=ImportError)

from textual.containers import VerticalScroll  # noqa: E402
from textual.widgets import DataTable, Static, TabbedContent, Tree  # noqa: E402

from plan_dashboard import attention, locate, model, tui  # noqa: E402
from plan_dashboard.tui_app import CHAT_LOADING, DashboardApp  # noqa: E402
from plan_dashboard_fixtures import build_fixture_root, read_only_guard  # noqa: E402
from plan_dashboard_support import (  # noqa: E402,F401
    ALPHA, BETA, NOW, STALE, at, console_root, event, isolated_home, iso, make_state,
)

SIZE = (150, 40)
PILOT_DEADLINE_S = 30


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


def make_app(root, clock=None, **options):
    return DashboardApp(state_root=root.state_root, cost_log=root.cost_log, transcripts_root=root.transcripts_root,
                        clock=clock or Clock(), **{"interval": 3600.0, **options})


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def run(scenario, root, **options):
    app = make_app(root, **options)

    async def main():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await scenario(app, pilot)

    asyncio.run(asyncio.wait_for(main(), PILOT_DEADLINE_S))
    return app


def table_ids(app):
    return [key.value for key in app.query_one(DataTable).rows]


def table_cells(app, index):
    cells = [cell.plain for cell in app.query_one(DataTable).get_row_at(index)]
    return dict(zip(tui.OVERVIEW_COLUMNS, cells))


def labels(node):
    return [node.label.plain] + [text for child in node.children for text in labels(child)]


def tree_text(app):
    return "\n".join(labels(app.query_one(Tree).root))


def count_collects(monkeypatch):
    calls = []
    real = model.collect

    def counting(*args, **kwargs):
        calls.append(kwargs.get("now"))
        return real(*args, **kwargs)

    monkeypatch.setattr(model, "collect", counting)
    return calls


def test_tasks_tab_lists_the_live_tasks(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        assert set(table_ids(app)) == {ALPHA, BETA, "broken"}
        by_id = {sid: index for index, sid in enumerate(table_ids(app))}
        alpha = table_cells(app, by_id[ALPHA])
        assert [alpha[name] for name in ("session", "task", "live", "node", "stage", "diff", "replans")] == [
            "alpha-00", f"task-{ALPHA}", "active", "EXECUTING", "stage 1/3 [###-----]", "0", "0"]
        assert alpha["cost"] == "≥$1.50"
        assert alpha["goal"] == "Build the thing [with brackets]"
        assert table_cells(app, by_id["broken"])["stage"] == "stage ?/?"
        assert "3 live tasks" in app.sub_title
        head = str(app.query_one("#overview-head").render())
        assert head.endswith("3 active  0 idle  3 total  waiting: 0") and "2026-10-22 12:00:00 UTC" in head

    run(scenario, root)


def test_enter_opens_the_detail_of_the_selected_task(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        index = table_ids(app).index(ALPHA)
        for _ in range(index):
            await pilot.press("down")
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail"
        text = tree_text(app)
        assert "[done] 1. Stage 1 (in_thread, measurable)" in text
        assert "[active] 2. Stage 2 (in_thread, measurable)" in text
        assert "stage 2: wall open (10m00s so far)" in text
        assert "alpha-00 EXECUTING  Build the thing [with brackets]" in text

    run(scenario, root)


def test_clicking_a_row_opens_its_detail(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        index = table_ids(app).index(ALPHA)
        await pilot.click(DataTable, offset=(4, 1 + index))
        await pilot.click(DataTable, offset=(4, 1 + index))
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail"
        assert "[active] 2. Stage 2" in tree_text(app)

    run(scenario, root)


def test_escape_goes_back_and_tab_returns_to_the_detail(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("escape")
        assert app.query_one(TabbedContent).active == "tasks"
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail"
        await pilot.press("tab")
        assert app.query_one(TabbedContent).active == "chat"
        await pilot.press("tab")
        assert app.query_one(TabbedContent).active == "tasks"

    run(scenario, root)


def test_a_toggles_between_live_and_all_tasks(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        assert STALE not in table_ids(app)
        await pilot.press("a")
        await settle(app, pilot)
        assert STALE in table_ids(app) and "all tasks" in app.sub_title
        await pilot.press("a")
        await settle(app, pilot)
        assert STALE not in table_ids(app) and "live tasks" in app.sub_title

    run(scenario, root)


def test_q_quits(tmp_path):
    root = console_root(tmp_path)
    exited = []

    async def scenario(app, pilot):
        await pilot.press("q")
        await pilot.pause()
        exited.append(app._exit)

    run(scenario, root)
    assert exited == [True]


def test_r_reads_again_at_once(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    calls = count_collects(monkeypatch)

    async def scenario(app, pilot):
        before = len(calls)
        await pilot.press("r")
        await settle(app, pilot)
        assert len(calls) == before + 1

    run(scenario, root)


def test_the_view_reads_again_once_the_interval_has_passed(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    calls = count_collects(monkeypatch)
    clock = Clock()

    async def scenario(app, pilot):
        before = len(calls)
        await pilot.pause(0.7)
        assert len(calls) == before
        clock.advance(10)
        await pilot.pause(0.7)
        await settle(app, pilot)
        assert len(calls) > before
        assert calls[-1] == NOW + dt.timedelta(seconds=10)
        assert "read 12:00:10" in app.sub_title

    run(scenario, root, clock=clock, interval=5.0)


def test_the_cursor_follows_its_task_when_a_read_reorders_the_rows(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    reads = []
    real = model.collect

    def reversing(*args, **kwargs):
        snapshot = real(*args, **kwargs)
        reads.append(1)
        if len(reads) > 1:
            tasks = sorted(snapshot["tasks"], key=lambda t: t["last_activity"])
            stamps = [task["last_activity"] for task in tasks]
            for task, stamp in zip(tasks, reversed(stamps)):
                task["last_activity"] = stamp
        return snapshot

    monkeypatch.setattr(model, "collect", reversing)

    async def scenario(app, pilot):
        before = table_ids(app)
        followed = before[0]
        await pilot.press("r")
        await settle(app, pilot)
        assert table_ids(app) == before[::-1]
        assert table_ids(app)[app.query_one(DataTable).cursor_row] == followed
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.view.selected == followed

    run(scenario, root)


def test_the_detail_tab_says_so_when_the_selected_task_vanishes(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        for _ in range(table_ids(app).index(ALPHA)):
            await pilot.press("down")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "Stage 2" in tree_text(app)
        (root.state_root / f"{app.view.selected}.json").unlink()
        await pilot.press("r")
        await settle(app, pilot)
        assert tree_text(app) == "no such session"

    run(scenario, root)


def test_the_app_builds_the_child_index_once_per_read(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    builds = []
    real = model.child_index
    monkeypatch.setattr(model, "child_index", lambda *a, **k: builds.append(1) or real(*a, **k))

    async def scenario(app, pilot):
        await pilot.press("enter")
        await settle(app, pilot)
        before = len(builds)
        await pilot.press("r")
        await settle(app, pilot)
        assert len(builds) == before + 1

    run(scenario, root)


def test_the_app_writes_nothing(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("a")
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)

    with read_only_guard(root.home):
        run(scenario, root)


def test_every_key_of_the_pure_layer_is_bound():
    bound = {binding.key for binding in DashboardApp.BINDINGS}
    assert {"up", "down", "j", "k", "enter", "escape", "backspace", "tab", "a", "r", "q"} <= bound
    assert {"j", "k", "space", "left", "right"} <= {b.key for b in DashboardApp.BINDINGS if "tree_key" in b.action}
    assert tui.TABS == ("tasks", "detail", "chat")
    assert {b.key for b in DashboardApp.BINDINGS if "chat_key" in b.action} == set(tui.CHAT_KEYS)


# --- the Detail tree: collapsed start, keyboard navigation, state kept across a refresh -------------

REVIEWED_A = "review-aaaa-0001"
REVIEWED_B = "review-bbbb-0002"
PHASE = ("phases", "phase:plan v1")
ROUND = PHASE + ("phase:review round 1",)


def reviewed_root(tmp_path):
    """Two tasks in plan review: an open ``plan v1`` phase holding two review-round children."""
    def reviewing(sid, minutes):
        return make_state(sid, node="PLAN_READY", stages=("PENDING",), history=[
            event("classify", at(minutes=minutes)), event("submit_plan", at(minutes=minutes - 10)),
            event("plan_review", at(minutes=minutes - 15)), event("plan_review", at(minutes=minutes - 18))])
    return build_fixture_root(tmp_path, [reviewing(REVIEWED_A, 40), reviewing(REVIEWED_B, 39)], now=NOW)


def tree_of(app):
    return app.query_one(Tree)


def find(tree, path):
    node = tree.root
    for depth in range(1, len(path) + 1):
        node = next(child for child in node.children if child.data == path[:depth])
    return node


def top_level(app):
    return {child.data[0]: child for child in tree_of(app).root.children}


async def open_task(app, pilot, sid):
    target = table_ids(app).index(sid)
    while app.view.cursor != target:
        await pilot.press("down" if app.view.cursor < target else "up")
    await pilot.press("enter")
    await settle(app, pilot)


def cursor_path(app):
    return tree_of(app).cursor_node.data


def test_the_detail_tree_opens_collapsed_with_focus_on_the_tree_by_enter(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        assert app.query_one(TabbedContent).active == "detail"
        assert app.focused is tree_of(app)
        top = top_level(app)
        assert {"totals", "stages", "phases"} <= set(top)
        assert top["stages"].children and not top["stages"].is_expanded
        assert not any(node.is_expanded for node in top.values())

    run(scenario, root)


def test_tab_and_a_click_on_the_tab_focus_the_tree_and_escape_the_table(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.focused is app.query_one(DataTable)
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.focused is tree_of(app)
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "chat" and app.focused is app.query_one(VerticalScroll)
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.focused is app.query_one(DataTable)
        await pilot.click("#--content-tab-detail")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "detail" and app.focused is tree_of(app)

    run(scenario, root)


def test_arrow_keys_move_the_tree_cursor_and_enter_and_space_toggle_a_node(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("down")
        assert cursor_path(app) == ("totals",)
        await pilot.press("down")
        assert cursor_path(app) == ("stages",)
        await pilot.press("enter")
        assert top_level(app)["stages"].is_expanded
        await pilot.press("down")
        assert cursor_path(app) == ("stages", "stage:1")
        await pilot.press("up")
        await pilot.press("space")
        assert not top_level(app)["stages"].is_expanded
        await pilot.press("space")
        assert top_level(app)["stages"].is_expanded

    run(scenario, root)


def test_j_k_right_and_left_drive_the_tree(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("j", "j")
        assert cursor_path(app) == ("stages",)
        await pilot.press("right")
        assert top_level(app)["stages"].is_expanded
        await pilot.press("j")
        assert cursor_path(app) == ("stages", "stage:1")
        await pilot.press("left")
        assert cursor_path(app) == ("stages",) and top_level(app)["stages"].is_expanded
        await pilot.press("left")
        assert not top_level(app)["stages"].is_expanded
        await pilot.press("k")
        assert cursor_path(app) == ("totals",)

    run(scenario, root)


def test_escape_hands_the_arrow_keys_back_to_the_table(tmp_path):
    root = console_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("escape")
        await settle(app, pilot)
        before = app.query_one(DataTable).cursor_row
        await pilot.press("down" if before == 0 else "up")
        assert app.query_one(DataTable).cursor_row != before

    run(scenario, root)


def test_a_click_expands_a_phase_node(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.click(Tree, offset=(6, 3))
        assert top_level(app)["phases"].is_expanded
        await pilot.click(Tree, offset=(6, 5))
        assert find(tree_of(app), PHASE).is_expanded

    run(scenario, root)


def test_a_refresh_keeps_what_the_user_opened_and_the_cursor_though_the_label_changes(tmp_path):
    root = reviewed_root(tmp_path)
    clock = Clock()

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right", "j", "j", "right", "j")
        assert cursor_path(app) == ROUND
        before = find(tree_of(app), PHASE).label.plain
        clock.advance(120)
        await pilot.press("r")
        await settle(app, pilot)
        after = find(tree_of(app), PHASE).label.plain
        assert before != after
        assert top_level(app)["phases"].is_expanded and find(tree_of(app), PHASE).is_expanded
        assert not top_level(app)["stages"].is_expanded
        assert cursor_path(app) == ROUND

    run(scenario, root, clock=clock)


def test_a_vanished_cursor_node_falls_back_to_its_nearest_surviving_ancestor(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right", "j", "j", "right", "j")
        assert cursor_path(app) == ROUND
        state_file = root.state_root / f"{REVIEWED_A}.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))
        data["history"] = [e for e in data["history"] if e.get("event") != "plan_review"]
        state_file.write_text(json.dumps(data), encoding="utf-8")
        await pilot.press("r")
        await settle(app, pilot)
        assert cursor_path(app) == PHASE

    run(scenario, root)


def test_another_task_opens_collapsed_though_the_same_key_path_was_open_in_the_first(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right")
        assert top_level(app)["phases"].is_expanded
        await pilot.press("escape")
        await settle(app, pilot)
        await open_task(app, pilot, REVIEWED_B)
        assert app.view.selected == REVIEWED_B
        assert ("phases",) in tui.key_paths(tui.detail_tree(model.detail(
            root.state_root, REVIEWED_B, cost_log=root.cost_log), NOW))
        assert not top_level(app)["phases"].is_expanded

    run(scenario, root)


def test_the_tree_pilot_runs_inside_the_read_only_guard(tmp_path):
    root = reviewed_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, REVIEWED_A)
        await pilot.press("j", "j", "j", "right", "r")
        await settle(app, pilot)

    with read_only_guard(root.home):
        run(scenario, root)


# --- waiting marks, session rows and the Chat tab ---------------------------------------------------

PROJECT = "-console-project"
ASK_SESSION, PERMISSION_SESSION, FREE_SESSION = "ask-session", "permission-session", "free-session"
QUESTION = {"questions": [{"header": "HEADER-TEXT", "question": "Which way?", "multiSelect": False, "options": [
    {"label": "Left", "description": "DESC-LEFT"}, {"label": "Right", "description": "DESC-RIGHT"}]}]}


@pytest.fixture(autouse=True)
def fresh_attention_cache():
    attention._CACHE.clear()
    yield
    attention._CACHE.clear()


def write_fixture_transcript(root, session, entries):
    path = root.transcripts_root / PROJECT / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def append_fixture_transcript(root, session, entries):
    with open(root.transcripts_root / PROJECT / f"{session}.jsonl", "a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(entry) + "\n" for entry in entries))


def line(role, minutes, content, stop=None):
    message = {"role": role, "content": content}
    if stop:
        message["stop_reason"] = stop
    return {"type": role, "timestamp": iso(at(minutes=minutes)), "message": message}


def tool_use(tool_id, name, tool_input=None):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input or {}}


ASKING = [line("user", 10, "go on"), line("assistant", 9, [tool_use("q1", "AskUserQuestion", QUESTION)], "tool_use")]
IDLE = [line("user", 10, "go on"), line("assistant", 9, [{"type": "text", "text": "done"}], "end_turn")]
WORKING = [line("user", 10, "go on"), line("assistant", 9, [tool_use("t1", "Bash", {"command": "ls"})], "tool_use")]


def fake_locator(sessions, *, permission=()):
    return SimpleNamespace(NAME="fake", sessions=lambda: sessions, permission_wait=lambda harness: harness in permission)


def attention_root(tmp_path, alpha=ASKING):
    root = console_root(tmp_path)
    write_fixture_transcript(root, ALPHA, alpha)
    write_fixture_transcript(root, BETA, IDLE)
    write_fixture_transcript(root, ASK_SESSION, ASKING)
    write_fixture_transcript(root, PERMISSION_SESSION, WORKING)
    write_fixture_transcript(root, FREE_SESSION, IDLE)
    return root


def attention_locator():
    return fake_locator([
        {"harness_session": ASK_SESSION, "cwd": "/work/ask", "name": "Asker"},
        {"harness_session": PERMISSION_SESSION, "cwd": None, "name": None},
        {"harness_session": FREE_SESSION, "cwd": "/work/free", "name": "Free"},
    ], permission=(PERMISSION_SESSION,))


def guarded(root):
    """Engine state is what the app must leave alone; transcripts stay writable so a test can grow one."""
    return read_only_guard(root.state_root.parent)


def row_of(app, sid):
    return table_ids(app).index(sid)


def chat_text(app):
    return str(app.query_one("#chat-body", Static).render())


def test_rows_carry_the_waiting_mark_style_and_the_header_count(tmp_path):
    root = attention_root(tmp_path)

    async def scenario(app, pilot):
        marks = {sid: table_cells(app, row_of(app, sid))["wait"] for sid in table_ids(app)}
        assert (marks[ALPHA], marks[BETA], marks["broken"]) == ("?", "…", "")
        assert (marks[ASK_SESSION], marks[PERMISSION_SESSION], marks[FREE_SESSION]) == ("?", "!", "…")
        table = app.query_one(DataTable)
        assert table.get_row_at(row_of(app, ALPHA))[0].style == tui.WAIT_STYLES["choice"]
        assert table.get_row_at(row_of(app, PERMISSION_SESSION))[0].style == tui.WAIT_STYLES["permission"]
        assert table.get_row_at(row_of(app, "broken"))[0].style == ""
        head = str(app.query_one("#overview-head").render())
        assert head.endswith("waiting: 3")

    with guarded(root):
        run(scenario, root, locator=attention_locator())


def test_session_rows_show_a_dash_where_a_task_has_its_columns(tmp_path):
    root = attention_root(tmp_path)

    async def scenario(app, pilot):
        cells = table_cells(app, row_of(app, ASK_SESSION))
        assert cells["session"] == "Asker" and {cells[name] for name in ("stage", "cost")} == {tui.NO_VALUE}

    run(scenario, root, locator=attention_locator())


def test_the_mark_and_the_chat_follow_a_transcript_that_grows_after_a_tick(tmp_path):
    root = attention_root(tmp_path, alpha=WORKING)
    clock = Clock()

    async def scenario(app, pilot):
        assert table_cells(app, row_of(app, ALPHA))["wait"] == ""
        await open_task(app, pilot, ALPHA)
        await pilot.press("tab")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "chat"
        assert "Bash" in chat_text(app) and "Which way?" not in chat_text(app)
        append_fixture_transcript(root, ALPHA, [ASKING[1]])
        clock.advance(5)
        await pilot.pause(1.2)
        await settle(app, pilot)
        assert "Which way?" in chat_text(app) and "- Left" in chat_text(app)
        await pilot.press("escape")
        await settle(app, pilot)
        assert table_cells(app, row_of(app, ALPHA))["wait"] == "?"

    with guarded(root):
        run(scenario, root, clock=clock, interval=1.0, locator=attention_locator())


def test_j_k_up_and_down_scroll_the_chat_and_leave_the_task_cursor(tmp_path):
    long = [line("user", 30, "go on")] + [
        line("assistant", 29 - i // 4, [{"type": "text", "text": f"entry {i}\nsecond line {i}"}], "end_turn")
        for i in range(40)]
    root = attention_root(tmp_path, alpha=long)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        cursor = (app.view.cursor, app.query_one(DataTable).cursor_row)
        await pilot.press("tab")
        await settle(app, pilot)
        await pilot.pause(0.2)
        scroll = app.query_one(VerticalScroll)
        assert scroll.max_scroll_y > 4 and scroll.scroll_y == scroll.max_scroll_y
        for key, sign in (("k", -1), ("up", -1), ("j", 1), ("down", 1)):
            before = scroll.scroll_y
            await pilot.press(key)
            await pilot.pause(0.1)
            assert (scroll.scroll_y - before) * sign > 0, key
        assert (app.view.cursor, app.query_one(DataTable).cursor_row) == cursor
        assert app.query_one(TabbedContent).active == "chat"

    run(scenario, root, locator=attention_locator())


def test_escape_leaves_the_chat_for_the_tasks(tmp_path):
    root = attention_root(tmp_path)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("tab")
        await settle(app, pilot)
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "tasks" and app.focused is app.query_one(DataTable)

    run(scenario, root, locator=attention_locator())


def test_enter_on_a_session_row_opens_its_chat(tmp_path):
    root = attention_root(tmp_path)

    async def scenario(app, pilot):
        target = row_of(app, ASK_SESSION)
        while app.view.cursor != target:
            await pilot.press("down")
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "chat"
        text = chat_text(app)
        assert f"chat: {ASK_SESSION}" in text and "Which way?" in text and "- Left" in text and "- Right" in text
        assert "HEADER-TEXT" not in text and "DESC-LEFT" not in text

    with guarded(root):
        run(scenario, root, locator=attention_locator())


def gate_chat_reads(monkeypatch, held):
    """Make ``tui.read_chat`` of the target ``held`` wait for the returned event; every target it was asked for is
    appended to the returned list."""
    release, reads = threading.Event(), []
    real = tui.read_chat

    def gated(target, locator, transcripts_root):
        reads.append(target)
        if target == held:
            release.wait(PILOT_DEADLINE_S)
        return real(target, locator, transcripts_root)

    monkeypatch.setattr(tui, "read_chat", gated)
    return release, reads


async def cursor_to(app, pilot, sid):
    target = table_ids(app).index(sid)
    while app.view.cursor != target:
        await pilot.press("down" if app.view.cursor < target else "up")


def test_opening_another_task_shows_no_chat_of_the_previous_one_while_its_own_loads(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    release, _ = gate_chat_reads(monkeypatch, ("task", BETA))

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("tab")
        await settle(app, pilot)
        assert "Which way?" in chat_text(app)
        await pilot.press("escape")
        await open_task(app, pilot, BETA)
        assert "Which way?" not in chat_text(app)
        await pilot.press("tab")
        await pilot.pause(0.3)
        assert "Which way?" not in chat_text(app) and CHAT_LOADING in chat_text(app)
        release.set()
        await settle(app, pilot)
        assert "done" in chat_text(app) and "Which way?" not in chat_text(app) and CHAT_LOADING not in chat_text(app)

    run(scenario, root, locator=attention_locator())


def test_a_chat_read_that_returns_after_another_task_was_opened_is_dropped(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    release, _ = gate_chat_reads(monkeypatch, ("task", ALPHA))

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("tab")
        await pilot.pause(0.3)
        await pilot.press("escape")
        await cursor_to(app, pilot, BETA)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.view.chat == ("task", BETA)
        release.set()
        await settle(app, pilot)
        assert "Which way?" not in chat_text(app) and CHAT_LOADING in chat_text(app)
        await pilot.press("tab")
        await settle(app, pilot)
        assert "done" in chat_text(app) and "Which way?" not in chat_text(app)

    run(scenario, root, locator=attention_locator())


@pytest.mark.parametrize("failing", ["read_attention", "read_chat"])
def test_a_failing_attention_or_chat_read_keeps_the_task_table_and_names_the_error(tmp_path, monkeypatch, failing):
    root = attention_root(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("locator down")

    async def scenario(app, pilot):
        if failing == "read_chat":
            await open_task(app, pilot, ALPHA)
            monkeypatch.setattr(tui, failing, boom)
            await pilot.press("tab")
            await settle(app, pilot)
        assert ALPHA in table_ids(app)
        assert "live tasks" in app.sub_title and "read failed: locator down" in app.sub_title
        assert table_cells(app, row_of(app, ALPHA))["wait"] == ("" if failing == "read_attention" else "?")

    if failing == "read_attention":
        monkeypatch.setattr(tui, failing, boom)
    run(scenario, root, locator=attention_locator())


def test_the_chat_is_read_only_while_the_chat_tab_is_open(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    _, reads = gate_chat_reads(monkeypatch, None)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        await pilot.press("r")
        await settle(app, pilot)
        assert reads == []
        await pilot.press("tab")
        await settle(app, pilot)
        assert reads and set(reads) == {("task", ALPHA)}
        await pilot.press("escape")
        await settle(app, pilot)
        seen = len(reads)
        await pilot.press("r")
        await settle(app, pilot)
        assert len(reads) == seen

    run(scenario, root, locator=attention_locator())


# --- g: jump to the session's own window ------------------------------------------------------

def jumping_locator():
    def jump_target(harness):
        return {"argv": ["placeholder-no-op", harness], "label": f"window of {harness}", "return_hint": "tab keys back"}

    base = attention_locator()
    return SimpleNamespace(NAME="fake", sessions=base.sessions, permission_wait=base.permission_wait,
                           jump_target=jump_target)


def record_jumps(monkeypatch, returncode=0, stderr=""):
    """Replace the process runner below locate's own, so the runner the app really uses is the one tested."""
    calls = []

    def run_process(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    monkeypatch.setattr(locate.subprocess, "run", run_process)
    return calls


def status_text(app):
    return str(app.query_one("#status", Static).render())


def test_g_is_bound():
    assert "g" in {binding.key for binding in DashboardApp.BINDINGS}


def test_g_on_the_tasks_tab_jumps_to_the_task_under_the_cursor_and_names_the_way_back(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    calls = record_jumps(monkeypatch)

    async def scenario(app, pilot):
        target = row_of(app, ALPHA)
        while app.view.cursor != target:
            await pilot.press("down")
        await pilot.press("g")
        await settle(app, pilot)
        assert [argv for argv, _ in calls] == [["placeholder-no-op", ALPHA]]
        assert calls[0][1]["shell"] is False and calls[0][1]["timeout"] == 5
        assert f"window of {ALPHA}" in status_text(app) and "tab keys back" in status_text(app)
        assert app.query_one(TabbedContent).active == "tasks"

    with guarded(root):
        run(scenario, root, locator=jumping_locator())


@pytest.mark.parametrize("tabs", [1, 2])
def test_g_works_from_the_detail_and_chat_tabs(tmp_path, monkeypatch, tabs):
    root = attention_root(tmp_path)
    calls = record_jumps(monkeypatch)

    async def scenario(app, pilot):
        await open_task(app, pilot, ALPHA)
        for _ in range(tabs - 1):
            await pilot.press("tab")
            await settle(app, pilot)
        assert app.query_one(TabbedContent).active == ("detail", "chat")[tabs - 1]
        await pilot.press("g")
        await settle(app, pilot)
        assert [argv for argv, _ in calls] == [["placeholder-no-op", ALPHA]]
        assert "tab keys back" in status_text(app)

    with guarded(root):
        run(scenario, root, locator=jumping_locator())


def test_g_on_a_session_row_uses_that_rows_own_harness_session(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    calls = record_jumps(monkeypatch)

    async def scenario(app, pilot):
        target = row_of(app, ASK_SESSION)
        while app.view.cursor != target:
            await pilot.press("down")
        await pilot.press("g")
        await settle(app, pilot)
        assert [argv for argv, _ in calls] == [["placeholder-no-op", ASK_SESSION]]
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("g")
        await settle(app, pilot)
        assert [argv for argv, _ in calls][-1] == ["placeholder-no-op", ASK_SESSION]

    run(scenario, root, locator=jumping_locator())


def test_a_failed_jump_shows_its_reason_and_the_view_goes_on(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    record_jumps(monkeypatch, returncode=1, stderr="no such window")

    async def scenario(app, pilot):
        target = row_of(app, ALPHA)
        while app.view.cursor != target:
            await pilot.press("down")
        await pilot.press("g")
        await settle(app, pilot)
        assert "failed" in status_text(app) and "no such window" in status_text(app)
        await pilot.press("r")
        await settle(app, pilot)
        assert table_ids(app)

    run(scenario, root, locator=jumping_locator())


def test_g_without_a_locator_says_so_and_runs_nothing(tmp_path, monkeypatch):
    root = attention_root(tmp_path)
    calls = record_jumps(monkeypatch)

    async def scenario(app, pilot):
        await pilot.press("g")
        await settle(app, pilot)
        assert status_text(app).startswith("jump:") and calls == []

    run(scenario, root)
