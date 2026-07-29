#!/usr/bin/env python3
"""Stop hook: nudge when a review this session AUTHORED has no monitor armed to
drive it to mergeable.

Difficulty (recurring, recorded 2026-07-29): the agent opens a review request
and moves straight to the next stage. A formatter/CI check goes red, reviewer
comments arrive, approval never comes — and it all sits there until the user
says *"the tests failed in your review"*. Opening a review is the START of the
author's ownership, not the end. The rule already lives in prose twice
(`memory-global/leaves/review-accompanies-code.md` — the policy; and
`memory-global/leaves/long-job-monitoring.md` § Generalization — the zero-token
mechanism) and kept being missed, because "open the review" reads as the
done-signal of the code stage. This hook is the structural (turn-end) guard for
that already-decided rule: whether a review the agent opened has a monitor
armed is deterministically decidable from the transcript plus the
monitored-reviews registry.

Vendor-neutral by design: no platform host is hard-coded. A URL qualifies as a
review URL when its path carries a review/pull/merge-request segment plus a
numeric id, and the "I opened it" create-verb list is operator-configurable —
both live in the shared `review_open_detect.py`, so this hook and any other
consumer cannot drift apart.

Stricter than its sibling `hook-run-url-surfaced-reminder.py` on purpose: that
one fires on any run URL merely seen, while this one needs the review to be tied
to a create command — its URL printed by that command, named on its command
line, or referenced there by numeric id. Nagging about every review the agent
happens to READ is the failure mode that would make it noise, so authorship is
correlated per review rather than per session.

Fail-open advisory: exit 0 always; emit stdout (becomes additional system
context). Never blocks a turn.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_open_detect  # noqa: E402
import transcript_read  # noqa: E402

MAX_LISTED = transcript_read.MAX_LISTED


def analyze(entries, armed=None) -> dict:
    """Return {identity -> sample_url} for reviews authored this session that
    have no monitor armed."""
    authored = review_open_detect.authored_reviews(entries)
    if not authored:
        return {}
    armed_set = review_open_detect.armed_identities() if armed is None else armed
    return {i: u for i, u in authored.items() if i not in armed_set}


def _state_dir() -> Path:
    """Durable marker dir `<agent-home>/state/review-guardian/`, mirroring
    hook-turn-end-gate's `state/turn-gate/`."""
    try:
        from lib.config_root import agent_home

        home = agent_home()
    except Exception:
        home = Path.home() / ".claude-agent"
    return home / "state" / "review-guardian"


def _marker_path(session_id: str, identity: str) -> Path:
    digest = hashlib.sha256(
        (session_id + "\0" + identity).encode("utf-8")
    ).hexdigest()[:32]
    return _state_dir() / digest


def _unnudged(session_id: str, unmonitored: dict) -> dict:
    """Drop identities already nudged for in this session. Without the durable
    marker the same review is re-nudged at every turn end for the rest of the
    session — the reminder is a prompt to act once, not a per-turn drumbeat."""
    if not session_id:
        return unmonitored
    fresh = {}
    for ident, url in unmonitored.items():
        try:
            if not _marker_path(session_id, ident).exists():
                fresh[ident] = url
        except Exception:
            fresh[ident] = url
    return fresh


def _mark_nudged(session_id: str, identities) -> None:
    if not session_id:
        return
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        for ident in identities:
            _marker_path(session_id, ident).write_text("", encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    # Loop guard: never nudge twice in one stop cycle.
    if payload.get("stop_hook_active"):
        return 0
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return 0
    try:
        unmonitored = analyze(list(transcript_read.iter_transcript(path)))
        session_id = str(payload.get("session_id") or "")
        unmonitored = _unnudged(session_id, unmonitored)
    except Exception:
        return 0
    if not unmonitored:
        return 0
    urls = list(unmonitored.values())[:MAX_LISTED]
    listed = ", ".join(urls)
    extra = "" if len(unmonitored) <= MAX_LISTED else f" (+{len(unmonitored) - MAX_LISTED} more)"
    print(
        f"[review-mergeable] You opened review {listed}{extra} this session, and "
        "no monitor is armed for it. Per leaves/review-accompanies-code.md, "
        "opening a review is the START of your ownership: drive it to MERGEABLE "
        "unprompted — CI green, every reviewer comment answered, approval "
        "obtained. Arm `scripts/review-monitor.sh` (with your platform's probe) "
        "to poll its checks, comments and approval per "
        "leaves/long-job-monitoring.md § Generalization; don't leave it red or "
        "unanswered until the user points at it."
    )
    _mark_nudged(session_id, unmonitored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
