"""ledger-add / ledger-check: the CLI surface for recording claims into the
active claim-provenance ledger bag (agentctl/ledger.py's pure validate_ledger is
the closure check; these two commands only read/write the bag).

Covers: ledger-add UPSERTs by --id (last write wins — grounding a claim is
re-adding the same id with --source/--premise/--basis filled in); ledger-add is a
recoverable noop-error when the ledger plugin is not active (never a crash);
ledger-check surfaces validate_ledger's blockers on an open ledger and reports
green (ok=True, no blockers) on a closed one. Neither command fires a plugin
event or (for ledger-check) mutates state.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, plugins
from agentctl.state import SessionState


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


def _add(store, sid, *, id, status, statement="x", source="", premises=None, basis="",
         load_bearing=True):
    return cli.cmd_ledger_add(Namespace(
        session=sid, id=id, status=status, statement=statement, source=source,
        premises=premises, basis=basis, load_bearing=load_bearing,
    ), store=store)


def _check(store, sid):
    return cli.cmd_ledger_check(Namespace(session=sid), store=store)


# --- ledger-add: guard on the active bag ---------------------------------------

def test_ledger_add_refused_when_plugin_inactive(store):
    state = _new_state()
    store.save(state)
    d = _add(store, "s", id="c1", status="axiom", source="ticket:ABC-1")
    assert d.ok is False
    assert d.action == "noop"
    assert "not active" in d.detail
    assert "ledger" not in store.load("s").plugins


# --- ledger-add: upsert last-wins by id -----------------------------------------

def test_ledger_add_inserts_new_claim(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    d = _add(store, "s", id="c1", status="axiom", statement="latency spiked", source="dashboard")
    assert d.ok is True
    claims = store.load("s").plugins["ledger"]["claims"]
    assert len(claims) == 1
    assert claims[0]["id"] == "c1"
    assert claims[0]["status"] == "axiom"
    assert claims[0]["source"] == "dashboard"


def test_ledger_add_upserts_existing_claim_last_wins(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    _add(store, "s", id="c1", status="axiom", statement="x", source="")  # ungrounded
    d = _add(store, "s", id="c1", status="axiom", statement="x", source="measured 2026-07-14")
    assert d.ok is True

    claims = store.load("s").plugins["ledger"]["claims"]
    assert len(claims) == 1  # upsert, not append
    assert claims[0]["source"] == "measured 2026-07-14"


def test_ledger_add_does_not_fire_plugin_event(store, capsys):
    """ledger-add must not re-trigger the resolve nudge (invariants.py, stage 3)."""
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    d = _add(store, "s", id="c1", status="assumption", statement="x", basis="stated by user")
    assert d.ok is True
    # no observer fired => nothing in `data` about a directive/nudge beyond the claim list
    assert set(d.data) == {"claims"}


# --- ledger-check: read-only report of validate_ledger's blockers --------------

def test_ledger_check_refused_when_plugin_inactive(store):
    state = _new_state()
    store.save(state)
    d = _check(store, "s")
    assert d.ok is False
    assert d.action == "noop"


def test_ledger_check_reports_blockers_on_open_ledger(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    _add(store, "s", id="a1", status="axiom", statement="x", source="")  # no source

    d = _check(store, "s")
    assert d.ok is False
    assert d.data["blockers"]
    assert any("a1" in b for b in d.data["blockers"])


def test_ledger_check_green_on_closed_ledger(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    state.plugins["ledger"]["enumerated"] = True  # cross-check run (stage 5 blocker)
    store.save(state)
    _add(store, "s", id="a1", status="axiom", statement="x", source="ticket:ABC-1")

    d = _check(store, "s")
    assert d.ok is True
    assert d.data["blockers"] == []


def test_ledger_check_does_not_mutate_state(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    before = store.load("s").to_json()

    _check(store, "s")

    after = store.load("s").to_json()
    assert before == after
