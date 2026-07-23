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

At or above WARN_FRACTION of any ceiling a non-fatal WARN line is printed
(exit code unchanged): a limit that only signals at 100% is discovered as a
crisis; the early warning turns it into routine maintenance.
"""
from __future__ import annotations

import argparse
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

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="(accepted for verify-all parity; this lint always reads from disk)",
    )
    parser.parse_args(argv)

    constants = parse_config_md()

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
