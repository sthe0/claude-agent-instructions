"""Effort-divergence trigger: the pure estimate / actual / divergence layer.

The invariants under test are the ones that decide whether the trigger fires on WORK
rather than on the shape of the session — THE WINDOW; ARMED-ONLY, AND ARMED AT MOST ONCE;
MONOTONE ACTUALS; RE-ARM (both belts); and SUB-PLAN CUSTODY's baseline sentinel, each a
literal heading in effort.py's module docstring, which says what it means and why. ARMED-ONLY alone is driven through the
REAL SMALL_CHANGE route (cmd_classify -> execute_small), not by asserting on a
weight_class field, because the property is a claim about machine.py's transitions.

Every test drives the module's own writers rather than assigning the state fields
directly, so a writer that stops honouring an invariant fails here rather than silently
producing a state no writer would have produced.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agentctl import cli, effort
from agentctl.config import Thresholds
from agentctl.state import (
    Actor,
    Criterion,
    Means,
    Node,
    SessionState,
    Stage,
    Subject,
    WeightClass,
)
from ast_purity import impure_names

# Fixed constants: the assertions below state expected NUMBERS, so they must not move
# when config.md is retuned. Tests that care about a real row build Thresholds() instead.
THR = Thresholds(
    {
        "budget-small-usd": "1.00",
        "budget-medium-usd": "3.00",
        "budget-large-usd": "8.00",
        "effort-stage-minutes-small": "10",
        "effort-stage-minutes-medium": "25",
        "effort-stage-minutes-large": "60",
        "effort-divergence-multiple": "5.0",
        "effort-replan-absolute": "3",
        "effort-absolute-interactions": "0",
        "substantive-wall-clock-min": "30",
    }
)


def stage(index: int, executor: str, tier: str | None = None) -> Stage:
    return Stage(
        index=index,
        title=f"stage {index}",
        subject=Subject(material="m", result="r"),
        means=Means(means="means", method="method"),
        actor=Actor(executor=executor, cost_tier=tier),
        criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
    )


def substantive(stages: list[Stage] | None = None, **kw) -> SessionState:
    state = SessionState(
        session_id="s",
        task_id="t",
        goal="g",
        overall_done_criterion="dc",
        **kw,
    )
    state.weight_class = WeightClass.SUBSTANTIVE.value
    state.stages = list(stages or [])
    return state


# --- estimate: what the plan claims ------------------------------------------

def test_estimate_sums_declared_tiers_over_spawn_stages():
    """Spend folds the DECLARED tier of each spawn stage, plus the mandated reviews."""
    state = substantive([stage(0, "spawn:developer", "large"), stage(1, "spawn:thinker", "small")])
    # 8.00 + 1.00 spawns, + 3.00 plan review + 3.00 code review for the one developer.
    assert effort.estimate(state, THR)[effort.SCALE_SPEND] == pytest.approx(15.0)


def test_estimate_defaults_undeclared_spawn_to_medium():
    state = substantive([stage(0, "spawn:developer")])
    # 3.00 (default medium) + 3.00 plan review + 3.00 code review.
    assert effort.estimate(state, THR)[effort.SCALE_SPEND] == pytest.approx(9.0)


def test_in_thread_stages_cost_no_spend_but_do_cost_wall_clock():
    """cost.py attributes no ledger row to an in_thread stage, so counting it on the
    estimate side alone would systematically deflate the spend ratio. Wall-clock is the
    one scale on which the two executor kinds are symmetric."""
    state = substantive([stage(0, "in_thread"), stage(1, "in_thread")])
    est = effort.estimate(state, THR)
    assert est[effort.SCALE_SPEND] == pytest.approx(3.0)  # the mandated plan review only
    assert est[effort.SCALE_WALL_CLOCK] == pytest.approx(20.0)  # 2 x the small row


def test_wall_clock_uses_declared_tier_over_the_executor_default():
    state = substantive([stage(0, "in_thread", "large"), stage(1, "spawn:thinker")])
    assert effort.estimate(state, THR)[effort.SCALE_WALL_CLOCK] == pytest.approx(85.0)


def test_non_substantive_plan_mandates_no_reviews_so_spend_is_inapplicable():
    """Both review gates are SUBSTANTIVE-only. A non-substantive, spawn-free plan
    therefore estimates 0 — and a 0 estimate makes the scale inapplicable rather than a
    division by zero."""
    state = substantive([stage(0, "in_thread")])
    state.weight_class = WeightClass.SMALL_CHANGE.value
    assert effort.estimate(state, THR)[effort.SCALE_SPEND] == 0.0

    effort.arm(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 500.0
    assert effort.ratios(state, THR)[effort.SCALE_SPEND] is None
    assert effort.divergence(state, THR) is None


def test_absolute_scales_carry_no_estimate():
    est = effort.estimate(substantive([stage(0, "in_thread")]), THR)
    assert set(est) == set(effort.RATIO_SCALES)


def test_mandated_reviews_grow_with_each_replan_since_the_baseline():
    """gates.plan_review_blockers requires a thinker review bound to the exact plan
    bytes at EVERY replan, not only the first approval, and the actual side
    (refresh_spend) sums every one of those review-round spawns by plan_path. A flat
    `1` would silently inflate the spend ratio on every replan — this pins that the
    mandated-review count tracks replans SINCE THE BASELINE, matching the window the
    actual side is compared over."""
    state = substantive([stage(0, "spawn:developer")])
    effort.arm(state, THR)
    assert state.effort_estimate[effort.SCALE_SPEND] == pytest.approx(9.0)  # 1 review

    state.log("replan", kind="substantive")
    state.log("replan", kind="substantive")
    est = effort.rederive(state, THR)  # what a stage-4 replan branch calls
    # 3.00 spawn + (1 initial + 1 developer + 2 replans) reviews x 3.00 = 3 + 12 = 15.
    assert est[effort.SCALE_SPEND] == pytest.approx(15.0)


def test_replans_logged_before_arming_do_not_inflate_the_review_estimate():
    """A pre-approval correction (a PLAN_READY revise loop) is outside the window
    arm() opens; only replans logged AFTER the baseline snapshot count."""
    state = substantive([stage(0, "spawn:developer")])
    state.log("replan", kind="refinement")
    effort.arm(state, THR)
    assert state.effort_estimate[effort.SCALE_SPEND] == pytest.approx(9.0)  # unchanged

    state.log("replan", kind="substantive")
    est = effort.rederive(state, THR)
    assert est[effort.SCALE_SPEND] == pytest.approx(12.0)  # only the post-arming one counts


# --- ARMED-ONLY ---------------------------------------------------------------

def test_unarmed_session_never_fires_however_large_the_actual():
    state = substantive([stage(0, "spawn:developer")])
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 10_000.0
    state.user_prompt_count = 10_000
    assert effort.armed(state) is False
    assert effort.divergence(state, THR) is None


def test_rederive_alone_does_not_arm():
    """Both halves are required: an estimate with no baseline has no window to compare
    over, so it must stay inert."""
    state = substantive([stage(0, "spawn:developer")])
    effort.rederive(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 10_000.0
    assert effort.divergence(state, THR) is None


def test_small_change_route_leaves_the_trigger_inert(store, monkeypatch):
    """The real route, not a field assertion: cmd_classify synthesizes a SMALL_CHANGE
    session that reaches EXECUTING via execute_small, bypassing cmd_approve entirely —
    so nothing ever arms it. This is the property that makes the trigger safe to leave
    unconditional at every future fire site."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    sid = "small-change-session"
    cli.cmd_start(
        Namespace(
            session=sid, task="demo", goal="g", done_criterion="dc",
            criterion_type="measurable", recursion_depth=0,
        ),
        store=store,
    )
    cli.cmd_classify(
        Namespace(
            session=sid, chat=False, changed_lines=5, files=1, wall_clock_min=5,
            tracker_key=None, architectural=False, external_effect=False,
            new_dependency=False, public_api_change=False, deliverable_kind=None,
        ),
        store=store,
    )
    cli.cmd_next_stage(Namespace(session=sid), store=store)  # ROUTED -> EXECUTING

    state = store.load(sid)
    assert state.weight_class == WeightClass.SMALL_CHANGE.value
    assert state.node == Node.EXECUTING.value
    assert state.effort_estimate is None and state.effort_baseline is None

    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 999.0
    state.user_prompt_count = 999
    assert effort.divergence(state, THR) is None


