"""Effort divergence: is this session running far past the plan it committed to?

*Difficulty removed:* a task can run many times over its own plan and nothing in the
system notices — the only detector is the user's attention, which is the resource the
engine exists to spend less of. This module is the measurement half of the answer: it
derives what the CURRENT plan claims the remaining work costs, accumulates what the
session has actually spent SINCE THE PLAN WAS APPROVED, and reports the scale on which
the two have diverged past the configured multiple. Acting on that report — entering
the declare -> investigate -> critique cycle — is the core spine's job (cli.py), not
this module's.

Four scales, two shapes:

  scale         shape      estimate                        actual
  ------------- ---------- ------------------------------- -----------------------------
  spend         ratio      Σ budget-<tier>-usd over spawn  monotone accumulator over the
                           stages + the engine-MANDATED    cost ledger, summed by
                           review spawns                   plan_path ALONE
  wall_clock    ratio      Σ effort-stage-minutes-<tier>   monotone ACTIVE-minutes
                           over every stage                accumulator (hook-stamped)
  replans       absolute   none (structurally 0)           count of `replan` history events
  interactions  absolute   none (not derivable)            user_prompt_count (hook-stamped)

THE WINDOW. Every actual is compared over the window that opens at ARMING (cmd_approve),
because that is the only window the estimate describes: the estimate is derived from the
approved plan's stages, all of which execute after approval. `state.effort_baseline` is a
snapshot of the actual vector taken at arming and every comparison uses `actual - baseline`.
Without this a pre-classify prompt or an overnight idle interval would fire the trigger.

ARMED-ONLY, AND ARMED AT MOST ONCE.
  (a) `divergence()` returns None unless BOTH `effort_estimate` and `effort_baseline` are
      set. Only `arm()` sets them and only cmd_approve calls it, so a session that never
      passes `approve` — machine.py's `execute_small` takes ROUTED -> EXECUTING directly —
      is inert BY CONSTRUCTION rather than by a weight_class test a future route could
      forget.
  (b) `arm()` ALWAYS re-derives the estimate but snapshots the baseline ONLY IF unset. A
      substantive replan re-arms the approval gate, so cmd_approve runs a SECOND time for
      the same task; re-snapshotting there would rebase onto the accumulated actual, zero
      every ratio, and destroy precisely the scenario this trigger exists for (stages
      dropped, actual already far past the contracted estimate).

STORED, NOT RECOMPUTED. `divergence()` reads the STORED `state.effort_estimate`; it never
calls `estimate()` itself. `rederive()` is that field's only writer, called at arming and
on every replan branch, which makes re-derivation an explicit auditable event — and is
what makes (a) hold, since an unapproved session has no stored estimate to compare against.

RE-ARM, two belts, both required: the baseline is REBASED to the actual vector at each
firing (so a plan that contracts below the accumulated actual cannot re-fire on the next
command), AND a further fire requires at least one `replan` event logged since the last
one (the "silent until a replan sets a new estimate" rule, read literally). Belt 2 reads
`effort_fires`, whose other and independent purpose is the AUDIT TRAIL — one record per
firing, carried into the quality ledger so the thresholds can be recalibrated against
what they actually caught.

MONOTONE ACTUALS. Every accumulator here only grows and the baseline is always a PAST
snapshot of the same vector, so `actual >= baseline` holds with no clamp. The
writer-by-writer argument, and what a future writer breaking it should look like, is on
`deltas()`.

CROSS-SESSION (item B). `divergence()` accepts an optional `cross_session_totals`
dict — the current `per_axis_totals` from `task_accumulator.get(state.task_id)`,
read by the CALLER (cli.py) and passed in as plain data, keeping this module pure
exactly as `refresh_spend(state, rows, path)` already does for the cost ledger.
Only the REPLANS scale consults it: a restarted session on the same `task_id`
starts `deltas()`'s SCALE_REPLANS at 0 (a fresh session has empty `history`), so a
task that hit 2 replans, closed, and reopened would get a brand-new 3-replan
budget every time — the exact symptom `round_release.py` closed WITHIN a session,
still open ACROSS one. The accumulator's `replan_count` total already includes
this session's own replans (cli.py adds 1 to it at every `state.log("replan", ...)`
site, the same three sites `replan_count()` itself counts from), so
`effective_deltas()` — the one vector `divergence()` decides on AND the read-only
`effort-check` report reads, so the two can never disagree — takes
`max(delta[SCALE_REPLANS], cross_session_totals["replan_count"])` rather
than adding the two — using the larger of "what this session alone has logged"
and "what this task has accumulated across every session that touched it" as the
scale's actual, so a fire happens when EITHER exceeds the threshold, never both
counted twice. The other three accumulator axes (`plan_review_rounds`,
`plan_enumerate_rounds`, `code_review_rounds`) are recorded by cli.py for the same
cross-session visibility but are not, in this stage, consulted by any live gate.

SUB-PLAN CUSTODY. `cmd_push_subplan` resets `state.stages` and re-runs the full
classify -> ... -> approve spine for a service sub-plan, so a naive second `arm()` would
compare the PARENT's whole accumulated actual against the CHILD's tiny estimate — a
spurious fire on the sub-plan's first command. The fix lives at FIVE call-site seams
OUTSIDE this module: `cmd_push_subplan`'s reset list, the `PlanFrame` dataclass,
`SessionState.from_dict`'s `PlanFrame` rebuild in state.py, `cmd_push_subplan`'s
`PlanFrame(...)` construction (the snapshot itself), and `cmd_pop_subplan`'s restore
list. They do NOT all fail the same way. `PlanFrame` declares no field defaults, so a
field missing from the dataclass, from `cmd_push_subplan`'s construction, or from
`from_dict`'s rebuild raises `TypeError` at construction time — LOUD, and therefore
hard to ship. The two LIST-shaped seams fail the opposite way: `cmd_push_subplan`'s
reset list and `cmd_pop_subplan`'s restore list are ordinary statements, not
constructor arguments, so a field missing from either one simply keeps whatever value
was already there — SILENT, and the one failure mode this docstring warns loudest
about. Push snapshots all five effort fields into the frame and resets them
(estimate and baseline to `None`, fires to `[]`, `effort_spend_seen` to `{}`,
`effort_actuals` to a fresh zero vector); pop restores the parent's five fields and ADDS
the child's `effort_actuals` into the restored parent's, because push zeroed it so the
child's vector is pure child consumption — effort spent inside a service sub-plan is
effort spent on the parent's task. `user_prompt_count` lives outside `effort_actuals` and
is deliberately not zeroed by push.

THE SENTINEL THAT CUSTODY DEPENDS ON: `effort_baseline` is `None` when unarmed and a dict
when armed (ARMED-ONLY above) — a ZEROED dict, which is what push resets it to, still
reads as armed, and `arm()`'s "snapshot only if unset" check must not re-snapshot it. A
future edit that treats an all-zero baseline as equivalent to unset would silently re-open
the window for a freshly-pushed child and skip its snapshot. `None` is the unarmed value
everywhere — fresh session, after push, after reset — never a zeroed dict.

SUBTREE ROLL-UP. A stage that dispatched linked children also owns their spend. A spawn
cost row names the engine session it was made for in `spawner_session`; the CALLER passes
the set of this session's linked descendants as plain data and `refresh_subtree_spend`
books each descendant's rows once, against `state.effort_subtree_seen[descendant]`, never
against `plan_path`. `effort_subtree_seen` is session-scoped and deliberately outside
`PlanFrame`, so push and pop neither reset nor merge it and a descendant is counted once
across sub-plan custody. With no descendants the result is exactly `refresh_spend`.

CHILD RUNS. The wall-clock scale also owns the time a linked child ran. `cmd_dispatch`
records each linked dispatch as an interval in `state.effort_child_runs` ({child_session,
stage_index, started, ended | None, dispatcher_pid}); `actual(state, now=...)` adds the
measure of the UNION of those intervals (an open one ends at `now`) to the session's own
`active_minutes`. The prompt-gap hook books only the part of a gap no child run covers
(`covered_seconds`), so an interval is counted once by whichever clock owns it. The union
is session-scoped like `effort_subtree_seen` and a grandchild's run lives in the child's
state, so it reaches the parent only inside the child's own interval. Without `now`
(or without runs) the wall-clock actual is exactly `active_minutes`. A run whose dispatcher
died is closed in the state it belongs to (`child_runs.close_dead_runs`, shared by the CLI
and the hook) before any of this reads it, so the hook and `actual` see one `ended`.

Purity: this module reads only the state object it is handed plus config.md's constants.
It never shells out, opens a socket, or reads a clock — the cost-ledger read belongs to
the CALLER (cli.py) and arrives as data, and the wall-clock accumulation happens in
hook-engine-start.py. scripts/tests/test_effort.py pins that with the same
`ast_purity.impure_names` predicate verify-agentctl applies to gates.py.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from .child_runs import pid_alive
from .config import Thresholds
from .state import EFFORT_FIRE_FINISHED_WORK, SessionState, WeightClass, is_skipped

# --- the four scales ---------------------------------------------------------

SCALE_SPEND = "spend"
SCALE_WALL_CLOCK = "wall_clock"
SCALE_REPLANS = "replans"
SCALE_INTERACTIONS = "interactions"

#: Scales compared as `delta / estimate >= effort-divergence-multiple`. An estimate of 0
#: makes a ratio scale INAPPLICABLE (and is the division-by-zero guard) — there is no
#: separate numeric floor row; see config.md's `effort-divergence-multiple` for why.
RATIO_SCALES = (SCALE_SPEND, SCALE_WALL_CLOCK)

#: Scales compared as `delta >= <configured absolute count>`. They carry no ratio: a
#: plan's intended replan count is structurally 0, and its intended interaction count is
#: not derivable at all (cmd_drive collapses the spine into one call). A threshold of 0
#: means the scale is ACCOUNTING-ONLY — measured, reported, never fired on.
ABSOLUTE_SCALES = (SCALE_REPLANS, SCALE_INTERACTIONS)

#: Deterministic order for reporting and for breaking a tie between two scales that
#: diverge by the same multiple.
SCALE_ORDER = (SCALE_SPEND, SCALE_WALL_CLOCK, SCALE_REPLANS, SCALE_INTERACTIONS)

#: Scales armed but excluded from divergence(): crossing the threshold is observed and
#: appended to SessionState.effort_crossings, but never returned as a Divergence — no
#: fire, no baseline rebase, no DIAGNOSING route. See config.md's
#: `effort-absolute-interactions` for why this scale specifically is record-only.
RECORD_ONLY_SCALES = frozenset({SCALE_INTERACTIONS})

_LABEL = {
    SCALE_SPEND: "attributed spend",
    SCALE_WALL_CLOCK: "active wall-clock",
    SCALE_REPLANS: "replan count",
    SCALE_INTERACTIONS: "user interactions",
}
_UNIT = {
    SCALE_SPEND: "USD",
    SCALE_WALL_CLOCK: "min",
    SCALE_REPLANS: "replans",
    SCALE_INTERACTIONS: "prompts",
}

#: Keys inside `state.effort_actuals` — the two accumulators that are WRITTEN over time
#: (the other two actuals are derived on read from history / user_prompt_count).
ORDER_SPEND_KEY = "order_spend"
ORDER_WALL_CLOCK_KEY = "order_wall_clock"
ACTUAL_SPEND_KEY = "spend_usd"
ACTUAL_MINUTES_KEY = "active_minutes"

#: Default cost tier for a stage that declares none. Spawn stages default to the same
#: "medium" cmd_dispatch falls back to; in_thread stages are priced at the SMALL row on
#: the wall-clock scale (in-thread work consumes wall-clock though it consumes no
#: attributable spend). An explicitly declared tier always wins over both, mirroring
#: dispatch's own flag > declaration > default ladder.
_DEFAULT_TIER_SPAWN = "medium"
_DEFAULT_TIER_IN_THREAD = "small"

#: Tier of the engine-MANDATED review spawns folded into the spend estimate. These are
#: derivable from the plan (gates.plan_review_blockers requires one thinker review of
#: every substantive plan; gates.code_review_blockers requires one code review per
#: spawn:developer stage), they are charged to this task, and the actual counts them —
#: so omitting them from the estimate would make every plan look over-budget by its own
#: mandated review cost.
_REVIEW_TIER = "medium"

_DEVELOPER_KIND = "developer"


@dataclass(frozen=True)
class Divergence:
    """One scale's verdict that the session has run past its plan.

    `actual` and `estimate` are the arming-relative delta and the quantity it was
    compared against — the stored estimate for a ratio scale, the configured absolute
    count for an absolute one. `multiple` is their quotient in both cases, so a caller
    can rank or report the two shapes uniformly."""
    scale: str
    kind: str  # "ratio" | "absolute"
    actual: float
    estimate: float
    multiple: float
    framing: str


# --- the estimate: what the CURRENT plan claims -------------------------------

def _stage_tier(stage) -> str:
    if stage.actor.cost_tier:
        return stage.actor.cost_tier
    return _DEFAULT_TIER_SPAWN if stage.is_spawn() else _DEFAULT_TIER_IN_THREAD


def _replans_since_baseline(state: SessionState) -> int:
    """Replan events logged since the window opened — mirrors `deltas()`'s
    SCALE_REPLANS delta, read directly here because `estimate()` runs before there is
    anything for `deltas()` to compare against. `effort_baseline` is `None` until
    `arm()` sets it (ARMED-ONLY), so a session mid-arm or never armed has no window
    yet and counts 0 — matching the flat `1` this replaced for a freshly-approved
    plan's first estimate. Named for the BASELINE, not for arming: `record_fire`
    rebases the baseline at every firing, so after one the count restarts there.

    The `max(0, …)` floor is deliberate where `deltas()` refuses one: a negative
    review COUNT is not a diagnosable signal, it is nonsense that would silently
    shrink the comparand, whereas a negative DELTA is exactly the broken-monotonicity
    evidence `deltas()` wants to surface.

    An approximation, not an exact spawn count: a replan retried against
    byte-identical, already-reviewed bytes needs no new spawn (over-counts here); a
    reviewer round that returns `revise` before a passing verdict costs more than one
    spawn per replan (under-counts here). Both are rare corners; what this preserves
    is the WINDOW match with the actual side, which is what a divergence comparison
    needs — exact review-dollar accounting is the ledger's job, not this module's."""
    if state.effort_baseline is None:
        return 0
    base = float(state.effort_baseline.get(SCALE_REPLANS) or 0.0)
    return max(0, replan_count(state) - int(base))


def _replan_events_since_baseline(state: SessionState) -> int:
    """Replan events of ANY class logged since the window opened. A bookkeeping or
    check-only replan changes the plan's bytes, so `gates.plan_review_blockers` still
    owes it a thinker review; the mandated-review estimate must count it even though the
    replans SCALE (`replan_count`) does not. The baseline snapshot records the all-class
    total under `REPLAN_EVENTS_KEY`; a baseline written before that key existed falls
    back to the operative-only count."""
    if state.effort_baseline is None:
        return 0
    base = state.effort_baseline.get(REPLAN_EVENTS_KEY)
    if base is None:
        return _replans_since_baseline(state)
    return max(0, sum(replan_class_counts(state).values()) - int(base))


def _snapshot_baseline(state: SessionState, now: float | None) -> dict:
    snap = actual(state, now=now)
    snap[REPLAN_EVENTS_KEY] = float(sum(replan_class_counts(state).values()))
    return snap


def _mandated_reviews(state: SessionState) -> int:
    """Count of engine-mandated review spawns derivable from the plan and its history.

    One thinker plan review for the plan as currently approved, plus one code review
    per spawn:developer stage, plus one FURTHER thinker review per replan logged
    since arming — `gates.plan_review_blockers` requires a review bound to the exact
    plan bytes at EVERY replan, not only at the first approval, and the actual side
    (`refresh_spend`) sums every one of those spawns by `plan_path` with no
    stage_index filter. Counting only the flat initial review would silently inflate
    the spend ratio on every replan, biasing exactly the sessions already closest to
    the multiple (finding: a replan's mandated review must be counted in the SAME
    window the actual side is compared over). Both gates are SUBSTANTIVE-only, so a
    non-substantive session mandates none — which is what lets a plan with no spawn
    stages estimate 0 and leave the spend scale inapplicable.

    THE FLAT `1` IS WINDOW-RELATIVE TOO, BUT ONLY BECAUSE OF AN ORDERING cli.py OWNS.
    The initial thinker review is spawned BEFORE `cmd_approve`, so its ledger rows
    exist by arming time — yet they fall INSIDE the window, because no `refresh_spend`
    call site runs at `cmd_approve`: the baseline is snapshotted from an accumulator
    those rows have not been booked into, and the first post-arming refresh books them
    into the delta. So estimate and actual both carry that review. If a future edit
    adds a `refresh_spend` call to `cmd_approve` — or moves one before `arm()` — the
    review's cost lands in the baseline instead, the flat `1` becomes a pure
    over-charge, and this term must drop to `developers + _replan_events_since_baseline`."""
    if state.weight_class != WeightClass.SUBSTANTIVE.value:
        return 0
    developers = sum(
        1 for s in state.stages
        if s.is_spawn() and s.spawn_kind() == _DEVELOPER_KIND
        and not is_skipped(s)
    )
    return 1 + developers + _replan_events_since_baseline(state)


def estimate(state: SessionState, thr: Thresholds | None = None) -> dict:
    """The CURRENT plan's declared cost, per ratio scale.

    Derived over `state.stages`, plus — for the mandated-review count alone —
    `state.effort_baseline` and the replan events in `state.history` (see
    `_replan_events_since_baseline`); never `state.effort_estimate` itself, so `rederive()`
    calling this to overwrite that very field is not circular. `rederive()` is the
    only writer that stores the result; `divergence()` never calls this, so a session
    that has not been armed has nothing to be compared against. The absolute scales
    appear nowhere here — they have no estimate by construction (see ABSOLUTE_SCALES).

    in_thread stages contribute nothing to SPEND (cost.py attributes no cost to them, so
    counting them on the estimate side alone would systematically deflate the ratio) but
    do contribute to WALL_CLOCK, the one scale on which the executor kinds are symmetric.
    """
    thr = thr if thr is not None else Thresholds()

    spend = 0.0
    minutes = 0.0
    for stage in state.stages:
        if is_skipped(stage):
            continue
        tier = _stage_tier(stage)
        if stage.is_spawn():
            spend += thr.budget_usd_float(tier)
        minutes += float(thr.effort_stage_minutes(tier))

    spend += _mandated_reviews(state) * thr.budget_usd_float(_REVIEW_TIER)

    return {SCALE_SPEND: spend, SCALE_WALL_CLOCK: minutes}


# --- the actual: what the session has consumed --------------------------------

#: Key of the all-class replan-event total inside `state.effort_baseline`; not a scale, so
#: `deltas()` (which reads `SCALE_ORDER`) never sees it.
REPLAN_EVENTS_KEY = "replan_events"

#: The class a `replan` event carries when none is recorded (a history written before the
#: classes existed, or a replan with no known old side): counted, never silently dropped.
OPERATIVE_CLASS = "operative"


def replan_class_counts(state: SessionState) -> dict:
    """Replans logged on this session, per class (`plan.REPLAN_CLASSES`). An event with no
    recorded class is operative."""
    counts = {"no_change": 0, "bookkeeping": 0, "check_only": 0, OPERATIVE_CLASS: 0}
    for h in state.history:
        if h.get("event") == "replan":
            cls = h.get("change_class")
            counts[cls if cls in counts else OPERATIVE_CLASS] += 1
    return counts


def replan_count(state: SessionState) -> int:
    """Operative replans logged on this session — the figure the REPLANS scale compares.
    A no_change, bookkeeping or check-only replan moves no operative surface, so it is
    kept apart (`replan_class_counts`) instead of spending the budget a replan loop on a
    plan that did not change would otherwise exhaust. Belt 2 of the re-arm rule
    (`_replans_since_last_fire`) still counts every logged replan."""
    return replan_class_counts(state)[OPERATIVE_CLASS]


def child_run_intervals(runs, now: float | None = None) -> list[tuple[float, float]]:
    """`(start, end)` seconds of each child run, malformed rows skipped.

    A closed run is `(started, max(started, ended))`. An open run is `(started, now)`;
    with `now=None` it is skipped. A run whose dispatcher died is closed by the caller
    first (`child_runs.close_dead_runs`); one it could not close yet (held open for a state
    file that may be its child) has no known end and counts nothing, never growing to `now`."""
    intervals: list[tuple[float, float]] = []
    for run in runs or ():
        if not isinstance(run, dict):
            continue
        started = run.get("started")
        if not isinstance(started, (int, float)):
            continue
        ended = run.get("ended")
        if isinstance(ended, (int, float)):
            end = float(ended)
        elif now is not None and pid_alive(run.get("dispatcher_pid")):
            end = now
        else:
            continue
        intervals.append((float(started), max(float(started), float(end))))
    return intervals


def _merge_intervals(intervals) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def union_seconds(intervals) -> float:
    """The measure of the union of `intervals` — overlapping runs count once."""
    return sum(end - start for start, end in _merge_intervals(intervals))


def covered_seconds(intervals, lo: float, hi: float) -> float:
    """The part of `[lo, hi]` that `intervals` cover, each instant once."""
    return sum(
        max(0.0, min(end, hi) - max(start, lo)) for start, end in _merge_intervals(intervals)
    )


def overlap_seconds(intervals, cover) -> float:
    """The measure of the part of `intervals` that `cover` also covers, each instant once."""
    return sum(covered_seconds(intervals, lo, hi) for lo, hi in _merge_intervals(cover))


def child_run_minutes(state: SessionState, now: float | None = None) -> float:
    """Minutes covered by this session's child runs, each instant once (see CHILD RUNS)."""
    return union_seconds(child_run_intervals(state.effort_child_runs, now)) / 60.0


