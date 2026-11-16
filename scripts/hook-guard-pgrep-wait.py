#!/usr/bin/env python3
"""PreToolUse hook: deny a shell wait-loop whose condition is `pgrep -f`.

Difficulty removed: `until ! pgrep -f "<pattern>"; do sleep 5; done` never exits,
because `pgrep -f` matches the full command line of the waiter itself (the loop's
own text contains the pattern). The waiter hangs, no completion notification
arrives, and the session goes silent. The ban is already prose in
memory-global/leaves/long-job-monitoring.md (incident 2026-08-25); it was violated
again on 2026-11-16/17. The shape is decidable from the command text, so it is
mechanized here.

Deny when the Bash command contains a loop keyword (`while` / `until`) AND a
`pgrep` invocation whose flags include `-f` (or `--full`). `pgrep` without `-f`
matches the process name only, which the waiter's own command line does not
contain, so it is allowed; so is any wait that involves no loop.

Always exits 0 — a hook crash must never wedge the workflow; any unexpected error
falls through to allow.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import shlex
import sys

from lib import hook_telemetry

_LOOP_KEYWORDS = frozenset({"while", "until"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n"})


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _pgrep_full_match(tokens: list[str]) -> bool:
    """True if some `pgrep` invocation in the command passes -f / --full."""
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) != "pgrep":
            continue
        for arg in tokens[i + 1:]:
            if arg in _SEPARATORS:
                break
            if arg == "--full":
                return True
            if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
                return True
    return False


def _deny_msg() -> str:
    return (
        "Refusing a shell wait-loop on `pgrep -f`: the waiter's own command line "
        "contains the pattern, so `pgrep -f` always matches it and the loop never "
        "exits. Launch the job itself with Bash run_in_background=true (the "
        "completion notification arrives on exit), or capture `JOB=$!` after a "
        "backgrounded launch and wait with `scripts/wait-for-pid.sh <pid>`. See "
        "memory-global/leaves/long-job-monitoring.md (anti-patterns)."
    )


def decide(command: str) -> str | None:
    try:
        tokens = _tokens(command)
    except ValueError:
        return None
    if not any(tok in _LOOP_KEYWORDS for tok in tokens):
        return None
    if not _pgrep_full_match(tokens):
        return None
    return _deny_msg()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name", "") != "Bash":
        return 0

    tool_input = payload.get("tool_input") or {}
    command = (tool_input.get("command") or "").strip()
    if not command:
        return 0

    reason = decide(command)
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))

    return 0


if __name__ == "__main__":
    sys.exit(hook_telemetry.run(main, mechanism="hook-guard-pgrep-wait"))