# --- the arming-relative window ----------------------------------------------

def test_baseline_excludes_effort_spent_before_approval():
    """A long pre-approval exchange (research spawn, clarification prompts) must not
    show up as divergence: the estimate only describes work that happens AFTER the plan
    is approved."""
    state = substantive([stage(0, "spawn:developer")])  # estimate: 9.00
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 40.0
    state.user_prompt_count = 30
    effort.arm(state, THR)

    assert state.effort_baseline[effort.SCALE_SPEND] == pytest.approx(40.0)
    assert effort.deltas(state)[effort.SCALE_SPEND] == 0.0
    assert effort.divergence(state, THR) is None

    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 40.0 + 44.9
    assert effort.divergence(state, THR) is None  # 44.9 / 9.00 = 4.99x, under the 5x
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 40.0 + 45.0
    fired = effort.divergence(state, THR)
    assert fired is not None and fired.scale == effort.SCALE_SPEND
    assert fired.actual == pytest.approx(45.0)  # the DELTA, never the raw total


def test_arm_is_idempotent_on_the_baseline_but_always_re_derives():
    """A substantive replan re-arms the approval gate, so cmd_approve runs a second time
    for the same task. Re-snapshotting there would zero the ratio at exactly the moment
    the accumulated actual matters most."""
    state = substantive([stage(0, "spawn:developer", "large")])
    effort.arm(state, THR)
    first_estimate = dict(state.effort_estimate)

    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 100.0
    state.stages = [stage(0, "spawn:developer", "small")]
    effort.arm(state, THR)

    assert state.effort_baseline[effort.SCALE_SPEND] == 0.0  # untouched
    assert state.effort_estimate != first_estimate  # re-derived
    assert effort.deltas(state)[effort.SCALE_SPEND] == pytest.approx(100.0)