def actual(state: SessionState, *, now: float | None = None) -> dict:
    """The accumulated actual vector, on all four scales.

    Two entries are read from monotone accumulators written elsewhere (`effort_actuals`,
    fed by refresh_spend and by hook-engine-start's prompt stamp); two are derived on
    read. Never a fold over `state.stages`: a substantive replan drops stages, so a fold
    would erase its own evidence at exactly the moment the true divergence is largest.

    `state.effort_actuals` is typed `dict` (default_factory, never Optional) unlike
    `effort_estimate`/`effort_baseline`, so it is read directly with no `or {}` guard —
    `refresh_spend`, its only writer, makes the same assumption."""
    actuals = state.effort_actuals
    return {
        SCALE_SPEND: float(actuals.get(ACTUAL_SPEND_KEY) or 0.0),
        SCALE_WALL_CLOCK: float(actuals.get(ACTUAL_MINUTES_KEY) or 0.0)
        + child_run_minutes(state, now),
        SCALE_REPLANS: float(replan_count(state)),
        SCALE_INTERACTIONS: float(state.user_prompt_count or 0),
    }


def deltas(state: SessionState, *, now: float | None = None) -> dict:
    """The arming-relative actual: `actual - baseline`, per scale.

    Never negative, by construction and with no clamp: every accumulator this
    module's writers touch is monotone (spend and minutes only grow; replans and
    interactions only increase), and the baseline is always a PAST snapshot of the
    same vector, so `actual >= baseline` always holds. A future writer that broke
    monotonicity should surface here as a negative delta, not be silently floored
    into looking like zero divergence.

    An unarmed session has no baseline; its deltas are then the raw actuals, which is
    reporting-only — `divergence()` refuses to fire without a baseline."""
    acts = actual(state, now=now)
    base = state.effort_baseline or {}
    return {
        scale: acts[scale] - float(base.get(scale) or 0.0)
        for scale in SCALE_ORDER
    }


