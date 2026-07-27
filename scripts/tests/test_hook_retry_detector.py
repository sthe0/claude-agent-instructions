"""hook-retry-detector.py: the cheap-command allowlist.

Core lists only org-neutral commands; a deployment's own VCS read verbs attach
through `retry_detector_allowlist=` in agent-identity.local. These tests use a
SYNTHETIC command name, so they assert the seam rather than any deployment's
real tooling.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "hook_retry_detector",
    Path(__file__).resolve().parents[1] / "hook-retry-detector.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_core_allowlist_covers_org_neutral_commands():
    rx = mod.build_allowlist_re(extra=())
    for cmd in ("ls -la", "pwd", "git status --short", "git log -n 5", "rg TODO"):
        assert rx.match(cmd), cmd


def test_core_allowlist_names_no_specific_vcs_beyond_git():
    """The org-neutrality invariant: a future entry naming another VCS's verbs
    fails here rather than shipping to every deployment."""
    vcs_entries = [e for e in mod._CORE_ALLOWLIST if " " in e and not e.startswith(("cat", "python3"))]
    assert all(e.startswith("git ") for e in vcs_entries), vcs_entries


def test_expensive_command_is_not_allowlisted():
    rx = mod.build_allowlist_re(extra=())
    assert rx.match("othervcs status") is None
    assert rx.match("make -j8 all") is None


def test_extra_allowlist_admits_a_machine_local_command():
    rx = mod.build_allowlist_re(extra=("othervcs status", "othervcs log"))
    assert rx.match("othervcs status")
    assert rx.match("othervcs log -n 5")
    # not a blanket pass for the tool — only the listed subcommands
    assert rx.match("othervcs push") is None


def test_extra_entries_are_escaped_not_interpreted_as_regex():
    """Entries are literal command prefixes; a metacharacter must not widen the
    match (nor raise, which would cost the whole allowlist)."""
    rx = mod.build_allowlist_re(extra=("othervcs st.tus",))
    assert rx.match("othervcs st.tus")
    assert rx.match("othervcs status") is None


def test_extra_allowlist_absent_identity_file_yields_no_extras():
    assert mod._extra_allowlist(Path("/nonexistent/agent-identity.local")) == ()