def test_arm_snapshots_only_a_none_baseline_never_a_zeroed_one():
    """The sentinel SUB-PLAN CUSTODY (cli.py's push/pop, stage 4) depends on: `None`
    means unarmed and gets a fresh snapshot; a zeroed-but-PRESENT dict — what push
    resets a child frame to — already reads as armed and must be left untouched, or a
    freshly-pushed child would silently skip its own snapshot."""
    state = substantive([stage(0, "spawn:developer")])
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 40.0
    effort.arm(state, THR)
    assert state.effort_baseline[effort.SCALE_SPEND] == pytest.approx(40.0)

    zeroed = substantive([stage(0, "spawn:developer")])
    zeroed.effort_baseline = {scale: 0.0 for scale in effort.SCALE_ORDER}
    zeroed.effort_actuals[effort.ACTUAL_SPEND_KEY] = 999.0
    effort.arm(zeroed, THR)
    assert zeroed.effort_baseline[effort.SCALE_SPEND] == 0.0  # untouched, not re-snapshotted
    assert effort.armed(zeroed) is True


# --- the actuals: monotone, replan-proof --------------------------------------

def rows(*pairs) -> list[dict]:
    return [{"plan_path": p, "cost_usd": c} for p, c in pairs]


def test_refresh_spend_reads_by_plan_path_alone():
    """Not cost.attribute_stage: --stage-index is optional on spawn-specialist.py, so a
    review-round spawn writes a row with stage_index None that stage attribution cannot
    see — precisely the mandated reviews the estimate counts."""
    state = substantive()
    ledger = [
        {"plan_path": "/p/plan.toml", "stage_index": 0, "cost_usd": 2.0},
        {"plan_path": "/p/plan.toml", "stage_index": None, "cost_usd": 3.0},
        {"plan_path": "/other/plan.toml", "stage_index": 0, "cost_usd": 90.0},
        {"plan_path": "/p/plan.toml"},  # malformed row: no cost, must not raise
    ]
    assert effort.refresh_spend(state, ledger, "/p/plan.toml") == pytest.approx(5.0)
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(5.0)