# --- the comparison -----------------------------------------------------------

def _thresholds_for_absolute(thr: Thresholds) -> dict:
    return {
        SCALE_REPLANS: float(thr.effort_replan_absolute()),
        SCALE_INTERACTIONS: float(thr.effort_absolute_interactions()),
    }


def comparands(state: SessionState, thr: Thresholds | None = None) -> dict:
    """What each scale's delta is measured against: the STORED estimate for a ratio
    scale, the configured absolute count for an absolute one. A zero means the scale
    is inapplicable (no stored estimate, or an accounting-only threshold).

    Split out of `ratios` so a REPORT can name the same comparand the comparison used,
    without either recomputing the ladder or reaching into `_thresholds_for_absolute`."""
    thr = thr if thr is not None else Thresholds()
    est = state.effort_estimate or {}
    absolute = _thresholds_for_absolute(thr)
    return {
        scale: (float(est.get(scale) or 0.0) if scale in RATIO_SCALES else absolute[scale])
        for scale in SCALE_ORDER
    }


def describe(scale: str) -> tuple[str, str]:
    """This scale's human label and unit — for a caller that formats its own numbers
    (the read-only `effort-check` report, the watch hook's one line) instead of taking
    the fully-formed `Divergence.framing`."""
    return _LABEL[scale], _UNIT[scale]


