"""Tests for agentctl's central --session defaulting (_inject_default_session).

The production-edit gate (hook-state-gate.py) authorizes by the HARNESS
conversation session_id ($CLAUDE_CODE_SESSION_ID). A self-chosen --session
silently decouples the engine state file from the gate's view, so an omitted
--session must default to the harness id. These tests pin the pure argv helper
and confirm parse_args over the injected argv yields the harness session.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS_DIR / "agentctl" / "cli.py"


def _load_cli():
    # Ensure the package's sibling imports (lib, .classify, ...) resolve.
    import sys

    for p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR / "agentctl")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location("agentctl.cli", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cli = _load_cli()
_inject = _cli._inject_default_session


def test_injects_when_absent():
    out = _inject(["classify", "--files", "4"], "harness-xyz")
    assert out == ["classify", "--files", "4", "--session", "harness-xyz"]


def test_noop_when_session_space_form_present():
    argv = ["approve", "--session", "chosen-id", "--by", "user"]
    assert _inject(argv, "harness-xyz") == argv


def test_noop_when_session_equals_form_present():
    argv = ["approve", "--session=chosen-id", "--by", "user"]
    assert _inject(argv, "harness-xyz") == argv


def test_noop_when_harness_empty():
    argv = ["classify", "--files", "4"]
    assert _inject(argv, "") == argv
    assert _inject(argv, None) == argv


def test_returns_a_copy_not_the_same_list():
    argv = ["classify", "--files", "4"]
    out = _inject(argv, "harness-xyz")
    assert out is not argv
    # no-op branches must also return a fresh list, never the caller's object
    assert _inject(argv, None) is not argv


def test_parse_args_over_injected_argv_yields_harness_session():
    raw = _inject(["classify", "--files", "4", "--changed-lines", "35"], "harness-xyz")
    args = _cli.build_parser().parse_args(raw)
    assert args.session == "harness-xyz"


def test_parse_args_keeps_explicit_session_over_harness():
    raw = _inject(["classify", "--files", "4", "--session", "chosen-id"], "harness-xyz")
    args = _cli.build_parser().parse_args(raw)
    assert args.session == "chosen-id"
