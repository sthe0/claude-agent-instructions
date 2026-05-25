#!/usr/bin/env python3
"""Audit memory leaves: orphans, broken index entries, stale leaves, frontmatter sanity.

Default target: global memory at ~/claude-agent-instructions/memory-global/.
Use --project DIR to audit a project's <DIR>/.claude/agent-memory/ tree
instead.

Informational only — exits 0 even when problems are found. The output is
candidates for human review, not violations to gate commits on. Memory
quality (is this lesson worth keeping?) is judgment, not code.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FRONTMATTER = ("name", "description", "type")
VALID_TYPES = ("user", "feedback", "project", "reference")

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str] | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    block = m.group(1)
    out: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


def list_leaf_files(memory_dir: Path) -> list[Path]:
    leaves_dir = memory_dir / "leaves"
    if not leaves_dir.is_dir():
        return []
    return sorted(p for p in leaves_dir.rglob("*.md"))


def indexed_targets(memory_dir: Path) -> set[Path]:
    """Return the set of leaf files referenced from MEMORY.md (resolved absolute)."""
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        return set()
    text = index.read_text(encoding="utf-8")
    out: set[Path] = set()
    for m in LINK_RE.finditer(text):
        target = m.group(1).split("#", 1)[0]
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        out.add((index.parent / target).resolve())
    return out


def audit_memory(memory_dir: Path, stale_days: int, label: str,
                 do_orphans: bool, do_frontmatter: bool, do_stale: bool, do_broken: bool) -> None:
    if not memory_dir.is_dir():
        print(f"memory-audit: skip {label} — directory not found: {memory_dir}")
        return

    leaves = list_leaf_files(memory_dir)
    indexed = indexed_targets(memory_dir)
    print(f"memory-audit: {label}  ({memory_dir})")
    print(f"  leaves on disk: {len(leaves)}   indexed entries: {len(indexed)}")

    if do_broken:
        broken = sorted(p for p in indexed if not p.exists())
        if broken:
            print(f"  BROKEN index entries (MEMORY.md → missing file): {len(broken)}")
            for p in broken:
                try:
                    rel = p.relative_to(memory_dir)
                except ValueError:
                    rel = p
                print(f"    {rel}")

    if do_orphans:
        leaf_resolved = {p.resolve() for p in leaves}
        orphans = sorted(leaf_resolved - indexed)
        if orphans:
            print(f"  ORPHAN leaves (on disk, not in MEMORY.md): {len(orphans)}")
            for p in orphans:
                rel = p.relative_to(memory_dir)
                print(f"    {rel}")

    if do_frontmatter:
        bad: list[tuple[Path, list[str]]] = []
        for p in leaves:
            text = p.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            problems: list[str] = []
            if fm is None:
                problems.append("no frontmatter")
            else:
                for field in REQUIRED_FRONTMATTER:
                    if field not in fm or not fm[field]:
                        problems.append(f"missing '{field}'")
                t = fm.get("type")
                if t and t not in VALID_TYPES:
                    problems.append(f"unknown type {t!r}")
            if problems:
                bad.append((p, problems))
        if bad:
            print(f"  FRONTMATTER issues: {len(bad)}")
            for p, problems in bad:
                rel = p.relative_to(memory_dir)
                print(f"    {rel}  ({', '.join(problems)})")

    if do_stale:
        now = dt.datetime.now()
        threshold = now - dt.timedelta(days=stale_days)
        stale: list[tuple[Path, dt.datetime]] = []
        ages: list[int] = []
        for p in leaves:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
            age_days = (now - mtime).days
            ages.append(age_days)
            if mtime < threshold:
                stale.append((p, mtime))
        if ages:
            ages.sort()
            median = ages[len(ages) // 2]
            print(f"  age (days):  median={median}  max={max(ages)}")
        if stale:
            print(f"  STALE leaves (mtime > {stale_days}d): {len(stale)}")
            for p, mtime in sorted(stale, key=lambda x: x[1]):
                rel = p.relative_to(memory_dir)
                fm = parse_frontmatter(p.read_text(encoding="utf-8")) or {}
                age = (now - mtime).days
                print(f"    {age:>4}d  {rel}  (type={fm.get('type','?')})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", type=Path, help="audit <DIR>/.claude/agent-memory/ instead of global memory")
    p.add_argument("--stale-days", type=int, default=180, help="staleness threshold in days (default: 180)")
    p.add_argument("--orphans-only", action="store_true")
    p.add_argument("--frontmatter-only", action="store_true")
    p.add_argument("--stale-only", action="store_true")
    args = p.parse_args(argv)

    do_orphans = do_frontmatter = do_stale = do_broken = True
    if args.orphans_only:
        do_frontmatter = do_stale = False
    if args.frontmatter_only:
        do_orphans = do_stale = False
    if args.stale_only:
        do_orphans = do_frontmatter = False
    # `do_broken` always runs unless explicitly suppressed (only frontmatter / stale).
    if args.frontmatter_only or args.stale_only:
        do_broken = False

    if args.project:
        memory_dir = args.project / ".claude" / "agent-memory"
        label = f"project memory ({args.project.name})"
    else:
        memory_dir = REPO_ROOT / "memory-global"
        label = "global memory"

    audit_memory(memory_dir, args.stale_days, label, do_orphans, do_frontmatter, do_stale, do_broken)
    return 0


if __name__ == "__main__":
    sys.exit(main())
