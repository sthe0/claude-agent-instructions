#!/usr/bin/env python3
"""PreToolUse hook: deny a recursive `rm` that could wipe the agent's own critical dirs.

Difficulty removed: a destructive `rm -rf` built with an interpolated path variable
that turns out empty collapses to a parent critical path and silently deletes the
agent's memory/config. Concretely, `rm -rf "$HOME/.claude/projects/$HASH"` with an
empty $HASH becomes `rm -rf "$HOME/.claude/projects/"` and wipes all auto-memory and
transcripts. A prose "be careful" rule did not prevent this; a mechanical gate does.

The gate is decidable from the command text: fire on a recursive `rm`, then for each
target argument simulate the WORST CASE (every unknown `$VAR` expands to empty) and
deny if the resulting path is — or is an ancestor of, or lies inside — a protected
critical directory: `/`, `$HOME`, `$HOME/.claude` (+ subpaths), `$HOME/claude-agent-instructions`.

Narrow by design: legitimate cleanup of mktemp / build / project paths is untouched;
only the agent's own root/home/config trees are protected. Always exits 0 — a hook
crash must never wedge the workflow; any unexpected error falls through to allow.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

_VAR_RE = re.compile(r"\$\{?\w+\}?")


# Two protection tiers:
#   TREE  — deny if the target equals, lies INSIDE, or is an ancestor of the dir
#           (the agent's own config/memory + the instruction repo: never rm -rf'd).
#   POINT — deny only if the target equals or is an ancestor of the dir
#           (/ and $HOME: legitimate deletes happen *inside* $HOME, so "inside"
#           must NOT match, only deleting $HOME itself or a parent of it).
def _protected_trees(home: str) -> list[str]:
    home = home.rstrip("/") or "/"
    return [os.path.join(home, ".claude"), os.path.join(home, "claude-agent-instructions")]


def _protected_points(home: str) -> list[str]:
    return ["/", home.rstrip("/") or "/"]


def _worst_case_path(token: str, home: str) -> str | None:
    """Normalize a target token, simulating every unknown $VAR expanding to empty.

    Returns an absolute, normalized path, or None if it cannot be resolved to one.
    """
    # ~ and known env vars expand normally; unknown $VAR -> '' (the collapse case).
    expanded = os.path.expanduser(token)
    expanded = os.path.expandvars(expanded)
    expanded = _VAR_RE.sub("", expanded)  # strip any remaining $VAR -> empty
    if not expanded:
        return None
    if not os.path.isabs(expanded):
        return None  # cwd-relative targets are not agent-critical dirs
    return os.path.normpath(expanded)


def _is_protected(path: str, home: str) -> bool:
    path = path.rstrip("/") or "/"
    for crit in _protected_trees(home):
        crit = crit.rstrip("/")
        if path == crit or path.startswith(crit + "/"):
            return True  # equals or lies inside the config/repo tree
        if crit.startswith(path + "/"):
            return True  # target is an ancestor of the tree (deleting it wipes it)
    for crit in _protected_points(home):
        crit = crit.rstrip("/") or "/"
        if path == crit:
            return True  # deleting / or $HOME itself
        if crit != "/" and crit.startswith(path + "/"):
            return True  # target is an ancestor of $HOME
    return False


def _rm_recursive_targets(command: str) -> list[str]:
    """If the command runs a recursive `rm`, return its target (non-flag) arguments.

    Returns [] when there is no recursive rm. Handles `-rf`, `-fr`, `-r -f`,
    `--recursive`, `-R`, and `command rm` / `/bin/rm` forms.
    """
    try:
        tokens = shlex.split(command)
    except Exception:
        return []

    targets: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        if os.path.basename(tokens[i]) == "rm":
            j = i + 1
            recursive = False
            args: list[str] = []
            while j < n:
                t = tokens[j]
                # stop at a shell separator token that shlex kept (rare); rm segment ends
                if t in (";", "&&", "||", "|", "&"):
                    break
                if t == "--":
                    j += 1
                    while j < n and tokens[j] not in (";", "&&", "||", "|", "&"):
                        args.append(tokens[j])
                        j += 1
                    break
                if t == "--recursive":
                    recursive = True
                elif t.startswith("--"):
                    pass  # other long option, ignore
                elif t.startswith("-") and len(t) > 1:
                    flags = t.lstrip("-")
                    if "r" in flags or "R" in flags:
                        recursive = True
                else:
                    args.append(t)
                j += 1
            if recursive:
                targets.extend(args)
            i = j
            continue
        i += 1
    return targets


def _deny_msg(token: str, resolved: str) -> str:
    return (
        f"Refusing a recursive rm whose target {token!r} resolves (worst-case, with any "
        f"empty variable) to {resolved!r} — a protected path (/, $HOME, ~/.claude, or the "
        f"instruction repo). An interpolated path variable that is empty collapses to a "
        f"parent critical dir and would wipe the agent's own memory/config. Guard every "
        f"variable is non-empty (e.g. [[ -n \"$VAR\" ]]), delete literal paths, or use "
        f"trap-cleanup on the exact mktemp path you captured at creation."
    )


def decide(command: str, home: str) -> str | None:
    targets = _rm_recursive_targets(command)
    if not targets:
        return None
    for token in targets:
        resolved = _worst_case_path(token, home)
        if resolved and _is_protected(resolved, home):
            return _deny_msg(token, resolved)
    return None


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

    home = os.environ.get("HOME") or os.path.expanduser("~")
    reason = decide(command, home)
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
    sys.exit(main())
