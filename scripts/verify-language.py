#!/usr/bin/env python3
"""Verify the instruction-language policy.

Rule (skills/self-improvement/policy.md § Instruction language):
- All instruction prose in this repo is English.
- A non-English fragment is allowed only if there is an explicit
  "Language exception" note in an adjacent line / same paragraph.
- Quoted examples ("ok", «push», `тикет`) and fenced code blocks
  do not require an exception.

This script scans .md / .mdc files (staged or all tracked) and prints
violations to stdout. Exit code 1 if any violation is found.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)

# Regions stripped before the Cyrillic check (they are quoted examples or code).
QUOTED_PATTERNS = [
    re.compile(r'"[^"\n]*"'),
    re.compile(r"«[^»\n]*»"),
    re.compile(r"`[^`\n]*`"),
]

# Forms of the exception note recognized in an adjacent line.
EXCEPTION_PATTERNS = [
    re.compile(r"<!--\s*Language exception", re.IGNORECASE),
    re.compile(r"\*\*Language exception:\*\*", re.IGNORECASE),
]

ADJACENT_WINDOW = 3  # lines above and below the offending line


def strip_inline_quotes(line: str) -> str:
    out = line
    for pat in QUOTED_PATTERNS:
        out = pat.sub("", out)
    return out


def blank_out_code_fences(text: str) -> str:
    """Replace lines inside ```...``` fences with empty strings, preserving line numbers."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def has_adjacent_exception(lines: list[str], idx: int, window: int = ADJACENT_WINDOW) -> bool:
    lo = max(0, idx - window)
    hi = min(len(lines), idx + window + 1)
    for j in range(lo, hi):
        for pat in EXCEPTION_PATTERNS:
            if pat.search(lines[j]):
                return True
    return False


def check_file(path: Path) -> list[tuple[int, str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    text = blank_out_code_fences(raw)
    lines = text.splitlines()
    violations: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if not CYRILLIC.search(line):
            continue
        prose = strip_inline_quotes(line)
        if not CYRILLIC.search(prose):
            continue  # all Cyrillic was inside quotes/backticks
        if has_adjacent_exception(lines, i):
            continue
        violations.append((i + 1, line.rstrip()))
    return violations


def list_paths(mode: str, repo_root: Path) -> list[Path]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    completed = subprocess.run(
        cmd, cwd=repo_root, capture_output=True, text=True, check=True
    )
    rels = [line for line in completed.stdout.splitlines() if line]
    return [repo_root / r for r in rels if r.endswith((".md", ".mdc"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Only check files staged for commit (pre-commit mode).",
    )
    args = parser.parse_args(argv)
    mode = "staged" if args.staged else "all"

    repo_root = Path(__file__).resolve().parent.parent
    paths = list_paths(mode, repo_root)

    violations: list[tuple[Path, int, str]] = []
    for p in paths:
        if not p.exists():
            continue
        for lineno, line in check_file(p):
            violations.append((p.relative_to(repo_root), lineno, line))

    if violations:
        files = {v[0] for v in violations}
        print(
            f"verify-language: FAIL — {len(violations)} violation(s) "
            f"in {len(files)} file(s) ({mode} mode)"
        )
        for path, lineno, line in violations:
            print(f"  {path}:{lineno}  {line[:200]}")
        print(
            "\nEach violation needs an adjacent 'Language exception' comment "
            f"(within {ADJACENT_WINDOW} lines) — either"
        )
        print("  <!-- Language exception: <reason> -->")
        print("  > **Language exception:** <reason>")
        return 1

    print(f"verify-language: OK — 0 violations in {len(paths)} file(s) scanned ({mode} mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