def test_refresh_spend_is_idempotent_across_repeated_calls():
    state = substantive()
    ledger = rows(("/p/plan.toml", 4.0))
    effort.refresh_spend(state, ledger, "/p/plan.toml")
    assert effort.refresh_spend(state, ledger, "/p/plan.toml") == 0.0
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(4.0)


def test_spend_accumulator_survives_a_plan_path_rewrite():
    """cmd_replan rewrites state.plan_path; the new path's ledger sums from its own 0.
    A single high-water scalar would freeze the accumulator until the new path
    out-grew the old total — the per-path seen map is what prevents that."""
    state = substantive()
    effort.refresh_spend(state, rows(("/p/old.toml", 20.0)), "/p/old.toml")
    effort.refresh_spend(state, rows(("/p/new.toml", 3.0)), "/p/new.toml")
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(23.0)

    effort.refresh_spend(state, rows(("/p/old.toml", 25.0)), "/p/old.toml")
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(28.0)


def test_refresh_spend_watermark_never_regresses():
    """A row disappearing from the ledger (or a transient re-read reporting a lower
    total) must not lower the per-path high-water mark — else the delta is
    double-booked once the ledger recovers to its prior total."""
    state = substantive()
    effort.refresh_spend(state, rows(("/p/plan.toml", 10.0)), "/p/plan.toml")
    assert effort.refresh_spend(state, rows(("/p/plan.toml", 4.0)), "/p/plan.toml") == 0.0
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(10.0)

    # ledger recovers to its old total: must not re-book the already-counted delta.
    assert effort.refresh_spend(state, rows(("/p/plan.toml", 10.0)), "/p/plan.toml") == 0.0
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(10.0)


def test_actuals_survive_stages_being_dropped_or_retried():
    """The actual is never a fold over state.stages: a replan drops stages, which would
    erase the very evidence the trigger exists to surface."""
    state = substantive([stage(0, "spawn:developer"), stage(1, "spawn:developer")])
    effort.refresh_spend(state, rows(("/p/plan.toml", 30.0)), "/p/plan.toml")
    state.stages = []
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(30.0)


def test_replans_actual_counts_history_events():
    state = substantive()
    state.log("replan", kind="substantive")
    state.log("dispatch")
    state.log("replan", kind="refinement")
    assert effort.actual(state)[effort.SCALE_REPLANS] == 2


# --- absolute scales ----------------------------------------------------------

def test_replan_count_fires_on_the_absolute_threshold():
    state = substantive([stage(0, "spawn:developer")])
    effort.arm(state, THR)
    for _ in range(2):
        state.log("replan", kind="substantive")
    assert effort.divergence(state, THR) is None
    state.log("replan", kind="substantive")
    fired = effort.divergence(state, THR)
    assert fired is not None
    assert fired.scale == effort.SCALE_REPLANS and fired.kind == "absolute"


def test_zero_threshold_makes_a_scale_accounting_only():
    """effort-absolute-interactions is 0 — the scale is measured and reported but must
    never fire, and must never be read as 'a threshold of 0, so everything fires'."""
    state = substantive([stage(0, "spawn:developer")])
    effort.arm(state, THR)
    state.user_prompt_count = 10_000
    assert effort.ratios(state, THR)[effort.SCALE_INTERACTIONS] is None
    assert effort.divergence(state, THR) is None
    assert effort.deltas(state)[effort.SCALE_INTERACTIONS] == 10_000


