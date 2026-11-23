"""The phase of the work a difficulty was met in: understanding the order, planning, or a stage.

One source of the three phase names, shared by the engine's difficulty record and by the memory
leaves that record a difficulty. A leaf or record that carries no phase is a record made before the
phase existed: it stays valid and is found by a search for any phase.

Callers reach every seam as `difficulty_phase.<name>` so a control can replace one at its module attribute.
"""
from __future__ import annotations

import re

PHASES = ("understanding", "planning", "stage")
FIELD = "met_in"
_LINE = re.compile(rf"^{FIELD}\s*:(.*)$", re.MULTILINE)


def in_frontmatter(frontmatter: str) -> str | None:
    found = _LINE.search(frontmatter)
    return found.group(1) if found else None


def phase_of(value) -> str | None:
    return value if isinstance(value, str) and value in PHASES else None


def format_met_in(phases) -> str:
    kept = [p for p in PHASES if p in set(phases or ())]
    return f"{FIELD}: [{', '.join(kept)}]" if kept else ""


def parse_met_in(raw: str | None) -> list[str]:
    if not raw:
        return []
    names = [part.strip().strip("\"'") for part in raw.strip().strip("[]").split(",")]
    return [n for n in names if n]


def met_in_of(frontmatter_value: str | None) -> list[str]:
    return [p for p in parse_met_in(frontmatter_value) if p in PHASES]


def met_in_problems(frontmatter_value: str | None) -> list[str]:
    unknown = [p for p in parse_met_in(frontmatter_value) if p not in PHASES]
    if not unknown:
        return []
    return [f"{FIELD} names {', '.join(repr(p) for p in unknown)}; "
            f"a phase is one of {', '.join(PHASES)}"]


def matches(frontmatter_value: str | None, wanted: str | None) -> bool:
    if not wanted:
        return True
    names = met_in_of(frontmatter_value)
    return not names or wanted in names


def names_phase(frontmatter_value: str | None, wanted: str | None) -> bool:
    return bool(wanted) and wanted in met_in_of(frontmatter_value)


def with_phase(frontmatter_value: str | None, phase: str) -> list[str]:
    return [p for p in PHASES if p in {*met_in_of(frontmatter_value), phase}]
