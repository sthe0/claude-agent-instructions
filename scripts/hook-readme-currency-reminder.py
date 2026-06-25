#!/usr/bin/env python3
"""PreToolUse hook (Bash): before `git commit` / `arc commit`, surface docs
that sit next to (or are concept-bound to) changed code but are NOT in the
changeset — i.e. the docs most likely to have gone stale.

Primary path (concept registry): loads scripts/doc-bindings.json; for each
changed file, tests every concept's object_glob (with brace alternation
expansion); when a concept matches and its bound doc.file is NOT in the
changeset, warns naming the concept id and doc file + section to review.

Fallback path (nearest-README heuristic): for changed files that match NO
registered concept, applies the original nearest-README walk — READMEs next to
changed code but absent from the changeset are listed.

Warn-only by design (house style; same stance as
hook-push-confirmation-reminder.py and
memory-global/leaves/feedback-no-hard-caps-on-memory.md — a false block of a
commit costs more than a false pass). Always exits 0. Any internal failure is
swallowed so a commit is never blocked by this hook.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

COMMIT_RE = re.compile(r"\b(?:git|arc)\s+commit(?=\s|$)")
# `git commit -a` / `-am` / `--all` also sweep in tracked-but-unstaged edits.
GIT_ALL_RE = re.compile(r"\bcommit\b.*?(?:\s--all\b|\s-[a-z]*a[a-z]*\b)")
README_RE = re.compile(r"^README(\.[A-Za-z0-9]+)?$")
MAX_FILES = 500
MAX_LISTED = 8


def _expand_braces(pattern: str) -> list[str]:
    """Expand one `{a,b,...}` group in pattern; recurse for multiple groups."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    prefix, suffix = pattern[: m.start()], pattern[m.end() :]
    result: list[str] = []
    for alt in m.group(1).split(","):
        result.extend(_expand_braces(prefix + alt + suffix))
    return result


def _glob_matches(pattern: str, path: str) -> bool:
    """Return True if path matches pattern, expanding brace alternation first."""
    return any(fnmatch.fnmatch(path, p) for p in _expand_braces(pattern))


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


def _load_registry(path: Path | None = None) -> list[dict] | None:
    """Load doc-bindings.json; return the concept list or None on any error."""
    try:
        if path is None:
            path = Path(__file__).resolve().parent / "doc-bindings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        concepts = data.get("concepts")
        if not isinstance(concepts, list):
            return None
        return concepts
    except Exception:
        return None


def _check_concepts(
    paths: list[str],
    changed_set: set[str],
    concepts: list[dict],
) -> tuple[set[str], list[str]]:
    """Return (matched_paths, warning_lines) for the concept-registry doc check.

    matched_paths: items from `paths` that matched at least one concept glob.
    warning_lines: one formatted line per concept whose bound doc is absent.
    """
    matched_paths: set[str] = set()
    warnings: list[str] = []
    warned: set[str] = set()
    for concept in concepts:
        try:
            cid = concept.get("id", "?")
            obj_glob = concept.get("object_glob", "")
            doc = concept.get("doc") or {}
            doc_file = doc.get("file", "")
            doc_section = doc.get("section", "")
            if not obj_glob or not doc_file:
                continue
            doc_in_cs = (
                os.path.normpath(doc_file) in changed_set
                or doc_file in changed_set
            )
            hit = False
            for p in paths:
                if _glob_matches(obj_glob, p):
                    matched_paths.add(p)
                    hit = True
            if hit and not doc_in_cs and cid not in warned:
                warned.add(cid)
                section = f" (§ {doc_section})" if doc_section else ""
                warnings.append(f"  concept [{cid}]: {doc_file}{section}")
        except Exception:
            continue
    return matched_paths, warnings


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
    changed_set = {os.path.normpath(p) for p in paths}
    changed_readmes = {os.path.normpath(p) for p in paths if _is_readme(p)}
    non_readme_paths = [p for p in paths if not _is_readme(p)]

    # Registry-driven path
    concepts = _load_registry()
    concept_matched: set[str] = set()
    concept_warnings: list[str] = []
    if concepts is not None:
        try:
            concept_matched, concept_warnings = _check_concepts(
                non_readme_paths, changed_set, concepts
            )
        except Exception:
            pass

    if concept_warnings:
        lines = [
            "hook-readme-currency-reminder: changed concept-bound code without touching its doc.",
            "  These concept docs are NOT in the changeset — review the section named:",
        ]
        lines += concept_warnings
        print("\n".join(lines), file=sys.stderr)

    # Fallback: nearest-README heuristic for files not matched by any concept
    readme_candidates: list[str] = []
    seen: set[str] = set()
    for p in non_readme_paths:
        if p in concept_matched:
            continue
        readme = _nearest_readme(root, p)
        if not readme or readme in changed_readmes or readme in seen:
            continue
        seen.add(readme)
        readme_candidates.append(readme)

    if not readme_candidates:
        return 0

    listed = readme_candidates[:MAX_LISTED]
    more = len(readme_candidates) - len(listed)
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
