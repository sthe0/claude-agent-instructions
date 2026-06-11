#!/usr/bin/env python3
"""PreToolUse hook (Bash): before `git commit` / `arc commit`, surface the
README files that sit next to the changed code but are NOT themselves part of
the changeset — i.e. the docs most likely to have gone stale.

Rule (user request 2026-06-11): verify the currency of *relevant* READMEs
before committing — applies to both global commits (instructions repo,
product code under git) and Arcadia commits (`arc commit`). The hook does the
mechanical part — finding which READMEs are plausibly affected — and leaves the
currency judgement (read it, is it still accurate?) to the agent.

Warn-only by design (house style; same stance as
hook-push-confirmation-reminder.py and
memory-global/leaves/feedback-no-hard-caps-on-memory.md — a false block of a
commit costs more than a false pass). Always exits 0. Any internal failure is
swallowed so a commit is never blocked by this hook.

Detection: a changed *non-README* file is found; the nearest README walking up
from its directory to the repo root exists on disk but is not itself in the
changeset. Those nearest READMEs are listed (deduped, capped). If the changeset
cannot be computed at all, a generic one-line reminder is emitted instead.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

COMMIT_RE = re.compile(r"\b(?:git|arc)\s+commit(?=\s|$)")
# `git commit -a` / `-am` / `--all` also sweep in tracked-but-unstaged edits.
GIT_ALL_RE = re.compile(r"\bcommit\b.*?(?:\s--all\b|\s-[a-z]*a[a-z]*\b)")
README_RE = re.compile(r"^README(\.[A-Za-z0-9]+)?$")
MAX_FILES = 500
MAX_LISTED = 8


def _run(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=4
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _git_changeset(cwd: str, sweep_all: bool) -> tuple[str, list[str]] | None:
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None
    root = root.strip()
    if not root:
        return None
    paths: list[str] = []
    staged = _run(["git", "diff", "--cached", "--name-only"], cwd)
    if staged:
        paths.extend(staged.split("\n"))
    if sweep_all:
        tracked = _run(["git", "diff", "--name-only"], cwd)
        if tracked:
            paths.extend(tracked.split("\n"))
    return root, [p for p in paths if p]


def _arc_changeset(cwd: str) -> tuple[str, list[str]] | None:
    root = _run(["arc", "root"], cwd)
    if not root:
        return None
    root = root.strip()
    raw = _run(["arc", "status", "--json"], cwd)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    changed = (data.get("status") or {}).get("changed") or []
    paths = [
        e.get("path", "")
        for e in changed
        if isinstance(e, dict) and e.get("type") == "file" and e.get("path")
    ]
    return root, paths


def _is_readme(path: str) -> bool:
    return bool(README_RE.match(os.path.basename(path)))


def _nearest_readme(root: str, rel_file: str) -> str | None:
    """Walk up from the file's directory to root, return repo-relative path of
    the first existing README*, else None."""
    d = os.path.dirname(rel_file)
    while True:
        abs_dir = os.path.join(root, d) if d else root
        try:
            for name in os.listdir(abs_dir):
                if README_RE.match(name) and os.path.isfile(
                    os.path.join(abs_dir, name)
                ):
                    return os.path.normpath(os.path.join(d, name)) if d else name
        except OSError:
            pass
        if not d:
            return None
        d = os.path.dirname(d)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    if not isinstance(command, str) or not COMMIT_RE.search(command):
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    sweep_all = bool(GIT_ALL_RE.search(command))

    cs = _git_changeset(cwd, sweep_all) or _arc_changeset(cwd)
    if cs is None:
        print(
            "hook-readme-currency-reminder: about to commit.\n"
            "  rule: verify relevant READMEs are still current before committing.\n"
            "  action: check READMEs next to the changed code; update any that\n"
            "          the change made stale.",
            file=sys.stderr,
        )
        return 0

    root, paths = cs
    paths = paths[:MAX_FILES]
    changed_readmes = {os.path.normpath(p) for p in paths if _is_readme(p)}

    candidates: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if _is_readme(p):
            continue
        readme = _nearest_readme(root, p)
        if not readme or readme in changed_readmes or readme in seen:
            continue
        seen.add(readme)
        candidates.append(readme)

    if not candidates:
        return 0

    listed = candidates[:MAX_LISTED]
    more = len(candidates) - len(listed)
    lines = [
        "hook-readme-currency-reminder: committing code without touching its README(s).",
        "  These READMEs sit next to changed files but are NOT in the changeset —",
        "  read each and confirm it is still accurate (or update it before commit):",
    ]
    lines += [f"    - {r}" for r in listed]
    if more > 0:
        lines.append(f"    … and {more} more")
    print("\n".join(lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