def effective_deltas(
    state: SessionState, *, cross_session_totals: dict | None = None, now: float | None = None,
) -> dict:
    """The delta vector every comparison in this module runs against: `deltas()`, with
    the REPLANS scale raised to the task's cross-session replan total when that is larger
    (see the module docstring's CROSS-SESSION section for why larger-of, not sum).

    Named and shared because it has TWO consumers, and they must not disagree:
    `divergence()`, which decides whether a fire site acts, and the read-only
    `effort-check` report, which is what the coordinator and its UserPromptSubmit watch
    hook actually read. A report computed from session-local `deltas()` while the
    decision ran on this vector went silent exactly where the loop it watches lives: a
    resolved re-entry builds a fresh SessionState whose own `replan_count` is 0 while the
    accumulator still holds the prior laps, so the report said "no scale over threshold"
    on the very session `divergence()` would have fired on. One function, both callers.

    `cross_session_totals=None` reproduces `deltas()` exactly."""
    delta = deltas(state, now=now)
    totals = cross_session_totals or {}
    cross_replans = float(totals.get("replan_count") or 0.0)
    if cross_replans > delta[SCALE_REPLANS]:
        delta[SCALE_REPLANS] = cross_replans
    for scale, key in ((SCALE_SPEND, ORDER_SPEND_KEY), (SCALE_WALL_CLOCK, ORDER_WALL_CLOCK_KEY)):
        order_total = float(totals.get(key) or 0.0)
        if order_total > delta[scale]:
            delta[scale] = order_total
    return delta


