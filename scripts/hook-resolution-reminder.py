#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge the agent to ask for explicit resolution
when the user's prompt is brief gratitude — ambiguous between "thanks
for the work" and "task is over".

Safety net for the prose rule in CLAUDE.md § On task resolution:
the agent should close substantive tasks proactively when the plan's
`## Final verification` has passed (recap + explicit ask). If that
proactive close was missed and the user replies with bare gratitude,
this hook prevents the agent from treating "спасибо" / "thanks" as
silent confirmation.

Recurring failure mode this addresses (see experience leaves
2026-05-25 code-driven-enforcement-arc, 2026-05-26 cron-tz-user-
crontab-trap, 2026-05-26 plan-verify-loop): agent finishes work,
user thanks, agent closes without writing the experience leaf or
asking for resolution.

Match: prompt has ≤ MAX_WORDS tokens AND contains a gratitude keyword.
Detection is intentionally permissive — false positives (extra
reminder when the user is fine) are cheap; false negatives (silent
miss of a resolution gate) are expensive.

Exit 0 always; emit stdout (becomes additional system context).
"""
from __future__ import annotations

import json
import re
import sys

MAX_WORDS = 6

# Brief gratitude keywords across languages. Excludes "ok"/"good" (too
# common as mid-task acknowledgments) and "done" (often used by the
# agent / user about completing a sub-step, not the task).
GRATITUDE_RE = re.compile(
    r"\b(thanks|thank\s*you|thx|спасибо|спс|пасиба|merci|"
    r"perfect|идеально|отлично|cool|круто|super|супер|"
    r"great|превосходно|nice|wonderful|amazing|excellent|"
    r"окей|👍|🙏|❤️|💯|🎉)\b",
    re.IGNORECASE | re.UNICODE,
)
WORD_RE = re.compile(r"\w+", re.UNICODE)


def is_brief_gratitude(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if not is_brief_gratitude(prompt):
        return 0
    print(
        "[resolution-reminder] User prompt is brief gratitude — ambiguous "
        "between 'thanks for the work' and 'task is resolved'. Per "
        "CLAUDE.md § On task resolution, do NOT treat bare gratitude as "
        "confirmation. If the plan's Final verification has passed, close "
        "with a one-line recap (`Requested: X. Delivered: Y.`) and ask "
        "`Considered resolved?` explicitly. Otherwise continue the work."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
