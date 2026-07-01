#!/usr/bin/env python3
"""PreToolUse hook: deny/warn on a live cross-session filesystem-scope overlap
(Component B wiring — see scripts/session_scope/detector.py).

Fires on Edit|Write. Loads every session's scope record (written by
hook-scope-track.py), then asks session_scope.detector whether realpath(file_path)
overlaps a path already held by ANOTHER live session — liveness being a heartbeat
within LIVE_TTL_S of now. Severity is delegated to detector.classify_severity:

  - 'block' → a gated path already held by another live session: emit a PreToolUse
    permissionDecision deny naming the holding session and pointing at
    session-isolate for remediation. A hard block is reserved for this case
    because it is the one where a second writer would corrupt a path the
    coordination engine itself governs; everything else stays advisory.
  - 'warn'  → a non-gated held path: print a loud stdout advisory offering
    isolation, then allow (exit 0). Warning-not-blocking here is what preserves
    isolate-not-serialize: two sessions that genuinely need the same tree isolate
    into separate worktrees/mounts rather than being serialized by the gate.
  - no other live session (single session, or two sessions in distinct
    worktrees/mounts whose paths do not overlap) → silent allow (exit 0), so the
    single-session flow is completely unchanged.

Strictly fail-open: malformed stdin, a missing registry, or any internal error
falls through to allow (exit 0). A hook crash must never wedge a tool call.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout (shape
copied from hook-state-gate.py):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_scope import registry  # noqa: E402
from session_scope.detector import classify_severity, detect_conflicts  # noqa: E402

# Heartbeat-freshness window: a session whose last tool-fire heartbeat is older
# than this is treated as dead and can never produce a conflict. 30 min is long
# enough to span normal think/type gaps between tool calls, short enough that a
# genuinely abandoned session's scope stops shadowing the tree within one window.
LIVE_TTL_S = 1800.0


def evaluate(
    payload: dict, now_ts: float, scopes_dir: "str | Path | None" = None
) -> "tuple[str, str]":
    """Pure decision over an already-parsed payload. Returns (decision, message)
    where decision is 'allow' | 'warn' | 'block'. Kept free of stdin/stdout and
    of the wall clock (now_ts injected) so it is unit-testable in-process; main()
    supplies time.time() and the I/O."""
    if payload.get("tool_name") not in ("Edit", "Write"):
        return "allow", ""

    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not file_path:
        return "allow", ""

    session_id = payload.get("session_id") or ""
    candidate = os.path.realpath(file_path)
    sd = registry.DEFAULT_SCOPES_DIR if scopes_dir is None else scopes_dir

    records = registry.load_all(sd)
    conflicts = detect_conflicts(records, session_id, [candidate], now_ts, LIVE_TTL_S)
    if not conflicts:
        return "allow", ""

    holders = ", ".join(sorted({c.other_session for c in conflicts}))
    severity = classify_severity(candidate, held_by_other_live=True)

    if severity == "block":
        reason = (
            f"filesystem-scope collision: {candidate} is already in the active "
            f"scope of another live session ({holders}). A second writer on the "
            "shared tree would clobber that session's uncommitted work. Isolate "
            "this task into its own worktree/mount instead of serializing on the "
            "shared tree: run `scripts/session-isolate.sh <task-name>` (git "
            "worktree / arc mount), then retry. See "
            "docs/operations/cross-session-scope-isolation.md."
        )
        return "block", reason

    advisory = (
        f"[scope-conflict] {candidate}\n"
        f"is already in the active scope of another live session ({holders}).\n"
        "Two live sessions writing the same tree risk clobbering each other's "
        "uncommitted work.\n"
        "Per CLAUDE.md § isolate-not-serialize: isolate this task into its own "
        "worktree/mount\n"
        "  → scripts/session-isolate.sh <task-name>   (git worktree / arc mount)\n"
        "then continue. See docs/operations/cross-session-scope-isolation.md."
    )
    return "warn", advisory


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        decision, message = evaluate(payload, time.time())
    except Exception:
        return 0
    if decision == "block":
        deny(message)
    elif decision == "warn":
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
