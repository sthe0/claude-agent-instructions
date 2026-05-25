#!/usr/bin/env python3
"""Hard ceiling on instruction-file line counts.

Goal: prevent uncontrolled growth of the always-loaded policy surface
(`CLAUDE.md`, cursor mirror) and skill prompts. Limits live in `config.md`,
checked here. Above the limit → exit 1.

How to fix a FAIL: extract a section into `memory-global/leaves/<slug>.md`
(or a sibling `policy.md` for a skill body) and replace it in the parent
file with a one-line pointer.

Governed files (per `config.md` keys):

  CLAUDE.md                              claude-md-max-lines
  cursor-rules/claude-code-sync.mdc      cursor-mirror-max-lines
  skills/*/SKILL.md                      skill-md-max-lines
  skills/specializations/*/SKILL.md      skill-md-max-lines
  skills/*/policy.md                     policy-md-max-lines
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
    ("cursor-rules/claude-code-sync.mdc", "cursor-mirror-max-lines"),
    ("skills/*/SKILL.md", "skill-md-max-lines"),
    ("skills/specializations/*/SKILL.md", "skill-md-max-lines"),
    ("skills/*/policy.md", "policy-md-max-lines"),
]

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")


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
    scanned = 0
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
            if n > limit:
                rel = path.relative_to(REPO_ROOT)
                failures.append(f"{rel}: {n} lines, limit {limit} ({key})")

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
