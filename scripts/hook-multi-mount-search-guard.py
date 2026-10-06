#!/usr/bin/env python3
"""PreToolUse hook: deny recursive filesystem traversal rooted at, or above, a FUSE mount.

Some machines carry network-backed FUSE mountpoints under the user's home directory
(a VCS virtual filesystem, a remote share). Such a filesystem fetches lazily and caches
what it reads into a local backing store, so a whole-tree walk -- not only a write --
can exhaust the local disk, and it hammers the network mount. The rule keys off the
`fuse.*` fstype alone, so it is VCS- and vendor-neutral: any FUSE mount counts.

The hook intercepts Bash, Grep and Glob calls and DENY-signals a traversal whose root
is a mount point itself, or a directory above one or more mounts (the home directory,
~, $HOME, a parent of a mount). A root strictly inside a mount is allowed, as are
non-recursive commands and `fuser -m <path>` (lists holders without walking the tree).

Bash detection: find, rg, fd, du (any form), grep-family with -r/-R/--recursive,
ls -R, and lsof +D (plain or glued `+D<dir>`). The command is split into segments on
&& || ; | & and unquoted newlines with a running cwd (a bare `cd` goes to ~) (`cd X && ...`), redirections are stripped, and each
recursive segment's roots are taken from its own positional arguments (the grep/rg/fd
pattern is never a root); with no root the segment's cwd is the root.

Known residuals (accepted): unexpanded globs, `(cd X && ...)` and `bash -c '...'`
are not segmented, `$(...)`, `grep -d recurse`, other walkers (tree, tar, rsync,
cp -r, git grep), rg-only value options missing from the grep arity table (`-j`,
`-M`). Accepted false positives: bounded-depth walks at a mount root, a command name
or a path ending in one appearing as an argument (`which du`, `src/find`), heredoc
bodies tokenised as arguments, `rg --files robot`, Glob without a path at a mount-root
cwd.

Always exits 0 -- a hook crash must never wedge the workflow. Any unexpected error,
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

_RECURSIVE_ALWAYS = frozenset(["find", "rg", "fd", "du"])
_GREP_VARIANTS = frozenset(["grep", "egrep", "fgrep", "zgrep"])
_SEGMENT_OPS = frozenset(["&&", "||", ";", ";;", "|", "|&", "&"])
_END = "\0"
_FAMILY = {"grep": "grep", "egrep": "grep", "fgrep": "grep", "zgrep": "grep",
           "rg": "grep", "fd": "fd"}
_PATTERN_FIRST = frozenset(["grep", "fd"])
_VALUE_SHORT = {"grep": frozenset("efgtTABCmd"), "fd": frozenset("etEd")}
_VALUE_LONG = {
    "grep": frozenset(["--regexp", "--file", "--glob", "--type", "--type-not",
                       "--max-count", "--max-depth", "--include", "--exclude"]),
    "fd": frozenset(["--extension", "--type", "--exclude", "--max-depth"]),
}
_FIND_GLOBALS = frozenset(["-L", "-H", "-P"])


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
    if n >= 2:
        return (
            f"This search is rooted at {root!r}, which spans {n} FUSE mounts under /home "
            f"(network-backed filesystems — recursive traversal is pathologically slow and hammers "
            f"the mount). Re-scope the search root to the specific repository or directory you "
            f"need (e.g. a path inside one project), not the home directory / ~ / $HOME."
        )
    return (
        f"This recursive traversal is rooted at {root!r}, which is (or contains) a FUSE mount "
        f"under /home. Walking a lazily-fetched filesystem materialises data into the local "
        f"backing store and can exhaust the disk. Re-scope to a subdirectory inside the mount; "
        f"to see which processes hold the mount use `fuser -m {root}`."
    )


def _tokenize(command: str) -> list[str]:
    # An unquoted newline separates commands, so it is punctuation, not whitespace.
    lex = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


def _is_separator(tok: str) -> bool:
    if tok in _SEGMENT_OPS:
        return True
    rest = tok.replace("\n", "")
    return "\n" in tok and (not rest or rest in _SEGMENT_OPS)


def _is_redirect(tok: str) -> bool:
    return (bool(tok) and set(tok) <= set("<>&|")
            and ("<" in tok or ">" in tok))


def _strip_redirects(tokens: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for t in tokens:
        if skip:
            skip = False
            continue
        if _is_redirect(t):
            if out and out[-1].isdigit():
                out.pop()
            skip = True
            continue
        out.append(t)
    return out


def _resolve(tok: str, cwd: str) -> str:
    p = os.path.expandvars(os.path.expanduser(tok))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(os.path.abspath(p))


def _after_cd(tokens: list[str], cwd: str) -> str:
    if not tokens or tokens[0] != "cd":
        return cwd
    args = [a for a in tokens[1:] if a == "-" or not a.startswith("-")]
    if args and args[0] == "-":
        return cwd
    try:
        return _resolve(args[0] if args else "~", cwd)
    except Exception:
        return cwd


def _segments(command: str, cwd: str) -> list[tuple[list[str], str]]:
    out: list[tuple[list[str], str]] = []
    cur: list[str] = []
    seg_cwd = cwd
    for t in _tokenize(command) + [_END]:
        if t == _END or _is_separator(t):
            if cur:
                out.append((_strip_redirects(cur), seg_cwd))
                seg_cwd = _after_cd(cur, seg_cwd)
            cur = []
        else:
            cur.append(t)
    return out


def _recursive_command(tokens: list[str]) -> tuple[str, int] | None:
    for i, t in enumerate(tokens):
        b = os.path.basename(t)
        rest = tokens[i + 1:]
        if b in _RECURSIVE_ALWAYS:
            return b, i
        if b == "lsof" and any(a.startswith("+D") for a in rest):
            return b, i
        if b in _GREP_VARIANTS:
            for a in rest:
                if a == "--recursive":
                    return b, i
                if a.startswith("-") and not a.startswith("--"):
                    flags = a.lstrip("-")
                    if "r" in flags or "R" in flags:
                        return b, i
        if b == "ls":
            for a in rest:
                if a.startswith("-") and not a.startswith("--") and "R" in a.lstrip("-"):
                    return b, i
    return None


def _takes_value(cmd: str, tok: str) -> bool:
    if cmd not in _VALUE_SHORT:
        return False
    if tok.startswith("--"):
        return tok in _VALUE_LONG[cmd]
    return tok[-1] in _VALUE_SHORT[cmd]


def _supplies_pattern(cmd: str, tok: str) -> bool:
    if cmd != "grep":
        return False
    if tok.startswith("--"):
        return tok.split("=")[0] in ("--regexp", "--file")
    return tok[-1] in "ef"


def _positionals(cmd: str, args: list[str]) -> list[str]:
    if cmd == "find":
        i = 0
        while i < len(args) and args[i] in _FIND_GLOBALS:
            i += 1
        out = []
        for t in args[i:]:
            if t.startswith(("-", "(", "!", ",")):
                break
            out.append(t)
        return out

    positionals: list[str] = []
    pattern_given = False
    opts_done = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if opts_done or tok == "-" or not tok.startswith(("-", "+")):
            positionals.append(tok)
            continue
        if tok == "--":
            opts_done = True
            continue
        if tok.startswith("+D") and len(tok) > 2:
            positionals.append(tok[2:])
            continue
        if _supplies_pattern(cmd, tok):
            pattern_given = True
        if _takes_value(cmd, tok):
            i += 1
    if cmd in _PATTERN_FIRST and not pattern_given:
        positionals = positionals[1:]
    return positionals


def _is_root_candidate(tok: str, cwd: str) -> bool:
    if tok.startswith(("/", "~", "$")) or tok in (".", ".."):
        return True
    return os.path.exists(os.path.join(cwd, tok))


def _extract_roots(tokens: list[str], found: tuple[str, int], cwd: str) -> list[str]:
    name, idx = found
    cmd = _FAMILY.get(name, name)
    roots = [_resolve(t, cwd) for t in _positionals(cmd, tokens[idx + 1:])
             if _is_root_candidate(t, cwd)]
    return roots if roots else [_resolve(cwd, cwd)]


def decide(tool_name: str, tool_input: dict, cwd: str, mounts: list[str]) -> str | None:
    if not mounts:
        return None

    if tool_name in ("Grep", "Glob"):
        raw = tool_input.get("path") or cwd
        n = spans(raw, mounts)
        if n >= 1:
            resolved = os.path.realpath(
                os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
            )
            return _deny_msg(resolved, n)
        return None

    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        if not command:
            return None
        try:
            segments = _segments(command, cwd)
        except Exception:
            segments = [(command.split(), cwd)]
        for tokens, seg_cwd in segments:
            found = _recursive_command(tokens)
            if not found:
                continue
            for root in _extract_roots(tokens, found, seg_cwd):
                n = spans(root, mounts)
                if n >= 1:
                    return _deny_msg(root, n)
        return None

    return None


def _run() -> int:
    payload = json.load(sys.stdin)

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


def main() -> int:
    try:
        return _run()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
