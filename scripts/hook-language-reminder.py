#!/usr/bin/env python3
"""UserPromptSubmit hook: when the user writes in a non-English (here: Cyrillic)
language, remind that user-facing replies must match the user's language.

Rule (CLAUDE.md § Instruction language): instruction-repo text is English by
default, but user-facing replies — including analyses, retrospectives,
self-improvement proposals, technical narratives, and the question + option
labels of every AskUserQuestion — match the language the user writes in.
"Structured / technical" is not an exemption. This hook lifts that recall to a
deterministic script-detection scan.

Detection: the prompt contains a meaningful amount of Cyrillic (≥ MIN_CYRILLIC
letters AND ≥ MIN_RATIO of its alphabetic characters), so a stray transliterated
word or a ticket key does not trip it. Advisory only: stdout (appended to the
model's context for the turn), exit 0, never blocks.
"""
from __future__ import annotations

import json
import re
import sys

CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
LATIN_RE = re.compile(r"[A-Za-z]")

MIN_CYRILLIC = 4      # at least this many Cyrillic letters
MIN_RATIO = 0.30      # Cyrillic as a share of all alphabetic chars


def is_cyrillic_prompt(prompt: str) -> bool:
    cyr = len(CYRILLIC_RE.findall(prompt))
    if cyr < MIN_CYRILLIC:
        return False
    lat = len(LATIN_RE.findall(prompt))
    total = cyr + lat
    if total == 0:
        return False
    return (cyr / total) >= MIN_RATIO


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    if not is_cyrillic_prompt(prompt):
        return 0

    print(
        "[language-reminder] The user is writing in Russian. Per CLAUDE.md § Instruction "
        "language: user-facing replies — including technical narratives, retrospectives, "
        "and every AskUserQuestion's question + option labels — must be in Russian. "
        "(Instruction-repo files stay English.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
