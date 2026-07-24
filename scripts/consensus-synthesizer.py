#!/usr/bin/env python3
"""consensus-synthesizer — active synthesizer that PROPOSES, never executes (ADR-0001 Variant D).

Pipeline over the flagged clusters from ``core-difficulty-digest.py``:

    normalize-to-difficulty
      -> cluster-by-functional-ground      (REUSES the digest's clustering — stage 9)
      -> detect-conflict                    (A-vs-not-A over two edits)
      -> induce-invariant                   (the critique primitive: commonality / difference)
      -> ranked menu                        (surfaced via AskUserQuestion — HUMAN-gated)
      -> promote-to-layer                   (HANDOFF to planner -> approval -> developer)

The agent proposes a ranked menu; it has **no veto** and **graduated authority**. The
deterministic stages (normalize / cluster / detect-conflict / induce-invariant) are pure and
tested. The menu presentation and promotion are human-gated handoffs — a synthesizer run leaves
Core **byte-identical** until a human approves (the no-auto-write invariant IS tested).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from difficulty_channel import DifficultyRecord  # noqa: E402


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Reuse the digest's clustering (stage 9) and the record-experience tokenizer — no reinvention.
_digest = _load("core_difficulty_digest", "core-difficulty-digest.py")
cluster_records = _digest.cluster_records
is_flagged = _digest.is_flagged
read_mass_threshold = _digest.read_mass_threshold
tokenize = _digest.tokenize

REPO_ROOT = SCRIPTS_DIR.parent


@dataclass(frozen=True)
class Edit:
    """A proposed edit / desired-state assertion competing to resolve a difficulty."""

    target: str        # the rule/path the edit is about
    directive: str     # the asserted behaviour text
    assertion: bool = True  # True = "do X"; False = "do NOT X" (the A-vs-not-A axis)


def normalize(records: list[DifficultyRecord]) -> list[DifficultyRecord]:
    """Ensure every item is a DifficultyRecord (adapters already return them)."""
    for r in records:
        if not isinstance(r, DifficultyRecord):
            raise TypeError(f"not a DifficultyRecord: {r!r}")
    return list(records)


def _same_directive(a: Edit, b: Edit, ratio: float = 0.6) -> bool:
    ta, tb = set(tokenize(a.directive)), set(tokenize(b.directive))
    if not ta or not tb:
        return False
    return len(ta & tb) / max(len(ta), len(tb)) >= ratio


def detect_conflict(a: Edit, b: Edit) -> bool:
    """A-vs-not-A: same target, the same core directive, opposite assertion."""
    return a.target == b.target and a.assertion != b.assertion and _same_directive(a, b)


def induce_invariant(a: Edit, b: Edit) -> dict:
    """The critique primitive: commonality (the invariant -> Core candidate) and difference
    (the residue -> Team/Personal). Reused verbatim, never reinvented."""
    ta, tb = set(tokenize(a.directive)), set(tokenize(b.directive))
    return {
        "commonality": sorted(ta & tb),            # shared invariant -> Core
        "difference": {                            # residue -> lower layers
            "a_only": sorted(ta - tb),
            "b_only": sorted(tb - ta),
        },
    }


@dataclass
class Proposal:
    functional_ground: str
    mass: int
    has_critical: bool
    n_reports: int
    reporters: list[str]
    invariant: dict | None = None  # induced when the cluster carries divergent grounds


@dataclass
class SynthesisResult:
    menu: list[Proposal] = field(default_factory=list)
    core_written: bool = False  # a dry-run NEVER writes Core


def rank_menu(records: list[DifficultyRecord], threshold: int) -> list[Proposal]:
    clusters = cluster_records(normalize(records))
    flagged = [c for c in clusters if is_flagged(c, threshold)]
    flagged.sort(key=lambda c: (-int(c.has_critical), -c.mass))
    menu: list[Proposal] = []
    for c in flagged:
        invariant = None
        if len(c.items) >= 2:
            # apply the critique primitive to the first and last grounds in the cluster
            a = Edit(target=c.items[0].target, directive=c.items[0].functional_ground)
            b = Edit(target=c.items[-1].target, directive=c.items[-1].functional_ground)
            invariant = induce_invariant(a, b)
        menu.append(Proposal(
            functional_ground=c.functional_ground,
            mass=c.mass,
            has_critical=c.has_critical,
            n_reports=len(c.items),
            reporters=sorted(c.reporters),
            invariant=invariant,
        ))
    return menu


def promote_to_layer(proposal: Proposal) -> dict:
    """Promotion is a HANDOFF to the human spine — it returns a route descriptor and writes
    NOTHING. Core changes go through planner -> approval -> developer; never an auto-edit."""
    return {
        "action": "handoff",
        "route": "planner -> approval -> developer",
        "functional_ground": proposal.functional_ground,
        "core_written": False,
    }


def run_synthesis(records: list[DifficultyRecord], threshold: int, dry_run: bool = True) -> SynthesisResult:
    """Build the ranked menu. NEVER writes Core (propose-not-execute); promotion is a separate
    human-approved handoff. dry_run is the default and the only mode that exists in code."""
    return SynthesisResult(menu=rank_menu(records, threshold), core_written=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", action="append", default=[], dest="channels")
    p.add_argument("--threshold", type=int, default=None)
    a = p.parse_args(argv)
    channels = a.channels or _digest.default_channels()
    threshold = read_mass_threshold(override=a.threshold)
    records = _digest.pull_all(channels)
    result = run_synthesis(records, threshold)
    if not result.menu:
        print("consensus-synthesizer: no flagged clusters to synthesize.")
        return 0
    print(f"consensus-synthesizer: ranked menu of {len(result.menu)} proposal(s) (PROPOSE-only):")
    for i, prop in enumerate(result.menu, 1):
        crit = " CRITICAL" if prop.has_critical else ""
        print(f"  {i}. [mass {prop.mass}{crit}] {prop.functional_ground!r} "
              f"({prop.n_reports} report(s) by {prop.reporters})")
        if prop.invariant:
            print(f"     invariant(commonality)={prop.invariant['commonality']}")
    print("  → choose via AskUserQuestion; promotion routes through planner → approval → developer.")
    print("  Core is byte-unchanged by this run (no veto, no auto-edit).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