def effective_ratios(
    state: SessionState, thr: Thresholds | None = None, *,
    cross_session_totals: dict | None = None, now: float | None = None,
) -> dict:
    """Per-scale `effective_delta / comparand`, or None where the scale is inapplicable.

    The comparand is the STORED estimate for a ratio scale and the configured absolute
    count for an absolute one. None means "this scale cannot fire and cannot be ranked":
    an unarmed session, a zero estimate, or a zero (accounting-only) threshold."""
    comparand = comparands(state, thr)
    delta = effective_deltas(state, cross_session_totals=cross_session_totals, now=now)
    return {
        scale: (delta[scale] / comparand[scale]) if comparand[scale] > 0 else None
        for scale in SCALE_ORDER
    }


def ratios(state: SessionState, thr: Thresholds | None = None, *, now: float | None = None) -> dict:
    """The session-local ratio vector — `effective_ratios` with no cross-session totals.

    Kept as its own name because the quality-ledger row wants exactly this: what THIS
    session consumed, not what the task accumulated across every session that touched
    it. A caller deciding or reporting on a divergence wants `effective_ratios`."""
    return effective_ratios(state, thr, now=now)


def _replans_since_last_fire(state: SessionState) -> int:
    """Replan events logged after the most recent firing (belt 2 of the re-arm rule)."""
    if not state.effort_fires:
        return 0
    start = int(state.effort_fires[-1].get("history_len") or 0)
    return sum(1 for h in state.history[start:] if h.get("event") == "replan")


def armed(state: SessionState) -> bool:
    """True iff `arm()` has run: both the stored estimate and the baseline are present."""
    return state.effort_estimate is not None and state.effort_baseline is not None


def _framing(scale: str, kind: str, act: float, comparand: float, multiple: float) -> str:
    label, unit = _LABEL[scale], _UNIT[scale]
    if kind == "ratio":
        against = f"a re-derived estimate of {comparand:.2f} {unit}"
    else:
        against = f"an absolute trigger of {comparand:g} {unit}"
    return (
        f"Accumulated {label} since plan approval is {act:.2f} {unit} against {against} "
        f"— {multiple:.1f}x over. The chosen norm is visibly missing something essential "
        f"about the real situation: find WHAT the plan does not account for, rather than "
        f"re-estimating the same plan."
    )


