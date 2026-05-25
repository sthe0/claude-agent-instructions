#!/usr/bin/env python3
"""Lint the cursor-rules mirror for structural drift against `skills/`.

Three presence checks — no full-text comparison (formulations legitimately
differ between Claude Code and Cursor contexts):

  1. Flat-skill parity. Every directory `skills/<name>/` with a SKILL.md
     (excluding `skills/specializations/`) must appear as a `### \`<name>\``
     subheading inside the cursor mirror's `## Skills ...` section, and vice
     versa (no mirror entries without a real skill on disk).

  2. Specialization parity. Every `skills/specializations/<name>/SKILL.md`
     must appear as a `| \`<name>\` | ... |` row inside the cursor mirror's
     `## Specializations ...` table, and vice versa.

  3. Trigger marker presence. Each skill block in the mirror's Skills
     section must contain `**TRIGGER:**` — confirming the mirror at least
     declares the trigger contract, even if the wording diverges from the
     SKILL.md frontmatter.

Exit code 1 on any drift.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
MIRROR_FILE = REPO_ROOT / "cursor-rules" / "claude-code-sync.mdc"

SKILL_HEADING_RE = re.compile(r"^###\s+`([^`]+)`")
SPEC_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|")
SECTION_HEADING_RE = re.compile(r"^##\s")


def disk_skills() -> tuple[set[str], set[str]]:
    """Return (flat_skills, specializations) from the skills/ directory."""
    flat: set[str] = set()
    spec: set[str] = set()
    if not SKILLS_DIR.is_dir():
        return flat, spec
    for child in SKILLS_DIR.iterdir():
        if not child.is_dir():
            continue
        if child.name == "specializations":
            for s in child.iterdir():
                if s.is_dir() and (s / "SKILL.md").exists():
                    spec.add(s.name)
        else:
            if (child / "SKILL.md").exists():
                flat.add(child.name)
    return flat, spec


def slice_section(lines: list[str], heading_prefix: str) -> list[str]:
    """Return the lines between (and including) the heading that starts with
    `heading_prefix` and the next top-level (## ) heading."""
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start = i
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if SECTION_HEADING_RE.match(lines[j]):
            end = j
            break
    return lines[start:end]


def parse_mirror() -> dict:
    """Parse the cursor mirror; return structure for the lints."""
    if not MIRROR_FILE.exists():
        return {"error": f"mirror file not found: {MIRROR_FILE}"}
    text = MIRROR_FILE.read_text(encoding="utf-8")
    lines = text.splitlines()

    skills_section = slice_section(lines, "## Skills ")
    spec_section = slice_section(lines, "## Specializations ")

    # Flat skills: each `### `name`` subheading + the body between it and the
    # next `### ` (or end of section).
    flat_blocks: dict[str, list[str]] = {}
    current_name: str | None = None
    current_body: list[str] = []
    for line in skills_section[1:]:  # skip the `## Skills ...` line itself
        m = SKILL_HEADING_RE.match(line)
        if m:
            if current_name is not None:
                flat_blocks[current_name] = current_body
            current_name = m.group(1)
            current_body = []
        else:
            if current_name is not None:
                current_body.append(line)
    if current_name is not None:
        flat_blocks[current_name] = current_body

    # Specializations: first-column ``name`` rows in the markdown table.
    spec_names: set[str] = set()
    for line in spec_section[1:]:
        m = SPEC_ROW_RE.match(line)
        if m:
            name = m.group(1)
            # Skip table header row (`Specialization`) and separator-like.
            if name.lower() != "specialization":
                spec_names.add(name)

    return {
        "skills_section_present": bool(skills_section),
        "spec_section_present": bool(spec_section),
        "flat_blocks": flat_blocks,
        "spec_names": spec_names,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true",
                        help="(accepted for interface parity with verify-all; this lint always reads the full mirror)")
    parser.parse_args(argv)

    disk_flat, disk_spec = disk_skills()
    mirror = parse_mirror()
    if "error" in mirror:
        print(f"lint-cursor-mirror: FAIL — {mirror['error']}")
        return 1

    errors: list[str] = []

    if not mirror["skills_section_present"]:
        errors.append(f"mirror is missing the '## Skills ...' section")
    if not mirror["spec_section_present"]:
        errors.append(f"mirror is missing the '## Specializations ...' section")

    mirror_flat = set(mirror["flat_blocks"])
    missing_from_mirror = sorted(disk_flat - mirror_flat)
    orphan_in_mirror = sorted(mirror_flat - disk_flat)
    for name in missing_from_mirror:
        errors.append(f"flat skill on disk not in mirror: skills/{name}/  (add a `### `{name}`` block under '## Skills ...')")
    for name in orphan_in_mirror:
        errors.append(f"mirror references flat skill not on disk: `### `{name}``")

    mirror_spec = mirror["spec_names"]
    missing_spec = sorted(disk_spec - mirror_spec)
    orphan_spec = sorted(mirror_spec - disk_spec)
    for name in missing_spec:
        errors.append(f"specialization on disk not in mirror table: skills/specializations/{name}/")
    for name in orphan_spec:
        errors.append(f"mirror specialization table references missing skill: `{name}`")

    for name, body in mirror["flat_blocks"].items():
        if not any("**TRIGGER:**" in line for line in body):
            errors.append(f"mirror skill block lacks **TRIGGER:** marker: `{name}`")

    if errors:
        print(f"lint-cursor-mirror: FAIL — {len(errors)} drift(s) in {MIRROR_FILE.relative_to(REPO_ROOT)}")
        for e in errors:
            print(f"  {e}")
        return 1

    print(
        f"lint-cursor-mirror: OK — {len(disk_flat)} flat skill(s), "
        f"{len(disk_spec)} specialization(s) match the mirror"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
