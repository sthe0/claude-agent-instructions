#!/usr/bin/env python3
"""Verify README inventory sentinels (scripts/flat skills/specializations) match the filesystem.

Reads three sentinel regions delimited by HTML comments in README.md and compares
each against the filesystem. Use --fix to reconcile rows in place, preserving
existing purpose cells. Use --root for project-repo reuse; --staged is accepted
but ignored (README check is always whole-repo).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REGIONS = ("scripts", "skills", "specializations")

# Source file (repo-relative) each sentinel region lives in. The scripts inventory
# is heavy and operational, so it lives in scripts/README.md, not the conceptual root README.
REGION_FILES = {
    "scripts": "scripts/README.md",
    "skills": "docs/components/skills.md",
    "specializations": "docs/components/skills.md",
}

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_TICK_RE = re.compile(r"`([^`]+)`")

_DEFAULT_HEADERS = {
    "scripts": "| Script | Purpose |",
    "skills": "| name | Triggers (summary) | File |",
    "specializations": "| name | Spawns when a plan step calls for | File |",
}
_DEFAULT_SEPARATORS = {
    "scripts": "|---|---|",
    "skills": "|---|---|---|",
    "specializations": "|---|---|---|",
}


def _begin(name: str) -> str:
    return f"<!-- inventory:{name}:begin -->"


def _end(name: str) -> str:
    return f"<!-- inventory:{name}:end -->"


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return False
    segs = stripped[1:-1].split("|")
    return (
        all(all(c in "-: \t" for c in seg) for seg in segs)
        and any("-" in seg for seg in segs)
    )


def _parse_region(text: str, name: str) -> tuple[int, int, dict[str, str]] | None:
    """Return (begin_idx, end_idx, existing_rows) or None if markers are missing.

    begin_idx / end_idx: indices into text.splitlines() for the marker lines.
    existing_rows: ordered dict identifier -> full row text (no trailing newline).
    """
    bm, em = _begin(name), _end(name)
    lines = text.splitlines()
    bi = ei = None
    for i, line in enumerate(lines):
        ls = line.strip()
        if ls == bm:
            bi = i
        elif ls == em:
            ei = i
    if bi is None or ei is None or bi >= ei:
        return None

    existing: dict[str, str] = {}
    for line in lines[bi + 1 : ei]:
        if not line.startswith("|"):
            continue
        if _is_separator(line):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 3:
            continue
        col1 = cols[1]
        if name == "scripts":
            m = _LINK_RE.search(col1)
            if m:
                existing[m.group(2)] = line
        else:
            m = _TICK_RE.search(col1)
            if m:
                existing[m.group(1)] = line
    return bi, ei, existing


def _fs_set(name: str, root: Path) -> set[str]:
    if name == "scripts":
        # Identifiers are link targets relative to the region file's own directory,
        # so the markdown links resolve correctly (verify-cross-refs resolves them
        # relative to the file) — e.g. `setup-symlinks.sh`, `../cursor/scripts/x.py`.
        base = (root / REGION_FILES[name]).parent
        ids: set[str] = set()
        for pat in ("scripts/*.py", "scripts/*.sh", "cursor/scripts/*.py", "cursor/scripts/*.sh"):
            for p in root.glob(pat):
                ids.add(os.path.relpath(p, base))
        return ids
    elif name == "skills":
        d = root / "skills"
        if not d.is_dir():
            return set()
        return {x.name for x in d.iterdir() if x.is_dir() and x.name != "specializations"}
    else:
        d = root / "skills" / "specializations"
        if not d.is_dir():
            return set()
        return {x.name for x in d.iterdir() if x.is_dir()}


def _synthesize_row(ident: str, name: str) -> str:
    if name == "scripts":
        base = Path(ident).name
        return f"| [{base}]({ident}) | TODO |"
    # skills / specializations: the File link must resolve relative to the
    # region file's own directory (verify-cross-refs resolves it there), so
    # prefix the repo-root-relative target with one `../` per directory level
    # of REGION_FILES[name]. Derived, not a literal, so it stays correct if the
    # region file moves again.
    region_dir = os.path.dirname(REGION_FILES[name])
    prefix = "../" * len(Path(region_dir).parts)
    subdir = "skills" if name == "skills" else "skills/specializations"
    path = f"{prefix}{subdir}/{ident}/SKILL.md"
    return f"| `{ident}` | TODO | [{path}]({path}) |"


def _fix_region(
    text: str,
    name: str,
    bi: int,
    ei: int,
    existing: dict[str, str],
    fs_ids: set[str],
) -> str:
    """Return updated text with the sentinel region rebuilt from fs_ids, preserving existing row text."""
    lines_ke = text.splitlines(keepends=True)
    content = [l.rstrip("\r\n") for l in lines_ke[bi + 1 : ei]]

    header = next((l for l in content if l.startswith("|") and not _is_separator(l)), None)
    separator = next((l for l in content if _is_separator(l)), None)
    if header is None:
        header = _DEFAULT_HEADERS[name]
    if separator is None:
        separator = _DEFAULT_SEPARATORS[name]

    new_rows = [header, separator]
    for ident in sorted(fs_ids):
        new_rows.append(existing.get(ident, _synthesize_row(ident, name)))

    new_middle = [row + "\n" for row in new_rows]
    return "".join(lines_ke[: bi + 1] + new_middle + lines_ke[ei:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fix", action="store_true", help="Reconcile README rows from filesystem")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Accepted but ignored; README check is always whole-repo",
    )
    args = parser.parse_args(argv)

    root = args.root if args.root is not None else Path(__file__).resolve().parent.parent

    if args.fix:
        by_file: dict[str, list[str]] = {}
        for name in REGIONS:
            by_file.setdefault(REGION_FILES[name], []).append(name)
        for relpath, names in by_file.items():
            f = root / relpath
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8")
            for name in names:
                parsed = _parse_region(text, name)
                if parsed is None:
                    continue
                bi, ei, existing = parsed
                text = _fix_region(text, name, bi, ei, existing, _fs_set(name, root))
            f.write_text(text, encoding="utf-8")
        return main(["--root", str(root)])

    all_ok = True
    failures: list[str] = []
    counts: dict[str, int] = {}

    for name in REGIONS:
        relpath = REGION_FILES[name]
        f = root / relpath
        if not f.exists():
            failures.append(f"  {name}: source file not found: {relpath}")
            all_ok = False
            counts[name] = 0
            continue
        text = f.read_text(encoding="utf-8")
        parsed = _parse_region(text, name)
        if parsed is None:
            failures.append(f"  {name}: marker pair not found in {relpath}")
            all_ok = False
            counts[name] = 0
            continue
        _, _, existing = parsed
        fs_ids = _fs_set(name, root)
        for m in sorted(fs_ids - set(existing)):
            failures.append(f"  {name}: missing from {relpath}: {m}")
            all_ok = False
        for d in sorted(set(existing) - fs_ids):
            failures.append(f"  {name}: dangling in {relpath} (not on FS): {d}")
            all_ok = False
        counts[name] = len(fs_ids)

    if all_ok:
        n = counts.get("scripts", 0)
        s = counts.get("skills", 0)
        p = counts.get("specializations", 0)
        print(f"verify-readme: OK — {n} scripts, {s} flat skills, {p} specializations")
        return 0

    print("verify-readme: FAIL")
    for f in failures:
        print(f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