def divergence(
    state: SessionState, thr: Thresholds | None = None, *,
    cross_session_totals: dict | None = None, now: float | None = None,
) -> Divergence | None:
    """The scale on which this session has diverged past the multiple, or None.

    Returns None — no fire — when any of these hold, and each is deliberate:
      * the session is not ARMED (no stored estimate or no baseline): only cmd_approve
        arms, so a SMALL_CHANGE session routed straight to EXECUTING can never fire;
      * a firing has already happened and no `replan` has been logged since (belt 2) —
        except a firing on finished work, after which no difficulty and so no replan
        follows: the rebased baseline alone re-arms it;
      * every scale is inapplicable (zero estimate / accounting-only threshold) or below
        the multiple.

    `cross_session_totals` (item B, module docstring's CROSS-SESSION section) is the
    optional caller-supplied `per_axis_totals` dict from `task_accumulator.get`. Only
    its `"replan_count"` entry is consulted, and only for the REPLANS scale, whose
    actual becomes `max(delta[SCALE_REPLANS], cross_session_totals["replan_count"])` —
    the larger of this session's own count and the task's accumulated cross-session
    count, so a restarted session on the same stuck task inherits the prior session's
    count instead of starting a fresh budget. `None` (the default) reproduces the
    session-local-only behavior exactly, unchanged from before item B.

    When several scales fire, the one furthest past its OWN trigger is returned (ties
    break on SCALE_ORDER) — NOT the one with the largest raw `multiple`. A ratio scale's
    `multiple` trips at `effort-divergence-multiple` (e.g. 5.0) while an absolute scale's
    is already normalized by `ratios()` to trip at 1.0, so ranking on the raw field would
    always favor a ratio scale barely past its own trigger over an absolute scale far
    past its own. Dividing a ratio scale's `multiple` by the configured multiple puts
    both kinds back on the same "how far past its own line" footing; this only changes
    WHICH fired scale is reported, never WHETHER one fires (that decision already ran,
    above).

    CALLER OBLIGATION: a caller that ACTS on a returned Divergence — enters
    declare -> investigate -> critique — MUST call `record_fire()` afterward. Both
    re-arm belts above read only what `record_fire` writes (the rebased baseline, the
    appended `effort_fires` entry); a caller that diagnoses but never records leaves
    the same divergence eligible to fire on every subsequent command, turning a
    one-time diagnosis into a loop that spends exactly the user attention this module
    exists to save. This module cannot enforce the call — `divergence()` only reads
    state, `record_fire` is the only writer — so the obligation is stated here
    because `divergence()`, not `record_fire()`, is the entry point a fire site
    actually calls.
    """
    if not armed(state):
        return None
    if (
        state.effort_fires
        and state.effort_fires[-1].get("disposition") != EFFORT_FIRE_FINISHED_WORK
        and _replans_since_last_fire(state) < 1
    ):
        return None

    thr = thr if thr is not None else Thresholds()
    multiple = thr.effort_divergence_multiple()
    comparands_by_scale = comparands(state, thr)
    delta = effective_deltas(state, cross_session_totals=cross_session_totals, now=now)
    rat = effective_ratios(state, thr, cross_session_totals=cross_session_totals, now=now)
    # True when the accumulator, not this session's own history, supplied the replan
    # count — the only thing this module still needs the session-local vector for.
    replans_cross_session = delta[SCALE_REPLANS] > deltas(state, now=now)[SCALE_REPLANS]

    candidates: list[Divergence] = []
    for scale in SCALE_ORDER:
        if scale in RECORD_ONLY_SCALES:
            continue
        observed = rat[scale]
        if observed is None:
            continue
        comparand = comparands_by_scale[scale]
        if scale in RATIO_SCALES:
            kind, fires = "ratio", observed >= multiple
        else:
            kind, fires = "absolute", delta[scale] >= comparand
        if not fires:
            continue
        act = delta[scale]
        framing = _framing(scale, kind, act, comparand, observed)
        if scale == SCALE_REPLANS and replans_cross_session:
            framing = (
                f"{framing} (includes {act:g} replans accumulated across prior "
                f"sessions on this task, via the cross-session task accumulator)"
            )
        candidates.append(
            Divergence(
                scale=scale,
                kind=kind,
                actual=act,
                estimate=comparand,
                multiple=observed,
                framing=framing,
            )
        )

    if not candidates:
        return None

    def _past_own_trigger(d: Divergence) -> float:
        if d.kind == "ratio" and multiple > 0:
            return d.multiple / multiple
        return d.multiple

    return max(candidates, key=lambda d: (_past_own_trigger(d), -SCALE_ORDER.index(d.scale)))


# --- the writers --------------------------------------------------------------
# rederive / arm / refresh_spend are the ONLY writers of the estimate, the baseline and
# the spend accumulator respectively. record_fire is the fire SITE's writer (cli.py), kept
# here so the two re-arm belts live beside the predicate that reads them.

def rederive(state: SessionState, thr: Thresholds | None = None) -> dict:
    """Recompute and STORE the estimate from the current stage list. The sole writer of
    `state.effort_estimate` — called at arming and on every replan branch, so a plan that
    grows or contracts is always compared against what it currently claims."""
    est = estimate(state, thr)
    if state.order_effort_frozen:
        for scale in RATIO_SCALES:
            if scale in state.order_effort_frozen:
                est[scale] = float(state.order_effort_frozen[scale])
    state.effort_estimate = est
    return state.effort_estimate


