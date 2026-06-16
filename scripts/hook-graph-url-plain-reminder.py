#!/usr/bin/env python3
"""PreToolUse hook (Bash): when posting a comment (Tracker st-api or Arcanum
PR), nudge to put Nirvana graph/run URLs as PLAIN urls, not markdown-wrapped.

Difficulty (functional ground): Tracker *does* render markdown, but a plain
Nirvana graph URL (`nirvana.yandex-team.ru/.../graph`) gets a special
status-aware embed — the run's success/failure is visible inline. Wrapping it
as `[text](url)` loses that widget, so the reader can't see the run status at a
glance (recurred DEEPAGENT-433: the resolution comment markdown-wrapped both
run URLs). The body is usually read from a file (`$(cat …)`), so this fires on
the comment-post action as a reminder rather than parsing the payload.

Fires on: Tracker comment POST (`st-api…/issues/<key>/comments`) and
`arcanum-cli pr create-comment|update-comment`. Warn-only; always exits 0.
"""
from __future__ import annotations

import json
import re
import sys

POST_PATTERNS = [
    (re.compile(r"st-api\.yandex-team\.ru/v2/issues/[^/\s]+/comments"), "Tracker comment"),
    (re.compile(r"arcanum-cli\s+pr\s+(create-comment|update-comment)"), "Arcanum PR comment"),
]


def detect(command: str) -> str | None:
    for pat, label in POST_PATTERNS:
        if pat.search(command):
            return label
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    if not isinstance(command, str) or not command:
        return 0

    label = detect(command)
    if label is None:
        return 0

    print(
        f"hook-graph-url-plain-reminder: posting a {label}.\n"
        "  rule: put Nirvana graph/run URLs as PLAIN urls\n"
        "        (https://nirvana.yandex-team.ru/.../graph), NOT markdown\n"
        "        [text](url). Tracker / the review render a plain graph URL\n"
        "        with a status-aware widget (run success/failure visible\n"
        "        inline); markdown-wrapping loses it.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
