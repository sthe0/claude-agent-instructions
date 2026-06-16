#!/usr/bin/env python3
"""PreToolUse hook (Bash): nudge to post each verified manual run as a PR
review comment when an Arcanum PR is created or published.

Difficulty (functional ground): the rule "after a manual test run reaches a
terminal state, post its result + plain-URL link + rerun command as a PR
*review comment* (not only in the PR description or the ticket)" is fully
written — project leaf `feedback-vcs-and-review.md` plus an active imperative
in the project MEMORY.md always-loaded index — yet it is reliably forgotten at
the moment the PR is touched (DEEPAGENT-414: runs during open review;
DEEPAGENT-433: runs before PR creation — both missed). Text reinforcement
failed twice, so a mechanical nudge fires at the detectable PR event where the
miss happens. `arc pr create` covers the run-before-PR case; `arc pr publish`
covers the runs-during-open-review / republish case.

Warn-only by design (PR ops are rare; false-block cost > false-pass cost).
Always exits 0.
"""
from __future__ import annotations

import json
import re
import sys

PR_PATTERNS = [
    (re.compile(r"\barc\s+pr\s+create\b"), "arc pr create"),
    (re.compile(r"\barc\s+pr\s+publish\b"), "arc pr publish"),
]


def detect(command: str) -> str | None:
    for pat, label in PR_PATTERNS:
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
        f"hook-pr-run-comment-reminder: about to run {label}.\n"
        "  rule (feedback-vcs-and-review.md): for EVERY manual run you\n"
        "        verified this session (Nirvana WI / smoke / CI / script),\n"
        "        post a PR *review comment* with the run result, a\n"
        "        plain-URL link, and the exact rerun command — not only in\n"
        "        the PR description or the ticket.\n"
        "  mechanism: arcanum-cli pr create-comment <prId> --content \"...\"\n"
        "  why: the reviewer (and future you) must reproduce/locate the run\n"
        "       from the review thread without digging.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
