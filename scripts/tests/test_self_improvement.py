"""The self-improvement two-beat side-flow (Phase 3, Variant A): si-propose arms the
self_improvement gate; si-apply refuses unless armed + an approver is named + the
sync-before-edit contract (--synced) is met, then hands back the commit trailer.
The gate is a plain state field — no guardian hook (verify-agentctl stays green)."""
from argparse import Namespace

from agentctl import cli
from agentctl.state import GateRecord, SessionState


def ns(**kw):
    return Namespace(**kw)


def _seed(store, sid):
    state = SessionState(session_id=sid, task_id="t")
    store.save(state)
    return state


def test_si_propose_arms_gate(store):
    _seed(store, "si1")
    d = cli.cmd_si_propose(ns(session="si1"), store=store)
    assert d.ok is True
    assert d.action == "await_si_approval"
    assert d.marker == "PLAN-READY"
    assert d.data["sync_cmd"] == "scripts/sync-instructions-repo.sh pull"
    assert "where_to_put" in d.data
    state = store.load("si1")
    assert state.self_improvement.armed is True
    assert state.self_improvement.passed is False


def test_si_apply_refused_when_not_armed(store):
    _seed(store, "si2")
    d = cli.cmd_si_apply(ns(session="si2", by="user", synced=True), store=store)
    assert d.ok is False
    assert d.action == "fix_si"
    assert any("si-propose" in b for b in d.data["blockers"])


def test_si_apply_refused_when_by_empty(store):
    _seed(store, "si3")
    cli.cmd_si_propose(ns(session="si3"), store=store)
    d = cli.cmd_si_apply(ns(session="si3", by="  ", synced=True), store=store)
    assert d.ok is False
    assert any("approver" in b for b in d.data["blockers"])


def test_si_apply_refused_when_not_synced(store):
    _seed(store, "si4")
    cli.cmd_si_propose(ns(session="si4"), store=store)
    d = cli.cmd_si_apply(ns(session="si4", by="user", synced=False), store=store)
    assert d.ok is False
    assert any("sync-before-edit" in b for b in d.data["blockers"])


def test_si_apply_passes_with_armed_by_synced(store):
    _seed(store, "si5")
    cli.cmd_si_propose(ns(session="si5"), store=store)
    d = cli.cmd_si_apply(ns(session="si5", by="user", synced=True), store=store)
    assert d.ok is True
    assert d.action == "apply_edits"
    assert d.data["commit_trailer"] == "[self-improvement-reviewed]"
    state = store.load("si5")
    assert state.self_improvement.passed is True
    assert state.self_improvement.by == "user"


def test_self_improvement_gate_round_trips(store):
    state = SessionState(session_id="si6", task_id="t")
    state.self_improvement = GateRecord("self_improvement", armed=True, passed=False)
    rebuilt = SessionState.from_dict(state.to_dict())
    assert rebuilt.self_improvement.name == "self_improvement"
    assert rebuilt.self_improvement.armed is True
    assert rebuilt.self_improvement.passed is False


def test_legacy_state_without_si_field_loads(store):
    state = SessionState(session_id="si7", task_id="t")
    data = state.to_dict()
    del data["self_improvement"]
    rebuilt = SessionState.from_dict(data)
    assert rebuilt.self_improvement.armed is False
    assert rebuilt.self_improvement.passed is False
    assert rebuilt.self_improvement.name == "self_improvement"
