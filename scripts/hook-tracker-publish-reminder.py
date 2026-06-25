#!/usr/bin/env python3
"""UserPromptSubmit hook: state-aware nudge to publish the mandatory tracker
comments before a tracker-backed task closes.

Companion to hook-tracker-reminder.py (which detects tracker references in the
prompt and nudges *invoking* the skill) and to hook-resolution-reminder.py (which
guards the resolution gate). This one is the publish-side safety net for the
`tracker` agentctl plugin (scripts/agentctl/plugins_tracker.py):

When the acting session has the tracker plugin ACTIVE and one of its mandatory
publications (the plan, the final result) has not been recorded via
`agentctl plugin-record --plugin tracker --phase <p>`, this hook surfaces a
reminder. The nudge is loudest at the resolution gate (node == RESOLUTION) — the
plugin's gate will block `resolve` there until the phases are recorded, so the
reminder tells the coordinator exactly what to post first.

Mirrors hook-resolution-reminder.py: reads the agentctl state JSON keyed by
session_id; missing/corrupt state or an inactive plugin -> silent (exit 0, no
output). Stdout becomes additional system context for the upcoming turn.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "agentctl" / "state"

# Must mirror plugins_tracker.MANDATORY_PHASES. Duplicated (not imported) so the
# hook stays a dependency-free stdin filter that never imports the engine.
MANDATORY_PHASES = ("plan", "result")


def _safe(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe or "nosession"


def _load_state(session_id: str) -> dict | None:
    if not session_id:
        return None
    path = STATE_ROOT / f"{_safe(session_id)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def unpublished_phases(state: dict) -> list[str]:
    """Mandatory tracker phases not yet recorded as published. [] when the tracker
    plugin is inactive or every mandatory phase is recorded."""
    bag = (state.get("plugins") or {}).get("tracker")
    if not isinstance(bag, dict):
        return []
    published = bag.get("published_phases") or {}
    return [p for p in MANDATORY_PHASES if p not in published]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    state = _load_state(payload.get("session_id") or "")
    if state is None:
        return 0
    missing = unpublished_phases(state)
    if not missing:
        return 0

    at_gate = state.get("node") == "RESOLUTION"
    phases = ", ".join(missing)
    if at_gate:
        print(
            "[tracker-publish-reminder] The session is at the resolution gate and "
            f"the tracker plugin still has unpublished mandatory phase(s): {phases}. "
            "`agentctl resolve` will be BLOCKED until each is posted to the ticket "
            "and recorded with `agentctl plugin-record --plugin tracker --phase <p>`. "
            "Publish the comment(s) first, then record, then resolve."
        )
    else:
        print(
            "[tracker-publish-reminder] The tracker plugin is active with "
            f"unpublished mandatory phase(s): {phases}. Post the corresponding "
            "ticket comment at its phase boundary (plan before approval, result at "
            "close) and record it via `agentctl plugin-record --plugin tracker "
            "--phase <p>` — the resolution gate enforces this before the task closes."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
