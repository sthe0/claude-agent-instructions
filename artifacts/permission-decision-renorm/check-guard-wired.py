"""Final-check helper for permission-decision-renorm: the guard must be LIVE in the running
settings chain, not merely present in the installer table.

`lib.hook_wiring.probe` returns a `Wiring` dataclass, which is always truthy — a check written as
`sys.exit(0 if probe(x) else 1)` can never fail, which is the exact defect class this plan's review
was called to catch. The verdict lives in `.status` ('wired' / 'absent' / 'unknown'), and the
matcher lives on each `Registration`, not in `.events`.

Exits 0 when the hook is wired on PreToolUse and Edit, Write and Bash are covered by the UNION of
its PreToolUse matchers. Coverage is a union rather than a single matcher because the plan's own
named exemplar, `hook-guard-canon-readonly.py`, is installed as two rows ("Edit|Write" and "Bash")
— demanding one combined row would fail an implementation that correctly follows the exemplar.
"""
import sys

sys.path.insert(0, "/Users/the0/claude-agent-instructions/scripts")
from lib.hook_wiring import probe  # noqa: E402

BASENAME = "hook-guard-permission-widening.py"
REQUIRED_TOOLS = ("Edit", "Write", "Bash")

w = probe(BASENAME)
if w.status != "wired":
    print(f"{BASENAME}: status={w.status} (expected wired); "
          f"unreadable={w.members_unreadable} unmodelled={w.members_unmodelled}")
    sys.exit(1)

pre = [r for r in w.registrations if r.event == "PreToolUse"]
matchers = [r.matcher or "" for r in pre]
missing = [t for t in REQUIRED_TOOLS if not any(t in m for m in matchers)]
if missing:
    print(f"{BASENAME}: wired, but PreToolUse coverage is missing {missing}; "
          f"matchers seen: {matchers}")
    sys.exit(1)

print(f"{BASENAME}: wired on PreToolUse, {REQUIRED_TOOLS} covered across "
      f"{len(pre)} registration(s): {matchers}")
