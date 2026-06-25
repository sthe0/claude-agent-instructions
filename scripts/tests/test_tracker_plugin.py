"""The tracker plugin: the first real consumer of the plugin layer.

Proves the tracker sub-state-machine attaches to the core spine with ZERO edits
to the three core literals (it registers at import via plugins.py's bottom import):
activation seeds the bag, the publish observers fire on the matching transitions,
the publish gate blocks `resolve` until the mandatory phases are recorded, and the
task-scoped plugin auto-retires once resolution actually passes. End-to-end paths
run through cli.main() so the real _fire_plugins wiring exercises it.

Also covers the state-aware reminder hook (hook-tracker-publish-reminder.py).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agentctl import cli, plugins
from agentctl import plugins_tracker as tp
from agentctl.directive import Directive
from agentctl.state import Node, SessionState

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-tracker-publish-reminder.py"


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


# --- registration & activation -----------------------------------------------

def test_tracker_registered_at_import():
    # importing the plugins module pulls the built-in consumer into the catalog
    assert "tracker" in plugins.REGISTRY
    assert plugins.REGISTRY["tracker"].scope == "task"


def test_activate_seeds_bag_with_tracker_key():
    state = _new_state()
    plugins.activate(state, "tracker", {"tracker_key": "ABC-123"})
    bag = state.plugins["tracker"]
    assert bag["tracker_key"] == "ABC-123"
    assert bag["published_phases"] == {}


# --- observers ----------------------------------------------------------------

def test_submit_plan_emits_publish_plan():
    state = _new_state()
    plugins.activate(state, "tracker", {"tracker_key": "ABC-1"})
    d = Directive(True, state.node, "submit_plan")
    fired = plugins.fire("submit_plan", state, d)
    assert [f["action"] for f in fired] == ["publish_plan"]
    assert fired[0]["plugin"] == "tracker"
    assert fired[0]["blocking"] is True
    assert fired[0]["data"]["tracker_key"] == "ABC-1"


def test_passed_stage_emits_publish_progress_but_diagnosing_is_silent():
    state = _new_state(node=Node.VERIFYING.value)
    plugins.activate(state, "tracker")
    d = Directive(True, state.node, "record_result")
    fired = plugins.fire("record_result", state, d)
    assert [f["action"] for f in fired] == ["publish_progress"]
    # a failed stage routes to DIAGNOSING -> no progress post (publish_replan covers it)
    state.node = Node.DIAGNOSING.value
    d2 = Directive(False, state.node, "record_result")
    assert plugins.fire("record_result", state, d2) == []


def test_replan_emits_publish_replan():
    state = _new_state()
    plugins.activate(state, "tracker")
    d = Directive(True, state.node, "replan")
    fired = plugins.fire("replan", state, d)
    assert [f["action"] for f in fired] == ["publish_replan"]


def test_resolve_emits_publish_result_until_recorded():
    state = _new_state(node=Node.RESOLUTION.value)
    plugins.activate(state, "tracker")
    d = Directive(False, state.node, "resolve")
    assert [f["action"] for f in plugins.fire("resolve", state, d)] == ["publish_result"]
    # once the result phase is recorded, the nudge goes silent
    state.plugins["tracker"]["published_phases"]["result"] = True
    d2 = Directive(True, state.node, "resolve")
    assert plugins.fire("resolve", state, d2) == []


# --- gate ---------------------------------------------------------------------

def test_publish_gate_blocks_until_mandatory_phases_recorded():
    state = _new_state()
    plugins.activate(state, "tracker")
    assert plugins.plugin_gate_blockers(state, "resolution")  # nothing published yet
    state.plugins["tracker"]["published_phases"]["plan"] = True
    assert plugins.plugin_gate_blockers(state, "resolution")  # result still missing
    state.plugins["tracker"]["published_phases"]["result"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []  # both recorded -> passes


def test_publish_gate_does_not_touch_plan_approval():
    state = _new_state()
    plugins.activate(state, "tracker")
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


# --- end-to-end through cli.main() (the real _fire_plugins + plugin-record) ----

def _run(capsys, root, *argv):
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _drive_to_resolution(capsys, root, sid, plan, *, activate_tracker):
    _run(capsys, root, "start", "--session", sid, "--task", "ABC-9",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--architectural")
    _run(capsys, root, "plan", "--session", sid)
    if activate_tracker:
        _run(capsys, root, "plugin-activate", "--session", sid, "--plugin", "tracker",
             "--tracker-key", "ABC-9")
    _run(capsys, root, "submit-plan", "--session", sid, "--plan", plan)
    _run(capsys, root, "approve", "--session", sid, "--by", "user")
    _run(capsys, root, "partition", "--session", sid)
    for _ in range(2):
        _run(capsys, root, "next-stage", "--session", sid)
        _run(capsys, root, "record-result", "--session", sid, "--status", "passed", "--actual", "ok")
    _run(capsys, root, "verify-final", "--session", sid)
    # experience auto-activates for every substantive session; satisfy its gate so
    # these tests isolate the tracker plugin's own gating behavior
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "searched")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "recorded")


def test_e2e_resolve_blocked_until_published_then_passes_and_retires(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "tk-e2e"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan, activate_tracker=True)

    # submit-plan fired publish_plan along the way
    # the gate blocks resolve while plan + result are unrecorded
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 1
    assert any("tracker" in b for b in d["data"]["blockers"])
    # the blocked resolve still surfaces the publish_result nudge
    assert any(p["action"] == "publish_result" for p in d["data"]["plugin_directives"])

    # record the two mandatory publications, then resolve passes
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "plan")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "result")
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value
    assert d["marker"] == "COMPLETED"

    # task-scoped: auto-retired on the passing resolve, bag archived for audit
    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    assert "tracker" not in raw["plugins"]
    assert "tracker" in raw["plugins_archive"]


def test_e2e_non_tracker_session_has_no_tracker_effect(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "tk-none"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan, activate_tracker=False)
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 0  # no tracker gate -> resolve passes immediately
    assert d["node"] == Node.RESOLVED.value
    assert "plugin_directives" not in d["data"]  # byte-identical to a plugin-less session


# --- the state-aware reminder hook -------------------------------------------

def _load_hook():
    spec = importlib.util.spec_from_file_location("hook_tracker_publish_reminder", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_lists_unpublished_phases_when_tracker_active():
    mod = _load_hook()
    state = {"node": "RESOLUTION", "plugins": {"tracker": {"published_phases": {"plan": True}}}}
    assert mod.unpublished_phases(state) == ["result"]


def test_hook_silent_when_tracker_inactive_or_complete():
    mod = _load_hook()
    assert mod.unpublished_phases({"plugins": {}}) == []
    full = {"plugins": {"tracker": {"published_phases": {"plan": True, "result": True}}}}
    assert mod.unpublished_phases(full) == []
