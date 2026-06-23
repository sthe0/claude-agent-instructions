"""verify-agentctl.py: structural invariant checker for the agentctl engine."""
from __future__ import annotations

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
