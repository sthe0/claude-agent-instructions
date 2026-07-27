#!/usr/bin/env python3
"""Gate: no org-internal identifiers in text destined for a PUBLIC venue.

The Core repo (sthe0/claude-agent-instructions) and its GitHub Issues are
public: filing there is publication. Per the 3-tier queue model
(memory-global/leaves/instruction-dev-queues.md), the Core tier carries only
org-neutral content; org-specific halves go to the org-internal backlog.
Run every issue/PR/commit body through this check BEFORE posting - checking
the live artifact after publication re-creates the exposure (watcher e-mails
are sent at creation time and are irrecoverable).

Core carries no denylist of its own — the actual org-internal terms come
from a machine-local term ruleset (see ``lib/term_ruleset.py``). With no
ruleset discovered (a foreign clone with none installed), every input is
reported clean; this deliberately fails open on missing config while still
failing closed on any real hit.

Usage:
    check-org-neutral.py [--commit-msg] <file>   # or '-' for stdin

``--commit-msg`` first reduces the input to what git would keep as the log
message, so a caller gating a commit scans the message and nothing else.

Exit codes are a three-way contract, and every caller must keep the hit and
the error apart: a caller that treats any non-zero as a hit turns an
unrelated checker failure into a bogus "org-internal term found" refusal.
    0 = clean (also: no ruleset installed, the documented fail-open)
    1 = markers found, printed one per line
    2 = the checker itself failed (bad usage, unreadable file, broken ruleset)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import config_root  # noqa: E402
from lib import term_ruleset as tr  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent

# git's own scissors marker (`git commit --cleanup=scissors`, and what
# `commit -v` inserts): everything from this line down is the verbatim diff
# git appends for review, never part of the message.
SCISSORS_BODY = "------------------------ >8 ------------------------"


def _comment_char() -> str:
    """git's core.commentChar for the repo we are standing in. 'auto' picks a
    char per message, which we cannot reproduce here — treat it as the default."""
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "core.commentChar"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "#"
    value = proc.stdout.strip() if proc.returncode == 0 else ""
    return value if len(value) == 1 else "#"


def strip_commit_message_cruft(text: str, comment_char: str = "#") -> str:
    """Reduce a commit-message file to the text git would keep as the message.

    `git commit -v` appends the whole staged diff below the scissors line, so
    scanning the raw file makes a term the diff *removes* look like a term the
    message *introduces* — the gate would block the very commit that deletes it.
    """
    kept: list[str] = []
    for line in text.splitlines():
        if line.rstrip() == f"{comment_char} {SCISSORS_BODY}":
            break
        if not line.startswith(comment_char):
            kept.append(line)
    return "\n".join(kept)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    commit_msg = "--commit-msg" in argv
    positional = [a for a in argv if a != "--commit-msg"]
    if len(positional) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        text = (
            sys.stdin.read() if positional[0] == "-"
            else open(positional[0], encoding="utf-8").read()
        )
    except OSError as exc:
        print(f"error reading input: {exc}", file=sys.stderr)
        return 2
    if commit_msg:
        text = strip_commit_message_cruft(text, _comment_char())

    try:
        rulesets = tr.discover_rulesets(
            agent_home=config_root.agent_home(),
            project_dir=REPO_ROOT,
            guarded_repo_root=REPO_ROOT,
        )
    except tr.RulesetError as exc:
        print(f"error loading term ruleset: {exc}", file=sys.stderr)
        return 2

    if not rulesets:
        print("clean: no term ruleset installed (0 rulesets discovered)")
        return 0

    hits = tr.scan(text, rulesets)
    if hits:
        print("ORG-INTERNAL MARKERS FOUND (do not publish):")
        for h in hits:
            print(f"  {h.format()}")
        return 1
    print("clean: no org-internal markers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
