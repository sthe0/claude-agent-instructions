"""What the web page renders: its own script run under node against a DOM stand-in, plus the
colour rules of its stylesheet.

``test_plan_dashboard_web.py`` proves the server and the JSON; this file proves what a click
shows. The page script runs in ``plan_dashboard_dom_shim.js`` with the real serializer's JSON
behind a fake fetch, and the observations come back as JSON for the assertions here. The shim
lays nothing out, so "in view" is the CSS rule that places the panel, read through
getComputedStyle. Contrast is computed in Python from the page's own custom properties.

Node is found through ``PLAN_DASHBOARD_NODE`` or ``PATH``. When it is not found the node-backed
tests fail under ``PLAN_DASHBOARD_REQUIRE_NODE=1`` and skip otherwise; the stylesheet tests need
no node. The mutation plugin's ``WEB_PAGE_MUTATIONS`` break the page bytes one property at a time.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from plan_dashboard import attention, model, web
from plan_dashboard_support import ALPHA, BETA, NOW, at, console_root, isolated_home, iso  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
SHIM = SCRIPTS / "tests" / "plan_dashboard_dom_shim.js"
LIVENESS = ("active", "idle", "stale", "resolved", "stub")
WAITING = ("choice", "permission", "idle")
STATUSES = ("active", "failed", "passed", "pending")
MIN_CONTRAST = 4.5
MEDIA_DARK = "@media (prefers-color-scheme: dark)"


def find_node() -> str | None:
    candidate = os.environ.get("PLAN_DASHBOARD_NODE") or shutil.which("node")
    if not candidate:
        return None
    try:
        done = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if done.returncode == 0 else None


@pytest.fixture(scope="session")
def node():
    found = find_node()
    if found is None:
        if os.environ.get("PLAN_DASHBOARD_REQUIRE_NODE") == "1":
            pytest.fail("PLAN_DASHBOARD_REQUIRE_NODE=1 but node was not found (PLAN_DASHBOARD_NODE or PATH)")
        pytest.skip("node not found")
    return found


SCENARIO = """
const fs = require("fs");
const shim = require(%(shim)s);
const input = JSON.parse(fs.readFileSync(%(input)s, "utf8"));
(async () => {
  const win = shim.load({html: fs.readFileSync(%(page)s, "utf8"), routes: input.routes, dark: input.dark});
  const doc = win.document, out = {};
  const panel = doc.getElementById("detail");
  const rows = () => doc.getElementById("tasks").children;
  const rowOf = (id) => rows().find((r) => r.dataset.session === id);
  const pick = (style) => ({position: style.position, top: style.top, right: style.right, left: style.left,
    maxHeight: style["max-height"], overflowY: style["overflow-y"], display: style.display, background: style.background});
  win.emit("snapshot", input.snapshot);
  await win.settle();
%(steps)s
  console.log("RESULT " + JSON.stringify(out));
})().catch((e) => { console.error(e.stack); process.exit(2); });
"""


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


def say(text, minutes=9):
    return line("assistant", minutes, [{"type": "text", "text": text}], "end_turn")


ASKING = [line("user", 10, "go on"), line("assistant", 9, [tool_use("q1", "AskUserQuestion", QUESTION)], "tool_use")]
IDLE = [line("user", 10, "go on"), say("done")]
WORKING = [line("user", 10, "go on"), line("assistant", 9, [tool_use("t1", "Bash", {"command": "ls"})], "tool_use")]


def attention_root(tmp_path, alpha=ASKING):
    """ALPHA asks, BETA is idle, and three sessions no task owns: one asks, one waits on a permission
    prompt, one is idle."""
    root = console_root(tmp_path)
    write_fixture_transcript(root, ALPHA, alpha)
    write_fixture_transcript(root, BETA, IDLE)
    write_fixture_transcript(root, ASK_SESSION, ASKING)
    write_fixture_transcript(root, PERMISSION_SESSION, WORKING)
    write_fixture_transcript(root, FREE_SESSION, IDLE)
    return root


def attention_locator():
    sessions = [{"harness_session": ASK_SESSION, "cwd": "/work/ask", "name": "Asker"},
                {"harness_session": PERMISSION_SESSION, "cwd": None, "name": None},
                {"harness_session": FREE_SESSION, "cwd": "/work/free", "name": "Free"}]
    return SimpleNamespace(NAME="fake", sessions=lambda: sessions,
                           permission_wait=lambda harness: harness == PERMISSION_SESSION)


def attention_routes(root, locator, task_ids):
    """What ``/api/attention`` and the chat endpoints answer now, from the real attention reader."""
    overlays, rows = attention.collect(task_ids, locator, root.transcripts_root)
    routes = {"/api/attention": attention.event_payload(overlays, rows)}
    for sid in task_ids:
        routes[f"/api/task/{sid}/chat"] = attention.task_chat(sid, locator, root.transcripts_root)
    for row in rows:
        harness = row["harness_session"]
        routes[f"/api/session/{harness}/chat"] = attention.chat_tail(harness, locator, root.transcripts_root)
    return routes


def run_page(node, tmp_path, root, steps, *, dark=False, locator=None, grow=None):
    """Run ``steps`` (JavaScript with win, doc, panel, rows, rowOf, pick, out and input in scope)
    against the page the server would send now; returns the ``out`` object it filled. ``grow`` is
    called once the first answers are built; the answers read after it are ``input.later``."""
    now = dt.datetime.fromtimestamp(NOW.timestamp(), dt.timezone.utc)
    snapshot = json.loads(model.to_json(model.collect(
        root.state_root, now=now, window_hours=24, include_all=False, sessions=(), cost_log=root.cost_log)))
    routes = {"/api/snapshot": snapshot}
    task_ids = [task["session_id"] for task in snapshot["tasks"]]
    for sid in task_ids:
        detail = model.detail(root.state_root, sid, cost_log=root.cost_log, transcripts_root=root.transcripts_root)
        routes[f"/api/task/{sid}"] = json.loads(model.to_json(detail))
    routes.update(attention_routes(root, locator, task_ids))
    later = {}
    if grow is not None:
        grow(root)
        later = attention_routes(root, locator, task_ids)
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    page, data, script = work / "page.html", work / "input.json", work / "scenario.js"
    page.write_bytes(web.page_bytes())
    data.write_text(json.dumps({"snapshot": snapshot, "routes": routes, "later": later, "dark": dark,
                                "alpha": ALPHA, "beta": BETA, "ask": ASK_SESSION, "permission": PERMISSION_SESSION,
                                "free": FREE_SESSION}), encoding="utf-8")
    script.write_text(SCENARIO % {"shim": json.dumps(str(SHIM)), "input": json.dumps(str(data)),
                                  "page": json.dumps(str(page)), "steps": steps}, encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, cwd=work)
    assert done.returncode == 0, done.stderr[-2000:]
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT ")][-1]
    result = json.loads(line[len("RESULT "):])
    result["snapshot"], result["routes"] = snapshot, routes
    return result


# ---- the rendered page ----------------------------------------------------------------------

OPEN_ALPHA = """
  out.hiddenBefore = panel.hidden;
  out.displayBefore = win.computed(panel).display;
  out.rowIds = rows().map((r) => r.dataset.session);
  out.rowTabindex = rows().map((r) => r.getAttribute("tabindex"));
  rowOf(input.alpha).click();
  await win.settle();
  out.hidden = panel.hidden;
  out.text = panel.textContent;
  out.title = doc.getElementById("detail-title").textContent;
  out.titleAttr = doc.getElementById("detail-title").title;
  out.active = doc.activeElement.id;
  out.selected = rows().filter((r) => r.classList.contains("selected")).map((r) => r.dataset.session);
  out.requests = win.requests;
  out.style = pick(win.computed(panel));
  out.phaseRows = panel.querySelectorAll("tbody tr").length;
