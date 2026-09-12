#!/usr/bin/env python3
"""UserPromptSubmit: say so when a session has run past the norm its plan declared.

Difficulty removed
------------------

The effort-divergence trigger is armed at ``approve`` and compared at exactly two
kinds of moment: the fire sites ``record-result`` / ``verify-final``, and the
``gates.effort_fire_blockers`` refusal on ``dispatch`` / ``submit-plan`` /
``replan``. Every one of them is a command the coordinator CHOSE to run. A session
that runs long without reaching any of them — one stage worked for hours, a
specialist that never returns, a plan whose next command keeps being deferred —
diverges unobserved no matter how far past the multiple it goes. The trigger
"needs no human to notice it" only once something calls the engine.

This hook is the observation that does not wait for such a call: it runs on EVERY
prompt, asks ``agentctl effort-check`` (strictly read-only) where the session
stands, and prints one line when a scale is at or past its threshold.

Scope of the closure, stated plainly: this closes the case where the engine is
present but simply has not been CALLED. A session that never started the engine at
all still has no norm to be measured against, and this hook cannot invent one.

Two properties
--------------

NEVER BLOCKS, ALWAYS EXITS 0. It is an advisory line on the user's own prompt; a
missing session, an absent plan, an unarmed trigger, a broken subprocess or a
malformed payload are all silence, not a traceback.

NEVER WRITES ENGINE STATE. It drives ``effort-check``, which is read-only by
contract — deliberately not ``record-result`` or ``fire-acknowledge``. A hook that
fired the trigger itself would consume the one-fire-per-replan budget belt 2 keeps
(``effort._replans_since_last_fire``) without anyone having diagnosed anything, and
the real fire site would then find nothing left to say.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from lib import band_throttle  # noqa: E402

STATE_DIR_ENV = "CC_EFFORT_WATCH_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "claude-effort-divergence-watch"

# One stamp namespace PER SCALE: the four scales are independent statements about
# the same session, so announcing "spend is 6x over" must not silence a later
# "replan count reached its absolute trigger". `band_throttle._prune` treats the
# prefix as this caller's ownership marker, so a shared root stays safe.
BAND_PREFIX = "scale-"

# Seconds. effort-check loads one state file and reads the cost ledger; the budget
# is generous against that and still short of anything a user would feel on their
# own prompt. A timeout is silence, like every other failure here.
CHECK_TIMEOUT_S = 8


def state_root() -> Path:
    """Where this hook's per-session-per-scale stamps live.

    Under ``~/.local/state`` rather than ``/tmp`` for the same reason as
    ``hook-burn-rate-guard.py``: the memo must outlive a reboot for as long as the
    session might, and ``band_throttle`` prunes the directory on write since
    nothing else will.
    """
    base = os.environ.get(STATE_DIR_ENV, "").strip()
    return Path(base) if base else DEFAULT_STATE_DIR


def effort_check(session_id: str) -> dict | None:
    """The engine's own read-only verdict, or None when there is nothing to read.

    Run as a subprocess rather than an in-process import so a partially-installed
    or syntactically-broken engine cannot take the user's prompt down with it —
    the same isolation ``hook-engine-start.py`` uses for its autostart.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agentctl", "effort-check", "--session", session_id],
            cwd=str(SCRIPTS), capture_output=True, text=True,
            timeout=CHECK_TIMEOUT_S, check=False,
        )
        return json.loads(proc.stdout)
    except Exception:
        return None


def worst_scale(directive: dict) -> dict | None:
    """The scale furthest past its OWN trigger, or None when none is past it.

    Ranks on ``past_own_trigger`` rather than the raw ratio for the reason
    ``effort.divergence`` does: a ratio scale trips at the configured multiple
    while an absolute one is already normalized to trip at 1.0, so raw ratios would
    always favour the ratio scale barely over its line.
    """
    data = directive.get("data") or {}
    if not data.get("armed") or not data.get("active"):
        return None
    over = [s for s in (data.get("scales") or []) if s.get("at_or_past_threshold")]
    if not over:
        return None
    return max(over, key=lambda s: s.get("past_own_trigger") or 0.0)


def band_for(scale: dict) -> int:
    """How many times past its own trigger, floored at 1.

    A band rather than a boolean so a divergence that keeps GROWING speaks again:
    the throttle silences a repeat of the same band, not a worse one.
    """
    return max(1, int(scale.get("past_own_trigger") or 1))


def message(scale: dict, *, already_fired: bool) -> str:
    lead = (
        f"[effort-divergence] {scale.get('label')} on this task is "
        f"{scale.get('actual', 0):.2f} {scale.get('unit')} against "
        f"{scale.get('comparand', 0):.2f} — {scale.get('past_own_trigger', 0):.1f}x past its "
        "own trigger, and "
    )
    if already_fired:
        # `would_fire` is None: belt 2's one-fire-per-replan budget for this scale is
        # already spent, so the coordinator's actual blocked next act is
        # `fire-acknowledge`, not another `declare` — naming `declare` here would send
        # the coordinator to a command that is not what is stuck.
        return lead + (
            "this scale already fired and is awaiting acknowledgment — the blocked "
            "next act is `agentctl fire-acknowledge`, not another `declare`. "
            "`agentctl effort-check --session <id>` prints all four scales."
        )
    return lead + (
        # "not reported THIS TURN", not "never fired": the hook reads a report, and the
        # report does not carry `effort_fires`. That a fired scale is normally back under
        # its line (record_fire rebases the baseline) makes the stronger claim true most
        # of the time, which is exactly the kind of claim that is wrong when it matters.
        "no engine command has reported it this turn. The chosen norm is "
        "visibly missing something essential about the real situation. Run the "
        "difficulty cycle now — `agentctl declare` -> `investigate` -> `critique` -> "
        "`replan` (CLAUDE.md § When the work is stuck) — asking WHAT the plan does not "
        "account for, not re-estimating the same plan. `agentctl effort-check --session "
        "<id>` prints all four scales."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        session_id = payload.get("session_id") or ""
        if not session_id:
            return 0
        directive = effort_check(session_id)
        if not isinstance(directive, dict):
            return 0
        scale = worst_scale(directive)
        if scale is None:
            return 0
        band = band_for(scale)
        root = state_root()
        prefix = f"{BAND_PREFIX}{scale.get('scale')}-"
        if band <= band_throttle.fired_band(session_id, root, prefix):
            return 0
        band_throttle.record_band(session_id, band, root, prefix)
        # `would_fire` is None exactly when belt 2 (the one-fire-per-replan budget,
        # `effort._replans_since_last_fire`) has already spent this scale's fire and
        # no replan has happened since — the coordinator's blocked next act is then
        # `fire-acknowledge`, not `declare`, even though the scale is still over its line.
        already_fired = (directive.get("data") or {}).get("would_fire") is None
        print(message(scale, already_fired=already_fired))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
