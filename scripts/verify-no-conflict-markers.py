#!/usr/bin/env python3
"""Reject committed git merge-conflict markers in tracked text files.

Difficulty (functional ground):
  A conflict marker (``<<<<<<<``, ``=======``, ``>>>>>>>``, ``|||||||``) left
  in a committed file corrupts the file silently — it parses as ordinary
  content, so an index / list / config keeps "looking fine" while it is
  broken. This bit origin/main this session: literal `git stash` markers
  landed in `memory-global/leaves/experience/MEMORY.md` and passed
  verify-all 13/13, because no verifier looked for them. This check makes the
  "no markers in a commit" invariant mechanical instead of relying on a human
  noticing.

Detection rule (low false-positive by construction):
  A line is a marker only at column 0, in the exact 7-character form git emits:
    - ours marker:   seven '<' then a space or end-of-line  (``<<<<<<< name``)
    - theirs marker: seven '>' then a space or end-of-line  (``>>>>>>> name``)
    - base marker:   seven '|' then a space or end-of-line  (diff3 base)
    - separator:     a line that is exactly seven '=' — flagged ONLY when the
      same file also carries an ours marker, so a 7-char markdown setext
      underline / horizontal rule on its own never trips the check.
  The marker literals in THIS file live inside regex strings (never at column
  0), so the verifier does not flag itself. A doc that must show a conflict
  example should keep the example indented / fenced-with-indent, or bypass
  with ``git commit --no-verify``.

Invocation modes (mirrors verify-experience-leaf.py):
  (no args)   Scan all tracked text files. Used by verify-all.py default mode.
  --staged    Check files staged for commit (pre-commit hook).
  --hook      PreToolUse mode: tool-input JSON on stdin; validates a Write
              call's content. Exit 2 (block + stderr) on a marker.
  <path>      Ad-hoc CLI check for a single file path.

Exit codes:
  0   OK / not applicable
  1   violation in (default) / --staged / <path> mode
  2   violation in --hook mode (blocks the Write tool call)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Built from char classes so no 7-char marker literal ever appears at column 0
# in this source (which would make the verifier flag itself).
OURS_RE = re.compile(r"^" + "<" * 7 + r"( |$)")
THEIRS_RE = re.compile(r"^" + ">" * 7 + r"( |$)")
BASE_RE = re.compile(r"^" + r"\|" * 7 + r"( |$)")
SEP_RE = re.compile(r"^" + "=" * 7 + r"$")


def check_content(content: str) -> str | None:
    """Return None if OK, else a human-readable error naming the line numbers."""
    lines = content.splitlines()
    has_ours = any(OURS_RE.match(ln) for ln in lines)
    hits: list[str] = []
    for i, ln in enumerate(lines, start=1):
        if OURS_RE.match(ln):
            hits.append(f"line {i}: ours marker")
        elif THEIRS_RE.match(ln):
            hits.append(f"line {i}: theirs marker")
        elif BASE_RE.match(ln):
            hits.append(f"line {i}: base marker")
        elif has_ours and SEP_RE.match(ln):
            hits.append(f"line {i}: conflict separator")
    if hits:
        return "git conflict marker(s) present — " + "; ".join(hits)
    return None


def _list_paths(mode: str) -> list[str]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def _read_blob(path: str, mode: str) -> tuple[str | None, str | None]:
    """Return (content, skip_reason). Binary / unreadable files are skipped."""
    if mode == "staged":
        blob = subprocess.run(
            ["git", "show", f":{path}"],
            capture_output=True, check=False,
        )
        if blob.returncode != 0:
            return None, "cannot read staged blob"
        try:
            return blob.stdout.decode("utf-8"), None
        except UnicodeDecodeError:
            return None, "binary"
    p = Path(path)
    if not p.exists():
        return None, "file not found on disk"
    try:
        return p.read_text(encoding="utf-8"), None
    except (UnicodeDecodeError, OSError):
        return None, "binary"


def _scan(mode: str) -> int:
    paths = _list_paths(mode)
    failed: list[str] = []
    for path in paths:
        content, skip = _read_blob(path, mode)
        if content is None:
            # Binary / unreadable: not a text file, nothing to check.
            continue
        err = check_content(content)
        if err:
            print(f"verify-no-conflict-markers: FAIL {path}: {err}")
            failed.append(path)
    if failed:
        print(
            f"\n{len(failed)} file(s) contain git conflict markers. Resolve the "
            f"conflict and remove every `<<<<<<<` / `=======` / `>>>>>>>` / "
            f"`|||||||` line before committing."
        )
        return 1
    print(f"verify-no-conflict-markers: OK — no markers ({mode} mode)")
    return 0


def cmd_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"verify-no-conflict-markers: hook input not valid JSON: {e}",
              file=sys.stderr)
        return 0  # fail open on parse error
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    if tool != "Write":
        return 0
    content = tool_input.get("content", "") or ""
    err = check_content(content)
    if err is not None:
        file_path = tool_input.get("file_path", "") or ""
        print(
            "verify-no-conflict-markers: BLOCK\n"
            f"  Write target: {file_path}\n"
            f"  reason: {err}\n"
            "  rule: a committed conflict marker corrupts the file silently.\n"
            "  recovery: remove the marker lines (keep the intended side of the\n"
            "            conflict) and retry the Write.\n",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_file(path_str: str) -> int:
    p = Path(path_str)
    if not p.exists():
        print(f"verify-no-conflict-markers: FAIL {p}: file not found", file=sys.stderr)
        return 1
    try:
        content = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        print(f"verify-no-conflict-markers: SKIP {p} (binary/unreadable)")
        return 0
    err = check_content(content)
    if err:
        print(f"verify-no-conflict-markers: FAIL {p}: {err}")
        return 1
    print(f"verify-no-conflict-markers: OK {p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true",
                       help="check files staged for commit (pre-commit / verify-all)")
    group.add_argument("--hook", action="store_true",
                       help="PreToolUse hook mode: JSON tool input on stdin")
    parser.add_argument("path", nargs="?", help="check one file (ad-hoc CLI)")
    args = parser.parse_args(argv)

    if args.hook:
        return cmd_hook()
    if args.path:
        return cmd_file(args.path)
    mode = "staged" if args.staged else "all"
    return _scan(mode)


if __name__ == "__main__":
    sys.exit(main())