"""


def test_a_row_click_opens_the_hidden_panel_filled_with_that_task_focused_and_the_row_selected(node, tmp_path):
    root = console_root(tmp_path)
    out = run_page(node, tmp_path, root, OPEN_ALPHA)
    assert out["hiddenBefore"] is True and out["displayBefore"] == "none"
    assert {ALPHA, BETA, "broken"} <= set(out["rowIds"]) and set(out["rowTabindex"]) == {"0"}
    assert out["hidden"] is False and out["active"] == "detail"
    assert out["selected"] == [ALPHA]
    assert f"/api/task/{ALPHA}" in out["requests"]
    detail = out["routes"][f"/api/task/{ALPHA}"]
    assert out["title"] == ALPHA[:8] and out["titleAttr"] == ALPHA
    assert "Phases" in out["text"] and "Stages" in out["text"]
    assert all(stage["title"] in out["text"] for stage in detail["stages"])
    assert detail["phases"] and all(phase["label"] in out["text"] for phase in detail["phases"])
    assert out["phaseRows"] >= len(detail["phases"])


def test_a_task_row_starts_with_the_task_name_and_shows_only_the_first_eight_characters_of_the_session_id(node, tmp_path):
    steps = """
  out.rows = rows().map((r) => ({id: r.dataset.session, title: r.title, cells: r.children.map((c) => c.textContent)}));
  out.head = doc.getElementById("head").children.map((c) => c.textContent);
