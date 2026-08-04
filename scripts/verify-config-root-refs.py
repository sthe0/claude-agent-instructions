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

Content anchors (``path:line:<sha8>``):
  A bare ``path:line`` pin is POSITIONAL, and a line number does not see the
  reference it names. That misfires in both directions: inserting a line
  ABOVE the reference reddens a pin whose reference is untouched (nothing to
  re-read — pure churn), while rewriting the pinned line's own text leaves
  the pin green (a changed reference nobody re-reads — the silent hole).
  An entry may therefore carry a third field: the first 8 hex of the sha256
  over the referenced line's STRIPPED text. The anchor resolves against the
  file's OCCURRENCE lines — the lines ``find_occurrences`` already emits, the
  mechanism's own domain — never against every line of the file, which keeps
  collisions rare. Five outcomes, in this order:
    (i)   anchor matches at the pinned line -> covered, exactly as a bare pin;
    (ii)  it matches EXACTLY ONE other occurrence -> RELOCATED: covered, and a
          notice naming old->new is printed on the DEFAULT run, so a reference
          that travelled far is visible in the check's own output;
    (iii) it matches no occurrence -> stale, the pre-existing hard failure;
    (iv)  it matches MORE THAN ONE other occurrence -> ambiguous hard failure
          naming the candidates, never a silent guess;
    (v)   two entries for one file resolving to the SAME occurrence -> hard
          failure, since otherwise one silently double-covers while the other
          occurrence surfaces as unallowed with no stated cause.
  A bare ``path:line`` keeps its previous behaviour byte-identical. ``--repin``
  rewrites the allowlist in place with relocated line numbers and freshly
  computed anchors, preserving each entry's reason text verbatim.

Self-reference guard (R4): the allowlist file itself, and the sweep's
worklist artifact (which enumerates occurrences by construction — its rows
and reasons routinely quote the very pattern being searched for), are
excluded from the scanned domain outright rather than allowlisted line by
line — otherwise the allowlist would need to cover itself circularly.

Exhaustiveness cross-check (standing, default-on — runs every invocation):
  ``iter_doc_files`` and the sibling S2 enumerator split the repo by suffix
  (``*.py``/``*.sh`` vs everything else), so that split holds by construction
  for any file this scan actually visits. The gap it does NOT cover on its
  own: ``find_occurrences`` reads each doc-scope file with strict
  ``read_text(encoding="utf-8")`` and silently skips one that fails to
  decode — a file could carry a legacy reference and vanish from the doc
  enumerator's occurrence list without a trace, while still not being
  ``*.py``/``*.sh`` (so the S2 enumerator never looks at it either). That is
  an ungoverned file: invisible to both enumerators at once.
  ``find_ungoverned`` re-derives the domain independently — decoding every
  file in ``_iter_repo_files`` (the tracked-shape domain, not the working
  tree) with ``errors="replace"`` instead of strict UTF-8 — so a legacy
  reference in an undecodable file still surfaces as text. It then asserts
  every file in that independently-derived domain is covered by doc scope,
  code scope (``*.py``/``*.sh``, by suffix), or the two named self-reference
  exclusions above — nothing else. Anything left over is named in the
  failure output.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import subprocess
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

# Anchored form first: an all-decimal anchor (~2% of them, since 10 of the 16
# hex digits are decimal) would otherwise be swallowed as the line number by
# the bare pattern's greedy path component, silently dropping the anchor.
ANCHORED_PIN_RE = re.compile(r"^(?P<file>.+):(?P<line>\d+):(?P<anchor>[0-9a-f]{8})$")
BARE_PIN_RE = re.compile(r"^(?P<file>.+):(?P<line>\d+)$")


class AllowlistError(ValueError):
    """Raised on a malformed scripts/config-root-refs-allowlist.txt entry."""


def anchor_of(line_text: str) -> str:
    """The content anchor for a referenced line: first 8 hex of sha256 over its
    STRIPPED text, so re-indenting a line does not invalidate its pin."""
    return hashlib.sha256(line_text.strip().encode("utf-8")).hexdigest()[:8]


