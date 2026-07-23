#!/usr/bin/env python3
"""Hard ceiling on instruction-file line counts.

Goal: prevent uncontrolled growth of the always-loaded policy surface
(`CLAUDE.md`, cursor mirror) and skill prompts. Limits live in `config.md`,
checked here. Above the limit → exit 1.

How to fix a FAIL: extract a section into `memory-global/leaves/<slug>.md`
(or a sibling `policy.md` for a skill body) and replace it in the parent
file with a one-line pointer.

Governed files (per `config.md` keys):

  CLAUDE.md                              claude-md-max-lines, claude-md-max-chars
  README.md                              readme-max-lines
  cursor/rules/claude-code-sync.mdc      cursor-mirror-max-lines
  skills/*/SKILL.md                      skill-md-max-lines
  skills/specializations/*/SKILL.md      skill-md-max-lines
  skills/*/policy.md                     policy-md-max-lines
  skills/specializations/*/policy.md     policy-md-max-lines

CLAUDE.md also has a char ceiling (`claude-md-max-chars`), measured in UTF-16
code units — the unit the harness's own `content.length` check uses, not
UTF-8 bytes. Crossing it produces a display-only `/doctor` warning, not
truncation (verified against the installed client bundle and an over-limit
sentinel test; see the `claude-md-max-chars` row in config.md). The
line-count guard does not catch char growth (a few long lines can cross the
char budget while staying well under the line limit), so it is checked
explicitly.

Every `skills/*/SKILL.md` and `skills/specializations/*/SKILL.md` frontmatter
`description:` value is ALSO checked against `skill-description-max-chars` —
that description is always-visible index cost (loaded into every session's
skill list), unlike the skill body, which loads only on invocation.

At or above WARN_FRACTION of any ceiling a non-fatal WARN line is printed
(exit code unchanged): a limit that only signals at 100% is discovered as a
crisis; the early warning turns it into routine maintenance.

`--surface-report` prints a separate, report-only view: the aggregate
always-loaded surface (CLAUDE.md + config.md + memory-global/MEMORY.md + the
sum of all skill descriptions) against the ADVISORY (never FAIL)
`always-loaded-surface-advisory-chars` ceiling, plus a per-surface
breakdown. It never changes the exit code and runs instead of (not
alongside) the governed-ceiling checks above. With `--include-dynamic` it
additionally reports OBSERVED UserPromptSubmit hook-injection volume from
recent session transcripts, labelled DYNAMIC — never summed into the static
total. Without that flag, no transcript I/O happens at all.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_MD = REPO_ROOT / "config.md"

# (glob pattern relative to repo root, config-key for the limit).
GOVERNED = [
    ("CLAUDE.md", "claude-md-max-lines"),
    ("README.md", "readme-max-lines"),
    ("cursor/rules/claude-code-sync.mdc", "cursor-mirror-max-lines"),
    ("skills/*/SKILL.md", "skill-md-max-lines"),
    ("skills/specializations/*/SKILL.md", "skill-md-max-lines"),
    ("skills/*/policy.md", "policy-md-max-lines"),
    ("skills/specializations/*/policy.md", "policy-md-max-lines"),
]

# The always-visible frontmatter description of every skill (loaded into the
# skill index on every session, unlike the lazily-loaded body).
SKILL_DESCRIPTION_GLOBS = [
    "skills/*/SKILL.md",
    "skills/specializations/*/SKILL.md",
]

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")
FRONTMATTER_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)

# Fraction of a ceiling at which a non-fatal WARN is emitted.
WARN_FRACTION = 0.90


def check_level(value: int, limit: int) -> str:
    """Classify a measured value against its ceiling: 'ok' | 'warn' | 'fail'."""
    if value > limit:
        return "fail"
    if value >= limit * WARN_FRACTION:
        return "warn"
    return "ok"


def parse_config_md() -> dict[str, str]:
    constants: dict[str, str] = {}
    for line in CONFIG_MD.read_text(encoding="utf-8").splitlines():
        m = CONFIG_KEY_RE.match(line)
        if m:
            constants[m.group(1)] = m.group(2)
    return constants


def extract_frontmatter_description(path: Path) -> str | None:
    """The `description:` value from a file's `---`-delimited frontmatter, or
    None if there is no frontmatter or no description line — single-line
    values only (mirrors self-diagnose.py's own `_FM_DESC_RE`)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = FRONTMATTER_DESC_RE.search(text[:end])
    return m.group(1) if m else None


def _surface_files() -> list[tuple[str, Path]]:
    """(label, path) for the --surface-report breakdown — built from the
    current REPO_ROOT/CONFIG_MD globals at call time (not a module-level
    constant) so tests can point them at a throwaway tree."""
    return [
        ("CLAUDE.md", REPO_ROOT / "CLAUDE.md"),
        ("config.md", CONFIG_MD),
        ("memory-global/MEMORY.md", REPO_ROOT / "memory-global" / "MEMORY.md"),
    ]


def _iter_skill_descriptions() -> list[tuple[Path, str]]:
    """(path, description) for every skill whose frontmatter has one —
    shared by the enforced per-skill cap check and the --surface-report
    aggregate so the two never drift apart on which files count."""
    out: list[tuple[Path, str]] = []
    for glob_pat in SKILL_DESCRIPTION_GLOBS:
        for path in sorted(REPO_ROOT.glob(glob_pat)):
            if not path.is_file():
                continue
            desc = extract_frontmatter_description(path)
            if desc is not None:
                out.append((path, desc))
    return out


def _skill_description_total() -> tuple[int, int]:
    """(summed chars, file count) over every skill's frontmatter description."""
    descriptions = _iter_skill_descriptions()
    return sum(len(desc) for _, desc in descriptions), len(descriptions)


def _load_cost_report():
    """Import scripts/cost-report.py by path (repo idiom: self-diagnose.py's
    own _load_lint_prose_length) — reuses its transcript iterator and
    projects-root resolution instead of re-deriving them here."""
    path = REPO_ROOT / "scripts" / "cost-report.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("lint_prose_length_cost_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan_dynamic_injection(max_sessions: int = 20) -> dict[str, int] | None:
    """OBSERVED UserPromptSubmit hook-injection volume over the most recent
    session transcripts — DYNAMIC data, never summed into the static
    surface total. Returns None if no transcripts are found at all (fresh
    machine / no history); a dict with n_events == 0 if sessions were
    scanned but none carried a UserPromptSubmit injection."""
    cost_report = _load_cost_report()
    if cost_report is None:
        return None
    projects_dir = cost_report.PROJECTS_DIR
    if not projects_dir.is_dir():
        return None
    files = sorted(
        projects_dir.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_sessions]
    if not files:
        return None
    sizes: list[int] = []
    for path in files:
        for d in cost_report._iter_jsonl(path):
            if d.get("type") != "attachment":
                continue
            att = d.get("attachment")
            if not isinstance(att, dict) or att.get("hookEvent") != "UserPromptSubmit":
                continue
            content = att.get("content")
            if isinstance(content, str) and content:
                sizes.append(len(content))
    if not sizes:
        return {"n_events": 0, "n_sessions": len(files), "mean": 0, "max": 0}
    return {
        "n_events": len(sizes),
        "n_sessions": len(files),
        "mean": sum(sizes) // len(sizes),
        "max": max(sizes),
    }


def surface_breakdown() -> tuple[list[tuple[str, int]], int]:
    """((label, chars) rows, summed total) for the always-loaded surface.

    The single source of the surface number: `cmd_surface_report` renders what
    this returns, and out-of-tree consumers (rule-salience-report.py's Phase-3
    pressure arm) read it instead of re-deriving or scraping stdout. Dynamic
    hook-injection volume is deliberately not part of it — it is never summed
    into the static total."""
    rows: list[tuple[str, int]] = []
    for label, path in _surface_files():
        n = len(path.read_text(encoding="utf-8")) if path.is_file() else 0
        rows.append((label, n))

    skill_total, skill_count = _skill_description_total()
    rows.append((f"skill descriptions ({skill_count} skills)", skill_total))

    return rows, sum(n for _, n in rows)


def cmd_surface_report(constants: dict[str, str], include_dynamic: bool) -> int:
    """Report-only: the aggregate always-loaded surface plus its breakdown.
    Never fails — the aggregate is disclosed, not enforced (see
    always-loaded-surface-advisory-chars in config.md)."""
    breakdown, total = surface_breakdown()

    print("lint-prose-length: always-loaded-surface-report")
    for label, n in breakdown:
        print(f"  {label}: {n} chars")
    print(f"  TOTAL: {total} chars")

    advisory_raw = constants.get("always-loaded-surface-advisory-chars")
    if advisory_raw is not None:
        try:
            advisory = int(advisory_raw)
        except ValueError:
            advisory = 0
        if advisory:
            level = check_level(total, advisory)
            if level in ("warn", "fail"):
                print(
                    f"  ADVISORY: {total} chars is {total * 100 // advisory}% of "
                    f"always-loaded-surface-advisory-chars ({advisory}) — "
                    "advisory only, does not fail"
                )

    if include_dynamic:
        dyn = scan_dynamic_injection()
        if dyn is None or dyn["n_events"] == 0:
            print("  DYNAMIC (OBSERVED UserPromptSubmit injection): no transcript data found")
        else:
            print(
                f"  DYNAMIC (OBSERVED UserPromptSubmit injection): mean {dyn['mean']} chars, "
                f"max {dyn['max']} chars, over {dyn['n_events']} firing(s) across "
                f"{dyn['n_sessions']} session(s) scanned — NOT summed into TOTAL"
            )

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="(accepted for verify-all parity; this lint always reads from disk)",
    )
    parser.add_argument(
        "--surface-report",
        action="store_true",
        help="report-only: print the aggregate always-loaded surface and a "
        "per-surface breakdown; never changes the exit code",
    )
    parser.add_argument(
        "--include-dynamic",
        action="store_true",
        help="with --surface-report, also print OBSERVED UserPromptSubmit "
        "hook-injection volume from recent transcripts (DYNAMIC, not summed "
        "into the static total); without --surface-report this is ignored",
    )
    args = parser.parse_args(argv)

    constants = parse_config_md()

    if args.surface_report:
        return cmd_surface_report(constants, include_dynamic=args.include_dynamic)

    failures: list[str] = []
    warnings: list[str] = []
    scanned = 0

    # Char-size ceiling for CLAUDE.md, measured in UTF-16 code units — the unit
    # the harness's own content.length check uses. The line-count guard above
    # does not catch char growth (a few long lines can cross the char budget
    # while staying well under the line limit), so check chars explicitly.
    char_key = "claude-md-max-chars"
    raw_chars = constants.get(char_key)
    if raw_chars is None:
        failures.append(f"config.md missing key: {char_key}")
    else:
        try:
            char_limit = int(raw_chars)
        except ValueError:
            failures.append(f"config.md key {char_key} is not an integer: {raw_chars!r}")
        else:
            claude_md = REPO_ROOT / "CLAUDE.md"
            if claude_md.is_file():
                scanned += 1
                nchars = len(claude_md.read_text(encoding="utf-8"))
                level = check_level(nchars, char_limit)
                if level == "fail":
                    failures.append(
                        f"CLAUDE.md: {nchars} chars, limit {char_limit} ({char_key})"
                    )
                elif level == "warn":
                    warnings.append(
                        f"CLAUDE.md: {nchars} chars, {nchars * 100 // char_limit}% "
                        f"of limit {char_limit} ({char_key})"
                    )

    # Per-skill frontmatter description ceiling — always-visible index cost
    # (loaded into every session's skill list), unlike the skill body, which
    # loads only on invocation.
    desc_key = "skill-description-max-chars"
    raw_desc_limit = constants.get(desc_key)
    if raw_desc_limit is None:
        failures.append(f"config.md missing key: {desc_key}")
    else:
        try:
            desc_limit = int(raw_desc_limit)
        except ValueError:
            failures.append(f"config.md key {desc_key} is not an integer: {raw_desc_limit!r}")
        else:
            for path, desc in _iter_skill_descriptions():
                scanned += 1
                n = len(desc)
                rel = path.relative_to(REPO_ROOT)
                level = check_level(n, desc_limit)
                if level == "fail":
                    failures.append(
                        f"{rel}: {n} chars description, limit {desc_limit} ({desc_key})"
                    )
                elif level == "warn":
                    warnings.append(
                        f"{rel}: {n} chars description, {n * 100 // desc_limit}% "
                        f"of limit {desc_limit} ({desc_key})"
                    )

    for glob_pat, key in GOVERNED:
        raw = constants.get(key)
        if raw is None:
            failures.append(f"config.md missing key: {key}")
            continue
        try:
            limit = int(raw)
        except ValueError:
            failures.append(f"config.md key {key} is not an integer: {raw!r}")
            continue
        for path in sorted(REPO_ROOT.glob(glob_pat)):
            if not path.is_file():
                continue
            scanned += 1
            n = len(path.read_text(encoding="utf-8").splitlines())
            rel = path.relative_to(REPO_ROOT)
            level = check_level(n, limit)
            if level == "fail":
                failures.append(f"{rel}: {n} lines, limit {limit} ({key})")
            elif level == "warn":
                warnings.append(
                    f"{rel}: {n} lines, {n * 100 // limit}% of limit {limit} ({key})"
                )

    for w in warnings:
        print(f"lint-prose-length: WARN — {w}")

    if failures:
        print(f"lint-prose-length: FAIL — {len(failures)} issue(s)")
        for f in failures:
            print(f"  {f}")
        print()
        print(
            "To fix: extract a section to memory-global/leaves/<slug>.md "
            "(or a sibling policy.md for a skill body) and replace it with "
            "a one-line pointer in the parent file."
        )
        return 1

    print(f"lint-prose-length: OK — {scanned} file(s) within ceilings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
