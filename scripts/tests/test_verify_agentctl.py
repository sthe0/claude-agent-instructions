"""verify-agentctl.py: structural invariant checker for the agentctl engine."""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = SCRIPTS_DIR / "verify-agentctl.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_agentctl", VERIFY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_happy_path_returns_zero():
    mod = _load_module()
    assert mod.main([]) == 0


def test_engine_start_hook_is_in_desired():
    mod = _load_module()
    desired = mod.parse_desired_hooks(mod.INSTALL_SCRIPT.read_text(encoding="utf-8"))
    assert "hook-engine-start.py" in desired


def test_check_reachability_detects_unreachable_node():
    mod = _load_module()
    from agentctl.state import Node

    all_nodes = {n.value for n in Node}
    # A transition table that loops CLASSIFIED -> CLASSIFIED: ROUTED and beyond
    # are all unreachable.
    broken = {"classify": ("CLASSIFIED", "CLASSIFIED")}
    problems = mod.check_reachability(all_nodes, broken, "CLASSIFIED", set())
    assert problems, "expected unreachable-node problems from a self-looping table"
    assert any("unreachable" in p for p in problems)


def test_check_dead_ends_detects_non_terminal_dead_end():
    mod = _load_module()
    # A table with CLASSIFIED -> STUCK, but STUCK has no outgoing edge and is
    # not in the terminal set.
    transitions = {"go": ("CLASSIFIED", "STUCK")}
    all_nodes = {"CLASSIFIED", "STUCK"}
    terminal = {"RESOLVED"}
    problems = mod.check_dead_ends(all_nodes, transitions, terminal)
    assert problems, "expected dead-end problem for STUCK"
    assert any("dead-end" in p for p in problems)


# --- the enumeration-writer pin: which fields, and which write shapes ----------

def test_runner_health_fields_are_pinned_keys():
    """`enumerated_runner_ok` is what the runner-failure blocker reads, so a write
    to it outside a real pass is the sharpest form of the bypass this pin exists to
    catch — the pin is worthless if that field is not among the watched keys."""
    mod = _load_module()
    assert {"enumerated_runner_ok", "enumerated_runner_stderr"} <= mod.ENUMERATION_KEYS


def test_walker_catches_a_plain_assignment_bypass():
    mod = _load_module()
    src = "def sneak(bag):\n    bag['enumerated_runner_ok'] = True\n"
    assert ("sneak", "enumerated_runner_ok") in mod.enumeration_bag_writers(ast.parse(src))


def test_walker_catches_an_augmented_assignment_bypass():
    """`bag['enumerate_pass'] += 1` is an AugAssign, a different node type from the
    plain assignment the walker was written against."""
    mod = _load_module()
    src = "def sneak(bag):\n    bag['enumerate_pass'] += 1\n"
    assert ("sneak", "enumerate_pass") in mod.enumeration_bag_writers(ast.parse(src))


def test_walker_catches_update_and_setdefault_bypasses():
    mod = _load_module()
    src = (
        "def by_dict(bag):\n"
        "    bag.update({'enumerated': True})\n"
        "def by_kwarg(bag):\n"
        "    bag.update(enumerated_runner_ok=True)\n"
        "def by_default(bag):\n"
        "    bag.setdefault('enumerated_at', '')\n"
    )
    found = mod.enumeration_bag_writers(ast.parse(src))
    assert ("by_dict", "enumerated") in found
    assert ("by_kwarg", "enumerated_runner_ok") in found
    assert ("by_default", "enumerated_at") in found


def test_walker_ignores_unrelated_keys_and_reads():
    mod = _load_module()
    src = (
        "def reader(bag):\n"
        "    x = bag['enumerated_runner_ok']\n"
        "    bag['unrelated'] = True\n"
        "    bag.update({'also_unrelated': 1})\n"
    )
    assert mod.enumeration_bag_writers(ast.parse(src)) == set()


def _agentctl_copy(tmp_path, monkeypatch, mod, *, cli_edit=None, module_edit=None):
    root = tmp_path / "agentctl"
    root.mkdir()
    for name, edit in (("cli.py", cli_edit), ("workspace_cleanup.py", module_edit)):
        text = (mod.AGENTCTL_ROOT / name).read_text(encoding="utf-8")
        if edit is not None:
            old, new = edit
            assert old in text
            text = text.replace(old, new, 1)
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(mod, "AGENTCTL_ROOT", root)


def test_workspace_cleanup_check_passes_on_the_real_sources():
    assert _load_module().check_workspace_cleanup() == []


def test_workspace_cleanup_check_flags_a_forced_removal_flag(tmp_path, monkeypatch):
    mod = _load_module()
    _agentctl_copy(tmp_path, monkeypatch, mod,
                   module_edit=('"worktree", "remove", item.path]', '"worktree", "remove", "-f", item.path]'))
    assert any("forced-removal" in p for p in mod.check_workspace_cleanup())


def test_workspace_cleanup_check_flags_a_missing_resolve_call(tmp_path, monkeypatch):
    mod = _load_module()
    _agentctl_copy(tmp_path, monkeypatch, mod,
                   cli_edit=("workspace_cleanup.run(", "workspace_cleanup.run_later("))
    assert any("exactly once" in p for p in mod.check_workspace_cleanup())


def test_workspace_cleanup_check_flags_execute_skipping_its_guard(tmp_path, monkeypatch):
    mod = _load_module()
    _agentctl_copy(tmp_path, monkeypatch, mod,
                   module_edit=("    refusal = _guard(item)\n", "    refusal = None\n"))
    assert any("_guard" in p for p in mod.check_workspace_cleanup())
