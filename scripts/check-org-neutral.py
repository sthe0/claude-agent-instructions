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
    check-org-neutral.py <file>     # or '-' for stdin
Exit 0 = clean; exit 1 = markers found (printed one per line).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import config_root  # noqa: E402
from lib import term_ruleset as tr  # noqa: E402

REPO_ROOT = SCRIPTS_DIR.parent


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    text = sys.stdin.read() if argv[0] == "-" else open(argv[0], encoding="utf-8").read()

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
