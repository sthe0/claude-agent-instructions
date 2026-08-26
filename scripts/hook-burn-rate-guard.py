#!/usr/bin/env python3
"""UserPromptSubmit hook: the FAST-BURN companion to the periodic spend scorecard.

Difficulty removed
------------------

``scripts/policy-scorecard.py`` already measures spend rate and already carries a
calibrated degradation flag (``SPEND_RATE_FACTOR``), but it is a 7-day window
whose baseline is the median of the trailing four. Its earliest possible fire on
an event is therefore AFTER the event's window closes. In SRE terms it is a
slow-burn tier with no fast-burn companion, so it structurally cannot speak while
an incident is in progress — it would not have caught the 2026-08-26 session
fa80b9a9 event (hours of quota burned inside one hour) at any moment during that
hour. No threshold change fixes that. Only a shorter window does.

The scheme, and what is reused rather than re-derived
-----------------------------------------------------

Google's SRE Workbook ch. 5 (Alerting on SLOs) establishes that a single-window
burn alert is either fast and noisy or precise and late, and that the remedy is
two windows — a long one for significance and a SHORT one, conventionally ~1/12
of it, as a confirmation gate — with BOTH required to fire. That is the shape
here: ``SLOW_WINDOW_H`` for significance, ``FAST_WINDOW_H`` for recency, and no
warning unless both exceed threshold.

Nothing numeric is invented:

- the MULTIPLE is ``policy-scorecard.py``'s own ``SPEND_RATE_FACTOR``, imported,
  not copied — the same constant that instrument calibrated against a named
  ledger snapshot;
- the CONJUNCTION requirement is that instrument's own
  ``SPEND_RATE_FLAG_MODE = "conjunction"``, i.e. the user's already-recorded
  disposition that a rate rise alone is budget consumption, not degradation;
- the NORMAL RATE the multiple applies to is config.md's own declared cost norm
  for a stage of typical size — ``budget-medium-usd`` over
  ``effort-stage-minutes-medium`` — read through ``agentctl.config.Thresholds``.

Why the conjunction is over two windows and not over the 7-day flag
-------------------------------------------------------------------

Requiring the periodic instrument's flag as the live partner would be a literal
reading of "fast AND slow" that cannot work: that flag is late BY CONSTRUCTION,
so during a fresh incident it is silent and the conjunction could never fire —
the guard would be dead in exactly the situation it exists for. (The stage that
commissioned this named that failure as its own refutation condition.) So the two
windows the conjunction ranges over are both live, and what the periodic
instrument supplies is its calibration and its disposition, not a second vote.

Why it warns and never blocks
-----------------------------

"Circuit breaker" is not the applicable pattern: in the literature the short
window GATES an alert, it does not trip a breaker. And the population a fast-burn
threshold flags includes every legitimately expensive session — a large
implementation stage, a deliberate deep research pass. A hard block converts each
of those false positives into lost work with no recourse, while an ignored
warning costs a line. At the higher band the hook asks the coordinator to put the
decision to the user (``AskUserQuestion``), which is this repo's standard way to
reach one; it never denies anything itself.

Calibration, replayed 2026-08-26 — and what it refutes
------------------------------------------------------

Every constant above was replayed against the 10 real transcripts reachable at
the time (5 root sessions + 5 subagent sessions of one worktree), by anchoring
both windows at each of their 293 assistant messages in turn. Result and the
numbers behind it, so the next reader re-derives instead of re-guessing:

- session spans 0.1-23.3 min; mean session rate $12.62-$99.10/h, and $19.4 /
  $12.6 / $28.4 per hour for the three sessions long enough to evaluate;
- at the shipped floor (one declared medium stage, 25 min) NOTHING in the sample
  fires — but no sampled session lasts 25 min, so this sample cannot tell a
  correctly-quiet guard from a dead one;
- sweeping the floor down: at 10 min it warns on 3 of the 3 sessions long enough
  to evaluate, i.e. on ALL ordinary stage work in the sample.

That last line refutes the THRESHOLD ANCHOR, not the mechanism. config.md's
declared medium-tier rate is $7.20/h ($3.00 over 25 active minutes), so warning
starts at $16.20/h — BELOW the observed mean rate of an ordinary opus stage
session (~$20/h). The declared dollar figure is a per-task telemetry label, and
reading it as a per-hour rate is what puts the threshold under the population it
is meant to sit above. Recalibrating it is `budget-calibration.py`'s existing
job (it already flags tiers to raise or lower against recorded spend) and is
deliberately NOT done here: this hook's contract is to reuse the calibration the
periodic instrument owns, never to mint a rate of its own.

So: shipped at the conservative end, where the failure mode is silence rather
than a warning on every stage, and honest that the discriminating power is
unproven until either the tier labels are recalibrated or a session longer than
one declared stage is replayed.

Fail-open in every direction — missing transcript, unreadable transcript,
malformed rows, missing state dir, an import that will not load, any exception at
all: exit 0, say nothing. A hook that raises puts an error in front of the user
on every prompt of every session, which is far worse than a missed warning.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from lib import transcript_cost  # noqa: E402

# Significance window: how far back "sustained" is allowed to look. Costs nothing
# when the session is younger, because window_rate divides by the span actually
# observed inside the window rather than by the window's nominal length.
#
# Its floor — how much observation is required before the slow tier will answer at
# all — is NOT a constant here. It is config.md's declared active-minutes for the
# same tier the threshold is read from (see slow_min_span_h), so the two halves of
# the comparison stay statements about one object: "the norm is B dollars per M
# active minutes, so wait M active minutes before judging against it."
#
# The superseded value was a flat 1 h, and replaying real transcripts is what
# refuted it: a 1 h floor keeps the guard silent for the first hour of every
# session, and the event that commissioned this burned hours of quota INSIDE one
# hour. A guard that cannot speak until the incident is over is the slow tier's
# defect wearing the fast tier's name.
SLOW_WINDOW_H = 3.0

# Confirmation window: the SRE convention's ~1/12 of the significance window,
# derived from it rather than chosen, so the two cannot drift apart.
#
# Its floor is HALF the window, not the whole window. The observed span is
# bounded above by the window length, so demanding a full one is satisfiable only
# by a message landing exactly on the boundary second — a floor nothing can meet
# is not a strict guard, it is a dead hook.
FAST_WINDOW_H = SLOW_WINDOW_H / 12.0
FAST_MIN_SPAN_H = FAST_WINDOW_H / 2.0

STATE_DIR_ENV = "CC_BURN_RATE_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "claude-burn-rate-guard"

# The tier whose declared dollar label and declared active-minutes are read as
# "one stage of typical size", i.e. the normal rate. Two config.md keys, one
# tier, so the ratio stays a statement about the same object.
NORMAL_TIER = "medium"


def load_policy_scorecard():
    """``policy-scorecard.py`` as a module, or None.

    Loaded by path because the filename is hyphenated — the same idiom
    ``agent-stats.py`` and ``lint-prose-length.py`` already use. Imported at all,
    rather than having its factor copied here, because a duplicated threshold is
    the defect this whole file is a companion to.
    """
    path = SCRIPTS / "policy-scorecard.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("burn_rate_policy_scorecard", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def normal_profile() -> "tuple[float, float] | None":
    """config.md's declared (USD per hour, active hours) for a typical stage.

    Both numbers come from the same tier's two rows, and both are returned
    together because they are one statement — "B dollars per M active minutes" —
    that the rest of this file then uses twice: as the rate to compare against,
    and as how long to observe before comparing.

    Imported inside the function, and every failure folded into None, because
    config.md is a file an operator edits: a reshaped table, or an agentctl that
    will not import, must leave the hook silent rather than raising on someone's
    prompt.
    """
    try:
        from agentctl.config import Thresholds

        thr = Thresholds()
        minutes = thr.effort_stage_minutes(NORMAL_TIER)
        if minutes <= 0:
            return None
        hours = minutes / 60.0
        return thr.budget_usd_float(NORMAL_TIER) / hours, hours
    except Exception:
        return None


def normal_rate_usd_per_h() -> float | None:
    profile = normal_profile()
    return profile[0] if profile else None


def slow_min_span_h() -> float | None:
    """How much observed activity the slow tier requires before it will answer.

    One typical stage's declared active time. Below that, a high rate has not yet
    outlasted a single unit of ordinary work and is a spike, which is the fast
    tier's business, not this one's.
    """
    profile = normal_profile()
    return profile[1] if profile else None


def thresholds() -> "tuple[float, float] | None":
    """(warn, escalate) in USD/hour, or None when either input is unavailable.

    The escalate band is the same factor applied twice rather than a second
    number: one calibrated constant governs both bands, so there is nothing here
    to tune independently and get wrong.
    """
    scorecard = load_policy_scorecard()
    if scorecard is None:
        return None
    factor = getattr(scorecard, "SPEND_RATE_FACTOR", None)
    normal = normal_rate_usd_per_h()
    if not isinstance(factor, (int, float)) or not normal:
        return None
    return normal * float(factor), normal * float(factor) ** 2


def band_for(fast: float | None, slow: float | None, warn: float, escalate: float) -> int:
    """0 = silent, 1 = warn, 2 = escalate. Either window abstaining means 0."""
    if fast is None or slow is None:
        return 0
    if fast >= escalate and slow >= escalate:
        return 2
    if fast >= warn and slow >= warn:
        return 1
    return 0


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    base = os.environ.get(STATE_DIR_ENV, "").strip()
    directory = Path(base) if base else DEFAULT_STATE_DIR
    return directory / (safe or "nosession")


def already_fired(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def record_fired(path: Path, band: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(band), encoding="utf-8")
    except OSError:
        pass


def message(band: int, fast: float, slow: float, warn: float, escalate: float) -> str:
    head = (
        f"[burn-rate] Spend is running at ${fast:.2f}/h over the last "
        f"{FAST_WINDOW_H * 60:.0f} min and ${slow:.2f}/h over the last "
        f"{SLOW_WINDOW_H:.0f} h"
    )
    if band >= 2:
        return (
            f"{head} — both past ${escalate:.2f}/h, the escalation threshold. "
            "Put the decision to the user NOW with an explicit AskUserQuestion "
            "offering: continue at this rate / narrow the scope / stop here. Do "
            "not decide it yourself."
        )
    return (
        f"{head} — both past ${warn:.2f}/h, the warning threshold "
        "(policy-scorecard.py's calibrated SPEND_RATE_FACTOR over config.md's "
        f"declared {NORMAL_TIER}-tier rate). If this is a deliberately expensive "
        "stage, carry on; otherwise delegate verbose exploration to a cheaper "
        "sub-agent, avoid re-reading large files, and consider `/clear` on a task "
        "switch (CLAUDE.md § Cost discipline)."
    )


def evaluate(transcript_path: str) -> "tuple[int, float, float, float, float] | None":
    """(band, fast, slow, warn, escalate), or None when there is nothing to say."""
    limits = thresholds()
    if limits is None:
        return None
    warn, escalate = limits
    slow_floor = slow_min_span_h()
    if slow_floor is None:
        return None
    rows = transcript_cost.usage_rows(transcript_path)
    if not rows:
        return None
    fast = transcript_cost.window_rate(
        rows, FAST_WINDOW_H, min_span_h=FAST_MIN_SPAN_H)
    slow = transcript_cost.window_rate(
        rows, SLOW_WINDOW_H, min_span_h=slow_floor)
    band = band_for(fast, slow, warn, escalate)
    if band == 0:
        return None
    return band, fast, slow, warn, escalate


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        transcript_path = payload.get("transcript_path") or ""
        session_id = payload.get("session_id") or ""
        if not transcript_path:
            return 0
        verdict = evaluate(transcript_path)
        if verdict is None:
            return 0
        band, fast, slow, warn, escalate = verdict
        sp = state_path(session_id)
        if band <= already_fired(sp):
            return 0
        record_fired(sp, band)
        print(message(band, fast, slow, warn, escalate))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
