"""The record-experience plugin: a skill-less consumer of the plugin layer.

Proves the experience sub-state-machine attaches to the core spine with ZERO edits
to the three core literals (it registers at import via plugins.py's bottom import):
the engine AUTO-ACTIVATES it for substantive sessions (no owning skill), the
resolve observer surfaces the record_experience nudge, the gate blocks `resolve`
until the leaf flow is complete (searched AND (recorded OR skipped)), a skip must
carry a reason, and the task-scoped plugin auto-retires once resolution passes.
End-to-end paths run through cli.main() so the real _fire_plugins wiring runs.
"""
from __future__ import annotations

import json
from pathlib import Path

from agentctl import cli, plugins
from agentctl.directive import Directive
from agentctl.state import Node, SessionState, WeightClass


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


# --- registration & activation -----------------------------------------------

def test_experience_registered_at_import():
    assert "experience" in plugins.REGISTRY
    assert plugins.REGISTRY["experience"].scope == "task"


def test_auto_activate_true_for_substantive_false_for_chat():
    plugin = plugins.REGISTRY["experience"]
    assert plugin.auto_activate is not None
    subst = _new_state(weight_class=WeightClass.SUBSTANTIVE.value)
    chat = _new_state(weight_class=WeightClass.CHAT.value)
    assert plugin.auto_activate(subst) is True
    assert plugin.auto_activate(chat) is False


def test_activate_seeds_bag():
    state = _new_state()
    plugins.activate(state, "experience")
    bag = state.plugins["experience"]
    assert bag == {
        "searched": False, "decision": "", "recorded": False,
        "skipped": False, "skip_reason": "",
    }


# --- observer -----------------------------------------------------------------

def test_resolve_emits_record_experience_until_complete():
    state = _new_state(node=Node.RESOLUTION.value)
    plugins.activate(state, "experience")
    d = Directive(False, state.node, "resolve")
    fired = plugins.fire("resolve", state, d)
    assert [f["action"] for f in fired] == ["record_experience"]
    assert fired[0]["plugin"] == "experience"
    assert fired[0]["blocking"] is True
    # once searched + recorded, the nudge goes silent
    bag = state.plugins["experience"]
    bag["searched"] = True
    bag["recorded"] = True
    d2 = Directive(True, state.node, "resolve")
    assert plugins.fire("resolve", state, d2) == []


def test_resolve_silent_when_searched_and_skipped():
    state = _new_state(node=Node.RESOLUTION.value)
    plugins.activate(state, "experience")
    bag = state.plugins["experience"]
    bag["searched"] = True
    bag["skipped"] = True
    d = Directive(True, state.node, "resolve")
    assert plugins.fire("resolve", state, d) == []


# --- gate ---------------------------------------------------------------------

def test_gate_blocks_until_searched_and_recorded_or_skipped():
    state = _new_state()
    plugins.activate(state, "experience")
    bag = state.plugins["experience"]
    assert plugins.plugin_gate_blockers(state, "resolution")  # nothing done yet
    bag["searched"] = True
    assert plugins.plugin_gate_blockers(state, "resolution")  # decision still missing
    bag["recorded"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []  # searched + recorded -> passes


def test_gate_passes_on_searched_and_skipped():
    state = _new_state()
    plugins.activate(state, "experience")
    bag = state.plugins["experience"]
    bag["searched"] = True
    bag["skipped"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_gate_does_not_touch_plan_approval():
    state = _new_state()
    plugins.activate(state, "experience")
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


# --- end-to-end through cli.main() (real _fire_plugins + plugin-record) --------

def _run(capsys, root, *argv):
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _drive_to_resolution(capsys, root, sid, plan, *, chat=False):
    _run(capsys, root, "start", "--session", sid, "--task", "T-1",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    if chat:
        _run(capsys, root, "classify", "--session", sid, "--chat")
        return
    _run(capsys, root, "classify", "--session", sid, "--architectural")
    _run(capsys, root, "plan", "--session", sid)
    _run(capsys, root, "submit-plan", "--session", sid, "--plan", plan)
    _run(capsys, root, "approve", "--session", sid, "--by", "user")
    _run(capsys, root, "partition", "--session", sid)
    for _ in range(2):
        _run(capsys, root, "next-stage", "--session", sid)
        _run(capsys, root, "record-result", "--session", sid, "--status", "passed", "--actual", "ok")
    _run(capsys, root, "verify-final", "--session", sid)


def test_e2e_auto_activates_and_blocks_resolve_until_recorded(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "exp-e2e"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan)

    # experience auto-activated at classify (no manual plugin-activate)
    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    assert "experience" in raw["plugins"]

    # the gate blocks resolve while the leaf flow is incomplete
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 1
    assert any("experience" in b for b in d["data"]["blockers"])
    assert any(p["action"] == "record_experience" for p in d["data"]["plugin_directives"])

    # search + record, then resolve passes
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "searched")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "recorded")
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value
    assert d["marker"] == "COMPLETED"

    # task-scoped: auto-retired on the passing resolve, bag archived for audit
    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    assert "experience" not in raw["plugins"]
    assert "experience" in raw["plugins_archive"]


def test_e2e_skip_requires_reason_then_passes(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "exp-skip"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan)

    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "searched")
    # a skip without a reason is refused
    rc, d = _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "skipped")
    assert rc == 1
    # the gate is still blocked (skipped was not set)
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 1
    # skip with a reason records the bag and unblocks resolve
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience",
         "--phase", "skipped", "--note", "below the quality bar")
    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    assert raw["plugins"]["experience"]["skip_reason"] == "below the quality bar"
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value


def test_e2e_chat_session_never_activates_experience(capsys, tmp_path):
    root = str(tmp_path / "state")
    sid = "exp-chat"
    _drive_to_resolution(capsys, root, sid, plan=None, chat=True)
    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    assert "experience" not in raw.get("plugins", {})
    assert "experience" not in raw.get("plugins_archive", {})
