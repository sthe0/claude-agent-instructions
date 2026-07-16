#!/usr/bin/env python3
"""Verify onboarding docs never teach bare `claude` as the first-task launch.

Isolated-root invariant: bare `claude` is the user's untouched personal
install; the system entry point is `claude-agent` / `claude-task` /
`claude-<profile>`. A prior migration redefined this split but left one
onboarding surface teaching the old bare-`claude` launch, undetected because
nothing mechanized the check.

Scope (rule vs perception — CLAUDE.md "Separate rule from perception"):
this script covers ONLY fenced code-block command lines in the onboarding
docs listed in TARGETS. A line is flagged iff, after stripping a trailing
shell comment and a leading `$ ` prompt, `claude` is the terminal token of
a command (standalone, or after `&&` / `;` / `|`) — e.g. `cd ~/x && claude`
or a lone `claude`. Non-interactive/auxiliary forms are NOT flagged:
`claude-agent`, `claude-task`, `claude-<profile>` (hyphen breaks the match),
`claude auth login`, `claude --version`, `claude -p ...` (claude is not the
terminal token). Inline PROSE mentions of `claude` (e.g. contrasting "bare
`claude` = your personal install") are deliberately OUT of scope — telling
a legitimate prose contrast from a regression is a perception task, not a
deterministic one, and is left to review.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TARGETS = ("README.md", "docs/operations/setup.md")

_COMMENT_RE = re.compile(r"\s*#.*$")
_PROMPT_RE = re.compile(r"^\s*\$\s+")
_BARE_CLAUDE_RE = re.compile(r"(?:^|&&|;|\|)\s*claude\s*$")


def _fenced_blocks(text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) pairs for every line inside a ``` fence."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append((lineno, line))
    return out


def check_file(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits: list[tuple[int, str]] = []
    for lineno, raw_line in _fenced_blocks(text):
        line = _PROMPT_RE.sub("", _COMMENT_RE.sub("", raw_line)).rstrip()
        if _BARE_CLAUDE_RE.search(line):
            hits.append((lineno, raw_line.strip()))
    return hits


def list_targets(root: Path, staged: bool) -> list[Path]:
    paths = [root / t for t in TARGETS]
    if not staged:
        return paths
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    staged_rels = set(out.stdout.splitlines())
    return [p for t, p in zip(TARGETS, paths) if t in staged_rels]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    targets = list_targets(root, args.staged)

    all_hits: list[tuple[Path, int, str]] = []
    scanned = 0
    for path in targets:
        if not path.exists():
            continue
        scanned += 1
        for lineno, line in check_file(path):
            all_hits.append((path.relative_to(root), lineno, line))

    if all_hits:
        print(
            f"verify-onboarding-entrypoint: FAIL — {len(all_hits)} bare-claude "
            f"launch line(s) in onboarding docs (use claude-agent/claude-task)"
        )
        for rel_path, lineno, line in all_hits:
            print(f"  {rel_path}:{lineno}: {line}")
        return 1

    print(
        f"verify-onboarding-entrypoint: OK — {scanned} onboarding doc(s) "
        f"scanned, no bare-claude launch command"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