def arm(state: SessionState, thr: Thresholds | None = None, *, now: float | None = None) -> dict:
    """Open the measurement window: re-derive the estimate, and snapshot the baseline
    ONLY IF it is unset (ARMED AT MOST ONCE / SUB-PLAN CUSTODY's sentinel note, module
    docstring — "unset" means `is None`, never a zeroed dict). `now` counts a child run
    still in progress up to the moment of arming, so the baseline does not leave out time
    every later delta includes."""
    rederive(state, thr)
    if state.effort_baseline is None:
        state.effort_baseline = _snapshot_baseline(state, now)
    return state.effort_estimate


def refresh_spend(state: SessionState, rows: list[dict], path: str | None) -> float:
    """Book any cost-ledger spend not yet accumulated under `path`. Returns the delta.

    Sums EVERY row whose `plan_path` equals `path`, with no stage_index condition:
    `--stage-index` is optional on spawn-specialist.py, so every review-round spawn
    writes a row cost.attribute_stage cannot see — precisely the mandated reviews the
    estimate now counts. `rows` and `path` are ARGUMENTS, not reads: the ledger read
    belongs to the caller (keeping this module pure), and passing the path lets
    cmd_replan refresh against the OLD plan_path after state.plan_path was rewritten.

    `effort_spend_seen` is keyed BY PATH rather than being one high-water scalar,
    because a replan rewrites plan_path and the new path sums from its own 0 — a single
    scalar would freeze the accumulator until the new path out-grew the old total. The
    per-path entry is itself a high-water mark (`max(total, seen)`, never lowered): a
    row disappearing from the ledger, or a transient regression in a re-read total,
    must not lower the watermark, or the delta would be double-booked once the ledger
    recovers to its prior total."""
    if not path:
        return 0.0
    key = str(path)
    total = 0.0
    for row in rows:
        if row.get("plan_path") != key:
            continue
        value = row.get("cost_usd")
        if isinstance(value, (int, float)):
            total += float(value)
    seen = float(state.effort_spend_seen.get(key) or 0.0)
    delta = max(0.0, total - seen)
    state.effort_actuals[ACTUAL_SPEND_KEY] = (
        float(state.effort_actuals.get(ACTUAL_SPEND_KEY) or 0.0) + delta
    )
    state.effort_spend_seen[key] = max(total, seen)
    return delta


def is_subtree_row(row: dict, descendants: Collection[str]) -> bool:
    """Whether a cost row was spawned by one of `descendants` (its `spawner_session`)."""
    spawner = row.get("spawner_session")
    return isinstance(spawner, str) and spawner in descendants


def own_rows(rows: list[dict], descendants: Collection[str]) -> list[dict]:
    """The rows `refresh_spend` books by `plan_path`: every row NOT spawned by a descendant."""
    return [row for row in rows if not is_subtree_row(row, descendants)]


def refresh_subtree_spend(
    state: SessionState,
    rows: list[dict],
    descendants: Iterable[str],
    path: str | None,
) -> tuple[float, dict[str, tuple[float, float]]]:
    """`refresh_spend` plus the spend of every linked descendant of this session.

    Returns `(delta, booked)`: the total newly booked, and for each descendant that gained
    `session -> (earlier, gain)` — its watermark before this call and the amount booked now.

    A row spawned by a descendant (`spawner_session` in `descendants`) is booked ONLY
    here, against that descendant's watermark in `state.effort_subtree_seen`, whatever
    its `plan_path` — so a descendant row that names this session's own plan is not also
    summed by `refresh_spend`. Every other row goes through `refresh_spend` unchanged, so
    with no descendants this is exactly `refresh_spend`. `path` is the plan path to book
    by (the OLD one when a replan has already rewritten `state.plan_path`). Like
    `refresh_spend`, a watermark is a high-water mark and never lowered."""
    descendants = frozenset(descendants)
    delta = refresh_spend(state, own_rows(rows, descendants), path)
    booked: dict[str, tuple[float, float]] = {}
    totals: dict[str, float] = {}
    for row in rows:
        if not is_subtree_row(row, descendants):
            continue
        value = row.get("cost_usd")
        if isinstance(value, (int, float)):
            totals[row["spawner_session"]] = totals.get(row["spawner_session"], 0.0) + float(value)
    for session in sorted(totals):
        seen = float(state.effort_subtree_seen.get(session) or 0.0)
        gain = max(0.0, totals[session] - seen)
        state.effort_subtree_seen[session] = max(totals[session], seen)
        if not gain:
            continue
        state.effort_actuals[ACTUAL_SPEND_KEY] = (
            float(state.effort_actuals.get(ACTUAL_SPEND_KEY) or 0.0) + gain
        )
        delta += gain
        booked[session] = (seen, gain)
    return delta, booked


def merge_actuals(parent: dict, child: dict) -> dict:
    """Add a popped sub-plan's accumulated actuals onto the restored parent's (SUB-PLAN
    CUSTODY, module docstring). Used ONLY by `cmd_pop_subplan`: push zeroed the child's
    `effort_actuals` to a fresh vector, so everything the child accumulated is pure child
    consumption, and effort spent inside a service sub-plan is effort spent on the
    parent's task. The other four custody fields are restored straight from the frame —
    this is the one field pop adds instead of overwriting."""
    return {
        ACTUAL_SPEND_KEY: float(parent.get(ACTUAL_SPEND_KEY) or 0.0) + float(child.get(ACTUAL_SPEND_KEY) or 0.0),
        ACTUAL_MINUTES_KEY: float(parent.get(ACTUAL_MINUTES_KEY) or 0.0) + float(child.get(ACTUAL_MINUTES_KEY) or 0.0),
    }


#: Observed tier of a stage whose attributed actual exceeds even the large estimate.
TIER_ABOVE_LARGE = "above_large"

#: `retier` attribution sources: the stage's own cost-ledger rows (spawn stages), or the
#: plan alone when the engine holds no stage-indexed actual — then `actual` and
#: `observed_tier` are None rather than a guessed share of the session total.
ATTRIBUTION_COST_ROWS = "cost_rows"
ATTRIBUTION_PLAN = "plan"

