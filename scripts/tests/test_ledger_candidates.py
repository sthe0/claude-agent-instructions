"""agentctl.ledger.Candidate / validate_candidates + the ledger-candidate /
ledger-dispose CLI surface + the extended _ledger_gate (claim closure AND
candidate disposition-completeness).

Covers: a raised (undispositioned) candidate always blocks; a dismissed
candidate without a reason blocks; a recorded candidate with no --claim, or one
pointing at a missing / non-load-bearing claim id, blocks; a recorded candidate
linked to an existing closed load-bearing claim closes, as does a dismissed one
with a reason; unknown disposition blocks. _ledger_gate ANDs both validators —
blocks while any candidate is raised even with claims closed, passes only once
both are closed. ledger-candidate/ledger-dispose UPSERT/guard like ledger-add;
ledger-dispose refuses early when its own required flag (--claim for recorded,
--reason for dismissed) is missing.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, plugins
from agentctl import plugins_ledger as lp
from agentctl.ledger import Candidate, Claim, candidates_from_dicts, candidates_to_dicts, validate_candidates
from agentctl.state import SessionState
from ast_purity import impure_names


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


# --- validate_candidates: pure structural closure -------------------------------

def test_recorded_candidate_linked_to_closed_load_bearing_claim_closes():
    claims = [Claim(id="c1", status="axiom", statement="x", source="dashboard")]
    candidates = [Candidate(id="e1", statement="chose approach A", disposition="recorded", claim="c1")]
    assert validate_candidates(candidates, claims) == []


def test_dismissed_candidate_with_reason_closes():
    candidates = [Candidate(id="e1", statement="considered approach B", disposition="dismissed",
                             reason="out of scope for this ticket")]
    assert validate_candidates(candidates, []) == []


def test_raised_candidate_blocks():
    candidates = [Candidate(id="e1", statement="chose approach A")]
    blockers = validate_candidates(candidates, [])
    assert any("e1" in b and "undispositioned" in b for b in blockers)


def test_dismissed_without_reason_blocks():
    candidates = [Candidate(id="e1", statement="x", disposition="dismissed")]
    blockers = validate_candidates(candidates, [])
    assert any("e1" in b and "reason" in b for b in blockers)


def test_recorded_without_claim_blocks():
    candidates = [Candidate(id="e1", statement="x", disposition="recorded")]
    blockers = validate_candidates(candidates, [])
    assert any("e1" in b and "no linked claim" in b for b in blockers)


def test_recorded_pointing_at_missing_claim_blocks():
    candidates = [Candidate(id="e1", statement="x", disposition="recorded", claim="ghost")]
    blockers = validate_candidates(candidates, [])
    assert any("e1" in b and "unknown claim" in b for b in blockers)


def test_recorded_pointing_at_non_load_bearing_claim_blocks():
    claims = [Claim(id="c1", status="axiom", statement="x", source="dashboard", load_bearing=False)]
    candidates = [Candidate(id="e1", statement="x", disposition="recorded", claim="c1")]
    blockers = validate_candidates(candidates, claims)
    assert any("e1" in b and "non-load-bearing claim" in b for b in blockers)


def test_unknown_disposition_blocks():
    candidates = [Candidate(id="e1", statement="x", disposition="guess")]
    blockers = validate_candidates(candidates, [])
    assert any("e1" in b and "unknown disposition" in b for b in blockers)


def test_candidates_dicts_roundtrip():
    candidates = [
        Candidate(id="e1", statement="x", disposition="dismissed", reason="n/a"),
        Candidate(id="e2", statement="y", disposition="recorded", claim="c1"),
    ]
    assert candidates_from_dicts(candidates_to_dicts(candidates)) == candidates


def test_validate_candidates_is_pure():
    assert impure_names(validate_candidates) == set()


# --- _ledger_gate: ANDs claim closure + candidate disposition-completeness -----

def test_gate_blocks_on_raised_candidate_even_when_claims_closed():
    state = _new_state()
    plugins.activate(state, "ledger", {
        "claims": [{"id": "c1", "status": "axiom", "statement": "x", "source": "dashboard"}],
        "candidates": [{"id": "e1", "statement": "chose A"}],
    })
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert any("e1" in b for b in blockers)


def test_gate_passes_once_claims_closed_and_candidate_recorded():
    state = _new_state()
    plugins.activate(state, "ledger", {
        "enumerated": True,  # cross-check run (stage 5 third blocker); isolate to closure
        "claims": [{"id": "c1", "status": "axiom", "statement": "x", "source": "dashboard"}],
        "candidates": [{"id": "e1", "statement": "chose A", "disposition": "recorded", "claim": "c1"}],
    })
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_gate_still_blocks_on_claims_when_candidates_are_closed():
    state = _new_state()
    plugins.activate(state, "ledger", {
        "claims": [{"id": "c1", "status": "axiom", "statement": "x"}],  # no source
        "candidates": [{"id": "e1", "statement": "y", "disposition": "dismissed", "reason": "n/a"}],
    })
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert any("c1" in b for b in blockers)


# --- resolve observer: echoes dispositioned candidates while still blocked -----

def test_resolve_observer_echoes_dispositioned_candidates_while_blocked():
    state = _new_state()
    plugins.activate(state, "ledger", {
        "claims": [{"id": "c1", "status": "axiom", "statement": "x", "source": "dashboard"}],
        "candidates": [
            {"id": "e1", "statement": "chose A", "disposition": "dismissed", "reason": "out of scope"},
            {"id": "e2", "statement": "chose B"},  # still raised -> gate still blocked
        ],
    })
    fired = lp._observe_resolve(state, state.plugins["ledger"])
    actions = [f.action for f in fired]
    assert "close_ledger" in actions
    assert "echo_dispositions" in actions
    echo = next(f for f in fired if f.action == "echo_dispositions")
    assert "e1" in echo.detail and "out of scope" in echo.detail


def test_resolve_observer_silent_once_claims_and_candidates_both_closed():
    state = _new_state()
    plugins.activate(state, "ledger", {
        "enumerated": True,  # cross-check run (stage 5 third blocker); isolate to closure
        "claims": [{"id": "c1", "status": "axiom", "statement": "x", "source": "dashboard"}],
        "candidates": [{"id": "e1", "statement": "chose A", "disposition": "recorded", "claim": "c1"}],
    })
    assert lp._observe_resolve(state, state.plugins["ledger"]) == []


# --- ledger-candidate: guard + upsert -------------------------------------------

def _candidate(store, sid, *, id, statement="x"):
    return cli.cmd_ledger_candidate(Namespace(session=sid, id=id, statement=statement), store=store)


def _dispose(store, sid, *, id, as_, reason="", claim=""):
    return cli.cmd_ledger_dispose(Namespace(session=sid, id=id, as_=as_, reason=reason, claim=claim), store=store)


def test_ledger_candidate_refused_when_plugin_inactive(store):
    state = _new_state()
    store.save(state)
    d = _candidate(store, "s", id="e1")
    assert d.ok is False
    assert d.action == "noop"


def test_ledger_candidate_inserts_new_raised_candidate(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    d = _candidate(store, "s", id="e1", statement="chose approach A")
    assert d.ok is True
    candidates = store.load("s").plugins["ledger"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["id"] == "e1"
    assert candidates[0]["disposition"] == "raised"


def test_ledger_candidate_upserts_last_wins_and_resets_disposition(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    _candidate(store, "s", id="e1")
    _dispose(store, "s", id="e1", as_="dismissed", reason="n/a")
    _candidate(store, "s", id="e1", statement="re-raised")  # upsert resets disposition

    candidates = store.load("s").plugins["ledger"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["disposition"] == "raised"
    assert candidates[0]["statement"] == "re-raised"


def test_ledger_candidate_does_not_fire_plugin_event(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    d = _candidate(store, "s", id="e1")
    assert set(d.data) == {"candidates"}


# --- ledger-dispose: guard + required-flag refusal + upsert --------------------

def test_ledger_dispose_refused_when_plugin_inactive(store):
    state = _new_state()
    store.save(state)
    d = _dispose(store, "s", id="e1", as_="dismissed", reason="n/a")
    assert d.ok is False
    assert d.action == "noop"


def test_ledger_dispose_refused_when_candidate_unknown(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    d = _dispose(store, "s", id="ghost", as_="dismissed", reason="n/a")
    assert d.ok is False
    assert "no such candidate" in d.detail


def test_ledger_dispose_recorded_requires_claim(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    _candidate(store, "s", id="e1")
    d = _dispose(store, "s", id="e1", as_="recorded")
    assert d.ok is False
    assert "--claim" in d.detail
    # refused before mutating the candidate's disposition
    assert store.load("s").plugins["ledger"]["candidates"][0]["disposition"] == "raised"


def test_ledger_dispose_dismissed_requires_reason(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    _candidate(store, "s", id="e1")
    d = _dispose(store, "s", id="e1", as_="dismissed")
    assert d.ok is False
    assert "--reason" in d.detail


def test_ledger_dispose_recorded_sets_claim(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    _candidate(store, "s", id="e1")
    d = _dispose(store, "s", id="e1", as_="recorded", claim="c1")
    assert d.ok is True
    candidate = store.load("s").plugins["ledger"]["candidates"][0]
    assert candidate["disposition"] == "recorded"
    assert candidate["claim"] == "c1"


def test_ledger_dispose_dismissed_sets_reason(store):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    _candidate(store, "s", id="e1")
    d = _dispose(store, "s", id="e1", as_="dismissed", reason="out of scope")
    assert d.ok is True
    candidate = store.load("s").plugins["ledger"]["candidates"][0]
    assert candidate["disposition"] == "dismissed"
    assert candidate["reason"] == "out of scope"


# --- verify-agentctl.py stays green (no new gate/plugin introduced) ------------

def test_verify_agentctl_still_green():
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(repo_root / "verify-agentctl.py")],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
