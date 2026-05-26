#!/usr/bin/env python3
"""PreToolUse hook (Bash): nudge to verify user push-confirmation before
running `git push` or `sync-instructions-repo.sh push`.

Rule (CLAUDE.md / policy.md § Git sync § After editing): push only after
the user explicitly confirms. The existing `githooks/post-commit` runs
*after* a commit lands — useful as a reminder but does not intercept
the push call itself. This hook fires *before* the push and reminds the
agent that the user-confirmation gate must already be satisfied.

Warn-only by design (per project preference; see
memory-global/leaves/feedback-no-hard-caps-on-memory.md for the broader
"soft control over hard blocks" stance — the same logic applies here:
push is a rare action, false-block cost > false-pass cost). Always
exits 0.

Detection (regex; word-bounded so `git config push.default …` and
similar do not trigger):

  - `\\bgit\\s+push\\b`
  - `\\bsync-instructions-repo\\.sh\\s+(push|sync)\\b`
"""
from __future__ import annotations

import json
import re
import sys

PUSH_PATTERNS = [
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\bsync-instructions-repo\.sh\s+(push|sync)\b"),
     "sync-instructions-repo.sh push/sync"),
]


def detect(command: str) -> str | None:
    for pat, label in PUSH_PATTERNS:
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
        f"hook-push-confirmation-reminder: about to run {label}.\n"
        "  rule: CLAUDE.md / policy.md § Git sync § After editing —\n"
        "        push only after the user explicitly confirms.\n"
        "  action: verify the user's most recent message carries an\n"
        "          explicit push confirmation (e.g. 'да', 'push',\n"
        "          'подтверждаю', 'опубликуй'). If not — abort, ask\n"
        "          the user first.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