"""
    out = run_page(node, tmp_path, console_root(tmp_path), steps)
    by_id = {row["id"]: row for row in out["rows"]}
    task = {t["session_id"]: t for t in out["snapshot"]["tasks"]}[ALPHA]
    assert out["head"][:3] == ["task", "wait", "session"]
    cells = by_id[ALPHA]["cells"]
    assert cells[0] == task["task_id"] and task["task_id"]
    assert cells[out["head"].index("session")] == ALPHA[:8] and by_id[ALPHA]["title"] == ALPHA


def test_the_rule_for_the_panel_pins_it_to_the_viewport_and_bounds_its_height(node, tmp_path):
    out = run_page(node, tmp_path, console_root(tmp_path), OPEN_ALPHA)
    style = out["style"]
    assert style["position"] == "fixed" and style["display"] != "none"
    for side in ("top", "right"):
        assert re.fullmatch(r"\d+(\.\d+)?(px|rem|em|vh|vw)", style[side]), (side, style[side])
    assert re.search(r"\d\s*vh\b", style.get("maxHeight", "")), style
    assert style.get("overflowY") == "auto", style


def test_enter_on_a_focused_row_opens_the_panel_and_another_key_does_not(node, tmp_path):
    steps = """
  const row = rowOf(input.alpha);
  row.focus();
  out.focused = doc.activeElement === row;
  win.key("a");
  await win.settle();
  out.openedByA = !panel.hidden;
  win.key("Enter");
  await win.settle();
  out.opened = !panel.hidden;
  out.active = doc.activeElement.id;
  out.selected = rows().filter((r) => r.classList.contains("selected")).map((r) => r.dataset.session);
  out.text = panel.textContent;
"""
    out = run_page(node, tmp_path, console_root(tmp_path), steps)
    assert out["focused"] is True and out["openedByA"] is False
    assert out["opened"] is True and out["active"] == "detail" and out["selected"] == [ALPHA]
    assert ALPHA[:8] in out["text"]


@pytest.mark.parametrize("how", ["esc", "close"])
def test_esc_and_the_close_control_each_close_the_panel_and_return_focus_to_its_row(node, tmp_path, how):
    steps = OPEN_ALPHA + """
  if (input.how === "esc") win.key("Escape"); else doc.getElementById("detail-close").click();
  await win.settle();
  out.hiddenAfter = panel.hidden;
  out.activeAfter = doc.activeElement.dataset.session;
  out.selectedAfter = rows().filter((r) => r.classList.contains("selected")).length;
"""
    steps = steps.replace("input.how", json.dumps(how))
    out = run_page(node, tmp_path, console_root(tmp_path), steps)
    assert out["hidden"] is False
    assert out["hiddenAfter"] is True and out["activeAfter"] == ALPHA and out["selectedAfter"] == 0


def test_a_second_row_replaces_the_panel_content_and_esc_with_the_panel_closed_does_nothing(node, tmp_path):
    steps = """
  win.key("Escape", doc.body);
  out.stillHidden = panel.hidden;
  rowOf(input.alpha).click();
  await win.settle();
  rowOf(input.beta).click();
  await win.settle();
  out.text = panel.textContent;
  out.requests = win.requests;
"""
    out = run_page(node, tmp_path, console_root(tmp_path), steps)
    assert out["stillHidden"] is True
    assert BETA[:8] in out["text"] and ALPHA[:8] not in out["text"]
    details = [r for r in out["requests"] if r.startswith("/api/task/") and not r.endswith("/chat")]
    assert details == [f"/api/task/{ALPHA}", f"/api/task/{BETA}"]


def test_liveness_stays_in_the_row_text_and_each_row_takes_its_liveness_background(node, tmp_path):
    steps = """
  out.rows = rows().map((r) => ({id: r.dataset.session, text: r.textContent, classes: r.className,
    background: win.computed(r).background}));