_RETIER_TIERS = ("small", "medium", "large")


def fire_window_stages(state: SessionState) -> list[int]:
    """Stage indices with a `record_result` logged since the previous fire (or since the
    start of history for the first) — the work the fire being raised accounts for."""
    start = int(state.effort_fires[-1].get("history_len") or 0) if state.effort_fires else 0
    seen: list[int] = []
    for h in state.history[start:]:
        index = h.get("stage")
        if h.get("event") == "record_result" and isinstance(index, int) and index not in seen:
            seen.append(index)
    return seen


def _tier_estimate(scale: str, tier: str, thr: Thresholds) -> float:
    if scale == SCALE_SPEND:
        return float(thr.budget_usd_float(tier))
    return float(thr.effort_stage_minutes(tier))


def _stage_attribution(stage, scale: str) -> tuple[float | None, str]:
    """The stage's own actual on `scale` and where it came from. Only a stage with
    cost-ledger rows has one; an in-thread stage consumes wall-clock the engine does not
    attribute to it, so it reports the plan as its only source. The wall-clock figure is
    a proxy: the stage's summed spawn duration from its cost rows, not the active minutes
    `actual()` accumulates for the scale, so the observed tier it yields is approximate."""
    outcome = stage.outcome
    if scale == SCALE_SPEND and outcome.cost_usd is not None:
        return float(outcome.cost_usd), ATTRIBUTION_COST_ROWS
    if scale == SCALE_WALL_CLOCK and outcome.duration_ms is not None:
        return float(outcome.duration_ms) / 60000.0, ATTRIBUTION_COST_ROWS
    return None, ATTRIBUTION_PLAN


def retier_records(
    state: SessionState, scale: str, stage_indices, *, fire_ts: float, thr: Thresholds | None = None,
) -> list[dict]:
    """One datum per stage of a finished-work fire's window: the tier the stage declared
    against the smallest tier whose configured estimate on `scale` covers what the stage
    actually consumed (`above_large` past the largest). The read side of the cost-tier
    floor advisory: whether a stage's typed inputs make it expensive is a calibration
    claim these records measure."""
    thr = thr if thr is not None else Thresholds()
    by_index = {s.index: s for s in state.stages}
    records: list[dict] = []
    for index in stage_indices:
        stage = by_index.get(index)
        if stage is None:
            continue
        declared = _stage_tier(stage)
        act, source = _stage_attribution(stage, scale)
        observed = None
        if act is not None:
            observed = next(
                (t for t in _RETIER_TIERS if _tier_estimate(scale, t, thr) >= act), TIER_ABOVE_LARGE)
        records.append({
            "plan_path": state.plan_path,
            "fire_ts": fire_ts,
            "scale": scale,
            "stage_index": index,
            "declared_tier": declared,
            "observed_tier": observed,
            "actual": act,
            "estimate": _tier_estimate(scale, declared, thr),
            "attribution": source,
        })
    return records


def record_fire(
    state: SessionState, div: Divergence, *, now: float, finished_work: bool = False,
    thr: Thresholds | None = None,
) -> dict:
    """Record a firing and re-arm: REBASE the baseline onto the current actual vector
    (RE-ARM belt 1, module docstring; belt 2 is enforced by `divergence()` reading the
    appended record). MANDATORY after any caller acts on a `Divergence` — see
    `divergence()`'s CALLER OBLIGATION. `now` is REQUIRED and supplied by the caller —
    this module reads no clock, and a default would let a future call site silently
    ship an unstamped `ts` straight onto the durable quality-ledger row.

    The appended record starts with no "ack" key — gates.effort_fire_blockers reads
    exactly that absence to refuse dispatch/replan/submit_plan until
    `agentctl fire-acknowledge` appends one (never replaces the record; the audit
    trail stays append-only).

    `finished_work` marks a fire raised when every stage was already PASSED or SKIPPED:
    the record carries `disposition` finished_work (which `effort_fire_blockers` does not
    hold unacknowledged and `divergence()` exempts from belt 2) and one retier record per
    window stage is appended to `state.effort_retiers`."""
    replaced_baseline = dict(state.effort_baseline or {})
    window = fire_window_stages(state)
    state.effort_baseline = _snapshot_baseline(state, now)
    record = {
        "replaced_baseline": replaced_baseline,
        "scale": div.scale,
        "kind": div.kind,
        "actual": div.actual,
        "estimate": div.estimate,
        "multiple": div.multiple,
        "history_len": len(state.history),
        "window_stages": window,
        "ts": now,
    }
    if finished_work:
        record["disposition"] = EFFORT_FIRE_FINISHED_WORK
        state.effort_retiers.extend(retier_records(state, div.scale, window, fire_ts=now, thr=thr))
    state.effort_fires.append(record)
    return record


def record_crossing(
    state: SessionState, scale: str, *, actual: float, estimate: float, now: float
) -> dict | None:
    """Observe-only counterpart to `record_fire()` for RECORD_ONLY_SCALES: append a
    crossing to `SessionState.effort_crossings` — never `effort_fires`, never a baseline
    rebase, no DIAGNOSING route. Deduplicates against the SAME baseline: a scale that
    stays past its threshold across repeated `record_result`/`verify_final` calls with
    an unchanged baseline records once, not once per call; a later baseline change (a
    genuine re-arm elsewhere) allows a fresh record. `now` is REQUIRED, same reasoning
    as `record_fire()` — this module reads no clock."""
    baseline = float((state.effort_baseline or {}).get(scale) or 0.0)
    for prior in reversed(state.effort_crossings):
        if prior.get("scale") == scale:
            if float(prior.get("baseline") or 0.0) == baseline:
                return None
            break
    record = {
        "scale": scale,
        "actual": actual,
        "estimate": estimate,
        "baseline": baseline,
        "ts": now,
    }
    state.effort_crossings.append(record)
    return record
