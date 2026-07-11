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
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plugins
from agentctl import plugins_tracker as tp
from agentctl.directive import Directive
from agentctl.state import Node, Partition, PartitionUnit, SessionState, WeightClass

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


# --- auto-activation (#11 P1) --------------------------------------------------
# The plugin must never stay dark on a tracker-driven task just because the
# tracker-management skill was not explicitly invoked to plugin-activate it.

def test_registered_with_auto_activate_predicate():
    assert plugins.REGISTRY["tracker"].auto_activate is not None


def test_auto_activate_true_when_tracker_key_and_substantive():
    state = _new_state(tracker_key="ABC-9", weight_class=WeightClass.SUBSTANTIVE.value)
    assert tp._auto_activate(state) is True


def test_auto_activate_false_without_tracker_key():
    state = _new_state(tracker_key=None, weight_class=WeightClass.SUBSTANTIVE.value)
    assert tp._auto_activate(state) is False


def test_auto_activate_false_when_not_substantive():
    # a small-change session could in principle carry a tracker_key-shaped task id;
    # classify.py itself already forces SUBSTANTIVE whenever tracker_key matches, so
    # this only guards the predicate's own logic against a future relaxation there.
    state = _new_state(tracker_key="ABC-9", weight_class=WeightClass.SMALL_CHANGE.value)
    assert tp._auto_activate(state) is False