"""
    out = run_page(node, tmp_path, console_root(tmp_path), steps)
    light, _ = theme_vars()
    by_id = {t["session_id"]: t for t in out["snapshot"]["tasks"]}
    assert out["rows"]
    for row in out["rows"]:
        liveness = by_id[row["id"]]["liveness"]
        assert liveness in row["text"], row
        assert f"live-{liveness}" in row["classes"].split()
        assert row.get("background") == light[f"--bg-live-{liveness}"], row


def test_the_dark_theme_is_what_the_page_computes_when_the_browser_prefers_it(node, tmp_path):
    steps = OPEN_ALPHA + "\n  out.bodyBackground = win.computed(doc.body).background;\n"
    root = console_root(tmp_path)
    dark_out = run_page(node, tmp_path, root, steps, dark=True)
    light_out = run_page(node, tmp_path, root, steps)
    light, dark = theme_vars()
    assert dark_out["style"]["background"] == dark["--bg-panel"]
    assert light_out["style"]["background"] == light["--bg-panel"]
    assert dark_out["bodyBackground"] == dark["--bg"] and light_out["bodyBackground"] == light["--bg"]


# ---- waiting marks, session rows and the Chat section --------------------------------------

OPEN_THE_SOURCE = """
  const open = () => { if (win.sources[0].onopen) win.sources[0].onopen({type: "open"}); };
  const marks = () => rows().map((r) => ({id: r.dataset.session, classes: r.className.split(" "),
    mark: r.children[1].textContent, text: r.textContent, cells: r.children.map((c) => c.textContent),
    background: win.computed(r).background}));
  const attentionFetches = () => win.requests.filter((u) => u === "/api/attention").length;
  const chatEntries = () => doc.getElementById("detail-chat").children.slice(1).map((c) => c.textContent);
"""
LATER_BETA_ASKS = lambda root: append_fixture_transcript(root, BETA, [ASKING[1]])  # noqa: E731


def by_id(rows):
    return {row["id"]: row for row in rows}


def test_a_fresh_page_asks_for_the_attention_overlay_when_its_event_source_opens_and_marks_the_waiting_rows(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  out.before = marks();
  out.fetchesBefore = attentionFetches();
  open();
  await win.settle();
  out.after = marks();
  out.fetches = attentionFetches();
  out.waiting = doc.getElementById("waiting").textContent;
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    assert out["fetchesBefore"] == 0 and out["fetches"] == 1
    assert not [c for row in out["before"] for c in row["classes"] if c.startswith("wait-")]
    after, light = by_id(out["after"]), theme_vars()[0]
    expected = {ALPHA: "choice", BETA: "idle", ASK_SESSION: "choice", PERMISSION_SESSION: "permission", FREE_SESSION: "idle"}
    marks = {"choice": "?", "permission": "!", "idle": "…"}
    for sid, waiting in expected.items():
        assert f"wait-{waiting}" in after[sid]["classes"], sid
        assert after[sid]["mark"] == marks[waiting], sid
        assert after[sid]["background"] == light[f"--bg-wait-{waiting}"], sid
    assert after["broken"]["mark"] == "" and not [c for c in after["broken"]["classes"] if c.startswith("wait-")]
    assert out["waiting"] == "waiting: 3"


def test_idle_rows_are_marked_but_not_counted_among_the_waiting(node, tmp_path):
    root = attention_root(tmp_path, alpha=IDLE)
    write_fixture_transcript(root, ASK_SESSION, IDLE)
    write_fixture_transcript(root, PERMISSION_SESSION, IDLE)
    steps = OPEN_THE_SOURCE + """
  open();
  await win.settle();
  out.after = marks();
  out.waiting = doc.getElementById("waiting").textContent;
