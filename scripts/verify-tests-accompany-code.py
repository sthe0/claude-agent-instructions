#!/usr/bin/env python3
"""commit-msg advisory: warn when staged code carries no accompanying test.

Rule (CLAUDE.md delegation table + memory leaf tests-accompany-code): any
change that writes or modifies behavior ships, in the same change, with tests
that verify it. This hook is the soft mechanical backstop — it *warns*, it
never blocks (the real gate is the reviewer). A commit whose staged set
includes a non-test `scripts/**.py` change but no test delta under the tests
directory gets a one-line nudge on stderr.

Escape: a `[skip-test-guard: <reason>]` trailer anywhere in the commit message
suppresses the warning, for the named non-testable class (pure rename/move,
docs, config with no logic, or a fix whose trigger cannot be reached — state
the reason). The reason is for the human reading the log, not parsed.

Invocation:
  verify-tests-accompany-code.py <commit-msg-file>   (commit-msg hook; $1)
  verify-tests-accompany-code.py                      (no message → no escape)

Exit code is always 0 — this hook is advisory and must not block a commit.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ESCAPE_RE = re.compile(r"\[skip-test-guard:", re.IGNORECASE)


def is_code(path: str) -> bool:
    """A non-test Python source file under the instructions-repo scripts/ tree."""
    if not path.startswith("scripts/") or not path.endswith(".py"):
        return False
    return not is_test(path)


def is_test(path: str) -> bool:
    base = os.path.basename(path)
    return "/tests/" in path or base.startswith("test_")


def evaluate(staged: list[str], message: str) -> str | None:
    """Return a warning string if code is staged without a test, else None.

    Pure — the side-effect-free core the tests drive directly.
    """
    if ESCAPE_RE.search(message or ""):
        return None
    code = [p for p in staged if is_code(p)]
    if not code:
        return None
    if any(is_test(p) for p in staged):
        return None
    preview = ", ".join(code[:5]) + (" …" if len(code) > 5 else "")
    return (
        "[tests-accompany-code] Staged code change has no accompanying test delta:\n"
        f"  {preview}\n"
        "Per leaf tests-accompany-code: any behavioral change ships with a test that\n"
        "verifies it (for a fix: red-before / green-after). Add a test under scripts/tests/,\n"
        "or, for a genuinely non-testable change (pure rename/move, docs, config), add a\n"
        "  [skip-test-guard: <reason>]\n"
        "trailer to the commit message. Advisory only — this does not block the commit."
    )


def _staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=False,
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    message = ""
    if argv:
        try:
            with open(argv[0], encoding="utf-8") as fh:
                message = fh.read()
        except OSError:
            message = ""
    warning = evaluate(_staged_files(), message)
    if warning:
        print(warning, file=sys.stderr)
    return 0  # always advisory


if __name__ == "__main__":
    sys.exit(main())
