"""Sticky per-session binding: which coordination host (Claude Code or Cursor)
this session dispatches specialists and advisory judges through.

A session binds ONCE (best-effort at start/reset, required at classify) and
never rebinds mid-flight: the coordinating process itself does not change host
partway through a task, and every downstream spawn (dispatch.py's
spawn_cli_for) and every judge/extractor call (advisor.py, marker_extract.py)
reads this ONE field rather than re-detecting per call — so a transient
environment change (e.g. CURSOR_API_KEY unset mid-session) can never silently
flip which CLI a later stage shells out to.
"""
from __future__ import annotations

import os

from lib import host_llm
from lib.runtime_models import HOST_CLAUDE, HOST_CURSOR, HOSTS

__all__ = [
    "HOST_CLAUDE",
    "HOST_CURSOR",
    "HOSTS",
    "detect_host",
    "resolve_host",
    "bind_runtime_host",
    "require_bound_host",
    "HostAmbiguousError",
    "HostConflictError",
]


class HostAmbiguousError(Exception):
    """Neither an explicit --host nor an ambient signal determines the host."""


class HostConflictError(Exception):
    """An explicit --host contradicts the session's already-bound runtime_host."""


def detect_host() -> "str | None":
    """Best-effort ambient host detection; None when no signal is conclusive.

    CLAUDE_CODE_SESSION_ID wins outright — Claude Code stamps this on every
    real session. Absent that, Cursor is inferred only when its CLI is
    actually runnable (lib.host_llm.preflight: agent/cursor-agent on PATH AND
    a usable API key) — either half alone is too weak to name the host (a
    stray key file or a stray binary on PATH proves nothing about which host
    launched THIS process)."""
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return HOST_CLAUDE
    ok, _ = host_llm.preflight(HOST_CURSOR)
    return HOST_CURSOR if ok else None


def resolve_host(explicit: "str | None" = None) -> str:
    """Resolve the host for one bind attempt: an explicit host wins outright
    over auto-detection. Raises HostAmbiguousError when neither determines
    one."""
    if explicit:
        if explicit not in HOSTS:
            raise ValueError(f"unknown host {explicit!r}; must be one of {HOSTS}")
        return explicit
    host = detect_host()
    if host is None:
        raise HostAmbiguousError(
            "cannot determine runtime host: no CLAUDE_CODE_SESSION_ID, and no "
            "usable Cursor agent CLI (CURSOR_API_KEY/~/.cursor_api_key + "
            "agent/cursor-agent on PATH). Pass --host claude|cursor explicitly."
        )
    return host


def bind_runtime_host(state, explicit: "str | None" = None, *, require: bool = False) -> "str | None":
    """Bind state.runtime_host, sticky once set.

    Already bound: a contradicting explicit host raises HostConflictError (the
    host never changes mid-session); a matching or absent explicit host is a
    no-op returning the bound value.

    Unbound + require=False (cmd_start/cmd_reset's best-effort touchpoint): an
    ambiguous resolution is swallowed (returns None, leaves the session
    unbound) rather than raising — classify is the hard gate, not these.

    Unbound + require=True (cmd_classify): resolution failure raises
    HostAmbiguousError — a session must not reach EXECUTING without knowing
    which host to dispatch through."""
    if state.runtime_host is not None:
        if explicit and explicit != state.runtime_host:
            raise HostConflictError(
                f"session is already bound to runtime_host={state.runtime_host!r}; "
                f"refusing to rebind to {explicit!r} (the host is sticky for the "
                "life of a session)"
            )
        return state.runtime_host
    if require:
        host = resolve_host(explicit)
    else:
        try:
            host = resolve_host(explicit)
        except HostAmbiguousError:
            return None
    state.runtime_host = host
    return host


def require_bound_host(state) -> str:
    """The host a session dispatches through. Raises HostAmbiguousError if the
    session never bound one (e.g. a pre-schema-25 state, or one constructed
    directly without running classify)."""
    if state.runtime_host is None:
        raise HostAmbiguousError(
            "session has no bound runtime_host; run classify (or bind_runtime_host) first"
        )
    return state.runtime_host
