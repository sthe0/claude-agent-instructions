"""Resolve the working directory a `git`/`arc commit` command actually targets.

Difficulty removed: a hook that keys an enforcement/nudge decision off the
ambient session cwd misfires when the triggering command embeds its own
`cd <dir> &&` / `git -C <dir>` redirect — the command targets a DIFFERENT tree
than the session sits in (the standard isolated-worktree landing pattern). This
shared primitive parses that redirect so every consumer keys off the tree the
command really commits to. Two hooks need it: hook-guard-canon-readonly.py (its
original home, #44) and hook-readme-currency-reminder.py; extracting it here
gives one implementation and one test surface, so the rule cannot drift.
"""
from __future__ import annotations

import os
import shlex


def effective_git_cwd(command: str, payload_cwd: str) -> str:
    """The directory a `git commit` in `command` actually targets: the redirect
    the command itself selects (`git -C <dir> commit` or a leading `cd <dir> &&`
    / `cd <dir> ;`), or `payload_cwd` unchanged when the command has no such
    redirect. Best-effort: any parse doubt (or the harness's tracked shell cwd
    getting reset out from under a `cd`/`-C` the command actually issues) falls
    back to `payload_cwd`, never to a MORE permissive guess."""
    try:
        tokens = shlex.split(command)
    except Exception:
        return payload_cwd

    def _resolve(candidate: str) -> str:
        if not os.path.isabs(candidate):
            candidate = os.path.join(payload_cwd, candidate)
        return candidate

    for i in range(len(tokens) - 3):
        if (os.path.basename(tokens[i]) == "git" and tokens[i + 1] == "-C"
                and tokens[i + 3] == "commit"):
            return _resolve(tokens[i + 2])
    if len(tokens) >= 2 and tokens[0] == "cd":
        return _resolve(tokens[1])
    return payload_cwd
