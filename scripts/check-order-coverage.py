#!/usr/bin/env python3
"""Reusable resolver: assert a plan's `[meta.order.coverage]` is total over its
`[meta.order]` requirements AND that every named control resolves against the
plan's own stages / final_check.

Two totality directions, and only the second is this script's job:

  * every declared requirement id has at least one coverage entry, and no entry
    names an undeclared id — already enforced at the submission seam
    (`submission.py`'s `_order_violations`), whose own docstring is explicit that
    it "checks that every declared id HAS an entry, never that the control the
    entry names actually decides the requirement." Re-verified here too, since a
    caller with only a plan path (no session, no loaded PlanDoc) has no other way
    to ask the same question submission.py answers at approve-time.
  * every control string named in a coverage entry RESOLVES to something real in
    the plan — the half submission.py deliberately declines. This is the one
    place that resolution is checked.

Accepts exactly three control-name grammars, matched by PREFIX (never exact
equality) so an author may append a trailing free-prose parenthetical after the
fixed form — e.g. "stage 12 landed assertion (full: containment in main and
origin/main, plus reachability of the delivered commit)":

  "stage <n> verify_command"    stage <n> exists and declares a verify_command
  "final_check <n>"             meta.final_check has a 1-based entry at <n>
  "stage <n> landed assertion"  stage <n> exists and its criterion is verify_kind
                                 == "landed" — the third grammar exists because a
                                 landed-kind stage has NO verify_command at all
                                 (plan.py's R1 forbids declaring one alongside
                                 verify_kind == "landed"), so a resolver knowing
                                 only the first two grammars would report such a
                                 requirement's control unresolvable.

A control name outside all three grammars is a FAILURE, never a silent skip: a
typo in a coverage entry must be caught here rather than pass as an unrecognized
shape nobody checks.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from agentctl.plan import PlanError, load_plan  # noqa: E402

_STAGE_VERIFY_COMMAND = re.compile(r"^stage (\d+) verify_command\b")
_FINAL_CHECK = re.compile(r"^final_check (\d+)\b")
_STAGE_LANDED_ASSERTION = re.compile(r"^stage (\d+) landed assertion\b")


def resolve_control(name: str, doc) -> str | None:
    """None if `name` resolves against `doc`; otherwise the reason it does not."""
    stages_by_index = {s.index: s for s in doc.stages}

    m = _STAGE_VERIFY_COMMAND.match(name)
    if m:
        idx = int(m.group(1))
        stage = stages_by_index.get(idx)
        if stage is None:
            return f"{name!r}: no stage {idx}"
        if not stage.criterion.verify_command:
            return f"{name!r}: stage {idx} declares no verify_command"
        return None

    m = _FINAL_CHECK.match(name)
    if m:
        idx = int(m.group(1))
        if idx < 1 or idx > len(doc.meta.final_check):
            return f"{name!r}: no final_check {idx} (plan has {len(doc.meta.final_check)})"
        return None

    m = _STAGE_LANDED_ASSERTION.match(name)
    if m:
        idx = int(m.group(1))
        stage = stages_by_index.get(idx)
        if stage is None:
            return f"{name!r}: no stage {idx}"
        if stage.criterion.verify_kind != "landed":
            return f"{name!r}: stage {idx} is not a landed-kind criterion"
        return None

    return (
        f"{name!r}: matches none of the three accepted control-name grammars "
        f"('stage <n> verify_command', 'final_check <n>', 'stage <n> landed "
        f"assertion') — a name outside the grammar is a failure, never a skip"
    )


def coverage_violations(doc) -> list[str]:
    """Every way `[meta.order.coverage]` fails totality against `doc`. [] == clean."""
    order = doc.meta.order
    if order is None:
        return ["[meta.order] is absent — nothing to check"]
    missing_req = [r.id for r in order.requirements if r.id not in order.coverage]
    out = [f"requirement {rid!r} has no coverage entry" for rid in missing_req]
    declared = {r.id for r in order.requirements}
    stray = [key for key in order.coverage if key not in declared]
    out.extend(f"coverage key {key!r} names no declared requirement" for key in stray)
    for req_id, controls in sorted(order.coverage.items()):
        for control in controls:
            problem = resolve_control(control, doc)
            if problem:
                out.append(f"requirement {req_id}: {problem}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <plan.toml>", file=sys.stderr)
        return 2
    plan_path = argv[1]
    try:
        doc = load_plan(plan_path)
    except (OSError, PlanError) as exc:
        print(f"cannot load {plan_path!r}: {exc}", file=sys.stderr)
        return 1
    violations = coverage_violations(doc)
    if violations:
        print(f"FAIL — {len(violations)} coverage problem(s) in {plan_path}:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    order = doc.meta.order
    print(
        f"OK — {plan_path}: {len(order.requirements)} requirement(s), "
        f"{sum(len(v) for v in order.coverage.values())} control(s), all resolved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