def _classify(store, sid, *, tracker_key=None, architectural=False):
    cli.cmd_start(Namespace(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    return cli.cmd_classify(Namespace(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=tracker_key, architectural=architectural, external_effect=False,
        new_dependency=False, public_api_change=False,
    ), store=store)


def test_classify_with_tracker_key_auto_activates_plugin(store):
    _classify(store, "ta-1", tracker_key="ABC-9")
    state = store.load("ta-1")
    assert "tracker" in state.plugins
    assert state.tracker_key == "ABC-9"
    # auto_seed mirrors plugin-activate --tracker-key: the bag carries the key,
    # so every nudge names the ticket instead of an empty string
    assert state.plugins["tracker"]["tracker_key"] == "ABC-9"


def test_skill_activation_wins_over_later_auto_activation():
    """auto_activate_for skips an already-active plugin — an explicit skill
    activation's bag (key + recorded phases) is never reset by classify."""
    state = _new_state(tracker_key="ABC-9", weight_class=WeightClass.SUBSTANTIVE.value)
    plugins.activate(state, "tracker", {"tracker_key": "ABC-9"})
    state.plugins["tracker"]["published_phases"]["plan"] = "comment-1"
    assert "tracker" not in plugins.auto_activate_for(state)
    assert state.plugins["tracker"]["published_phases"] == {"plan": "comment-1"}


def test_classify_without_tracker_key_does_not_auto_activate(store):
    # substantive via --architectural, no tracker key at all: must stay dark
    _classify(store, "ta-2", architectural=True)
    state = store.load("ta-2")
    assert "tracker" not in state.plugins
    assert state.tracker_key is None


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


def test_approve_emits_start_progress_and_publish_plan_snapshot():
    state = _new_state(node=Node.APPROVED.value)
    # approve is where the immutable approved snapshot exists; the publish nudge
    # must carry THAT path, never the mutable plan file.
    state.plan_path = "/plans/mutable-source.toml"
    state.plan_snapshot_path = "/state/plan-approved-deadbeef.toml"
    plugins.activate(state, "tracker", {"tracker_key": "ABC-7"})
    d = Directive(True, state.node, "approve")
    fired = plugins.fire("approve", state, d)
    assert [f["action"] for f in fired] == ["start_progress", "publish_plan"]
    start, publish = fired
    assert start["plugin"] == "tracker"
    # start is hygiene, not load-bearing: it must NOT be a blocking directive
    assert start.get("blocking") is not True
    assert start["data"]["tracker_key"] == "ABC-7"
    # publish_plan is a non-blocking nudge — enforcement is the gate + skip form,
    # not a blocking approve directive
    assert publish["plugin"] == "tracker"
    assert publish.get("blocking") is not True
    assert publish["data"]["phase"] == "plan"
    # the IMMUTABLE snapshot, never the mutable plan file
    assert publish["data"]["plan_snapshot_path"] == "/state/plan-approved-deadbeef.toml"
    assert publish["data"]["plan_snapshot_path"] != state.plan_path
    # the nudge names the skip route so a backend without the verb degrades honestly
    assert "--skipped" in publish["detail"]


def test_approve_publish_plan_snapshot_path_empty_when_unset():
    # a legacy/best-effort approve with no snapshot recorded must not raise; the
    # nudge still fires, carrying an empty path the coordinator can act on.
    state = _new_state(node=Node.APPROVED.value)
    plugins.activate(state, "tracker", {"tracker_key": "ABC-7"})
    fired = plugins.fire("approve", state, Directive(True, state.node, "approve"))
    publish = fired[1]
    assert publish["action"] == "publish_plan"
    assert publish["data"]["plan_snapshot_path"] == ""


def test_approve_nudges_never_gate_resolution():
    # observing approve records nothing on the bag, so the resolution gate is
    # unaffected: neither start_progress nor publish_plan is part of the
    # mandatory-missing list until the coordinator actually records the phase.
    state = _new_state(node=Node.APPROVED.value)
    plugins.activate(state, "tracker")
    plugins.fire("approve", state, Directive(True, state.node, "approve"))
    # nothing recorded merely by observing: the gate still blocks
    assert plugins.plugin_gate_blockers(state, "resolution")
    for phase in ("plan", "result", "status"):
        state.plugins["tracker"]["published_phases"][phase] = True
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert blockers == []
    assert not any("start" in b for b in blockers)


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


def test_resolve_emits_publish_result_and_transition_status_until_recorded():
    state = _new_state(node=Node.RESOLUTION.value)
    plugins.activate(state, "tracker")
    d = Directive(False, state.node, "resolve")
    assert [f["action"] for f in plugins.fire("resolve", state, d)] == \
        ["publish_result", "transition_status"]
    # once the result phase is recorded, only the status nudge remains
    state.plugins["tracker"]["published_phases"]["result"] = True
    d2 = Directive(False, state.node, "resolve")
    assert [f["action"] for f in plugins.fire("resolve", state, d2)] == ["transition_status"]
    # once status is recorded too, the nudge goes fully silent
    state.plugins["tracker"]["published_phases"]["status"] = True
    d3 = Directive(True, state.node, "resolve")
    assert plugins.fire("resolve", state, d3) == []


# --- partition: propose_delivery_structure on a recommended verdict -----------

@pytest.mark.parametrize("verdict", ["possible", "not_required"])
def test_partition_silent_unless_recommended(verdict):
    state = _new_state(node=Node.PARTITIONED.value)
    plugins.activate(state, "tracker")
    state.partition = Partition(m1=True, m2=True, verdict=verdict)
    fired = plugins.fire("partition", state, Directive(True, state.node, "partition"))
    assert fired == []


def test_partition_recommended_emits_propose_delivery_structure_non_blocking():
    state = _new_state(node=Node.PARTITIONED.value)
    plugins.activate(state, "tracker", {"tracker_key": "ABC-9"})
    state.partition = Partition(m1=True, m3=True, verdict="recommended")
    fired = plugins.fire("partition", state, Directive(True, state.node, "partition"))
    assert [f["action"] for f in fired] == ["propose_delivery_structure"]
    directive = fired[0]
    assert directive["plugin"] == "tracker"
    assert directive.get("blocking") is not True
    assert directive["data"]["tracker_key"] == "ABC-9"
    assert directive["data"]["verdict"] == "recommended"


def test_partition_absent_is_silent():
    # partition event can fire before state.partition is populated defensively —
    # no assessment recorded yet must not raise or emit
    state = _new_state(node=Node.APPROVED.value)
    plugins.activate(state, "tracker")
    assert state.partition is None
    fired = plugins.fire("partition", state, Directive(True, state.node, "partition"))
    assert fired == []


# --- partition_units: create_subticket nudge for unmaterialized subtasks ------

def test_partition_units_nudges_subtask_without_ref():
    state = _new_state(node=Node.PARTITIONED.value)
    plugins.activate(state, "tracker", {"tracker_key": "ABC-9"})
    state.partition = Partition(verdict="recommended", units=[
        PartitionUnit(title="core change", stages=[1], mode="inline"),
        PartitionUnit(title="follow-up cleanup", stages=[2], mode="subtask"),
    ])
    fired = plugins.fire("partition_units", state, Directive(True, state.node, "partition_units"))
    assert [f["action"] for f in fired] == ["create_subticket"]
    directive = fired[0]
    assert directive.get("blocking") is not True
    assert directive["data"]["unit_index"] == 2
    assert directive["data"]["unit_title"] == "follow-up cleanup"


def test_partition_units_converges_silent_once_ref_recorded():
    # a subtask unit with its ref already assigned (subticket created and
    # re-recorded) must not re-nudge
    state = _new_state(node=Node.PARTITIONED.value)
    plugins.activate(state, "tracker")
    state.partition = Partition(verdict="recommended", units=[
        PartitionUnit(title="follow-up cleanup", stages=[2], mode="subtask", ref="ABC-10"),
    ])
    fired = plugins.fire("partition_units", state, Directive(True, state.node, "partition_units"))
    assert fired == []


def test_partition_units_ignores_non_subtask_modes():
    state = _new_state(node=Node.PARTITIONED.value)
    plugins.activate(state, "tracker")
    state.partition = Partition(verdict="recommended", units=[
        PartitionUnit(title="core change", stages=[1], mode="inline"),
        PartitionUnit(title="side change", stages=[2], mode="spawn"),
    ])
    fired = plugins.fire("partition_units", state, Directive(True, state.node, "partition_units"))
    assert fired == []


def test_partition_units_no_units_is_silent():
    state = _new_state(node=Node.PARTITIONED.value)
    plugins.activate(state, "tracker")
    state.partition = Partition(verdict="recommended")
    fired = plugins.fire("partition_units", state, Directive(True, state.node, "partition_units"))
    assert fired == []


# --- gate ---------------------------------------------------------------------

def test_publish_gate_blocks_until_mandatory_phases_recorded():
    state = _new_state()
    plugins.activate(state, "tracker")
    assert plugins.plugin_gate_blockers(state, "resolution")  # nothing published yet
    state.plugins["tracker"]["published_phases"]["plan"] = True
    assert plugins.plugin_gate_blockers(state, "resolution")  # result, status still missing
    state.plugins["tracker"]["published_phases"]["result"] = True
    assert plugins.plugin_gate_blockers(state, "resolution")  # status still missing
    state.plugins["tracker"]["published_phases"]["status"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []  # all three recorded -> passes


def test_publish_gate_discharged_by_skip_marker():
    # the gate tests membership, not truth: an honest SKIP marker (a dict carrying
    # the reason) under the mandatory key discharges the gate exactly like a real
    # post, so a backend with no publish transport never wedges resolution.
    state = _new_state()
    plugins.activate(state, "tracker")
    pub = state.plugins["tracker"]["published_phases"]
    pub["plan"] = {"skipped": "backend defines no tracker_publish_plan"}
    pub["result"] = True  # a plain True still discharges its key
    pub["status"] = {"skipped": "ticket left open pending follow-up PR"}
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_publish_gate_blocks_on_missing_status_alone():
    # plan + result recorded, status not yet -> still blocked; status closes it
    state = _new_state()
    plugins.activate(state, "tracker")
    state.plugins["tracker"]["published_phases"]["plan"] = True
    state.plugins["tracker"]["published_phases"]["result"] = True
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert blockers
    assert any("status" in b for b in blockers)
    state.plugins["tracker"]["published_phases"]["status"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []


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
        _run(capsys, root, "record-result", "--session", sid, "--status", "passed", "--actual", "ok", "--control", "reviewed: ok")
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

    # record the three mandatory publications, then resolve passes
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "plan")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "result")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "status")
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value
    assert d["marker"] == "COMPLETED"

    # task-scoped: auto-retired on the passing resolve, bag archived for audit
    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    assert "tracker" not in raw["plugins"]
    assert "tracker" in raw["plugins_archive"]


def test_e2e_skip_marker_unblocks_resolution_and_stores_reason(capsys, tmp_path, fixtures_dir):
    # a backend with no publish transport: the coordinator discharges the
    # mandatory `plan` phase with the SKIP form. Resolution passes, and the stored
    # marker keeps the reason for audit (never a silent unrecorded phase).
    root = str(tmp_path / "state")
    sid = "tk-skip"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan, activate_tracker=True)

    # missing a --note is refused: an honest skip must carry a reason
    rc, d = _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker",
                 "--phase", "plan", "--skipped")
    assert rc == 1

    reason = "backend defines no tracker_publish_plan"
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker",
         "--phase", "plan", "--skipped", "--note", reason)
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "result")
    _run(capsys, root, "plugin-record", "--session", sid, "--plugin", "tracker", "--phase", "status")

    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
    assert rc == 0
    assert d["node"] == Node.RESOLVED.value

    raw = json.loads((Path(root) / f"{sid}.json").read_text(encoding="utf-8"))
    marker = raw["plugins_archive"]["tracker"]["published_phases"]["plan"]
    assert marker == {"skipped": reason}


def test_e2e_non_tracker_session_has_no_tracker_effect(capsys, tmp_path, fixtures_dir):
    root = str(tmp_path / "state")
    sid = "tk-none"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _drive_to_resolution(capsys, root, sid, plan, activate_tracker=False)
    rc, d = _run(capsys, root, "resolve", "--session", sid, "--by", "user", "--quality", "4")
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
    assert mod.unpublished_phases(state) == ["result", "status"]


def test_hook_silent_when_tracker_inactive_or_complete():
    mod = _load_hook()
    assert mod.unpublished_phases({"plugins": {}}) == []
    full = {"plugins": {"tracker": {
        "published_phases": {"plan": True, "result": True, "status": True},
    }}}
    assert mod.unpublished_phases(full) == []
