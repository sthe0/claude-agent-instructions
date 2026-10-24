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

import pytest

from plan_dashboard import model, web
from plan_dashboard_support import ALPHA, BETA, NOW, console_root, isolated_home  # noqa: F401

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


def run_page(node, tmp_path, root, steps, *, dark=False):
    """Run ``steps`` (JavaScript with win, doc, panel, rows, rowOf, pick, out and input in scope)
    against the page the server would send now; returns the ``out`` object it filled."""
    now = dt.datetime.fromtimestamp(NOW.timestamp(), dt.timezone.utc)
    snapshot = json.loads(model.to_json(model.collect(
        root.state_root, now=now, window_hours=24, include_all=False, sessions=(), cost_log=root.cost_log)))
    routes = {"/api/snapshot": snapshot}
    for task in snapshot["tasks"]:
        detail = model.detail(root.state_root, task["session_id"], cost_log=root.cost_log,
                              transcripts_root=root.transcripts_root)
        routes[f"/api/task/{task['session_id']}"] = json.loads(model.to_json(detail))
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    page, data, script = work / "page.html", work / "input.json", work / "scenario.js"
    page.write_bytes(web.page_bytes())
    data.write_text(json.dumps({"snapshot": snapshot, "routes": routes, "dark": dark, "alpha": ALPHA, "beta": BETA}),
                    encoding="utf-8")
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
    assert ALPHA in out["text"] and "Phases" in out["text"] and "Stages" in out["text"]
    assert all(stage["title"] in out["text"] for stage in detail["stages"])
    assert detail["phases"] and all(phase["label"] in out["text"] for phase in detail["phases"])
    assert out["phaseRows"] >= len(detail["phases"])


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
    assert ALPHA in out["text"]


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
    assert BETA in out["text"] and ALPHA not in out["text"]
    assert [r for r in out["requests"] if r.startswith("/api/task/")] == [f"/api/task/{ALPHA}", f"/api/task/{BETA}"]


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
