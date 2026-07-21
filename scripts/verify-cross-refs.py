#!/usr/bin/env python3
"""Verify intra-repo cross-references in Markdown files (Tier A).

Two kinds of references are checked:

  1. Markdown links `[text](path)` and image links `![alt](path)`. The target
     is resolved relative to the file's own directory. External URLs
     (http://, https://, mailto:, tel:, ftp://) and pure intra-page anchors
     (`#section`) are skipped. For `path#anchor` only `path` is checked —
     anchor validation is Tier B (not done here).

  2. Inline-code paths inside backticks whose first segment is one of the
     known repo top-level directories (whitelist below). Example: a prose
     reference to `` `scripts/permissions-cli.py` `` is checked against the
     repo root.

The script ignores content inside fenced code blocks (``` ... ```) since the
samples in code blocks may be illustrative rather than real references.

Exit code 1 if any broken reference is found.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TOP_DIRS = (
    "agents",
    "agents-local",
    "config.md",
    "cursor-rules",
    "docs",
    "githooks",
    "mcp-local",
    "memory-global",
    "memory-meta",
    "permissions",
    "scripts",
    "skills",
    "skills-local",
    "tests",
)

# Markdown link / image: [text](target) or ![alt](target).
# We do not match link-reference style ([text][label]) — not used in this repo.
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# Inline-code references that look like repo-relative paths:
# `<top-dir>/<rest>` where <top-dir> is one of TOP_DIRS and <rest> is non-empty.
# Excludes whitespace / backticks inside.
_TOP_DIRS_ALT = "|".join(re.escape(d) for d in TOP_DIRS)
INLINE_PATH_RE = re.compile(rf"`((?:{_TOP_DIRS_ALT})/[^`\s]+)`")

URL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "ftp://", "//", "data:")

# Inline-code refs with these characters are glob patterns or placeholders
# (`agents/*.md`, `skills/<name>/`, `cursor/rules/*.mdc`),
# not literal paths — skip.
PATTERN_CHARS = set("*?<>[]{}")

# Files / directories that may legitimately reference paths that no longer
# exist (migration / changelog docs describe past state).
#
# benchmark-profile*/ are byte-exact frozen snapshots pinned by MANIFEST.sha256;
# their intra-tree links point outside the partial snapshot and editing any file
# to "fix" a ref would break the hash manifest, so they are out of cross-ref scope.
SKIP_PATH_PREFIXES = (
    "docs/migrations/",
    "benchmark-profile/",
    "benchmark-profile-spawn/",
)


def blank_out_code_fences(text: str) -> str:
    """Replace lines inside ```...``` fences with empty lines (preserve numbering)."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def strip_anchor(target: str) -> str:
    return target.split("#", 1)[0]


def is_external(target: str) -> bool:
    return target.startswith(URL_PREFIXES)


def is_pure_anchor(target: str) -> bool:
    return target.startswith("#")


def is_absolute_path_reference(target: str) -> bool:
    """References starting with `~/`, `/`, `$VAR`, `<placeholder>` are runtime / template
    references and are not checked here."""
    if target.startswith(("~/", "/", "$")):
        return True
    if target.startswith("<") and ">" in target:
        return True
    return False


def check_markdown_links(file_path: Path, lines: list[str]) -> list[tuple[int, str]]:
    broken: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        for match in LINK_RE.finditer(line):
            target = match.group(1).strip()
            if not target:
                continue
            if is_external(target) or is_pure_anchor(target):
                continue
            if is_absolute_path_reference(target):
                continue
            path_only = strip_anchor(target)
            if not path_only:
                continue
            resolved = (file_path.parent / path_only).resolve()
            if not resolved.exists():
                broken.append((i, f"[…]({target}) -> {path_only}"))
    return broken


def check_inline_paths(file_path: Path, lines: list[str]) -> list[tuple[int, str]]:
    broken: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        for match in INLINE_PATH_RE.finditer(line):
            ref = match.group(1)
            if any(c in PATTERN_CHARS for c in ref):
                continue  # glob pattern or placeholder, not a literal path
            path_only = strip_anchor(ref)
            # Inline-code refs are repo-root-relative by convention.
            resolved = (REPO_ROOT / path_only).resolve()
            if not resolved.exists():
                broken.append((i, f"`{ref}`"))
    return broken


def check_file(path: Path) -> list[tuple[int, str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    text = blank_out_code_fences(raw)
    lines = text.splitlines()
    return check_markdown_links(path, lines) + check_inline_paths(path, lines)


def list_paths(mode: str) -> list[Path]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    rels = [line for line in out.stdout.splitlines() if line]
    rels = [r for r in rels if r.endswith((".md", ".mdc"))]
    rels = [r for r in rels if not any(r.startswith(p) for p in SKIP_PATH_PREFIXES)]
    return [REPO_ROOT / r for r in rels]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)
    mode = "staged" if args.staged else "all"

    paths = list_paths(mode)
    all_broken: list[tuple[Path, int, str]] = []
    for p in paths:
        if not p.exists():
            continue
        for lineno, ref in check_file(p):
            all_broken.append((p.relative_to(REPO_ROOT), lineno, ref))

    if all_broken:
        files = {b[0] for b in all_broken}
        print(
            f"verify-cross-refs: FAIL — {len(all_broken)} broken reference(s) "
            f"in {len(files)} file(s) ({mode} mode)"
        )
        for path, lineno, ref in all_broken:
            print(f"  {path}:{lineno}  {ref}")
        print(
            "\nEach broken reference points at a path that does not exist. "
            "Fix the path, remove the reference, or — if the inline-code form "
            "is a false positive — adjust the inline-code phrasing so it does "
            "not start with a known top-level directory."
        )
        return 1
    print(f"verify-cross-refs: OK — 0 broken references in {len(paths)} file(s) scanned ({mode} mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
