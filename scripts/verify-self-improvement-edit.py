#!/usr/bin/env python3
"""Gate edits to the self-improvement skill files.

When any staged file lives under `skills/self-improvement/`, the commit
message must contain the literal marker `[self-improvement-reviewed]`.
The marker is the mechanical signal that the author (human or LLM)
consciously approved a change to the very skill that processes user
feedback — without it, a mid-session edit could silently change the
skill's behavior for the next invocation in the same conversation.

Invoked from the `commit-msg` hook with the message file path as argv[0].
Without argv[0], reads `.git/COMMIT_EDITMSG` (useful for testing).

Exit codes:
  0 — no self-improvement files staged, OR marker present
  1 — self-improvement files staged AND marker missing
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_PREFIX = "skills/self-improvement/"
MARKER = "[self-improvement-reviewed]"
DEFAULT_MSG_FILE = REPO_ROOT / ".git" / "COMMIT_EDITMSG"


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def read_message(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """Drop git comment lines (starting with `#`) — they are removed by git
    before the message is stored on the commit, so the marker must appear in
    the non-comment body."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    msg_path = Path(args[0]) if args else DEFAULT_MSG_FILE

    affected = [f for f in staged_files() if f.startswith(TARGET_PREFIX)]
    if not affected:
        return 0

    body = strip_comments(read_message(msg_path))
    if MARKER in body:
        print(f"verify-self-improvement-edit: ACK — marker present; commit edits {len(affected)} file(s):")
        for f in affected:
            print(f"  {f}")
        return 0

    print(f"verify-self-improvement-edit: FAIL — commit edits {len(affected)} self-improvement file(s):")
    for f in affected:
        print(f"  {f}")
    print()
    print(f"Self-improvement files require an explicit review marker because")
    print(f"editing them changes the skill that processes user feedback. Add")
    print(f"the literal marker")
    print(f"  {MARKER}")
    print(f"anywhere in the commit message body (not in a `#` comment line).")
    print()
    print(f"To bypass intentionally: `git commit --no-verify` (not recommended).")
    print()
    print(f"Diff preview ({', '.join(affected)}):")
    diff = subprocess.run(
        ["git", "diff", "--cached", "--", *affected],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    print(diff.stdout)
    return 1


if __name__ == "__main__":
    sys.exit(main())