"""
    out = run_page(node, tmp_path, root, steps, locator=SimpleNamespace(
        NAME="fake", sessions=attention_locator().sessions, permission_wait=lambda harness: False))
    idle = [row for row in out["after"] if "wait-idle" in row["classes"]]
    assert len(idle) >= 4 and all(row["mark"] == "…" for row in idle)
    assert out["waiting"] == "waiting: 0"


def test_a_reopened_event_source_asks_for_the_overlay_again(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  open();
  await win.settle();
  win.sources[0].fail();
  open();
  await win.settle();
  out.fetches = attentionFetches();
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    assert out["fetches"] == 2


def test_an_attention_event_marks_a_row_and_moves_the_count_without_a_request(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  open();
  await win.settle();
  const beta = () => marks().find((r) => r.id === input.beta);
  out.beforeMark = beta().mark;
  out.fetches = attentionFetches();
  win.emit("attention", input.later["/api/attention"]);
  await win.settle();
  out.afterMark = beta().mark;
  out.afterClasses = beta().classes;
  out.fetchesAfter = attentionFetches();
  out.waiting = doc.getElementById("waiting").textContent;
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator(), grow=LATER_BETA_ASKS)
    assert out["beforeMark"] == "…" and out["afterMark"] == "?" and "wait-choice" in out["afterClasses"]
    assert out["fetchesAfter"] == out["fetches"] == 1
    assert out["waiting"] == "waiting: 4"


def test_a_session_row_shows_its_name_or_id_and_dashes_and_its_click_opens_only_the_chat(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  open();
  await win.settle();
  out.rows = marks();
  rowOf(input.ask).click();
  await win.settle();
  out.hidden = panel.hidden;
  out.text = panel.textContent;
  out.selected = rows().filter((r) => r.classList.contains("selected")).map((r) => r.dataset.session);
  out.requests = win.requests;
  out.entries = chatEntries();
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    rows = by_id(out["rows"])
    named, unnamed = rows[ASK_SESSION]["cells"], rows[PERMISSION_SESSION]["cells"]
    columns = ["task", "wait", "session", "live", "node", "stage", "wall", "cost", "goal"]
    assert named[columns.index("session")] == "Asker" and unnamed[columns.index("session")] == PERMISSION_SESSION
    assert {named[columns.index(c)] for c in ("node", "stage", "wall", "cost")} == {"—"}
    assert out["hidden"] is False and out["selected"] == [ASK_SESSION]
    assert f"/api/session/{ASK_SESSION}/chat" in out["requests"]
    assert not [r for r in out["requests"] if r == f"/api/task/{ASK_SESSION}"]
    assert "Which way?" in out["text"] and "Stages" not in out["text"] and "Phases" not in out["text"]
    assert len(out["entries"]) == 2


def test_the_chat_shows_an_ask_calls_question_and_labels_beside_its_tool_name(node, tmp_path):
    steps = """
  rowOf(input.alpha).click();
  await win.settle();
  out.entries = doc.getElementById("detail-chat").children.slice(1).map((c) => c.children.map((x) => x.textContent));
  out.text = panel.textContent;
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    who, text = [e for e in out["entries"] if "[AskUserQuestion]" in e[0]][0]
    assert who == "assistant [AskUserQuestion]"
    assert text.splitlines() == ["Which way?", "- Left", "- Right"]
    assert out["entries"][0] == ["you", "go on"]
    assert "HEADER-TEXT" not in out["text"] and "DESC-LEFT" not in out["text"]


def test_chat_text_is_shown_as_text_and_never_as_markup(node, tmp_path):
    markup = '<img src=x onerror="boom()"><b>bold</b>'
    root = attention_root(tmp_path, alpha=[line("user", 10, markup), say("ok")])
    steps = """
  rowOf(input.alpha).click();
  await win.settle();
  out.text = panel.textContent;
  out.images = panel.querySelectorAll("img").length;
"""
    out = run_page(node, tmp_path, root, steps, locator=attention_locator())
    assert markup in out["text"] and out["images"] == 0