def test_interactions_fire_once_the_scale_is_re_enabled():
    """The accounting-only state is a CONFIG choice, not a missing implementation."""
    enabled = Thresholds({**THR._c, "effort-absolute-interactions": "40"})
    state = substantive([stage(0, "spawn:developer")])
    effort.arm(state, enabled)
    state.user_prompt_count = 39
    assert effort.divergence(state, enabled) is None
    state.user_prompt_count = 40
    fired = effort.divergence(state, enabled)
    assert fired is not None and fired.scale == effort.SCALE_INTERACTIONS


# --- selection & framing ------------------------------------------------------

def test_the_largest_multiple_wins_when_several_scales_diverge():
    state = substantive([stage(0, "spawn:developer")])  # spend 9.00, wall-clock 25 min
    effort.arm(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 9.0 * 6
    state.effort_actuals[effort.ACTUAL_MINUTES_KEY] = 25.0 * 20
    fired = effort.divergence(state, THR)
    assert fired is not None and fired.scale == effort.SCALE_WALL_CLOCK
    assert fired.multiple == pytest.approx(20.0)


def test_ranking_uses_distance_past_own_trigger_not_raw_multiple():
    """A ratio scale trips its raw `multiple` field at ~5.0 (effort-divergence-multiple)
    while an absolute scale's is already normalized by ratios() to trip at 1.0 — so
    ranking on the raw field (the pre-stage-4 bug) always favors a ratio scale barely
    past its own line over an absolute scale far past its own. Spend here is only 1.01x
    past its own trigger (raw multiple 5.05, just over the 5.0 line); replans is 4x past
    its own (12 replans against a threshold of 3). The fixed ranking must pick replans."""
    state = substantive([stage(0, "spawn:developer")])  # spend estimate: 9.00
    effort.arm(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 9.0 * 5.05  # raw multiple 5.05
    for _ in range(12):
        state.log("replan", kind="substantive")  # 12 vs threshold 3 -> normalized 4.0x
    fired = effort.divergence(state, THR)
    assert fired is not None
    assert fired.scale == effort.SCALE_REPLANS
    assert fired.multiple == pytest.approx(4.0)


def test_ranking_ratio_far_past_beats_absolute_barely_past_its_own():
    """Complementary direction: a ratio scale far past its own trigger must outrank
    an absolute scale only barely past its own — the prior test only pinned
    absolute-beats-ratio, leaving the opposite direction unpinned (a broken
    normalization could still coincidentally pass that one test alone)."""
    state = substantive([stage(0, "spawn:developer")])  # spend estimate: 9.00
    effort.arm(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 9.0 * 5.0 * 10  # raw multiple 50 -> 10x past its own line
    for _ in range(4):
        state.log("replan", kind="substantive")  # 4 vs threshold 3 -> normalized ~1.33x
    fired = effort.divergence(state, THR)
    assert fired is not None
    assert fired.scale == effort.SCALE_SPEND
    assert fired.multiple == pytest.approx(50.0)


def test_framing_points_at_the_norm_not_at_the_estimate():
    state = substantive([stage(0, "spawn:developer")])
    effort.arm(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 90.0
    fired = effort.divergence(state, THR)
    assert "missing something essential" in fired.framing
    assert "re-estimating" in fired.framing


# --- re-arm: both belts -------------------------------------------------------

def diverged() -> SessionState:
    state = substantive([stage(0, "spawn:developer")])
    effort.arm(state, THR)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 90.0
    return state


def test_record_fire_rebases_the_baseline_onto_the_actual():
    """Belt 1: without the rebase a corrected plan smaller than the already-accumulated
    actual would re-fire on the very next command."""
    state = diverged()
    fired = effort.divergence(state, THR)
    effort.record_fire(state, fired, now=123.0)
    assert state.effort_baseline[effort.SCALE_SPEND] == pytest.approx(90.0)
    assert effort.deltas(state)[effort.SCALE_SPEND] == 0.0
    assert state.effort_fires[-1]["scale"] == effort.SCALE_SPEND
    assert state.effort_fires[-1]["ts"] == 123.0  # caller-supplied `now`, never unstamped


def test_a_replan_is_required_before_the_trigger_can_fire_again():
    """Belt 2, independent of belt 1: even with fresh spend past the multiple, the
    trigger stays silent until a replan has set a new norm to be measured against."""
    state = diverged()
    effort.record_fire(state, effort.divergence(state, THR), now=1.0)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 90.0 + 90.0
    assert effort.divergence(state, THR) is None

    state.log("replan", kind="substantive")
    fired = effort.divergence(state, THR)
    assert fired is not None and fired.actual == pytest.approx(90.0)


def test_a_replan_logged_before_the_firing_does_not_count():
    state = diverged()
    state.log("replan", kind="substantive")
    effort.record_fire(state, effort.divergence(state, THR), now=1.0)
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = 900.0
    assert effort.divergence(state, THR) is None


# --- persistence --------------------------------------------------------------

def test_effort_fields_round_trip():
    state = diverged()
    effort.refresh_spend(state, rows(("/p/plan.toml", 7.0)), "/p/plan.toml")
    effort.record_fire(state, effort.divergence(state, THR), now=9.0)
    back = SessionState.from_json(state.to_json())
    assert back.effort_estimate == state.effort_estimate
    assert back.effort_baseline == state.effort_baseline
    assert back.effort_actuals == state.effort_actuals
    assert back.effort_fires == state.effort_fires
    assert back.effort_spend_seen == state.effort_spend_seen


def test_a_state_written_before_these_fields_existed_loads_with_zeros():
    """Legacy states are migrated by LOAD-TIME TOLERANCE (absent key -> dataclass
    default via from_dict's cls(**data)), never by a rewrite — and the two None
    sentinels mean such a session reads as unarmed, i.e. inert."""
    raw = json.loads(substantive([stage(0, "spawn:developer")]).to_json())
    for key in (
        "effort_estimate", "effort_baseline", "effort_actuals",
        "effort_fires", "effort_spend_seen", "user_prompt_count",
    ):
        raw.pop(key, None)

    state = SessionState.from_json(json.dumps(raw))
    assert state.effort_estimate is None and state.effort_baseline is None
    assert state.effort_actuals == {} and state.effort_spend_seen == {}
    assert state.effort_fires == [] and state.user_prompt_count == 0
    assert effort.actual(state) == {
        effort.SCALE_SPEND: 0.0,
        effort.SCALE_WALL_CLOCK: 0.0,
        effort.SCALE_REPLANS: 0.0,
        effort.SCALE_INTERACTIONS: 0.0,
    }
    assert effort.divergence(state, THR) is None
    effort.refresh_spend(state, rows(("/p/plan.toml", 2.0)), "/p/plan.toml")  # no KeyError
    assert effort.actual(state)[effort.SCALE_SPEND] == pytest.approx(2.0)


# --- config wiring & purity ---------------------------------------------------

def test_real_config_md_supplies_every_row_the_module_reads():
    """The module must not silently fall back if a row is renamed: config.py raises
    KeyError('... not defined in config.md') instead."""
    live = Thresholds()
    assert live.effort_divergence_multiple() > 1.0
    assert live.effort_replan_absolute() >= 0
    assert live.effort_absolute_interactions() >= 0
    for tier in ("small", "medium", "large"):
        assert live.effort_stage_minutes(tier) > 0
        assert live.budget_usd_float(tier) > 0
    effort.estimate(substantive([stage(0, "spawn:developer", "large")]))


def test_effort_module_is_pure():
    """Same predicate verify-agentctl applies to gates.py: no subprocess, socket or HTTP.
    The cost-ledger read belongs to the caller and arrives as data; the clock is passed
    in. That is what makes every branch above testable without a fixture of the world."""
    assert impure_names(effort) == set()
