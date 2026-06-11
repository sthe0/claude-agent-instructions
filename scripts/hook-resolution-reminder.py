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

Recurring failure mode this addresses (see experience leaf
2026-05-25-resolution-gate-confirm-before-record): agent finishes
work, user thanks, agent closes without writing the experience leaf
or asking for resolution.

Matches either:
  (a) Brief gratitude: ≤ MAX_WORDS tokens AND a gratitude keyword.
  (b) Resolution meta-question: ≤ META_MAX_WORDS tokens AND a gratitude
      keyword AND a meta-keyword about asking / being resolved / done.
      Catches prompts like "спасибо, почему не спрашиваеш решена ли
      задача?" where the user explicitly reminds the agent of the gate
      but the prompt is too long for (a).

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
META_MAX_WORDS = 20

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
# Meta-keywords signaling a question about the resolution gate itself
# (the user pointing at "why didn't you ask if it's done?"). Paired with
# a gratitude keyword to keep the false-positive rate low.
META_RE = re.compile(
    r"(спрашивае(ш|шь)|спросил[аи]?|почему\s+не|реш(ен|ён)а?|"
    r"закрыт[аоы]?|готов[оаы]?|закры|ask(ed|ing)?|"
    r"why\s+(didn'?t|not|haven'?t|aren'?t|don'?t)|"
    r"resolved|done|finished|closed|ready)",
    re.IGNORECASE | re.UNICODE,
)
WORD_RE = re.compile(r"\w+", re.UNICODE)


def is_brief_gratitude(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt))


def is_resolution_meta_question(prompt: str) -> bool:
    words = WORD_RE.findall(prompt)
    if not words or len(words) > META_MAX_WORDS:
        return False
    return bool(GRATITUDE_RE.search(prompt) and META_RE.search(prompt))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if not (is_brief_gratitude(prompt) or is_resolution_meta_question(prompt)):
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