def test_opening_the_panel_fetches_the_chat_and_a_poll_one_interval_later_shows_a_new_entry(node, tmp_path):
    grow = lambda root: append_fixture_transcript(root, ALPHA, [say("NEW-ENTRY", minutes=8)])  # noqa: E731
    steps = OPEN_THE_SOURCE + """
  const url = "/api/task/" + input.alpha + "/chat";
  rowOf(input.alpha).click();
  await win.settle();
  out.first = chatEntries().join("|");
  out.fetchesFirst = win.requests.filter((u) => u === url).length;
  out.timers = win.clock.live().filter((t) => t.repeat).map((t) => t.ms);
  input.routes[url] = input.later[url];
  win.clock.advance(2000);
  await win.settle();
  out.second = chatEntries().join("|");
  out.fetchesSecond = win.requests.filter((u) => u === url).length;
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator(), grow=grow)
    assert "NEW-ENTRY" not in out["first"] and "NEW-ENTRY" in out["second"]
    assert out["fetchesFirst"] == 1 and out["fetchesSecond"] == 2
    assert 2000 in out["timers"] and web.DEFAULT_INTERVAL_S * 1000 == 2000


def test_closing_the_panel_stops_the_chat_poll(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  const url = "/api/task/" + input.alpha + "/chat";
  const polls = () => win.clock.live().filter((t) => t.repeat && t.ms === 2000).length;
  rowOf(input.alpha).click();
  await win.settle();
  out.pollsOpen = polls();
  win.key("Escape");
  await win.settle();
  out.pollsClosed = polls();
  const before = win.requests.filter((u) => u === url).length;
  win.clock.advance(10000);
  await win.settle();
  out.requestsAfterClose = win.requests.filter((u) => u === url).length - before;
  out.chatAfterClose = doc.getElementById("detail-chat").children.length;
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    assert out["pollsOpen"] == 1 and out["pollsClosed"] == 0
    assert out["requestsAfterClose"] == 0 and out["chatAfterClose"] == 0


HOLD_ONE_ANSWER = """
  const real = win.context.fetch;
  const hold = (path, where) => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    win.context.fetch = async (url) => {
      const response = await real(url);
      if (String(url).split("?")[0] !== path) return response;
      if (where === "fetch") { await gate; return response; }
      return {ok: response.ok, status: response.status, json: async () => { await gate; return response.json(); }};
    };
    return release;
  };
  const chatBody = () => doc.getElementById("detail-chat").textContent;
"""


@pytest.mark.parametrize("where", ["fetch", "json"])
def test_a_chat_answer_that_arrives_after_another_row_was_opened_is_dropped(node, tmp_path, where):
    steps = OPEN_THE_SOURCE + HOLD_ONE_ANSWER + """
  const late = hold("/api/task/" + input.alpha + "/chat", %r);
  rowOf(input.alpha).click();
  await win.settle();
  rowOf(input.beta).click();
  await win.settle();
  out.beforeRelease = chatBody();
  late();
  await win.settle();
  out.afterRelease = chatBody();
  out.shownBeta = chatEntries();
""" % where
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    assert "Which way?" not in out["beforeRelease"] and "Which way?" not in out["afterRelease"]
    assert any("done" in entry for entry in out["shownBeta"])


def test_opening_another_row_clears_the_chat_and_shows_that_rows_even_when_both_chats_are_equal(node, tmp_path):
    root = attention_root(tmp_path)
    write_fixture_transcript(root, BETA, ASKING)
    steps = OPEN_THE_SOURCE + HOLD_ONE_ANSWER + """
  rowOf(input.alpha).click();
  await win.settle();
  out.alpha = chatBody();
  const late = hold("/api/task/" + input.beta + "/chat", "fetch");
  rowOf(input.beta).click();
  await win.settle();
  out.whileLoading = chatBody();
  late();
  await win.settle();
  out.beta = chatBody();
"""
    out = run_page(node, tmp_path, root, steps, locator=attention_locator())
    assert "Which way?" in out["alpha"]
    assert "Which way?" not in out["whileLoading"] and "loading" in out["whileLoading"]
    assert "Which way?" in out["beta"] and "loading" not in out["beta"]


def test_a_failed_attention_fetch_leaves_the_overlay_already_shown(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  open();
  await win.settle();
  out.before = marks();
  const real = win.context.fetch;
  win.context.fetch = async (url) => String(url) === "/api/attention"
    ? {ok: false, status: 500, json: async () => ({error: "boom"})} : real(url);
  open();
  await win.settle();
  out.after = marks();
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    assert any(row["mark"] for row in out["before"]) and out["after"] == out["before"]


def test_an_open_panel_survives_a_new_snapshot_event_and_a_clock_tick(node, tmp_path):
    steps = OPEN_THE_SOURCE + """
  rowOf(input.alpha).click();
  await win.settle();
  win.emit("snapshot", input.snapshot);
  await win.settle();
  win.clock.advance(1000);
  await win.settle();
  out.hidden = panel.hidden;
  out.text = panel.textContent;
  out.selected = rows().filter((r) => r.classList.contains("selected")).map((r) => r.dataset.session);
  out.entries = chatEntries();
