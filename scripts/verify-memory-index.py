#!/usr/bin/env python3
"""Verify the memory-global index ↔ leaves correspondence and leaf frontmatter.

Two mechanical invariants that prose alone cannot keep true (see CLAUDE.md
§ Memory — "add a one-line pointer here"):

  1. Every leaf file under memory-global/leaves/ (recursively, excluding the
     MEMORY.md index files themselves) is referenced from at least one index:
     the main memory-global/MEMORY.md or a sub-index MEMORY.md. The reverse
     direction (index entry resolves to a file) is already covered by
     verify-cross-refs.py.

  2. Every leaf carries a top-level `type:` frontmatter key (one of
     user / feedback / project / reference) — not buried inside a nested
     `metadata:` block, which any tool reading `type` directly would miss.

Exit code 1 if any invariant is violated. --root for project-repo reuse;
--staged is accepted but ignored (whole-tree check).
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

import memory_dates as md

LEAVES_REL = "memory-global/leaves"
ALLOWED_TYPES = {"user", "feedback", "project", "reference"}

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_TOP_TYPE_RE = re.compile(r"^type:\s*(\S+)", re.MULTILINE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(s: str) -> datetime.date | None:
    """Return a date for a YYYY-MM-DD string, or None if invalid/missing."""
    if not s or not _ISO_DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def _fm_field(fm_body: str, key: str) -> str:
    m = re.search(rf"^{key}\s*:\s*(.*?)\s*$", fm_body, re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else ""


def _date_violations(rel: str, fm_body: str) -> list[str]:
    """Return a list of temporal-date violations for a leaf frontmatter block."""
    issues: list[str] = []
    created_s = _fm_field(fm_body, "created")
    last_verified_s = _fm_field(fm_body, "last_verified")
    last_accessed_s = _fm_field(fm_body, "last_accessed")

    if not created_s:
        issues.append(f"  dates: {rel} — missing required `created:` (ISO YYYY-MM-DD)")
    else:
        created = _parse_date(created_s)
        if created is None:
            issues.append(
                f"  dates: {rel} — `created: {created_s}` is not a valid ISO date (YYYY-MM-DD)"
            )

    if not last_verified_s:
        issues.append(f"  dates: {rel} — missing required `last_verified:` (ISO YYYY-MM-DD)")
    else:
        last_verified = _parse_date(last_verified_s)
        if last_verified is None:
            issues.append(
                f"  dates: {rel} — `last_verified: {last_verified_s}` is not a valid ISO date (YYYY-MM-DD)"
            )
        elif created_s:
            created = _parse_date(created_s)
            if created is not None and last_verified < created:
                issues.append(
                    f"  dates: {rel} — `last_verified` ({last_verified_s}) is before `created` ({created_s})"
                )

    if last_accessed_s and _parse_date(last_accessed_s) is None:
        issues.append(
            f"  dates: {rel} — `last_accessed: {last_accessed_s}` is not a valid ISO date (YYYY-MM-DD)"
        )

    return issues


def _leaf_files(leaves: Path) -> list[Path]:
    return [p for p in leaves.rglob("*.md") if p.name != "MEMORY.md"]


def _index_files(leaves: Path, root: Path) -> list[Path]:
    files = [root / "memory-global" / "MEMORY.md"]
    files += [p for p in leaves.rglob("MEMORY.md")]
    return [f for f in files if f.exists()]


def _referenced_leaves(index_files: list[Path]) -> set[Path]:
    """All leaf paths linked from any index, resolved relative to each index's dir."""
    refs: set[Path] = set()
    for idx in index_files:
        text = idx.read_text(encoding="utf-8")
        for m in _LINK_RE.finditer(text):
            target = m.group(1).split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "/", "~", "$")):
                continue
            resolved = (idx.parent / target).resolve()
            if resolved.suffix == ".md" and resolved.name != "MEMORY.md":
                refs.add(resolved)
    return refs


def _frontmatter_body(path: Path) -> str | None:
    """Return the raw YAML frontmatter body, or None if there is no block."""
    m = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _frontmatter_type(fm: str | None) -> str | None:
    """Return the top-level `type:` value, or None if absent at top level."""
    if fm is None:
        return None
    # Only top-level keys: lines that start at column 0 (no leading indent).
    top_level = "\n".join(l for l in fm.splitlines() if l[:1] not in (" ", "\t"))
    tm = _TOP_TYPE_RE.search(top_level)
    return tm.group(1) if tm else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--staged", action="store_true", help="Accepted but ignored")
    parser.add_argument(
        "--require-dates", action="store_true", default=False,
        help="Enforce created+last_verified temporal frontmatter on every leaf. "
             "Off by default until all existing leaves are backfilled (stage 4).",
    )
    args = parser.parse_args(argv)

    root = args.root if args.root is not None else Path(__file__).resolve().parent.parent
    leaves = root / LEAVES_REL
    if not leaves.is_dir():
        print(f"verify-memory-index: OK — no {LEAVES_REL}/ tree")
        return 0

    failures: list[str] = []
    leaf_files = _leaf_files(leaves)
    referenced = _referenced_leaves(_index_files(leaves, root))

    for leaf in sorted(leaf_files):
        rel = leaf.relative_to(root)
        if leaf.resolve() not in referenced:
            failures.append(f"  unindexed: {rel} — add a pointer in MEMORY.md (main or a sub-index)")
        fm = _frontmatter_body(leaf)
        t = _frontmatter_type(fm)
        if t is None:
            failures.append(f"  frontmatter: {rel} — missing top-level `type:` key (nested metadata.type is not read)")
        elif t not in ALLOWED_TYPES:
            failures.append(f"  frontmatter: {rel} — type '{t}' not in {sorted(ALLOWED_TYPES)}")
        # Temporal frontmatter: created + last_verified required & well-formed;
        # last_accessed optional but format-checked (memory-temporal-frontmatter.md).
        for issue in md.validate_temporal(fm or "", require=True):
            failures.append(f"  frontmatter: {rel} — {issue}")

    if failures:
        print("verify-memory-index: FAIL")
        for f in failures:
            print(f)
        return 1
    print(f"verify-memory-index: OK — {len(leaf_files)} leaf/leaves indexed, frontmatter type valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
