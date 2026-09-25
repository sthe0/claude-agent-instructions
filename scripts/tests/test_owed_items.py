"""D8 — the one owed threshold armed in RECORD-ONLY mode: `effort-absolute-interactions`.

Two properties, each pinned by its own mutation in prove_owed_items_discriminate.sh:
  1. the armed config.md value equals the derivation record's p90 (mutation: disarm)
  2. the interactions scale still never reaches effort.divergence() as a real
     Divergence, even once its own threshold is crossed (mutation:
     record_only_to_blocker)

Reads config.md VENUE-RELATIVE (real `Thresholds()`, no override dict) so a stale
armed value or a regressed RECORD_ONLY_SCALES skip is caught against the actual file,
not a fixture that could drift from it.
"""
from __future__ import annotations

import json
from pathlib import Path

import dataclasses

from agentctl import effort
from agentctl.config import Thresholds
from agentctl.state import Actor, Criterion, Means, PlanFrame, SessionState, Stage, Subject, WeightClass

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DERIVATION_PATH = _REPO_ROOT / "docs" / "operations" / "effort-interactions-arming.json"


def _substantive_state() -> SessionState:
    state = SessionState(session_id="s", task_id="t", goal="g", overall_done_criterion="dc")
    state.weight_class = WeightClass.SUBSTANTIVE.value
    state.stages = [
        Stage(
            index=0,
            title="stage 0",
            subject=Subject(material="m", result="r"),
            means=Means(means="means", method="method"),
            actor=Actor(executor="spawn:developer", cost_tier="small"),
            criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
        )
    ]
    return state


def test_armed_value_matches_derivation_record():
    derivation = json.loads(_DERIVATION_PATH.read_text(encoding="utf-8"))
    assert derivation["row_count"] >= 20, "arming precondition (>=20 rated rows) unmet"
    assert derivation["p90"] == derivation["armed_value"]
    thr = Thresholds()
    assert thr.effort_absolute_interactions() == derivation["armed_value"]


def test_interactions_scale_is_declared_record_only():
    assert effort.SCALE_INTERACTIONS in effort.RECORD_ONLY_SCALES


def test_interactions_never_reaches_divergence_once_armed_and_crossed():
    """Real config.md threshold, real arm(), real divergence() — the actual armed
    value from config.md, not a fixture, must still produce no Divergence."""
    thr = Thresholds()
    state = _substantive_state()
    effort.arm(state, thr)
    state.user_prompt_count = thr.effort_absolute_interactions() + 10
    div = effort.divergence(state, thr)
    assert div is None or div.scale != effort.SCALE_INTERACTIONS


def test_effort_crossings_field_is_session_scoped_not_plan_scoped():
    """Purely structural (no call to record_crossing) — deliberately immune to the
    record_only_to_blocker mutation, so it and
    test_engine_probe_record_only_never_blocks_and_other_scales_stay_live each
    discriminate exactly one of the two named mutations, never both."""
    session_fields = {f.name for f in dataclasses.fields(SessionState)}
    assert "effort_crossings" in session_fields
    plan_frame_fields = {f.name for f in dataclasses.fields(PlanFrame)}
    assert "effort_crossings" not in plan_frame_fields


def test_gates_gained_no_new_blocker_for_the_record_only_scale():
    import inspect

    from agentctl import gates

    names = [n for n, _ in inspect.getmembers(gates, inspect.isfunction) if n.endswith("_blockers")]
    assert len(names) == 19, f"expected 19 *_blockers functions, found {len(names)}: {sorted(names)}"


def test_engine_probe_record_only_never_blocks_and_other_scales_stay_live():
    """Read-only probe against the real effort/gates/state modules, one prompt past
    the armed threshold, on an otherwise-slack state. Pinned by the mutation-catalogue
    label `record_only_to_blocker` (prove_owed_items_discriminate.sh), which makes
    record_crossing delegate to record_fire — a well-formed call, so this test's own
    assertions (not an exception the mutation raises) are what go RED."""
    import os

    from agentctl import gates

    thr = Thresholds()
    state = _substantive_state()
    effort.arm(state, thr)
    state.user_prompt_count = thr.effort_absolute_interactions() + 1

    # (a) divergence() returns nothing on the interactions scale.
    div = effort.divergence(state, thr)
    assert div is None or div.scale != effort.SCALE_INTERACTIONS

    # (b) that same call wrote NOTHING to effort_fires.
    assert state.effort_fires == []

    # (c) record_crossing appends exactly one entry, leaves effort_fires empty and
    # effort_baseline unrebased.
    baseline_before = dict(state.effort_baseline or {})
    record = effort.record_crossing(
        state, effort.SCALE_INTERACTIONS,
        actual=float(state.user_prompt_count),
        estimate=float(thr.effort_absolute_interactions()),
        now=1000.0,
    )
    assert record is not None
    assert state.effort_crossings == [record]
    assert state.effort_fires == []
    assert state.effort_baseline == baseline_before

    # (d) gates.effort_fire_blockers returns no blocker for this state.
    prior_env = os.environ.get("AGENTCTL_EFFORT")
    os.environ["AGENTCTL_EFFORT"] = "1"
    try:
        assert gates.effort_fire_blockers(state) == []
    finally:
        if prior_env is None:
            os.environ.pop("AGENTCTL_EFFORT", None)
        else:
            os.environ["AGENTCTL_EFFORT"] = prior_env

    # (e) a hundredfold spend overrun on this SAME state still fires on spend — the
    # record-only scale must not collaterally silence a ratio scale it shares
    # divergence()'s loop with.
    state.effort_actuals[effort.ACTUAL_SPEND_KEY] = (
        float((state.effort_baseline or {}).get(effort.SCALE_SPEND) or 0.0)
        + 100.0 * max(float(state.effort_estimate.get(effort.SCALE_SPEND) or 1.0), 1.0)
    )
    spend_div = effort.divergence(state, thr)
    assert spend_div is not None and spend_div.scale == effort.SCALE_SPEND
