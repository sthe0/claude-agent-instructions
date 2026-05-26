#!/usr/bin/env python3
"""Structural validator for planner output (per skills/specializations/planner/SKILL.md § Plan format).

A plan file must contain, at minimum:
  - `## Problem and done criteria` section
  - `## Stages` section, with at least one stage and at least one
    `Expected result image:` line inside it
  - `## Final verification` section (end-to-end check against the
    user's done criterion)
  - `## Risks` section

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
            "image lines), Final verification, and Risks sections.",
            file=sys.stderr,
        )
        return 1
    print(f"verify-plan-file: OK {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