def _iter_repo_files(repo_root: Path) -> "list[Path]":
    """Every tracked-shape file under repo_root: the git index, not the
    working tree, so the verifier's verdict cannot depend on an untracked
    file that happens to exist on one machine's disk and not another's. Falls
    back to a full directory walk when repo_root is not inside a git work
    tree (e.g. a tarball export) or git is unavailable — the only case where
    the working tree IS the domain.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return _iter_repo_files_walk(repo_root)
    files = []
    for name in result.stdout.decode("utf-8").split("\0"):
        if not name:
            continue
        rel = Path(name)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        files.append(rel)
    return sorted(files)


def _iter_repo_files_walk(repo_root: Path) -> "list[Path]":
    """Fallback domain for a non-git-work-tree repo_root: every file found by
    a plain directory walk, skipping .git/__pycache__."""
    files = []
    for p in sorted(repo_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(repo_root)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        files.append(rel)
    return files


def iter_doc_files(repo_root: Path) -> "list[Path]":
    """Repo-relative paths in doc scope: complement of the code enumerator."""
    files = []
    for rel in _iter_repo_files(repo_root):
        if rel.suffix.lower() in CODE_SUFFIXES:
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

    ``spec`` is ``path:line`` or ``path:line:<sha8>`` (both cover every
    occurrence on the resolved line) or an exceptional glob matched against
    the repo-relative path. A blank line or a line starting with ``#`` is a
    comment. A reason is mandatory on every entry line — raises
    AllowlistError otherwise.
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
        match = ANCHORED_PIN_RE.match(spec) or BARE_PIN_RE.match(spec)
        if match:
            groups = match.groupdict()
            entries.append({
                "kind": "line",
                "path": groups["file"],
                "line": int(groups["line"]),
                "anchor": groups.get("anchor"),
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


def _occurrences_by_file(occurrences) -> "dict[str, list[tuple[int, str]]]":
    by_file: "dict[str, list[tuple[int, str]]]" = {}
    for rel, lineno, text in occurrences:
        by_file.setdefault(rel, []).append((lineno, text))
    return by_file


def resolve_entries(entries, occurrences) -> "tuple[list[dict], list[dict]]":
    """Bind every line entry to the occurrence it actually names.

    Annotates each line entry with ``resolved_line`` — the line every
    downstream check (coverage, staleness) then uses, or ``None`` when the
    entry names no occurrence at all — plus a ``resolution`` tag, and returns
    ``(relocations, ambiguities)``.

    An unanchored entry always resolves to its own pinned line, which is why
    its behaviour is unchanged. An anchored one that still matches at its
    pinned line is likewise left alone: agreement between pin and anchor is
    not ambiguity, so a second matching occurrence elsewhere cannot unseat an
    entry that is already exactly right.

    An anchor that matches NOTHING resolves to ``None`` even when its pinned
    line still carries some other legacy reference — that is the silent hole
    the anchors exist to close. The anchor, not the line number, is the
    entry's identity: a rewritten line is a different reference, and it must
    surface for a human to re-read rather than inherit the old reason.
    """
    by_file = _occurrences_by_file(occurrences)
    relocations: "list[dict]" = []
    ambiguities: "list[dict]" = []
    for e in entries:
        if e["kind"] != "line":
            continue
        e["resolved_line"] = e["line"]
        e["resolution"] = "pinned"
        anchor = e.get("anchor")
        if anchor is None:
            continue
        candidates = sorted(
            lineno for lineno, text in by_file.get(e["path"], [])
            if anchor_of(text) == anchor
        )
        if e["line"] in candidates:
            continue
        if len(candidates) == 1:
            e["resolved_line"] = candidates[0]
            e["resolution"] = "relocated"
            relocations.append({"entry": e, "old": e["line"], "new": candidates[0]})
        elif len(candidates) > 1:
            e["resolved_line"] = None
            e["resolution"] = "ambiguous"
            ambiguities.append({"entry": e, "candidates": candidates})
        else:
            e["resolved_line"] = None
            e["resolution"] = "unresolved"
    return relocations, ambiguities


def find_duplicate_coverage(entries) -> "list[tuple[str, int, list[dict]]]":
    """Line entries that resolve onto the same occurrence as another entry.

    Left unchecked, one of them silently double-covers while the occurrence
    the other used to name resurfaces as unallowed with no stated cause.
    """
    seen: "dict[tuple[str, int], list[dict]]" = {}
    for e in entries:
        if e["kind"] != "line":
            continue
        resolved = e.get("resolved_line", e["line"])
        if resolved is None:
            continue  # names no occurrence — reported as stale/ambiguous instead
        seen.setdefault((e["path"], resolved), []).append(e)
    return [
        (path, lineno, group)
        for (path, lineno), group in sorted(seen.items())
        if len(group) > 1
    ]


def _covers(entry: dict, rel: str, lineno: int) -> bool:
    if entry["kind"] == "line":
        return entry["path"] == rel and entry.get("resolved_line", entry["line"]) == lineno
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
            if e.get("resolution") == "ambiguous":
                continue  # already reported, and by too MANY matches not none
            if e.get("resolved_line", e["line"]) not in lines_by_file.get(e["path"], set()):
                stale.append(e)
        else:
            if not any(fnmatch.fnmatch(rel, e["pattern"]) for rel in lines_by_file):
                stale.append(e)
    return stale


def find_ungoverned(repo_root: Path, occurrences=None) -> "list[str]":
    """Repo-relative paths carrying a legacy reference that fall into NEITHER
    doc scope NOR code scope NOR the two self-reference exclusions.

    Independently re-derives the domain (``errors="replace"`` decode of every
    file in ``_iter_repo_files``) rather than reusing ``find_occurrences``'s
    strict-UTF-8 read, so a file that ``find_occurrences`` silently drops on a
    decode error still counts here — proving the doc/code split is
    exhaustive, not just self-consistent with its own blind spot. The FILE SET
    is shared with ``_iter_repo_files`` (the git index) so this cross-check
    cannot report an untracked file as ungoverned; only the decode strategy
    is independent.
    """
    if occurrences is None:
        occurrences = find_occurrences(repo_root)
    doc_scope_files = {rel for rel, _, _ in occurrences}

    all_legacy_files: "set[str]" = set()
    for rel in _iter_repo_files(repo_root):
        relpath = rel.as_posix()
        if relpath in SELF_REF_EXCLUDED:
            continue
        try:
            raw = (repo_root / rel).read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        if TILDE_RE.search(text) or HOME_RE.search(text):
            all_legacy_files.add(relpath)

    code_scope_files = {f for f in all_legacy_files if Path(f).suffix.lower() in CODE_SUFFIXES}
    governed = doc_scope_files | code_scope_files
    return sorted(all_legacy_files - governed)


def scan(repo_root: Path = REPO_ROOT, allowlist_path: Path = ALLOWLIST_PATH) -> int:
    try:
        entries = parse_allowlist(allowlist_path)
    except AllowlistError as exc:
        print(f"verify-config-root-refs: FAIL {exc}")
        return 1

    occurrences = find_occurrences(repo_root)
    relocations, ambiguities = resolve_entries(entries, occurrences)
    unallowed = find_unallowed(occurrences, entries)
    stale = find_stale_entries(entries, occurrences)
    duplicates = find_duplicate_coverage(entries)
    ungoverned = find_ungoverned(repo_root, occurrences)

    # Printed on the DEFAULT run, not only under --repin: a reference that
    # travelled is a fact about the repo the reader should see even when the
    # check passes.
    for r in relocations:
        print(
            f"verify-config-root-refs: relocated {r['entry']['path']}:"
            f"{r['old']} -> :{r['new']} (content anchor still matches)"
        )

    if ambiguities:
        print(
            f"verify-config-root-refs: {len(ambiguities)} ambiguous anchor(s) "
            "(matching more than one occurrence — refusing to guess):"
        )
        for a in ambiguities:
            candidates = ", ".join(str(c) for c in a["candidates"])
            print(f"  {allowlist_path.name}:{a['entry']['lineno']}: "
                  f"{a['entry']['raw'].strip()} -> lines {candidates}")
    if duplicates:
        print(
            f"verify-config-root-refs: {len(duplicates)} occurrence(s) covered "
            "by more than one allowlist entry:"
        )
        for path, lineno, group in duplicates:
            rows = ", ".join(f"{allowlist_path.name}:{e['lineno']}" for e in group)
            print(f"  {path}:{lineno}: {rows}")
    if unallowed:
        print(f"verify-config-root-refs: {len(unallowed)} non-allowlisted legacy reference(s):")
        for rel, lineno, line in unallowed:
            print(f"  {rel}:{lineno}: {line.strip()}")
    if stale:
        print(f"verify-config-root-refs: {len(stale)} stale allowlist entrie(s) (no longer match anything):")
        for e in stale:
            print(f"  {allowlist_path.name}:{e['lineno']}: {e['raw'].strip()}")
    if ungoverned:
        print(
            f"verify-config-root-refs: {len(ungoverned)} ungoverned file(s) "
            "(legacy reference invisible to BOTH the doc and code enumerators):"
        )
        for rel in ungoverned:
            print(f"  {rel}")

    if unallowed or stale or ungoverned or ambiguities or duplicates:
        print(
            "\nConvert each non-allowlisted reference to the root-generic form "
            "($CLAUDE_CONFIG_DIR / 'the config root'), or add a path:line "
            "allowlist entry with a reason if the reference is legitimately "
            "about the legacy location. Prune any stale entry that no longer "
            "matches anything. An ungoverned file usually means it failed to "
            "decode as UTF-8 in find_occurrences — fix its encoding or extend "
            "the doc/code scope split so it is actually scanned. For a pin "
            "whose line merely moved, re-run with --repin. An ambiguous or "
            "double-covering entry must be resolved by hand: --repin refuses "
            "to guess which occurrence was meant."
        )
        return 1

    print(
        f"verify-config-root-refs: OK — {len(occurrences)} reference(s), all "
        "allowlisted; exhaustiveness cross-check clean"
    )
    return 0


def _respec(raw: str, new_spec: str) -> str:
    """``raw`` with only its spec replaced — reason text, comment marker and
    the whitespace between them all survive verbatim, so a repin of the whole
    allowlist is a diff of line numbers and anchors and nothing else."""
    head, hash_marker, tail = raw.partition("#")
    indent = head[: len(head) - len(head.lstrip())]
    gap = head[len(head.rstrip()):]
    return f"{indent}{new_spec}{gap}{hash_marker}{tail}"


def repin(repo_root: Path = REPO_ROOT, allowlist_path: Path = ALLOWLIST_PATH) -> int:
    """Rewrite the allowlist in place: every line entry that resolves onto a
    real occurrence gets that occurrence's line number and a freshly computed
    anchor. An entry that is stale, ambiguous or double-covering is left
    untouched — repinning it would either invent an anchor for a reference
    that no longer exists or silently pick one of several candidates."""
    try:
        entries = parse_allowlist(allowlist_path)
    except AllowlistError as exc:
        print(f"verify-config-root-refs: FAIL {exc}")
        return 1

    occurrences = find_occurrences(repo_root)
    _, ambiguities = resolve_entries(entries, occurrences)
    by_file = _occurrences_by_file(occurrences)
    skipped: "dict[int, tuple[dict, str]]" = {}
    for e in find_stale_entries(entries, occurrences):
        if e["kind"] == "line":
            skipped[e["lineno"]] = (e, "stale")
    for a in ambiguities:
        skipped[a["entry"]["lineno"]] = (a["entry"], "ambiguous")
    for _, _, group in find_duplicate_coverage(entries):
        for e in group:
            skipped[e["lineno"]] = (e, "double-covering")

    lines = allowlist_path.read_text(encoding="utf-8").splitlines()
    rewritten = 0
    for e in entries:
        if e["kind"] != "line" or e["lineno"] in skipped:
            continue
        resolved = e["resolved_line"]
        text = next(t for ln, t in by_file[e["path"]] if ln == resolved)
        new_spec = f"{e['path']}:{resolved}:{anchor_of(text)}"
        new_raw = _respec(e["raw"], new_spec)
        if new_raw != e["raw"]:
            lines[e["lineno"] - 1] = new_raw
            rewritten += 1
    allowlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"verify-config-root-refs: repinned {rewritten} entrie(s)")
    for lineno, (e, why) in sorted(skipped.items()):
        print(f"  left alone ({why} — resolve by hand): "
              f"{allowlist_path.name}:{lineno}: {e['raw'].strip()}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on legacy ~/.claude / $HOME/.claude references outside the allowlist (doc scope)."
    )
    parser.add_argument("--staged", action="store_true", help="accepted; ignored (whole-repo check)")
    parser.add_argument(
        "--repin", action="store_true",
        help="rewrite the allowlist in place with current line numbers and fresh content anchors",
    )
    args = parser.parse_args(argv)
    if args.repin:
        return repin()
    return scan()


if __name__ == "__main__":
    sys.exit(main())
