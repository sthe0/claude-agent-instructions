#!/usr/bin/env python3
"""Structural validator for planner output (per skills/specializations/planner/SKILL.md § Plan format).

NOTE — this is the PROSE MIRROR, not the canonical contract. The canonical
definition and enforcer of plan structure is the typed model in
`agentctl/state.py` + `agentctl/plan.py` (grouped dataclasses Subject/Means/
Actor/Criterion/Principle/Supply/Outcome + the substantive, confidence-enum,
dangling-ref, unknown-element and acyclicity validators). This script only
checks that a human-readable plan file carries the matching prose labels; on
any divergence the code wins. See memory-global/leaves/plan-activity-ontology.md.

A plan file must contain, at minimum:
  - `## Problem and done criteria` section
  - `## Stages` section, with at least one stage and at least one
    `Expected result image:` line inside it
  - `## Final verification` section (end-to-end check against the
    user's done criterion)
  - `## Risks` section

For **substantive** plans — those containing a line that matches
``weight_class: substantive`` (case-insensitive, anywhere in the file)
— the `## Stages` section must additionally carry, at least once each:
  - `Material:` (element 2 — what is transformed, initial state)
  - `Means & method:` *or* both `Means:` and `Method:` (elements 4/4')
  - `Conditions & invariants:` *or* both `Conditions:` and `Invariants:` (element 5)
  - `Principle:` containing `Source:`, `Confidence:`, and `Refutation:` (element 7)

Substantive plans must also carry, at plan level (anywhere in the file):
  - `External research:` — the planner's recorded decision on whether
    internet/intranet research (for information or ideas) would improve the
    plan: what was found, or one line on why it is not warranted. Mirrors the
    `[meta] external_research` TOML key enforced by `agentctl/plan.py`. See
    planner SKILL.md § Research existing solutions, information, and ideas.

Legacy / non-substantive plans (no weight_class marker) are validated only
against the 4-section baseline, preserving backward compatibility.

A stage MAY optionally carry an executable form of its done criterion via a
`Verify command:` line (and the TOML keys `verify_command` / `expected_exit`):
when present on a measurable stage the engine runs it and gates the stage's
PASSED on the actual exit code. This is optional and not structurally required.

This is a structural check only — it cannot verify that the *content*
of each field is meaningful. The point is to make "did you remember
the verification image" mechanical instead of recall-dependent.

Used in two places:
  1. Standalone CLI: `verify-plan-file.py <path>` — ad-hoc check.
  2. From `scripts/spawn-specialist.py` after a `PLAN-READY:` return,
     against the path the planner declared on its `Plan:` line.

Exit codes:
  0 — OK
  1 — file missing or structural violation (stderr explains)
  2 — argv error
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = (
    "Problem and done criteria",
    "Stages",
    "Final verification",
    "Risks",
)

HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
EXPECTED_LINE_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Expected result image\**\s*:")

# Detects `weight_class: substantive` (or `weight class = substantive`) anywhere.
SUBSTANTIVE_RE = re.compile(r"(?im)^.*weight[_ ]?class\s*[:=]\s*substantive")

# Activity-structure labels required inside ## Stages for substantive plans.
_MATERIAL_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Material\**\s*:")
_MEANS_AND_METHOD_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Means\s*&\s*method\**\s*:")
_MEANS_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Means\**\s*:")
_METHOD_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Method\**\s*:")
_CONDITIONS_AND_INVARIANTS_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Conditions\s*&\s*invariants\**\s*:")
_CONDITIONS_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Conditions\**\s*:")
_INVARIANTS_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Invariants\**\s*:")
_CAPABILITY_RE = re.compile(r"(?im)^\s*[-*]?\s*\**(?:Capability|Actor)\**\s*:")
_PRINCIPLE_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Principle\**\s*:")
# Plan-level (not per-stage) external-research decision, required for substantive plans.
_EXTERNAL_RESEARCH_RE = re.compile(r"(?im)^\s*[-*]?\s*\**External research\**\s*:")
_SOURCE_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Source\**\s*:")
_CONFIDENCE_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Confidence\**\s*:")
_REFUTATION_RE = re.compile(r"(?im)^\s*[-*]?\s*\**Refutation\**\s*:")
# Element 7's OPTIONAL statement_kind label (знание/сущее | норма/должное). Absent =>
# grandfathered (older plans predate the field); present => value must be one of the
# two categories. The capture group grabs the value token for validation.
_STATEMENT_KIND_RE = re.compile(r"(?im)^\s*[-*]?\s*\**statement_kind\**\s*:\s*`?(\S+?)`?\s*$")
_STATEMENT_KINDS = ("сущее", "должное")


def slice_section(text: str, heading: str) -> str | None:
    """Return the text between `## <heading>` and the next `## ` heading,
    or None if the heading is absent."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE | re.IGNORECASE
    )
    m = pattern.search(text)
    if not m:
        return None
    start = m.end()
    next_m = HEADING_RE.search(text, start)
    end = next_m.start() if next_m else len(text)
    return text[start:end]


