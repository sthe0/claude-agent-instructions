#!/usr/bin/env python3
"""Fail on a judge-latency number written in prose that nobody has re-read.

Difficulty removed: the timeouts around this repo's judge calls are explained in
prose — comments, docstrings, the samples README — and that prose carried numbers
from three different undated measurements at once ("11.6-13.5s", "a 30s budget",
"the registered timeout (35s)"), several of them already contradicted by the
constants on the next line. A reader could not tell which sentence had been
re-read against the current calibration and which was a leftover, and no check
noticed, because a stale sentence is syntactically perfect.

The remedy is deliberately NOT a regex that decides what a number MEANS — that
would be determinizing perception at the wrong level (CLAUDE.md, and
memory-global/leaves/regex-not-for-semantic-classification.md). Instead:

  * `judge-prose-domain.txt` enumerates the files whose prose talks about judge
    latency. Enumeration is a committed list, not a heuristic sweep — the domain
    is the reviewable object;
  * inside those files a purely LEXICAL prefilter finds prose lines carrying a
    duration ("41s", "2 seconds") or a named statistic ("p90 37.58", "n=18").
    High recall on purpose: a false positive costs one allowlist row, a false
    negative is a number nobody re-reads;
  * every such occurrence must appear in `judge-prose-numbers-allowlist.txt`,
    pinned by a content anchor and carrying a MANDATORY reason that names where
    the number comes from. The reason is the perception; the check only verifies
    that a human wrote one for exactly this text.

Editing a governed sentence changes its anchor, which makes the entry stale and
fails the check until the reason is re-read and re-pinned (`--repin`). That is
the whole mechanism: prose and calibration cannot drift apart silently, and no
regex ever has to understand a sentence.

The anchor design (four-way resolution, `--repin` preserving reasons verbatim) is
cloned from verify-config-root-refs.py, whose allowlist header states the
rationale and the accepted limits in full.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DOMAIN_PATH = SCRIPTS_DIR / "judge-prose-domain.txt"
ALLOWLIST_PATH = SCRIPTS_DIR / "judge-prose-numbers-allowlist.txt"

_NAME = "verify-judge-prose-numbers"

# A duration written in seconds, and a named statistic written with its value.
# Both are LEXICAL patterns over the way a latency figure is spelled — never a
# claim about what the figure is for; that is what the allowlist reason says.
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:s\b|sec\b|secs\b|seconds?\b)")
_STAT_RE = re.compile(r"\b(?:n|K|p90|median|min|max)\s*[= ]\s*\d+(?:\.\d+)?\b")


class AllowlistError(Exception):
    pass


def anchor_of(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]


def load_domain(path: Path = DOMAIN_PATH) -> "list[str]":
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def _prose_lines(path: Path) -> "set[int] | None":
    """The line numbers of `path` that are prose, or None for a file that is
    prose throughout. Structural per language, not a guess: Python is read with
    `tokenize` so comments and every string literal (docstrings included) are
    exact; a shell comment is a `#` line; markdown is prose end to end."""
    suffix = path.suffix
    if suffix in {".md", ".txt", ".tsv"}:
        return None
    text = path.read_text(encoding="utf-8")
    if suffix == ".py":
        lines: "set[int]" = set()
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    lines.update(range(token.start[0], token.end[0] + 1))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # An unparseable file is reported by other checks; here, fail LOUD
            # rather than silently governing nothing.
            raise AllowlistError(f"{path} does not tokenize as Python")
        return lines
    return {
        i for i, line in enumerate(text.splitlines(), 1)
        if line.lstrip().startswith("#")
    }


def find_occurrences(
    repo_root: Path = REPO_ROOT, domain_path: Path = DOMAIN_PATH
) -> "list[tuple[str, int, str]]":
    """Every (domain-relative path, line number, line text) the prefilter fires on."""
    found: "list[tuple[str, int, str]]" = []
    for rel in load_domain(domain_path):
        path = repo_root / rel
        if not path.exists():
            raise AllowlistError(f"domain names a missing file: {rel}")
        prose = _prose_lines(path)
        for i, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if prose is not None and i not in prose:
                continue
            if _DURATION_RE.search(text) or _STAT_RE.search(text):
                found.append((rel, i, text))
    return found


def parse_allowlist(path: Path = ALLOWLIST_PATH) -> "list[dict]":
    entries: "list[dict]" = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        spec, marker, reason = raw.partition("#")
        if not marker or not reason.strip():
            raise AllowlistError(
                f"{path.name}:{lineno}: entry without a reason — the reason IS the "
                "check; a bare path only says somebody saw a number once"
            )
        parts = spec.strip().split(":")
        if len(parts) != 3:
            raise AllowlistError(
                f"{path.name}:{lineno}: expected `path:line:sha8`, got {spec.strip()!r}"
            )
        rel, lineno_s, sha = parts
        if not lineno_s.isdigit():
            raise AllowlistError(f"{path.name}:{lineno}: {lineno_s!r} is not a line number")
        entries.append({
            "lineno": lineno, "raw": raw, "path": rel,
            "pinned_line": int(lineno_s), "anchor": sha, "reason": reason.strip(),
        })
    return entries


def resolve_entries(
    entries: "list[dict]", occurrences: "list[tuple[str, int, str]]"
) -> "tuple[list[dict], list[dict]]":
    """Resolve each entry onto an occurrence by ANCHOR, not by ordinal.

    Still at the pinned line -> covered. At exactly one other occurrence ->
    relocated and covered (the move is reported). Nowhere -> stale, even if the
    pinned line still carries some other number: a rewritten sentence is a
    different claim and must be re-read. At several -> ambiguous, a hard failure.
    """
    problems: "list[dict]" = []
    for e in entries:
        same_file = [(ln, text) for rel, ln, text in occurrences if rel == e["path"]]
        matches = [ln for ln, text in same_file if anchor_of(text) == e["anchor"]]
        e["resolved_line"] = None
        if e["pinned_line"] in matches:
            e["resolved_line"] = e["pinned_line"]
        elif len(matches) == 1:
            e["resolved_line"] = matches[0]
            e["relocated_from"] = e["pinned_line"]
        elif len(matches) > 1:
            problems.append({"entry": e, "kind": "ambiguous", "candidates": matches})
        else:
            problems.append({"entry": e, "kind": "stale", "candidates": []})
    return entries, problems


def find_duplicate_coverage(entries: "list[dict]") -> "list[tuple[str, int, list[dict]]]":
    """Two entries resolving onto ONE occurrence: one silently double-covers
    while the occurrence the other named resurfaces as unallowed."""
    seen: "dict[tuple[str, int], list[dict]]" = {}
    for e in entries:
        if e.get("resolved_line") is None:
            continue
        seen.setdefault((e["path"], e["resolved_line"]), []).append(e)
    return [(rel, ln, group) for (rel, ln), group in sorted(seen.items()) if len(group) > 1]


def scan(
    repo_root: Path = REPO_ROOT,
    allowlist_path: Path = ALLOWLIST_PATH,
    domain_path: Path = DOMAIN_PATH,
) -> int:
    try:
        occurrences = find_occurrences(repo_root, domain_path)
        entries = parse_allowlist(allowlist_path)
    except AllowlistError as exc:
        print(f"{_NAME}: FAIL — {exc}")
        return 1

    entries, problems = resolve_entries(entries, occurrences)
    duplicates = find_duplicate_coverage(entries)
    covered = {(e["path"], e["resolved_line"]) for e in entries if e.get("resolved_line")}
    unallowed = [o for o in occurrences if (o[0], o[1]) not in covered]

    for e in entries:
        if "relocated_from" in e:
            print(f"{_NAME}: relocated {e['path']}:{e['relocated_from']} -> "
                  f":{e['resolved_line']} (content anchor still matches)")

    failed = bool(problems or duplicates or unallowed)
    for p in problems:
        e = p["entry"]
        detail = f" candidates: {p['candidates']}" if p["candidates"] else ""
        print(f"{_NAME}: FAIL — {p['kind']} entry {allowlist_path.name}:{e['lineno']}: "
              f"{e['path']}:{e['pinned_line']}:{e['anchor']}{detail}")
    for rel, ln, group in duplicates:
        rows = ", ".join(str(e["lineno"]) for e in group)
        print(f"{_NAME}: FAIL — {rel}:{ln} covered by several entries "
              f"({allowlist_path.name} rows {rows})")
    for rel, ln, text in unallowed:
        print(f"{_NAME}: FAIL — unallowed: {rel}:{ln}:{anchor_of(text)}  "
              f"# <reason>   <- {text.strip()[:90]}")

    if failed:
        print(f"{_NAME}: FAIL — {len(unallowed)} unallowed, {len(problems)} unresolved, "
              f"{len(duplicates)} double-covered of {len(occurrences)} occurrence(s)")
        return 1
    print(f"{_NAME}: OK — {len(occurrences)} prose number(s) in "
          f"{len(load_domain(domain_path))} domain file(s), all allowlisted with a reason")
    return 0


def _respec(raw: str, new_spec: str) -> str:
    """``raw`` with only its spec replaced — reason text, comment marker and the
    whitespace between them all survive verbatim, so a repin is a diff of line
    numbers and anchors and nothing else."""
    head, marker, tail = raw.partition("#")
    indent = head[: len(head) - len(head.lstrip())]
    gap = head[len(head.rstrip()):]
    return f"{indent}{new_spec}{gap}{marker}{tail}"


def repin(
    repo_root: Path = REPO_ROOT,
    allowlist_path: Path = ALLOWLIST_PATH,
    domain_path: Path = DOMAIN_PATH,
) -> int:
    try:
        occurrences = find_occurrences(repo_root, domain_path)
        entries = parse_allowlist(allowlist_path)
    except AllowlistError as exc:
        print(f"{_NAME}: FAIL — {exc}")
        return 1

    entries, problems = resolve_entries(entries, occurrences)
    skipped = {p["entry"]["lineno"]: p["kind"] for p in problems}
    for _rel, _ln, group in find_duplicate_coverage(entries):
        for e in group:
            skipped[e["lineno"]] = "double-covering"

    text_at = {(rel, ln): text for rel, ln, text in occurrences}
    lines = allowlist_path.read_text(encoding="utf-8").splitlines()
    rewritten = 0
    for e in entries:
        if e["lineno"] in skipped or e.get("resolved_line") is None:
            continue
        spec = f"{e['path']}:{e['resolved_line']}:{anchor_of(text_at[(e['path'], e['resolved_line'])])}"
        new_raw = _respec(e["raw"], spec)
        if new_raw != e["raw"]:
            lines[e["lineno"] - 1] = new_raw
            rewritten += 1
    allowlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"{_NAME}: repinned {rewritten} entrie(s)")
    for lineno, why in sorted(skipped.items()):
        print(f"  left alone ({why} — resolve by hand): {allowlist_path.name}:{lineno}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on a judge-latency number in prose that no allowlist reason covers."
    )
    parser.add_argument("--staged", action="store_true",
                        help="accepted; ignored (the domain is a committed list)")
    parser.add_argument("--repin", action="store_true",
                        help="rewrite the allowlist with current line numbers and fresh anchors")
    args = parser.parse_args(argv)
    return repin() if args.repin else scan()


if __name__ == "__main__":
    sys.exit(main())
