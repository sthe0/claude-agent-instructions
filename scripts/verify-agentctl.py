#!/usr/bin/env python3
"""Verify the agentctl engine's structural invariants (verify-all check).

This is the static guard for the coordination engine itself — the analogue of
verify-cross-refs.py for prose. It does NOT run the LLM or spawn anything; it
asserts that the deterministic skeleton the engine relies on is internally
consistent:

  1. State schema parses — a minimal SessionState constructs and round-trips
     through the JSON the store (de)serializes (from_json(to_json(s)) == s).
  2. Transition table is sound — every Node is reachable from the start node
     (BLOCKED via the block side-channel, which is intentional and not in the
     pure table) and no non-terminal Node is a dead end with no outgoing edge.
  3. Every cognitive leaf the engine can emit (spawn kinds + the difficulty
     skill) resolves to an existing ~/.claude/skills/<name>/SKILL.md.
  4. Every engine gate has a guardian hook wired in install-reminder-hooks.sh
     DESIRED (plan_approval -> hook-state-gate.py; resolution ->
     hook-resolution-reminder.py).

The reachability / dead-end / gate checks are pure functions taking the table
and node set as arguments so tests can feed a deliberately broken table and
confirm the check fails.

Accepts (and ignores) --staged so verify-all can pass it uniformly. Exit 1 on
any problem.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SKILLS_ROOT = Path.home() / ".claude" / "skills"
INSTALL_SCRIPT = SCRIPTS_DIR / "install-reminder-hooks.sh"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Skill / specialist names the engine can emit: spawn kinds dispatched via
# spawn-specialist.py (`spawn:<kind>` stage executors) plus the difficulty skill
# routed to on a failed stage. Kept explicit because the engine names them in
# prose Directive detail, not yet a structured registry.
COGNITIVE_LEAVES = ["planner", "developer", "overcome-difficulty"]

# Each engine gate (gates.GUARDIANS key) must have a non-skippable guardian hook
# wired in DESIRED.
GATE_TO_HOOK = {
    "plan_approval": "hook-state-gate.py",
    "resolution": "hook-resolution-reminder.py",
}

# Nodes allowed to have no outgoing table edge.
TERMINAL_NODES = {"RESOLVED", "BLOCKED"}
# Nodes reachable by a side-channel command rather than the pure table.
SIDE_CHANNEL_REACHABLE = {"BLOCKED"}


def check_state_roundtrip() -> list[str]:
    from agentctl.state import (
        Actor,
        Critique,
        Criterion,
        Declaration,
        Difficulty,
        Investigation,
        Partition,
        GateRecord,
        Means,
        Node,
        Outcome,
        SessionState,
        Stage,
        StageStatus,
        Subject,
    )

    problems: list[str] = []
    s = SessionState(session_id="vfy", task_id="vfy-task", goal="g")
    if s.node != Node.CLASSIFIED.value:
        problems.append(f"fresh SessionState should start at CLASSIFIED, got {s.node}")
    rt = SessionState.from_json(s.to_json())
    if rt != s:
        problems.append("from_json(to_json(s)) != s for a minimal SessionState")

    # round-trip a richer state (gates + a stage) — exercises the field-wise rebuild
    s2 = SessionState(
        session_id="vfy2",
        task_id="vfy2-task",
        node=Node.EXECUTING.value,
        weight_class="SUBSTANTIVE",
        route="SPAWN",
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
        partition=Partition(m1=True, m2=True, verdict="recommended"),
        stages=[
            Stage(
                index=1,
                title="t",
                subject=Subject(material="m", result="img"),
                means=Means(means="Edit", method="do"),
                actor=Actor(executor="spawn:developer"),
                criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    if SessionState.from_json(s2.to_json()) != s2:
        problems.append("from_json(to_json(s2)) != s2 for a populated SessionState")

    # round-trip a DIAGNOSING state carrying a complete Difficulty record — the
    # overcome-difficulty sub-spine's serialization seam
    s3 = SessionState(
        session_id="vfy3",
        task_id="vfy3-task",
        node=Node.DIAGNOSING.value,
        difficulty=Difficulty(
            declaration=Declaration(expected="e", actual="a", mismatch="m"),
            investigation=Investigation(localized_expectation="le", localized_actual="la"),
            critique=Critique(functional_ground="fg", replanning_task="rt"),
        ),
    )
    rt3 = SessionState.from_json(s3.to_json())
    if rt3 != s3:
        problems.append("from_json(to_json(s3)) != s3 for a DIAGNOSING state with a Difficulty")
    if not (rt3.difficulty and rt3.difficulty.complete()):
        problems.append("round-tripped Difficulty did not report complete()")
    return problems


def reachable_nodes(transitions: dict, start: str) -> set[str]:
    """Forward-reachable node set from `start` over the (event -> (from, to)) table."""
    edges: dict[str, list[str]] = {}
    for frm, to in transitions.values():
        edges.setdefault(frm, []).append(to)
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def check_reachability(
    all_nodes: set[str], transitions: dict, start: str, side_channel: set[str]
) -> list[str]:
    reached = reachable_nodes(transitions, start) | set(side_channel)
    unreachable = sorted(all_nodes - reached)
    if unreachable:
        return [f"unreachable node(s) from {start}: {unreachable}"]
    return []


def check_dead_ends(all_nodes: set[str], transitions: dict, terminal: set[str]) -> list[str]:
    has_out = {frm for frm, _ in transitions.values()}
    dead = sorted(n for n in all_nodes if n not in terminal and n not in has_out)
    if dead:
        return [f"non-terminal dead-end node(s) with no outgoing edge: {dead}"]
    return []


def check_cognitive_leaves(names: list[str], skills_root: Path) -> list[str]:
    problems: list[str] = []
    for name in names:
        if not (skills_root / name / "SKILL.md").is_file():
            problems.append(
                f"cognitive leaf {name!r} has no SKILL.md at {skills_root / name / 'SKILL.md'}"
            )
    return problems


def parse_desired_hooks(install_script_text: str) -> set[str]:
    """Hook basenames listed in the DESIRED block of install-reminder-hooks.sh."""
    hooks: set[str] = set()
    in_desired = False
    for line in install_script_text.splitlines():
        if re.match(r"\s*DESIRED\s*=\s*\[", line):
            in_desired = True
            continue
        if in_desired:
            if line.strip().startswith("]"):
                break
            for m in re.finditer(r'"([^"]*?\.py)(?:\s[^"]*)?"', line):
                hooks.add(Path(m.group(1)).name)
    return hooks


def check_gate_guardians(gate_to_hook: dict, desired_hooks: set[str]) -> list[str]:
    problems: list[str] = []
    for gate, hook in gate_to_hook.items():
        if hook not in desired_hooks:
            problems.append(f"gate {gate!r} has no guardian hook {hook!r} wired in DESIRED")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="ignored; accepted for verify-all uniformity")
    parser.parse_args(argv)

    from agentctl import gates, machine
    from agentctl.state import Node

    problems: list[str] = []
    problems += check_state_roundtrip()

    all_nodes = {n.value for n in Node}
    problems += check_reachability(
        all_nodes, machine.TRANSITIONS, Node.CLASSIFIED.value, SIDE_CHANNEL_REACHABLE
    )
    problems += check_dead_ends(all_nodes, machine.TRANSITIONS, TERMINAL_NODES)

    problems += check_cognitive_leaves(COGNITIVE_LEAVES, SKILLS_ROOT)

    # the gate set is taken from the engine itself so a new gate without a mapping
    # surfaces here rather than being silently unguarded
    engine_gates = set(gates.GUARDIANS)
    unmapped = sorted(engine_gates - set(GATE_TO_HOOK))
    if unmapped:
        problems.append(f"engine gate(s) with no guardian-hook mapping: {unmapped}")
    desired = parse_desired_hooks(INSTALL_SCRIPT.read_text(encoding="utf-8"))
    problems += check_gate_guardians(GATE_TO_HOOK, desired)

    if problems:
        print("verify-agentctl: FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("verify-agentctl: OK — engine schema, transitions, leaves, gate guardians consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
