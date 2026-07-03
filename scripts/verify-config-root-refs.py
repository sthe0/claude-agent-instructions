#!/usr/bin/env python3
"""Doc-surface enumerator for legacy ``~/.claude`` / ``$HOME/.claude`` references.

Difficulty (functional ground):
  The config-root migration (``~/.claude`` -> ``~/.claude-agent``) shipped a
  code-side enumerator (``scripts/tests/test_config_root.py::_root_offenders``)
  that governs every ``*.py``/``*.sh`` install-target hardcode. It has no
  opinion on prose — CLAUDE.md, skills, memory leaves, settings, docs — where
  most of the remaining legacy references live. Without a doc-side mechanism,
  "every reference converted or explicitly kept" can only be checked by
  re-reading whatever files someone remembers to look at, which silently
  regresses on the next prose edit. This script enumerates the COMPLEMENT of
  the code enumerator's scope (every tracked-shape file that is not
  ``*.py``/``*.sh``) and fails on any legacy reference not named in
  ``config-root-refs-allowlist.txt``.

Scope and matching:
  - Doc scope = every file under the repo root except ``.git/`` and anything
    ending in ``.py`` or ``.sh`` (those are code scope, S2-governed). A
    FUTURE extension (``.toml``, extensionless, whatever comes next) falls
    into doc scope automatically — no edit needed here when a new prose
    format shows up.
  - A reference is ``~/.claude`` or ``$HOME/.claude`` NOT followed by ``-``,
    so the CORRECT new path ``~/.claude-agent`` never matches — a bare
    substring match would inflate the domain with the very path this
    migration produced.
  - The allowlist is PER-LINE: an entry names either an exact ``path:line``
    (covers every occurrence on that line) or an exceptional glob (matched
    against the repo-relative path); a ``# reason`` is mandatory on every
    entry. An entry that no longer matches any current occurrence is STALE
    and fails the check too (mirrors the code enumerator's currency test),
    so a fixed reference doesn't silently leave a dead allowlist row behind.

Self-reference guard (R4): the allowlist file itself, and the sweep's
worklist artifact (which enumerates occurrences by construction — its rows
and reasons routinely quote the very pattern being searched for), are
excluded from the scanned domain outright rather than allowlisted line by
line — otherwise the allowlist would need to cover itself circularly.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "config-root-refs-allowlist.txt"

# Code-scope suffixes governed by the sibling S2 enumerator
# (scripts/tests/test_config_root.py::_root_offenders). Doc scope is the
# COMPLEMENT — everything else — so it never needs editing for a new prose
# file type.
CODE_SUFFIXES = {".py", ".sh"}

# Generated artifacts that record occurrences BY CONSTRUCTION. Scanning them
# would make the allowlist need to cover its own reason text circularly.
SELF_REF_EXCLUDED = {
    "scripts/config-root-refs-allowlist.txt",
    "docs/migrations/config-root-tails-worklist.tsv",
}

TILDE_RE = re.compile(r"~/\.claude(?!-)")
HOME_RE = re.compile(r"\$HOME/\.claude(?!-)")


class AllowlistError(ValueError):
    """Raised on a malformed scripts/config-root-refs-allowlist.txt entry."""


def iter_doc_files(repo_root: Path) -> "list[Path]":
    """Repo-relative paths in doc scope: complement of the code enumerator."""
    files = []
    for p in sorted(repo_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(repo_root)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        if p.suffix.lower() in CODE_SUFFIXES:
            continue
        if rel.as_posix() in SELF_REF_EXCLUDED:
            continue
        files.append(rel)
    return files


def find_occurrences(repo_root: Path) -> "list[tuple[str, int, str]]":
    """(relpath, 1-based line number, line text) for every legacy ref in doc scope."""
    occurrences = []
    for rel in iter_doc_files(repo_root):
        try:
            text = (repo_root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary/unreadable — not prose
        for lineno, line in enumerate(text.splitlines(), start=1):
            if TILDE_RE.search(line) or HOME_RE.search(line):
                occurrences.append((rel.as_posix(), lineno, line))
    return occurrences


def parse_allowlist(path: Path) -> "list[dict]":
    """Parse the per-line allowlist grammar: ``spec  # reason`` per entry.

    ``spec`` is ``path:line`` (covers every occurrence on that line) or an
    exceptional glob matched against the repo-relative path. A blank line or
    a line starting with ``#`` is a comment. A reason is mandatory on every
    entry line — raises AllowlistError otherwise.
    """
    entries: "list[dict]" = []
    if not path.exists():
        return entries
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" not in line:
            raise AllowlistError(
                f"{path}:{lineno}: entry missing mandatory '# reason': {raw!r}"
            )
        spec, _, reason = line.partition("#")
        spec = spec.strip()
        reason = reason.strip()
        if not spec:
            raise AllowlistError(f"{path}:{lineno}: empty entry before '#'")
        if not reason:
            raise AllowlistError(f"{path}:{lineno}: empty reason for entry {spec!r}")
        match = re.match(r"^(?P<file>.+):(?P<line>\d+)$", spec)
        if match:
            entries.append({
                "kind": "line",
                "path": match.group("file"),
                "line": int(match.group("line")),
                "reason": reason,
                "raw": raw,
                "lineno": lineno,
            })
        else:
            entries.append({
                "kind": "glob",
                "pattern": spec,
                "reason": reason,
                "raw": raw,
                "lineno": lineno,
            })
    return entries


def _covers(entry: dict, rel: str, lineno: int) -> bool:
    if entry["kind"] == "line":
        return entry["path"] == rel and entry["line"] == lineno
    return fnmatch.fnmatch(rel, entry["pattern"])


def find_unallowed(occurrences, entries) -> "list[tuple[str, int, str]]":
    return [
        (rel, lineno, line)
        for rel, lineno, line in occurrences
        if not any(_covers(e, rel, lineno) for e in entries)
    ]


def find_stale_entries(entries, occurrences) -> "list[dict]":
    """Allowlist entries that no longer match any current occurrence."""
    lines_by_file: "dict[str, set[int]]" = {}
    for rel, lineno, _ in occurrences:
        lines_by_file.setdefault(rel, set()).add(lineno)
    stale = []
    for e in entries:
        if e["kind"] == "line":
            if e["line"] not in lines_by_file.get(e["path"], set()):
                stale.append(e)
        else:
            if not any(fnmatch.fnmatch(rel, e["pattern"]) for rel in lines_by_file):
                stale.append(e)
    return stale


def scan(repo_root: Path = REPO_ROOT, allowlist_path: Path = ALLOWLIST_PATH) -> int:
    try:
        entries = parse_allowlist(allowlist_path)
    except AllowlistError as exc:
        print(f"verify-config-root-refs: FAIL {exc}")
        return 1

    occurrences = find_occurrences(repo_root)
    unallowed = find_unallowed(occurrences, entries)
    stale = find_stale_entries(entries, occurrences)

    if unallowed:
        print(f"verify-config-root-refs: {len(unallowed)} non-allowlisted legacy reference(s):")
        for rel, lineno, line in unallowed:
            print(f"  {rel}:{lineno}: {line.strip()}")
    if stale:
        print(f"verify-config-root-refs: {len(stale)} stale allowlist entrie(s) (no longer match anything):")
        for e in stale:
            print(f"  {allowlist_path.name}:{e['lineno']}: {e['raw'].strip()}")

    if unallowed or stale:
        print(
            "\nConvert each non-allowlisted reference to the root-generic form "
            "($CLAUDE_CONFIG_DIR / 'the config root'), or add a path:line "
            "allowlist entry with a reason if the reference is legitimately "
            "about the legacy location. Prune any stale entry that no longer "
            "matches anything."
        )
        return 1

    print(f"verify-config-root-refs: OK — {len(occurrences)} reference(s), all allowlisted")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on legacy ~/.claude / $HOME/.claude references outside the allowlist (doc scope)."
    )
    parser.add_argument("--staged", action="store_true", help="accepted; ignored (whole-repo check)")
    parser.parse_args(argv)
    return scan()


if __name__ == "__main__":
    sys.exit(main())
