#!/usr/bin/env python3
"""PostToolUse hook: nudge to invoke `self-improvement` after writing an
experience leaf with a substantive § Self-critique section.

Rule (CLAUDE.md § On task resolution § Auto-trigger self-improvement
from the self-critique): if the leaf's self-critique names concrete
agent-system friction, invoke `self-improvement` in the same turn. This
hook lifts the rule from prose-recall to a deterministic nudge.

Scope:
  - `tool_name == "Write"` AND `file_path` matches `**/experience/*.md`.
  - Reads the file post-write (it exists on disk).
  - Locates a heading containing "self-critique" / "self-критика" /
    variants (case-insensitive, multilingual).
  - Measures the section content (lines until the next ## or ###
    heading).
  - If the section's non-blank character count ≥ MIN_CHARS — emits a
    reminder to stderr.

Always exits 0 — failures in this hook never block the workflow.
PostToolUse stderr is surfaced back to the model as feedback.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

EXPERIENCE_PATH_RE = re.compile(r"(^|/)experience/[^/]+\.md$")
SELF_CRITIQUE_HEADING_RE = re.compile(
    r"^(#{2,})\s+.*?(self[-\s]?critique|self[-\s]?критик)",
    re.IGNORECASE | re.MULTILINE,
)
HEADING_RE = re.compile(r"^#{2,}\s", re.MULTILINE)

MIN_CHARS = 80  # non-blank character threshold; ~one short sentence


def section_after(text: str, start: int) -> str:
    """Return the lines after `start` up to (but not including) the next
    `##`/`###`/... heading."""
    next_m = HEADING_RE.search(text, start)
    end = next_m.start() if next_m else len(text)
    return text[start:end]


def find_self_critique(text: str) -> str | None:
    m = SELF_CRITIQUE_HEADING_RE.search(text)
    if not m:
        return None
    # Skip to end of the heading line.
    nl = text.find("\n", m.end())
    section_start = nl + 1 if nl != -1 else len(text)
    return section_after(text, section_start)


def is_substantive(section: str) -> bool:
    non_blank = [ln for ln in section.splitlines() if ln.strip()]
    chars = sum(len(ln) for ln in non_blank)
    return chars >= MIN_CHARS


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Write":
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path", "") or ""
    if not EXPERIENCE_PATH_RE.search(file_path):
        return 0

    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return 0

    section = find_self_critique(text)
    if section is None:
        return 0
    if not is_substantive(section):
        return 0

    print(
        "hook-self-critique-reminder: the experience leaf you just wrote\n"
        "  has substantive content in its § Self-critique section.\n"
        "  rule: CLAUDE.md § On task resolution § Auto-trigger\n"
        "        self-improvement from the self-critique.\n"
        "  action: invoke the `self-improvement` skill in this turn,\n"
        "          before the final user reply — treat the self-critique\n"
        "          as the input signal.\n"
        f"  leaf: {file_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
