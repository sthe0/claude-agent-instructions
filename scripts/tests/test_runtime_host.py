"""Sticky per-session host binding (agentctl.runtime_host, Host-isolated spawn
plan step 1): detection, explicit-wins resolution, sticky bind/rebind refusal,
and the require_bound_host hard-read gate.

Every test either sets/deletes the two ambient env vars the module reads
(CLAUDE_CODE_SESSION_ID, CURSOR_API_KEY) or monkeypatches lib.host_llm.preflight
directly — none of these depend on `claude`/`agent` actually being installed.
"""
from __future__ import annotations

import pytest

from agentctl import runtime_host
from agentctl.state import SessionState
from lib.runtime_models import HOST_CLAUDE, HOST_CURSOR, HOSTS


def _state(sid="rh-test") -> SessionState:
    return SessionState(session_id=sid, task_id="t")


# --- detect_host ---------------------------------------------------------------

def test_detect_host_claude_session_id_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    # Even with a fully usable Cursor CLI, CLAUDE_CODE_SESSION_ID wins outright.
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (True, ""))
    assert runtime_host.detect_host() == HOST_CLAUDE


def test_detect_host_falls_back_to_cursor_when_preflight_ok(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (True, ""))
    assert runtime_host.detect_host() == HOST_CURSOR


def test_detect_host_none_when_neither_signal_present(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (False, "no key"))
    assert runtime_host.detect_host() is None


# --- resolve_host ----------------------------------------------------------------

def test_resolve_host_explicit_wins_over_detection(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    assert runtime_host.resolve_host(HOST_CURSOR) == HOST_CURSOR


def test_resolve_host_falls_back_to_detection_when_no_explicit(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    assert runtime_host.resolve_host(None) == HOST_CLAUDE


def test_resolve_host_raises_ambiguous_when_neither_determines_one(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (False, "no key"))
    with pytest.raises(runtime_host.HostAmbiguousError):
        runtime_host.resolve_host(None)


def test_resolve_host_rejects_unknown_explicit_host():
    with pytest.raises(ValueError):
        runtime_host.resolve_host("windows")


# --- bind_runtime_host: unbound state --------------------------------------------

def test_bind_unbound_state_explicit_host_binds_and_persists():
    state = _state()
    result = runtime_host.bind_runtime_host(state, HOST_CURSOR, require=False)
    assert result == HOST_CURSOR
    assert state.runtime_host == HOST_CURSOR


def test_bind_unbound_state_require_false_ambiguous_stays_unbound(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (False, "no key"))
    state = _state()
    result = runtime_host.bind_runtime_host(state, None, require=False)
    assert result is None
    assert state.runtime_host is None


def test_bind_unbound_state_require_true_ambiguous_raises(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (False, "no key"))
    state = _state()
    with pytest.raises(runtime_host.HostAmbiguousError):
        runtime_host.bind_runtime_host(state, None, require=True)
    assert state.runtime_host is None


def test_bind_unbound_state_require_true_detects_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    state = _state()
    result = runtime_host.bind_runtime_host(state, None, require=True)
    assert result == HOST_CLAUDE
    assert state.runtime_host == HOST_CLAUDE


# --- bind_runtime_host: sticky once bound ----------------------------------------

def test_bind_already_bound_state_same_explicit_is_a_noop():
    state = _state()
    runtime_host.bind_runtime_host(state, HOST_CLAUDE, require=False)
    result = runtime_host.bind_runtime_host(state, HOST_CLAUDE, require=True)
    assert result == HOST_CLAUDE
    assert state.runtime_host == HOST_CLAUDE


def test_bind_already_bound_state_no_explicit_returns_bound_value_without_redetecting(monkeypatch):
    state = _state()
    runtime_host.bind_runtime_host(state, HOST_CLAUDE, require=False)
    # A later call with no --host must NOT re-run detection (which would see
    # neither signal here and would otherwise raise/return None under require).
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host.host_llm, "preflight", lambda host: (False, "no key"))
    result = runtime_host.bind_runtime_host(state, None, require=True)
    assert result == HOST_CLAUDE


def test_bind_already_bound_state_contradicting_explicit_raises_conflict():
    state = _state()
    runtime_host.bind_runtime_host(state, HOST_CLAUDE, require=False)
    with pytest.raises(runtime_host.HostConflictError):
        runtime_host.bind_runtime_host(state, HOST_CURSOR, require=True)
    # The sticky value survives the refused rebind attempt.
    assert state.runtime_host == HOST_CLAUDE


# --- require_bound_host -----------------------------------------------------------

def test_require_bound_host_raises_when_unbound():
    state = _state()
    with pytest.raises(runtime_host.HostAmbiguousError):
        runtime_host.require_bound_host(state)


def test_require_bound_host_returns_the_bound_value():
    state = _state()
    state.runtime_host = HOST_CURSOR
    assert runtime_host.require_bound_host(state) == HOST_CURSOR


# --- module surface --------------------------------------------------------------

def test_hosts_tuple_has_exactly_claude_and_cursor():
    assert set(HOSTS) == {HOST_CLAUDE, HOST_CURSOR}
