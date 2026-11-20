"""The negative run: the same phase with the probe plugin's throw and deny made inert.

It holds only when the cases that rest on the plugin deny (including the phase anchor) fail
with their marker executed, and every other case still matches the phase's last pass.
Output goes under ``<state>/negative/``, which ``check-record`` never reads.
"""
from __future__ import annotations

from pathlib import Path

from . import core, record, runner

HOLDS_LINE = "NEGATIVE-HOLDS"
BROKEN_LINE = "NEGATIVE-BROKEN"


def last_pass_cases(phase: core.Phase) -> dict | None:
    entry = core.read_pin().get("last_pass", {}).get(phase.name)
    rec = record.read_record(Path(entry["record"])) if entry else None
    return rec["cases"] if rec else None


def judge(phase: core.Phase, negative_cases: dict, last_pass: dict | None) -> list[str]:
    """Reasons the negative run does not hold; empty when it does."""
    infos = {c.case_id: c for c in core.phase_cases(phase)}
    resting = {cid for cid in negative_cases if infos[cid].rests_on_plugin_deny}
    if phase.anchor not in resting:
        return [f"the anchor {phase.anchor} is not among the deny-resting cases that ran"]
    if last_pass is None:
        return [f"{phase.name} has no recorded last pass to compare the other cases with"]
    problems = []
    for cid in sorted(resting):
        entry = negative_cases[cid]
        if entry["status"] != record.FAIL or not entry["evidence"].get("marked_executed"):
            problems.append(f"{cid}: expected fail with its marker executed, got {entry['status']}")
    for cid, entry in sorted(negative_cases.items()):
        if cid in resting:
            continue
        if entry["status"] == record.UNOBSERVED:
            problems.append(f"{cid}: unobserved under the inert run")
        elif cid in last_pass and entry["status"] != last_pass[cid]["status"]:
            problems.append(f"{cid}: {entry['status']} differs from its last pass {last_pass[cid]['status']}")
    return problems


def run_negative(phase: core.Phase, case_ids: set[str] | None = None) -> int:
    """Exit 1 (and print NEGATIVE-HOLDS) iff the negative holds; 0 otherwise."""
    rec_dir, cases = runner.run_phase(phase, case_ids=case_ids, inert=True)
    problems = judge(phase, cases, last_pass_cases(phase))
    if problems:
        print(BROKEN_LINE)
        for problem in problems:
            print(f"  {problem}")
        print(f"record: {rec_dir}")
        return 0
    print(f"record: {rec_dir}")
    print(HOLDS_LINE)
    return 1
