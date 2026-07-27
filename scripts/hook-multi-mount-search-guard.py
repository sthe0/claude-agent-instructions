#!/usr/bin/env python3
"""PreToolUse hook: deny recursive filesystem searches that span ≥2 FUSE mounts.

Some machines carry several network-backed FUSE mountpoints under the user's home
directory (a VCS virtual filesystem, a remote share). A recursive search rooted at
the home directory, ~, $HOME, or any ancestor of those mounts fans out across all of
them and is pathologically slow / hammers the network mount. The rule keys off the
`fuse.*` fstype alone, so it is VCS- and vendor-neutral: any FUSE mount counts.
This hook intercepts Bash (find/grep -r/rg/fd/ls -R), Grep, and Glob tool calls,
detects multi-mount fan-out, and DENY-signals any call that would cross ≥2 mounts.

Always exits 0 — a hook crash must never wedge the workflow. Any unexpected error,
missing key, or non-matching tool falls through to allow.

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

_RECURSIVE_ALWAYS = frozenset(["find", "rg", "fd"])
_GREP_VARIANTS = frozenset(["grep", "egrep", "fgrep", "zgrep"])


def fuse_mounts_from_text(text: str) -> list[str]:
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if not parts[2].startswith("fuse."):
            continue
        mountpoint = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
        if mountpoint.startswith("/home/"):
            mounts.append(mountpoint)
    return mounts


def fuse_mounts(proc_path: str = "/proc/self/mounts") -> list[str]:
    if not os.path.exists(proc_path):
        return []  # no /proc (e.g. macOS) -> no FUSE mounts to guard
    try:
        with open(proc_path, encoding="utf-8", errors="replace") as fh:
            return fuse_mounts_from_text(fh.read())
    except Exception:
        return []


def spans(root: str, mounts: list[str]) -> int:
    try:
        root = os.path.realpath(
            os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
        )
    except Exception:
        return 0
    norm = root.rstrip("/")
    return sum(1 for m in mounts if m == norm or m.startswith(norm + "/"))


def _deny_msg(root: str, n: int) -> str:
    return (
        f"This search is rooted at {root!r}, which spans {n} FUSE mounts under /home "
        f"(network-backed filesystems — recursive traversal is pathologically slow and hammers "
        f"the mount). Re-scope the search root to the specific repository or directory you "
        f"need (e.g. a path inside one project), not the home directory / ~ / $HOME."
    )


def _has_recursive_search(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()

    basenames = [os.path.basename(t) for t in tokens]

    if any(b in _RECURSIVE_ALWAYS for b in basenames):
        return True

    if any(b in _GREP_VARIANTS for b in basenames):
        for t in tokens:
            if t == "--recursive":
                return True
            if t.startswith("-") and not t.startswith("--"):
                flags = t.lstrip("-")
                if "r" in flags or "R" in flags:
                    return True

    if "ls" in basenames:
        for t in tokens:
            if t.startswith("-") and not t.startswith("--") and "R" in t.lstrip("-"):
                return True

    return False


def _extract_roots(command: str, cwd: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()

    roots = []
    for t in tokens:
        if t.startswith("/") or t.startswith("~") or t.startswith("$") or t == ".":
            expanded = os.path.expandvars(os.path.expanduser(t))
            if not os.path.isabs(expanded):
                expanded = os.path.join(cwd, expanded)
            try:
                roots.append(os.path.realpath(expanded))
            except Exception:
                roots.append(expanded)

    return roots if roots else [os.path.realpath(cwd)]


def decide(tool_name: str, tool_input: dict, cwd: str, mounts: list[str]) -> str | None:
    if not mounts:
        return None

    if tool_name in ("Grep", "Glob"):
        raw = tool_input.get("path") or cwd
        n = spans(raw, mounts)
        if n >= 2:
            resolved = os.path.realpath(
                os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
            )
            return _deny_msg(resolved, n)
        return None

    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        if not command or not _has_recursive_search(command):
            return None
        for root in _extract_roots(command, cwd):
            n = spans(root, mounts)
            if n >= 2:
                return _deny_msg(root, n)
        return None

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Bash", "Grep", "Glob"):
        return 0

    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()

    reason = decide(tool_name, tool_input, cwd, fuse_mounts())
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
