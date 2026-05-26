#!/usr/bin/env python3
"""UserPromptSubmit hook: detect tracker references in the user's prompt
and emit a system-context reminder to invoke the `tracker-management`
skill.

Rule (CLAUDE.md § Recognizing when to delegate, tracker-management skill
trigger description): invoke the skill when the user mentions a ticket
key (`ABC-123`), the words "ticket"/"issue"/"tracker"/"тикет"/"тред",
asks to post/update/close a ticket, or links a tracker URL.

This hook lifts the keyword-detection part from agent recall to a
deterministic regex scan. The skill decision still belongs to the
agent — the hook only nudges.

Scope:
  - Reads the UserPromptSubmit JSON on stdin.
  - Scans `prompt` text for two patterns:
      1. Ticket key shape: `\\b[A-Z][A-Z0-9]{1,9}-\\d+\\b` (excluding a
         small allow-list of common false positives — ISO/RFC/UTF/etc.).
      2. Keywords: ticket / тикет / issue / tracker / тред.
  - Emits a stdout line on match — UserPromptSubmit stdout is appended
    to the model's system context for the upcoming turn.
  - Exit 0 always.
"""
from __future__ import annotations

import json
import re
import sys

TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
KEYWORDS_RE = re.compile(
    r"\b(ticket|тикет|issue|tracker|тред)\b",
    re.IGNORECASE | re.UNICODE,
)

# Common ticket-key-shaped strings that are not tracker keys.
FALSE_POSITIVES = {
    "COVID-19", "ISO-8601", "ISO-4217", "ISO-3166",
    "RFC-822", "RFC-2822", "RFC-5322", "RFC-7231",
    "UTF-8", "UTF-16", "UTF-32",
    "ECMA-262", "ECMA-402",
    "SHA-1", "SHA-256", "SHA-512", "MD-5",
    "HTTP-1", "HTTP-2", "HTTP-3",
}


def find_signals(prompt: str) -> list[str]:
    signals: list[str] = []
    keys = sorted({k for k in TICKET_KEY_RE.findall(prompt) if k not in FALSE_POSITIVES})
    if keys:
        signals.append(f"ticket key(s): {', '.join(keys)}")
    keywords = sorted({m.group(0).lower() for m in KEYWORDS_RE.finditer(prompt)})
    if keywords:
        signals.append(f"keyword(s): {', '.join(keywords)}")
    return signals


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    signals = find_signals(prompt)
    if not signals:
        return 0

    print(
        f"[tracker-reminder] User prompt references a tracker — {'; '.join(signals)}. "
        f"Invoke the `tracker-management` skill (layered on top of normal coordination)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
