#!/usr/bin/env python3
"""PreToolUse hook: keep the serving/PRIMARY Core-instructions checkout on main.

Difficulty removed: the serving checkout (~/claude-agent-instructions — the tree
settings.json hook commands point at) is the PRIMARY git worktree, so its
checked-out branch IS the live hook code every session on this machine runs.
Doing feature work directly there on a non-default branch (a) makes live hooks
run stale/experimental code for ALL sessions and (b) contaminates the shared
working tree for parallel sessions. A once-per-session WARNING (check_branch in
hook-instructions-refresh-due.py) did not prevent it; this gate does.

Decidable from git state: DENY an Edit/Write (or a `git commit`) whose target
lies in the Core repo's PRIMARY worktree when that worktree's HEAD is off the
default branch — redirect to a linked `git worktree add`. Everything else is
ALLOWED (fail-open): a linked worktree, on-main, detached HEAD, writes under
memory-global/, paths outside the Core repo, and any git error. Always exits 0 —
a hook crash must never wedge the workflow.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout (mirrors
hook-guard-destructive-rm.py):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_BRANCH = "main"
GIT_TIMEOUT_S = 3


def _core_root() -> Path:
    return Path(os.environ.get("CLAUDE_INSTRUCTIONS_REPO", str(Path.home() / "claude-agent-instructions")))


def _nearest_existing_dir(path: str) -> str | None:
    """The nearest existing ancestor directory of `path` (which may not exist yet
    for a Write creating a new file), or None if none resolves."""
    p = Path(path)
    if not p.is_absolute():
        return None
    cur = p if p.is_dir() else p.parent
    while True:
        if cur.is_dir():
            return str(cur)
        if cur.parent == cur:
            return None
        cur = cur.parent


def _git_info(cwd: str):
    """(toplevel, git_dir_abs, git_common_dir_abs, branch) for `cwd`, or None on any
    failure. Relative git-dir / git-common-dir are resolved against `cwd`."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse",
             "--show-toplevel", "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.splitlines()
    if len(lines) < 4:
        return None
    toplevel, git_dir, git_common_dir, branch = lines[0], lines[1], lines[2], lines[3]
    git_dir_abs = os.path.realpath(os.path.join(cwd, git_dir))
    git_common_abs = os.path.realpath(os.path.join(cwd, git_common_dir))
    return os.path.realpath(toplevel), git_dir_abs, git_common_abs, branch


def _is_primary_core_offmain(target_dir: str) -> tuple[bool, str]:
    """(deny?, branch). True only when target_dir resolves to the PRIMARY worktree
    of the Core repo AND its HEAD is off the default branch. Fail-open (False) on
    any ambiguity: git error, linked worktree, detached HEAD ('HEAD'), on-main,
    or a toplevel other than the Core repo root."""
    info = _git_info(target_dir)
    if info is None:
        return False, ""
    toplevel, git_dir_abs, git_common_abs, branch = info
    if toplevel != os.path.realpath(str(_core_root())):
        return False, branch  # not the Core repo (or a linked worktree, whose toplevel differs)
    if git_dir_abs != git_common_abs:
        return False, branch  # linked worktree of the Core repo — off-main edits are the point
    if branch in (DEFAULT_BRANCH, "HEAD", ""):
        return False, branch  # on default, detached, or unknown — fail-open
    return True, branch


def _is_memory_exempt(file_path: str) -> bool:
    """Writes under the Core repo's memory-global/ are exempt: the difficulty is
    hook-code determinism, not memory content, and memory recording must never be
    blocked by a process gate (mirrors the agentctl production-gate exemption)."""
    mem_root = os.path.realpath(str(_core_root() / "memory-global"))
    target = os.path.realpath(file_path)
    return target == mem_root or target.startswith(mem_root + os.sep)


def _is_git_commit(command: str) -> bool:
    """True iff the command runs `git commit` (tokenized, not substring). Any
    parse doubt => False (allow)."""
    try:
        tokens = shlex.split(command)
    except Exception:
        return False
    for i in range(len(tokens) - 1):
        if os.path.basename(tokens[i]) == "git" and tokens[i + 1] == "commit":
            return True
    return False


def _deny_msg(toplevel: str, branch: str) -> str:
    return (
        f"Refusing to modify the serving/PRIMARY Core-instructions checkout ({toplevel}) "
        f"while it is on branch '{branch}', not '{DEFAULT_BRANCH}'. This checkout is the live "
        f"hook code for every session on the machine and must stay on '{DEFAULT_BRANCH}'. "
        f"Do feature work in a linked worktree instead: "
        f"`git worktree add -b <branch> <path> origin/{DEFAULT_BRANCH}` and edit/commit there. "
        f"On-main direct edits, edits inside a linked worktree, and writes under memory-global/ "
        f"are allowed."
    )


def decide(payload: dict) -> str | None:
    """Return a deny reason, or None to allow. Fail-open on any unexpected shape."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        if not command or not _is_git_commit(command):
            return None
        cwd = payload.get("cwd") or os.getcwd()
        target_dir = _nearest_existing_dir(cwd)
        if target_dir is None:
            return None
        deny, branch = _is_primary_core_offmain(target_dir)
        if deny:
            return _deny_msg(os.path.realpath(str(_core_root())), branch)
        return None

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if not os.path.isabs(file_path):
        return None  # relative path — not resolvable to a specific checkout, fail-open
    if _is_memory_exempt(file_path):
        return None
    target_dir = _nearest_existing_dir(file_path)
    if target_dir is None:
        return None
    deny, branch = _is_primary_core_offmain(target_dir)
    if deny:
        return _deny_msg(os.path.realpath(str(_core_root())), branch)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        reason = decide(payload)
    except Exception:
        return 0

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
