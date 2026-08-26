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
site, the same three sites `replan_count()` itself counts from), so `divergence()`
takes `max(delta[SCALE_REPLANS], cross_session_totals["replan_count"])` rather
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

Purity: this module reads only the state object it is handed plus config.md's constants.
It never shells out, opens a socket, or reads a clock — the cost-ledger read belongs to
the CALLER (cli.py) and arrives as data, and the wall-clock accumulation happens in
hook-engine-start.py. scripts/tests/test_effort.py pins that with the same
`ast_purity.impure_names` predicate verify-agentctl applies to gates.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Thresholds
from .state import SessionState, WeightClass

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
    over-charge, and this term must drop to `developers + _replans_since_baseline`."""
    if state.weight_class != WeightClass.SUBSTANTIVE.value:
        return 0
    developers = sum(
        1 for s in state.stages if s.is_spawn() and s.spawn_kind() == _DEVELOPER_KIND
    )
    return 1 + developers + _replans_since_baseline(state)


def estimate(state: SessionState, thr: Thresholds | None = None) -> dict:
    """The CURRENT plan's declared cost, per ratio scale.

    Derived over `state.stages`, plus — for the mandated-review count alone —
    `state.effort_baseline` and the replan events in `state.history` (see
    `_replans_since_baseline`); never `state.effort_estimate` itself, so `rederive()`
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
        tier = _stage_tier(stage)
        if stage.is_spawn():
            spend += thr.budget_usd_float(tier)
        minutes += float(thr.effort_stage_minutes(tier))

    spend += _mandated_reviews(state) * thr.budget_usd_float(_REVIEW_TIER)

    return {SCALE_SPEND: spend, SCALE_WALL_CLOCK: minutes}


# --- the actual: what the session has consumed --------------------------------

def replan_count(state: SessionState) -> int:
    """Replans logged on this session — the same predicate cmd_resolve's quality row uses."""
    return sum(1 for h in state.history if h.get("event") == "replan")


def actual(state: SessionState) -> dict:
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
        SCALE_WALL_CLOCK: float(actuals.get(ACTUAL_MINUTES_KEY) or 0.0),
        SCALE_REPLANS: float(replan_count(state)),
        SCALE_INTERACTIONS: float(state.user_prompt_count or 0),
    }


def deltas(state: SessionState) -> dict:
    """The arming-relative actual: `actual - baseline`, per scale.

    Never negative, by construction and with no clamp: every accumulator this
    module's writers touch is monotone (spend and minutes only grow; replans and
    interactions only increase), and the baseline is always a PAST snapshot of the
    same vector, so `actual >= baseline` always holds. A future writer that broke
    monotonicity should surface here as a negative delta, not be silently floored
    into looking like zero divergence.

    An unarmed session has no baseline; its deltas are then the raw actuals, which is
    reporting-only — `divergence()` refuses to fire without a baseline."""
    acts = actual(state)
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


def ratios(state: SessionState, thr: Thresholds | None = None) -> dict:
    """Per-scale `delta / comparand`, or None where the scale is inapplicable.

    The comparand is the STORED estimate for a ratio scale and the configured absolute
    count for an absolute one. None means "this scale cannot fire and cannot be ranked":
    an unarmed session, a zero estimate, or a zero (accounting-only) threshold. Read by
    the quality-ledger row as well as by `divergence()`."""
    thr = thr if thr is not None else Thresholds()
    est = state.effort_estimate or {}
    absolute = _thresholds_for_absolute(thr)
    delta = deltas(state)

    out: dict[str, float | None] = {}
    for scale in SCALE_ORDER:
        comparand = (
            float(est.get(scale) or 0.0) if scale in RATIO_SCALES else absolute[scale]
        )
        out[scale] = (delta[scale] / comparand) if comparand > 0 else None
    return out


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
    state: SessionState, thr: Thresholds | None = None, *, cross_session_totals: dict | None = None
) -> Divergence | None:
    """The scale on which this session has diverged past the multiple, or None.

    Returns None — no fire — when any of these hold, and each is deliberate:
      * the session is not ARMED (no stored estimate or no baseline): only cmd_approve
        arms, so a SMALL_CHANGE session routed straight to EXECUTING can never fire;
      * a firing has already happened and no `replan` has been logged since (belt 2);
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
    if state.effort_fires and _replans_since_last_fire(state) < 1:
        return None

    thr = thr if thr is not None else Thresholds()
    multiple = thr.effort_divergence_multiple()
    est = state.effort_estimate or {}
    absolute = _thresholds_for_absolute(thr)
    delta = deltas(state)
    rat = ratios(state, thr)

    cross_replans = float((cross_session_totals or {}).get("replan_count") or 0.0)
    replans_cross_session = cross_replans > delta[SCALE_REPLANS]
    effective_replans = cross_replans if replans_cross_session else delta[SCALE_REPLANS]

    candidates: list[Divergence] = []
    for scale in SCALE_ORDER:
        is_cross_replans = scale == SCALE_REPLANS and replans_cross_session
        if is_cross_replans:
            comparand = absolute[SCALE_REPLANS]
            observed = (effective_replans / comparand) if comparand > 0 else None
        else:
            observed = rat[scale]
        if observed is None:
            continue
        if scale in RATIO_SCALES:
            kind = "ratio"
            comparand, fires = float(est.get(scale) or 0.0), observed >= multiple
        else:
            kind, comparand = "absolute", absolute[scale]
            fires = effective_replans >= comparand if is_cross_replans else delta[scale] >= comparand
        if not fires:
            continue
        act = effective_replans if is_cross_replans else delta[scale]
        framing = _framing(scale, kind, act, comparand, observed)
        if is_cross_replans:
            framing = (
                f"{framing} (includes {cross_replans:g} replans accumulated across prior "
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
    state.effort_estimate = estimate(state, thr)
    return state.effort_estimate


def arm(state: SessionState, thr: Thresholds | None = None) -> dict:
    """Open the measurement window: re-derive the estimate, and snapshot the baseline
    ONLY IF it is unset (ARMED AT MOST ONCE / SUB-PLAN CUSTODY's sentinel note, module
    docstring — "unset" means `is None`, never a zeroed dict)."""
    rederive(state, thr)
    if state.effort_baseline is None:
        state.effort_baseline = actual(state)
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


def record_fire(state: SessionState, div: Divergence, *, now: float) -> dict:
    """Record a firing and re-arm: REBASE the baseline onto the current actual vector
    (RE-ARM belt 1, module docstring; belt 2 is enforced by `divergence()` reading the
    appended record). MANDATORY after any caller acts on a `Divergence` — see
    `divergence()`'s CALLER OBLIGATION. `now` is REQUIRED and supplied by the caller —
    this module reads no clock, and a default would let a future call site silently
    ship an unstamped `ts` straight onto the durable quality-ledger row."""
    state.effort_baseline = actual(state)
    record = {
        "scale": div.scale,
        "kind": div.kind,
        "actual": div.actual,
        "estimate": div.estimate,
        "multiple": div.multiple,
        "history_len": len(state.history),
        "ts": now,
    }
    state.effort_fires.append(record)
    return record
