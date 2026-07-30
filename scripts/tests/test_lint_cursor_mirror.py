"""Unit tests for cursor/scripts/lint-cursor-mirror.py path/hook invariants.

Skill-parity checks are covered by running the live linter against the real
mirror in verify-all; these tests pin the NEW invariants (invented
~/.claude-agent/scripts path, self-diagnose canon, hook caveats) so a prose
regression fails without re-auditing by hand.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

LINT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "cursor"
    / "scripts"
    / "lint-cursor-mirror.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("lint_cursor_mirror", LINT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lint():
    return _load()


def test_forbidden_agent_scripts_path(lint):
    text = "Run `~/.claude-agent/scripts/self-diagnose.py` periodically.\n"
    errors = lint.check_path_invariants(text)
    assert any("~/.claude-agent/scripts" in error for error in errors)


def test_self_diagnose_requires_canon_path(lint):
    text = "Run self-diagnose.py when the worklist is non-empty.\n"
    errors = lint.check_path_invariants(text)
    assert any("self-diagnose.py" in error and "canon path" in error for error in errors)


def test_self_diagnose_canon_path_ok(lint):
    text = (
        "Run `~/claude-agent-instructions/scripts/self-diagnose.py` periodically.\n"
    )
    assert lint.check_path_invariants(text) == []


def test_hook_gate_without_caveat_fails(lint):
    text = (
        "Enforced: the Stop gate (`hook-turn-end-gate.py`) blocks the turn "
        "when feedback is present.\n"
    )
    errors = lint.check_hook_caveat(text)
    assert len(errors) == 1


def test_hook_gate_with_cursor_caveat_ok(lint):
    text = (
        "In Claude Code the Stop gate (`hook-turn-end-gate.py`) blocks the turn; "
        "**in Cursor those hooks do not run** — prose-enforced.\n"
    )
    assert lint.check_hook_caveat(text) == []


def test_no_hook_mention_skips_caveat_check(lint):
    assert lint.check_hook_caveat("Invoke self-improvement in the same turn.\n") == []


def test_collect_errors_rejects_forbidden_path_even_with_empty_skills(lint):
    # Minimal skeleton so skill-parity does not drown the path error.
    text = (
        "## Skills (Cursor)\n\n"
        "## Specializations in Cursor\n\n"
        "| Specialization | Read when |\n|---|---|\n"
        "resolution_confirmed_by_user\n"
        "`~/.claude-agent/scripts/self-diagnose.py`\n"
    )
    errors = lint.collect_errors(text, disk_flat=set(), disk_spec=set())
    assert any("~/.claude-agent/scripts" in error for error in errors)