"""
    out = run_page(node, tmp_path, attention_root(tmp_path), steps, locator=attention_locator())
    detail = out["routes"][f"/api/task/{ALPHA}"]
    assert out["hidden"] is False and out["selected"] == [ALPHA]
    assert all(stage["title"] in out["text"] for stage in detail["stages"])
    assert any("Which way?" in entry for entry in out["entries"])


# ---- the stylesheet ------------------------------------------------------------------------


def page_css() -> str:
    text = web.page_bytes().decode("utf-8")
    return text.split("<style>", 1)[1].split("</style>", 1)[0]


def balanced(text: str, open_at: int) -> str:
    depth, end = 0, open_at
    while end < len(text):
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        end += 1
        if depth == 0:
            break
    return text[open_at + 1:end - 1]


def custom_properties(block: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def theme_vars() -> tuple[dict[str, str], dict[str, str]]:
    """The light theme's custom properties and the dark theme's, the dark one laid over the light."""
    css = page_css()
    media = css.index(MEDIA_DARK)
    light_start = css.index(":root", 0, media)
    light = custom_properties(balanced(css, css.index("{", light_start)))
    media_body = balanced(css, css.index("{", media))
    dark_block = balanced(media_body, media_body.index("{", media_body.index(":root")) )
    dark_only = custom_properties(dark_block)
    return light, {**light, **dark_only}


def dark_only_names() -> set[str]:
    css = page_css()
    media_body = balanced(css, css.index("{", css.index(MEDIA_DARK)))
    return set(custom_properties(balanced(media_body, media_body.index("{", media_body.index(":root")))))


def luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    assert re.fullmatch(r"[0-9a-fA-F]{6}", digits), colour
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_luminance_formula_matches_the_wcag_reference_values():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)


def test_the_dark_theme_redefines_every_colour_the_light_theme_defines():
    light, _ = theme_vars()
    assert set(light) == dark_only_names()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_text_colour_meets_wcag_aa_on_every_background_of_its_theme(theme):
    light, dark = theme_vars()
    colours = light if theme == "light" else dark
    texts = {n: v for n, v in colours.items() if n.startswith("--text")}
    backgrounds = {n: v for n, v in colours.items() if n.startswith("--bg")}
    assert len(texts) >= 5 and len(backgrounds) >= 10
    low = {(t, b): round(contrast(tv, bv), 2) for t, tv in texts.items() for b, bv in backgrounds.items()
           if contrast(tv, bv) < MIN_CONTRAST}
    assert not low, low


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_liveness_and_waiting_backgrounds_are_told_apart_from_each_other_and_the_page(theme):
    light, dark = theme_vars()
    colours = light if theme == "light" else dark
    names = [f"--bg-live-{n}" for n in LIVENESS] + [f"--bg-wait-{n}" for n in WAITING] + ["--bg", "--bg-selected", "--bg-hover"]
    values = [colours[n].lower() for n in names]
    assert len(set(values)) == len(values), dict(zip(names, values))


def test_every_liveness_waiting_and_status_class_has_a_colour_rule_and_every_variable_is_defined():
    css = page_css()
    for name in LIVENESS:
        assert re.search(rf"tr\.live-{name}\s*\{{[^}}]*background:\s*var\(--bg-live-{name}\)", css), name
    for name in WAITING:
        assert re.search(rf"tr\.wait-{name}\s*\{{[^}}]*background:\s*var\(--bg-wait-{name}\)", css), name
    for name in STATUSES:
        assert re.search(rf"\.st-{name}\b[^{{]*\{{[^}}]*color:\s*var\(--text-", css), name
    assert re.search(r"tr\.task:hover\s*\{[^}]*background:\s*var\(--bg-hover\)", css)
    assert re.search(r"tr\.task\.selected\s*\{[^}]*background:\s*var\(--bg-selected\)", css)
    light, _ = theme_vars()
    assert set(re.findall(r"var\((--[\w-]+)\)", css)) <= set(light)


def test_the_page_selects_its_theme_from_the_browser_preference_and_loads_no_asset():
    text = web.page_bytes().decode("utf-8")
    assert MEDIA_DARK in text
    assert not re.search(r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//|@import", text)
