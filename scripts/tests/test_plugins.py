"""The plugin layer: registration vs activation, observer firing, gate folding,
lifecycle (manual + phase-scoped terminal auto-retire), and schema-5 migration.

The built-in `dummy` plugin (plugins.py) is the primary fixture — its presence in
the REGISTRY proves import-time registration with ZERO edits to the three core
literals (machine.TRANSITIONS / gates.GUARDIANS / cli.COMMANDS). End-to-end firing
is exercised through cli.main() so the real central wiring (_fire_plugins) runs.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agentctl import cli, plugins
from agentctl.directive import Directive
from agentctl.state import Node, SessionState


def ns(**kw):
    return Namespace(**kw)


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


# --- registration vs activation ----------------------------------------------

def test_dummy_registered_at_import():
    # import-time registration: the catalog knows the plugin without any core edit
    assert "dummy" in plugins.REGISTRY
    assert plugins.REGISTRY["dummy"].scope == "phase"


def test_inactive_session_is_unaffected():
    state = _new_state()
    assert plugins.active(state) == []
    d = Directive(True, state.node, "x")
    assert plugins.fire("approve", state, d) == []
    assert "plugin_directives" not in d.data
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_activate_seeds_bag_and_is_idempotent():
    state = _new_state()
    plugins.activate(state, "dummy")
    assert state.plugins["dummy"] == {"observed": 0, "cleared": False}
    # re-activation merges a seed without wiping accumulated bag state
    state.plugins["dummy"]["observed"] = 3
    plugins.activate(state, "dummy", {"extra": 1})
    assert state.plugins["dummy"]["observed"] == 3
    assert state.plugins["dummy"]["extra"] == 1


# --- observer firing ----------------------------------------------------------

def test_observer_emits_directive_and_mutates_bag():
    state = _new_state()
    plugins.activate(state, "dummy")
    d = Directive(True, state.node, "approve")
    fired = plugins.fire("approve", state, d)
    assert len(fired) == 1
    assert fired[0]["plugin"] == "dummy"
    assert state.plugins["dummy"]["observed"] == 1
    assert d.data["plugin_directives"][0]["action"] == "noted_approve"


def test_unobserved_event_is_silent():
    state = _new_state()
    plugins.activate(state, "dummy")
    d = Directive(True, state.node, "classify")
    assert plugins.fire("classify", state, d) == []  # dummy observes only 'approve'


# --- gate folding -------------------------------------------------------------

def test_plugin_gate_blocks_then_clears():
    state = _new_state()
    plugins.activate(state, "dummy")
    assert plugins.plugin_gate_blockers(state, "resolution")  # bag not cleared -> blocks
    state.plugins["dummy"]["cleared"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []  # cleared -> passes


def test_plugin_gate_keyed_to_other_gate_does_not_leak():
    state = _new_state()
    plugins.activate(state, "dummy")
    # dummy gates only 'resolution'; it must not appear under 'plan_approval'
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


# --- lifecycle: phase-scoped terminal auto-retire + manual deactivate ---------

def test_phase_scoped_terminal_auto_retires():
    state = _new_state()
    plugins.activate(state, "dummy")
    d = Directive(True, state.node, "unblock")
    plugins.fire("unblock", state, d)  # dummy.terminal fires on 'unblock'
    assert "dummy" not in state.plugins          # dropped from active set
    assert "dummy" in state.plugins_archive       # archived for audit
    assert plugins.active(state) == []


def test_manual_deactivate_archives():
    state = _new_state()
    plugins.activate(state, "dummy")
    assert plugins.deactivate(state, "dummy") is True
    assert "dummy" not in state.plugins
    assert "dummy" in state.plugins_archive
    assert plugins.deactivate(state, "dummy") is False  # already gone


# --- schema migration ---------------------------------------------------------

def test_old_schema5_state_migrates():
    # a schema-5 JSON record has no plugin layer; it must load with empty bags
    legacy = {
        "session_id": "old", "task_id": "t", "goal": "", "overall_done_criterion": "",
        "overall_criterion_type": "measurable", "weight_class": None, "route": None,
        "node": "CLASSIFIED", "blocked_from": None, "plan_path": None,
        "plan_verified": False, "partition": None, "permission_request": None,
        "difficulty": None,
        "approval": {"name": "plan_approval", "armed": False, "passed": False, "by": None, "note": None},
        "resolution": {"name": "resolution", "armed": False, "passed": False, "by": None, "note": None},
        "stages": [], "current_stage": None, "recursion_depth": 0,
        "artifacts": [], "history": [], "schema_version": 5,
    }
    state = SessionState.from_dict(legacy)
    assert state.plugins == {}
    assert state.plugins_archive == {}
    # round-trips at the new schema
    assert SessionState.from_json(state.to_json()).plugins == {}


# --- custom plugin: gate keyed to plan_approval (generality, not just dummy) --

@pytest.fixture
def temp_plugin():
    name = "tplug_gate"
    plugins.register(plugins.Plugin(
        name=name,
        gates={"plan_approval": lambda state, bag: (["temp: not ready"] if not bag.get("ready") else [])},
        state_factory=lambda: {"ready": False},
    ))
    yield name
    plugins.REGISTRY.pop(name, None)


def test_custom_plugin_gates_plan_approval(temp_plugin):
    state = _new_state()
    plugins.activate(state, temp_plugin)
    assert plugins.plugin_gate_blockers(state, "plan_approval")
    state.plugins[temp_plugin]["ready"] = True
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


# --- end-to-end through cli.main() (the real _fire_plugins wiring) ------------

def _run(capsys, root, *argv):
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _drive_to_plan_ready(capsys, root, sid, plan):
    _run(capsys, root, "start", "--session", sid, "--task", "t",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--architectural")
    _run(capsys, root, "plan", "--session", sid)
    _run(capsys, root, "submit-plan", "--session", sid, "--plan", plan)


def test_cli_main_fires_observer_on_approve(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "e2e1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_plan_ready(capsys, root, sid, plan)
    _run(capsys, root, "plugin-activate", "--session", sid, "--plugin", "dummy")
    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 0
    assert d["node"] == Node.APPROVED.value
    pds = d["data"]["plugin_directives"]
    assert any(p["plugin"] == "dummy" and p["action"] == "noted_approve" for p in pds)


def test_cli_main_plugin_gate_blocks_resolve_then_passes(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "e2e2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_plan_ready(capsys, root, sid, plan)
    _run(capsys, root, "plugin-activate", "--session", sid, "--plugin", "dummy")
    _run(capsys, root, "approve", "--session", sid, "--by", "user")
    _run(capsys, root, "partition", "--session", sid)
    for _ in range(2):
        _run(capsys, root, "next-stage", "--session", sid)
        _run(capsys, root, "record-result", "--session", sid, "--status", "passed", "--actual", "ok")
    _run(capsys, root, "verify-final", "--session", sid)
    # dummy's resolution gate blocks the final resolve
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 1
    assert any("dummy" in b for b in d["data"]["blockers"])
    # manual deactivate removes the gate -> resolve passes
    _run(capsys, root, "plugin-deactivate", "--session", sid, "--plugin", "dummy")
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value
    assert d["marker"] == "COMPLETED"


def test_cli_main_plugin_less_session_has_no_plugin_directives(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "e2e3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_plan_ready(capsys, root, sid, plan)
    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 0
    assert "plugin_directives" not in d["data"]  # no plugin active -> byte-identical behavior
