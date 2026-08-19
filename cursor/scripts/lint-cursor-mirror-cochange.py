#!/usr/bin/env python3
"""Require cursor mirror co-change when CLAUDE.md cursor-relevant prose changes.

Pre-commit (--staged): if the staged diff touches CLAUDE.md and matches any
cursor-relevant pattern, the same commit must also stage either
cursor/rules/claude-code-sync.mdc or docs/components/cursor-mirror.md, unless
the commit message contains CURSOR_MIRROR_NA=1 (documented escape for edits
that genuinely do not affect Cursor behaviour).

Without --staged: no-op (exit 0). Used from verify-all in default mode.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE = REPO_ROOT / "CLAUDE.md"
MIRROR = REPO_ROOT / "cursor/rules/claude-code-sync.mdc"
MIRROR_DOC = REPO_ROOT / "docs/components/cursor-mirror.md"

CURSOR_RELEVANT = re.compile(
    r"runtime_host|Cursor|AskUserQuestion|Spawning specialists|never invoke `claude`|"
    r"Difficulty escape|agentctl dispatch|spawn-cursor|claude-code-sync|"
    r"Specializations in Cursor|Hard gate",
    re.IGNORECASE,
)

STAGED_PATHS = ("CLAUDE.md", "cursor/rules/claude-code-sync.mdc", "docs/components/cursor-mirror.md")


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(REPO_ROOT), *args], text=True)


def staged_diff() -> str:
    return git("diff", "--cached", "--", *STAGED_PATHS)


def staged_paths() -> set[str]:
    out = git("diff", "--cached", "--name-only", "--", *STAGED_PATHS)
    return {line.strip() for line in out.splitlines() if line.strip()}


def commit_message() -> str:
    try:
        return git("log", "-1", "--format=%B")
    except subprocess.CalledProcessError:
        return os.environ.get("CURSOR_MIRROR_NA", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)

    if not args.staged:
        print("lint-cursor-mirror-cochange: OK — skipped (not --staged)")
        return 0

    paths = staged_paths()
    if "CLAUDE.md" not in paths:
        print("lint-cursor-mirror-cochange: OK — CLAUDE.md not staged")
        return 0

    diff = staged_diff()
    if not CURSOR_RELEVANT.search(diff):
        print("lint-cursor-mirror-cochange: OK — no cursor-relevant CLAUDE.md delta")
        return 0

    if "CURSOR_MIRROR_NA=1" in commit_message() or os.environ.get("CURSOR_MIRROR_NA") == "1":
        print("lint-cursor-mirror-cochange: OK — CURSOR_MIRROR_NA=1")
        return 0

    if "cursor/rules/claude-code-sync.mdc" in paths or "docs/components/cursor-mirror.md" in paths:
        print("lint-cursor-mirror-cochange: OK — mirror co-change staged")
        return 0

    print(
        "lint-cursor-mirror-cochange: FAIL — staged CLAUDE.md changes match cursor-relevant "
        "patterns but neither cursor/rules/claude-code-sync.mdc nor docs/components/cursor-mirror.md "
        "is staged; update the mirror in the same commit or set CURSOR_MIRROR_NA=1 with justification"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
