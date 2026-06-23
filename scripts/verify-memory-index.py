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
import re
import sys
from pathlib import Path

LEAVES_REL = "memory-global/leaves"
ALLOWED_TYPES = {"user", "feedback", "project", "reference"}

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_TOP_TYPE_RE = re.compile(r"^type:\s*(\S+)", re.MULTILINE)


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


def _frontmatter_type(path: Path) -> str | None:
    """Return the top-level `type:` value, or None if absent at top level."""
    m = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        return None
    fm = m.group(1)
    # Only top-level keys: lines that start at column 0 (no leading indent).
    top_level = "\n".join(l for l in fm.splitlines() if l[:1] not in (" ", "\t"))
    tm = _TOP_TYPE_RE.search(top_level)
    return tm.group(1) if tm else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--staged", action="store_true", help="Accepted but ignored")
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
        t = _frontmatter_type(leaf)
        if t is None:
            failures.append(f"  frontmatter: {rel} — missing top-level `type:` key (nested metadata.type is not read)")
        elif t not in ALLOWED_TYPES:
            failures.append(f"  frontmatter: {rel} — type '{t}' not in {sorted(ALLOWED_TYPES)}")

    if failures:
        print("verify-memory-index: FAIL")
        for f in failures:
            print(f)
        return 1
    print(f"verify-memory-index: OK — {len(leaf_files)} leaf/leaves indexed, frontmatter type valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
