#!/usr/bin/env python3
"""Gate: no org-internal identifiers in text destined for a PUBLIC venue.

The Core repo (sthe0/claude-agent-instructions) and its GitHub Issues are
public: filing there is publication. Per the 3-tier queue model
(memory-global/leaves/instruction-dev-queues.md), the Core tier carries only
org-neutral content; org-specific halves go to the org-internal backlog.
Run every issue/PR/commit body through this check BEFORE posting - checking
the live artifact after publication re-creates the exposure (watcher e-mails
are sent at creation time and are irrecoverable).

Usage:
    check-org-neutral.py <file>     # or '-' for stdin
Exit 0 = clean; exit 1 = markers found (printed one per line).
"""
import re
import sys

# Case-insensitive; \b guards where a bare substring would false-positive
# (e.g. 'gena' inside 'general'). Extend when a new leak class is found.
MARKERS = [
    r"\bgena\b",
    r"\bt-run\b",
    r"theya",
    r"auto-solve",
    r"ccgram",
    r"telegram",
    r"startrek",
    r"yandex",
    r"junk/the0",
    r"ooseven",
    r"arcadia",
    r"arcanum",
    r"\bnirvana\b",
    r"deepagent",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    text = sys.stdin.read() if sys.argv[1] == "-" else open(sys.argv[1], encoding="utf-8").read()
    hits = []
    for pattern in MARKERS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            line_no = text.count("\n", 0, m.start()) + 1
            hits.append(f"{pattern}: line {line_no}: ...{text[max(0, m.start() - 30):m.end() + 30]!r}...")
    if hits:
        print("ORG-INTERNAL MARKERS FOUND (do not publish):")
        print("\n".join(hits))
        return 1
    print("clean: no org-internal markers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
