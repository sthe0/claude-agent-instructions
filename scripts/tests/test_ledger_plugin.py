"""The claim-provenance ledger plugin: a skill-less consumer of the plugin layer.

Proves the ledger sub-state-machine attaches to the core spine with ZERO edits to
the three core literals (it registers at import via plugins.py's bottom import):
the engine AUTO-ACTIVATES it whenever classify is told deliverable_kind is
'reasoning' or 'mixed' on a SUBSTANTIVE session (never for 'code'/'ops'/'' or a
non-substantive session), the resolution gate blocks on an unclosed claim bag
(ledger.validate_ledger) and passes once closed, and the task-scoped plugin
auto-retires once resolution actually passes. End-to-end paths run through
cli.main() so the real _fire_plugins wiring runs.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from agentctl import cli, plugins
from agentctl import plugins_ledger as lp
from agentctl.directive import Directive
from agentctl.state import Node, SessionState, WeightClass


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


# --- registration & activation -------------------------------------------------

def test_ledger_registered_at_import():
    assert "ledger" in plugins.REGISTRY
    assert plugins.REGISTRY["ledger"].scope == "task"


def test_auto_activate_true_for_reasoning_or_mixed_substantive():
    reasoning = _new_state(weight_class=WeightClass.SUBSTANTIVE.value, deliverable_kind="reasoning")
    mixed = _new_state(weight_class=WeightClass.SUBSTANTIVE.value, deliverable_kind="mixed")
    assert lp._auto_activate(reasoning) is True
    assert lp._auto_activate(mixed) is True


def test_auto_activate_false_for_code_ops_or_empty_kind():
    for kind in ("code", "ops", ""):
        state = _new_state(weight_class=WeightClass.SUBSTANTIVE.value, deliverable_kind=kind)
        assert lp._auto_activate(state) is False


def test_auto_activate_false_when_not_substantive():
    state = _new_state(weight_class=WeightClass.CHAT.value, deliverable_kind="reasoning")
    assert lp._auto_activate(state) is False


def _classify(store, sid, *, deliverable_kind="", chat=False, architectural=True):
    cli.cmd_start(Namespace(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    return cli.cmd_classify(Namespace(
        session=sid, chat=chat, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=None, architectural=architectural, external_effect=False,
        new_dependency=False, public_api_change=False, deliverable_kind=deliverable_kind,
    ), store=store)


def test_classify_with_reasoning_kind_auto_activates_plugin(store):
    _classify(store, "lg-1", deliverable_kind="reasoning")
    state = store.load("lg-1")
    assert "ledger" in state.plugins
    assert state.deliverable_kind == "reasoning"
    assert state.plugins["ledger"]["claims"] == []


def test_classify_with_mixed_kind_auto_activates_plugin(store):
    _classify(store, "lg-2", deliverable_kind="mixed")
    state = store.load("lg-2")
    assert "ledger" in state.plugins


def test_classify_with_code_kind_does_not_auto_activate(store):
    _classify(store, "lg-3", deliverable_kind="code")
    state = store.load("lg-3")
    assert "ledger" not in state.plugins


def test_classify_without_deliverable_kind_does_not_auto_activate(store):
    _classify(store, "lg-4")
    state = store.load("lg-4")
    assert "ledger" not in state.plugins
    assert state.deliverable_kind == ""


def test_classify_chat_does_not_auto_activate_even_with_reasoning_kind(store):
    _classify(store, "lg-5", deliverable_kind="reasoning", chat=True, architectural=False)
    state = store.load("lg-5")
    assert "ledger" not in state.plugins


# --- gate: blocks on an unclosed ledger, passes once closed --------------------

def test_gate_blocks_on_empty_claim_bag():
    state = _new_state()
    plugins.activate(state, "ledger")
    assert plugins.plugin_gate_blockers(state, "resolution")


def test_gate_blocks_on_invalid_claim_then_passes_once_grounded():
    state = _new_state()
    plugins.activate(state, "ledger")
    # enumerated=True isolates this test to the CLAIM-closure blocker (the mandatory
    # enumeration cross-check is exercised in test_ledger_enumerate.py).
    state.plugins["ledger"]["enumerated"] = True
    state.plugins["ledger"]["claims"] = [
        {"id": "c1", "status": "axiom", "statement": "x"},  # no source
    ]
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert blockers
    assert any("ledger" in b for b in blockers)
    state.plugins["ledger"]["claims"][0]["source"] = "measured 2026-07-14"
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_gate_does_not_touch_plan_approval():
    state = _new_state()
    plugins.activate(state, "ledger")
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


# --- observer: the resolve nudge -----------------------------------------------

def test_resolve_observer_silent_once_closed():
    state = _new_state(node=Node.RESOLUTION.value)
    plugins.activate(state, "ledger", {"enumerated": True, "claims": [
        {"id": "c1", "status": "assumption", "statement": "x", "basis": "stated by user"},
    ]})
    fired = plugins.fire("resolve", state, Directive(True, state.node, "resolve"))
    assert fired == []


def test_resolve_observer_blocking_nudge_while_unclosed():
    state = _new_state(node=Node.RESOLUTION.value)
    plugins.activate(state, "ledger")
    fired = plugins.fire("resolve", state, Directive(False, state.node, "resolve"))
    assert [f["action"] for f in fired] == ["close_ledger"]
    assert fired[0]["plugin"] == "ledger"
    assert fired[0]["blocking"] is True


# --- end-to-end through cli.main() (the real _fire_plugins wiring) ------------

def _run(capsys, root, *argv):
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _drive_to_resolution(capsys, root, sid, plan, *, deliverable_kind):
    _run(capsys, root, "start", "--session", sid, "--task", "t",
         "--goal", "g", "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--architectural",
         "--deliverable-kind", deliverable_kind)
    _run(capsys, root, "plan", "--session", sid)
    _run(capsys, root, "submit-plan", "--session", sid, "--plan", plan)
    _run(capsys, root, "approve", "--session", sid, "--by", "user")
    _run(capsys, root, "partition", "--session", sid)
    for _ in range(2):
        _run(capsys, root, "next-stage", "--session", sid)
        _run(capsys, root, "record-result", "--session", sid, "--status", "passed",
             "--actual", "ok", "--control", "reviewed: ok")
    _run(capsys, root, "verify-final", "--session", sid)
    # experience auto-activates for every substantive session; satisfy its gate so
    # these tests isolate the ledger plugin's own gating behavior
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "searched")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "experience", "--phase", "recorded")


def test_e2e_resolve_blocked_until_ledger_closed_then_passes_and_retires(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "lg-e2e"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan, deliverable_kind="reasoning")

    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user")
    assert rc == 1
    assert any("ledger" in b for b in d["data"]["blockers"])

    # ground the ledger via the plugin bag directly (CLI ledger-add is stage 3);
    # here we exercise the resolution gate + auto-retire wiring end to end
    raw_path = Path(root) / f"{sid}.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["plugins"]["ledger"]["claims"] = [
        {"id": "c1", "status": "axiom", "statement": "measured load", "source": "prod metrics dashboard"},
    ]
    raw["plugins"]["ledger"]["enumerated"] = True  # the cross-check having run is a
    # third resolution blocker (stage 5); set it directly here as the claims are
    raw_path.write_text(json.dumps(raw), encoding="utf-8")

    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert "ledger" not in raw["plugins"]
    assert "ledger" in raw["plugins_archive"]


def test_e2e_non_reasoning_session_has_no_ledger_effect(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "lg-none"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan, deliverable_kind="code")
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value
