#!/usr/bin/env python3
"""UserPromptSubmit hook: state-aware nudge when the experience leaf flow is incomplete.

Companion to hook-tracker-publish-reminder.py. Reads the agentctl state JSON
for the current session and inspects the `experience` plugin bag (shipped in
scripts/agentctl/plugins_experience.py).

The mandatory flow is: search existing leaves → extend-vs-new → write leaf (or
skip with a reason). Each phase is recorded via `agentctl plugin-record --plugin
experience --phase <searched|recorded|skipped>`. The plugin gate blocks
`agentctl resolve` until searched AND (recorded OR skipped).

This hook surfaces that gate early — before the user hits it — as a reminder.
Loudest at node == RESOLUTION (where `resolve` will be blocked); softer nudge
otherwise. Silent if state is missing/corrupt, the experience plugin is not
active (non-substantive sessions), or the flow is already complete.

Mirrors hook-tracker-publish-reminder.py: reads state JSON keyed by session_id;
missing/corrupt state or an inactive plugin → silent (exit 0, no output).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402


def _load_state(session_id: str) -> dict | None:
    if not session_id:
        return None
    path = config_root.resolve_agentctl_state_file(session_id)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _missing_phases(bag: dict) -> list[str]:
    """Phases not yet recorded. Empty list means the flow is complete."""
    phases = []
    if not bag.get("searched"):
        phases.append("searched")
    if not (bag.get("recorded") or bag.get("skipped")):
        phases.append("recorded|skipped")
    return phases


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    state = _load_state(payload.get("session_id") or "")
    if state is None:
        return 0

    bag = (state.get("plugins") or {}).get("experience")
    if not isinstance(bag, dict):
        return 0  # plugin inactive (non-substantive or not yet activated)

    missing = _missing_phases(bag)
    if not missing:
        return 0  # flow complete

    at_gate = state.get("node") == "RESOLUTION"
    phases = ", ".join(missing)

    if at_gate:
        print(
            "[experience-record-reminder] The session is at the resolution gate and "
            f"the experience leaf flow is incomplete — missing phase(s): {phases}. "
            "`agentctl resolve` will be BLOCKED until `searched` AND "
            "(`recorded` OR `skipped`) are recorded. "
            "Run `record-experience.py search <keywords>` first, then either "
            "`agentctl plugin-record --plugin experience --phase recorded` or "
            "`agentctl plugin-record --plugin experience --phase skipped --note <reason>`."
        )
    else:
        print(
            "[experience-record-reminder] The experience leaf flow has "
            f"incomplete phase(s): {phases}. Before the task closes, run "
            "`record-experience.py search <keywords>` to check for an existing "
            "leaf, then record or skip via `agentctl plugin-record --plugin "
            "experience --phase <searched|recorded|skipped>` (skip needs --note)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