def check(path: Path) -> list[str]:
    """Return a list of error strings; empty list means OK."""
    errors: list[str] = []
    if not path.exists():
        return [f"plan file not found: {path}"]
    text = path.read_text(encoding="utf-8")

    for section in REQUIRED_SECTIONS:
        if not slice_section(text, section):
            errors.append(f"missing required section: `## {section}`")

    stages = slice_section(text, "Stages")
    if stages is not None:
        if not EXPECTED_LINE_RE.search(stages):
            errors.append(
                "no `Expected result image:` line found inside `## Stages`. "
                "Each stage must declare what 'success' looks like as a "
                "concrete observable + expected value/state."
            )

    if SUBSTANTIVE_RE.search(text):
        stages_text = stages or ""

        # Plan-level: the planner must have recorded an external-research decision.
        if not _EXTERNAL_RESEARCH_RE.search(text):
            errors.append(
                "substantive plan: missing plan-level `External research:` line. "
                "Record whether internet/intranet research (for information or ideas) "
                "would improve the plan — what was found, or one line on why it is "
                "not warranted (planner SKILL.md § Research)."
            )

        if not _MATERIAL_RE.search(stages_text):
            errors.append(
                "substantive plan: missing `Material:` label inside `## Stages` "
                "(element 2 — what is transformed and its initial state)"
            )

        has_means_method = _MEANS_AND_METHOD_RE.search(stages_text) or (
            _MEANS_RE.search(stages_text) and _METHOD_RE.search(stages_text)
        )
        if not has_means_method:
            errors.append(
                "substantive plan: missing `Means & method:` label inside `## Stages` "
                "(elements 4/4' — what is used and how; "
                "use `Means & method:` or both `Means:` and `Method:` separately)"
            )

        has_cond_inv = _CONDITIONS_AND_INVARIANTS_RE.search(stages_text) or (
            _CONDITIONS_RE.search(stages_text) and _INVARIANTS_RE.search(stages_text)
        )
        if not has_cond_inv:
            errors.append(
                "substantive plan: missing `Conditions & invariants:` label inside `## Stages` "
                "(element 5 — execution conditions and properties that must stay unchanged; "
                "use `Conditions & invariants:` or both `Conditions:` and `Invariants:` separately)"
            )

        if not _CAPABILITY_RE.search(stages_text):
            errors.append(
                "substantive plan: missing `Capability:` label inside `## Stages` "
                "(element 6 — actor capability required to execute; use `Capability:` or `Actor:`)"
            )

        if not _PRINCIPLE_RE.search(stages_text):
            errors.append(
                "substantive plan: missing `Principle:` label inside `## Stages` "
                "(element 7 — the inference behind the chosen transformation)"
            )
        else:
            for label, label_re in [
                ("Source", _SOURCE_RE),
                ("Confidence", _CONFIDENCE_RE),
                ("Refutation", _REFUTATION_RE),
            ]:
                if not label_re.search(stages_text):
                    errors.append(
                        f"substantive plan: `Principle:` block inside `## Stages` is missing "
                        f"`{label}:` (element 7 — state the principle's {label.lower()})"
                    )
            # statement_kind is OPTIONAL (grandfathered when absent), but when present
            # its value must name one of the two categories — знание or норма.
            for m in _STATEMENT_KIND_RE.finditer(stages_text):
                value = m.group(1)
                if value not in _STATEMENT_KINDS:
                    errors.append(
                        f"substantive plan: `Principle:` block has `statement_kind: {value}` "
                        f"which is not one of {list(_STATEMENT_KINDS)} (element 7 — the "
                        f"principle is either знание/сущее or норма/должное)"
                    )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="path to plan markdown file")
    args = parser.parse_args(argv)

    errors = check(Path(args.path))
    if errors:
        print(f"verify-plan-file: FAIL {args.path}", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "Plan format reference: ~/.claude/skills/planner/SKILL.md § Plan format.\n"
            "A plan needs Problem/done-criteria, Stages (with Expected result\n"
            "image lines), Final verification, and Risks sections.\n"
            "Substantive plans (weight_class: substantive) additionally require\n"
            "Capability (or Actor), Material, Means & method, Conditions & invariants,\n"
            "and Principle (with Source, Confidence, Refutation) inside ## Stages,\n"
            "plus a plan-level External research: line.",
            file=sys.stderr,
        )
        return 1
    print(f"verify-plan-file: OK {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
