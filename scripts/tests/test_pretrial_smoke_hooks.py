"""Pre-trial defect #437: the pre-land smoke exercises the PreToolUse hook gate live.

The smoke's only live launch runs with every tool disabled, so a change to a tool-call hook
lands with no live check. The repair adds a required live check, `live:hook-gate-deny`, whose
launch enables an editing tool inside the sandbox.

A test asserts the repaired behaviour with a message that starts `BEHAVIOUR:`; every setup
assertion lacks that marker. The probe builder the repair adds is reached inside the test
body only, so this file collects on a tree that lacks it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from lib import instruction_smoke_gate

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
LIVE_SCRIPT = SCRIPTS_DIR / "instruction-sandbox-live.py"
HOOK_CHECK = "live:hook-gate-deny"
TOOL_LIST_FLAGS = ("--tools", "--allowedTools", "--allowed-tools")


def _load_live_script():
    spec = importlib.util.spec_from_file_location("instruction_sandbox_live", LIVE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enabled_tools(argv: list[str]) -> list[str]:
    """The tool names an argv hands to a tool-list flag."""
    return [
        tool
        for flag, value in zip(argv, argv[1:])
        if flag in TOOL_LIST_FLAGS
        for tool in value.replace(",", " ").split()
    ]


def test_i437_smoke_requires_live_hook_check():
    assert "live:core-marker" in instruction_smoke_gate.REQUIRED_CHECKS, (
        "setup: the existing live check is not required"
    )

    required = instruction_smoke_gate.REQUIRED_CHECKS

    assert HOOK_CHECK in required, (
        f"BEHAVIOUR: the pre-land smoke does not require a live hook check; the required "
        f"checks are {list(required)}"
    )


def test_i437_live_hook_probe_runs_with_edit_tool():
    live = _load_live_script()
    marker_argv = live.build_argv("haiku")
    assert ["--tools", ""] == marker_argv[-2:], "setup: the marker probe no longer disables its tools"

    build_hook_probe_argv = getattr(live, "build_hook_probe_argv", None)

    assert callable(build_hook_probe_argv), (
        "BEHAVIOUR: instruction-sandbox-live.py has no builder for a hook probe launch"
    )
    argv = build_hook_probe_argv("haiku")
    assert "Edit" in _enabled_tools(argv), (
        f"BEHAVIOUR: the hook probe launch does not enable the Edit tool: {argv}"
    )
    assert not any(flag == "--tools" and value == "" for flag, value in zip(argv, argv[1:])), (
        f"BEHAVIOUR: the hook probe launch still disables every tool: {argv}"
    )
