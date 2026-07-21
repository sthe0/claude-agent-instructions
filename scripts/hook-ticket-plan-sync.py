#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge a plan<->TOML sync check when a ticket-
referencing prompt continues a session that already has a plan attached.

Companion to hook-tracker-reminder.py (which detects a tracker reference and
nudges invoking the `tracker-management` skill) and to
scripts/verify-ticket-plan-sync.py (the tracker-agnostic comparator this hook
nudges toward). This hook covers the CONTINUATION gap the mechanism exists
for: a session can resume ticket work without ever confirming the posted
plan comment still matches the current TOML plan, and that drift is
otherwise only caught if the coordinator happens to remember to check.

Fires when BOTH are true:
  - the prompt carries a ticket key or tracker keyword (mirrors
    hook-tracker-reminder.py's own detection, duplicated rather than
    imported so this stays a dependency-free stdin filter), and
  - the agentctl state for this session has a non-empty plan_path.

Silent (exit 0, no output) on any other case, including missing/corrupt
state — this is a nudge, not a gate; the comparator itself is the gate-
worthy check, run by hand or by a project-specific continuation script.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402

TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
KEYWORDS_RE = re.compile(
    r"\b(ticket|тикет|issue|tracker|тред)\b",
    re.IGNORECASE | re.UNICODE,
)

# Mirrors hook-tracker-reminder.py's FALSE_POSITIVES — kept in sync by hand;
# both lists are short and reviewed together on change.
FALSE_POSITIVES = {
    "COVID-19", "ISO-8601", "ISO-4217", "ISO-3166",
    "RFC-822", "RFC-2822", "RFC-5322", "RFC-7231",
    "UTF-8", "UTF-16", "UTF-32",
    "ECMA-262", "ECMA-402",
    "SHA-1", "SHA-256", "SHA-512", "MD-5",
    "HTTP-1", "HTTP-2", "HTTP-3",
}


def mentions_tracker(prompt: str) -> bool:
    if KEYWORDS_RE.search(prompt):
        return True
    return any(k not in FALSE_POSITIVES for k in TICKET_KEY_RE.findall(prompt))


def session_plan_path(session_id: str) -> "str | None":
    if not session_id:
        return None
    path = config_root.resolve_agentctl_state_file(session_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    plan_path = data.get("plan_path")
    return plan_path if isinstance(plan_path, str) and plan_path else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if not mentions_tracker(prompt):
        return 0

    plan_path = session_plan_path(payload.get("session_id") or "")
    if not plan_path:
        return 0

    print(
        "[ticket-plan-sync] This continues ticket work with an attached plan "
        f"({plan_path}). Before proceeding, run `python3 scripts/"
        "verify-ticket-plan-sync.py --plan "
        f"{plan_path} --comment-file <last-plan-comment>` against the ticket's "
        "last-posted plan comment. On DRIFT or NO-PLAN, re-post the plan "
        "(with a fresh --emit-marker marker) before continuing — a mismatch "
        "here is a plan actualization, not silent divergence."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
