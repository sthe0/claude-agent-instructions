"""Review-cost triggers: `gates.review_trigger` and the counters `cli` keeps for it.

Three config.md rows (`review-cycle-acts`, `review-cycle-active-minutes`,
`review-stage-nonpass-streak`) route a PLAN_READY session into the overcome-difficulty cycle.
These tests pin the pure gate, the counting rules (what is an act, when a cycle opens and
closes, what moves a stage's streak) and the config rows against the calibration file. The
engine-level routing and the two exits are in test_review_trigger_e2e.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl import cli, config, effort, gates
from agentctl.config import Thresholds
from agentctl.state import SessionState

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CALIBRATION = REPO_ROOT / "docs" / "operations" / "review-cost-calibration.json"

ACTS_KEY = "review-cycle-acts"
MINUTES_KEY = "review-cycle-active-minutes"
STREAK_KEY = "review-stage-nonpass-streak"
ROWS = {ACTS_KEY: "4", MINUTES_KEY: "30", STREAK_KEY: "3"}


def thr(**overrides: str) -> Thresholds:
    rows = {**ROWS, "effort-replan-absolute": "5", **overrides}
    return Thresholds({k: v for k, v in rows.items() if v is not None})


def cycle(**kw) -> SessionState:
    base = dict(session_id="s", task_id="t", weight_class="SUBSTANTIVE", plan_path="/plan.toml",
                plan_verified=True, review_cycle_open=True, review_cycle_sha="sha-a",
                review_cycle_minutes_anchor=0.0)
    return SessionState(**{**base, **kw})


def book(state: SessionState, minutes: float) -> None:
    state.effort_actuals = {**(state.effort_actuals or {}), effort.ACTUAL_MINUTES_KEY: minutes}


def review(state: SessionState, verdict: str = "revise", reviewer: str = "thinker", *, stage: str = "") -> None:
    cli._count_review_act(state, verdict, reviewer, stage_scope=stage)


# --- the config rows ---------------------------------------------------------------------


def test_config_rows_match_calibration():
    derived = json.loads(CALIBRATION.read_text(encoding="utf-8"))["derived"]
    rows = Thresholds()
    assert rows.review_cycle_acts() == derived[ACTS_KEY]["value"]
    assert rows.review_cycle_active_minutes() == derived[MINUTES_KEY]["value"]
    assert rows.review_stage_nonpass_streak() == 3


def test_missing_and_non_integer_rows_disarm_their_trigger():
    state = cycle(review_cycle_acts=99, review_stage_streaks={"stage:1": 99})
    book(state, 999)
    assert gates.review_trigger(state, thr()) is not None
    disarmed = thr(**{ACTS_KEY: None, MINUTES_KEY: "n/a", STREAK_KEY: "three"})
    assert disarmed.review_cycle_acts() is None
    assert disarmed.review_cycle_active_minutes() is None
    assert disarmed.review_stage_nonpass_streak() is None
    assert gates.review_trigger(state, disarmed) is None


def test_rows_are_read_from_a_patched_config_md(tmp_path, monkeypatch):
    table = tmp_path / "config.md"
    table.write_text(
        "| `review-cycle-acts` | `2` | x |\n| `review-cycle-active-minutes` | `7` | x |\n"
        "| `review-stage-nonpass-streak` | `1` | x |\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_MD", table)
    rows = Thresholds()
    assert (rows.review_cycle_acts(), rows.review_cycle_active_minutes(),
            rows.review_stage_nonpass_streak()) == (2, 7, 1)
    state = cycle(review_cycle_acts=2)
    assert gates.review_trigger(state)["threshold"] == 2


def test_unreadable_config_md_disarms_every_trigger(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_MD", tmp_path / "absent.md")
    assert gates.review_trigger(cycle(review_cycle_acts=999)) is None


# --- the gate: each trigger just under and at its row -----------------------------------


def test_acts_trigger_fires_at_its_row_not_below():
    below = cycle(review_cycle_acts=3)
    assert gates.review_trigger(below, thr()) is None
    at = cycle(review_cycle_acts=4)
    assert gates.review_trigger(at, thr()) == {
        "trigger": "cycle_acts", "value": 4, "threshold": 4, "stage": None}


def test_minutes_trigger_fires_at_its_row_not_below():
    state = cycle(review_cycle_minutes_anchor=10.0)
    book(state, 39.9)
    assert gates.review_trigger(state, thr()) is None
    book(state, 40.0)
    assert gates.review_trigger(state, thr()) == {
        "trigger": "cycle_minutes", "value": 30.0, "threshold": 30, "stage": None}


def test_minutes_are_the_booked_delta_and_child_runs_are_not_added():
    state = cycle(review_cycle_minutes_anchor=5.0)
    book(state, 20.0)
    state.effort_child_runs = [{"started": "2026-01-01T00:00:00Z", "ended": "2026-01-01T09:00:00Z"}]
    assert gates.review_trigger(state, thr()) is None
    assert gates.review_booked_minutes(state) == 20.0


def test_streak_trigger_fires_at_its_row_not_below():
    assert gates.review_trigger(cycle(review_stage_streaks={"stage:2": 2}), thr()) is None
    assert gates.review_trigger(cycle(review_stage_streaks={"stage:2": 3}), thr()) == {
        "trigger": "stage_streak", "value": 3, "threshold": 3, "stage": "stage:2"}


def test_acts_and_minutes_need_an_open_cycle_but_the_streak_does_not():
    closed = cycle(review_cycle_open=False, review_cycle_acts=99, review_stage_streaks={"stage:1": 3})
    book(closed, 999)
    assert gates.review_trigger(closed, thr())["trigger"] == "stage_streak"
    closed.review_stage_streaks = {}
    assert gates.review_trigger(closed, thr()) is None


def test_no_state_means_no_trigger():
    assert gates.review_trigger(None, thr()) is None


# --- no re-fire on the same evidence -----------------------------------------------------


def test_a_fired_acts_trigger_waits_for_a_full_row_of_new_acts():
    state = cycle(review_cycle_acts=4, review_trigger_spent={"cycle_acts": 4})
    assert gates.review_trigger(state, thr()) is None
    state.review_cycle_acts = 7
    assert gates.review_trigger(state, thr()) is None
    state.review_cycle_acts = 8
    assert gates.review_trigger(state, thr())["value"] == 4


def test_a_fired_minutes_trigger_stays_quiet_for_the_cycle():
    state = cycle(review_trigger_spent={"cycle_minutes": 1})
    book(state, 500)
    assert gates.review_trigger(state, thr()) is None


def test_a_fired_streak_fires_again_only_on_a_larger_value():
    state = cycle(review_stage_streaks={"stage:2": 3}, review_trigger_spent={"stage_streak:stage:2": 3})
    assert gates.review_trigger(state, thr()) is None
    state.review_stage_streaks["stage:2"] = 4
    assert gates.review_trigger(state, thr())["value"] == 4


# --- what counts as an act, and what moves a streak -------------------------------------


@pytest.mark.parametrize("verdict, reviewer", [
    ("override", "user"),
    ("override", "agent"),
    ("pass", "topological-composition"),
])
def test_override_and_composition_records_are_not_acts(verdict, reviewer):
    state = cycle()
    review(state, verdict, reviewer, stage="stage:1")
    assert state.review_cycle_acts == 0
    assert state.review_stage_streaks == {}


def test_a_carried_record_is_not_an_act():
    assert cli._is_counted_review("carried", "thinker") is False
    state = cycle()
    review(state, "carried", "thinker")
    assert state.review_cycle_acts == 0


@pytest.mark.parametrize("verdict", ["pass", "revise"])
def test_pass_and_revise_are_acts(verdict):
    state = cycle()
    review(state, verdict)
    assert state.review_cycle_acts == 1


def test_acts_are_not_counted_without_an_open_cycle():
    state = cycle(review_cycle_open=False)
    review(state, "revise", stage="stage:1")
    assert state.review_cycle_acts == 0
    assert state.review_stage_streaks == {"stage:1": 1}


def test_the_stages_own_pass_resets_its_streak_and_its_spent_mark():
    state = cycle(review_trigger_spent={"stage_streak:stage:1": 3})
    for _ in range(3):
        review(state, "revise", stage="stage:1")
    assert state.review_stage_streaks == {"stage:1": 3}
    review(state, "pass", stage="stage:1")
    assert state.review_stage_streaks == {}
    assert "stage_streak:stage:1" not in state.review_trigger_spent


def test_whole_plan_and_pair_records_do_not_move_a_stage_streak():
    state = cycle(review_stage_streaks={"stage:1": 2})
    review(state, "revise")
    review(state, "pass")
    assert state.review_stage_streaks == {"stage:1": 2}
    assert state.review_cycle_acts == 2


def test_another_stage_passing_leaves_a_streak_alone():
    state = cycle(review_stage_streaks={"stage:1": 2})
    review(state, "pass", stage="stage:2")
    assert state.review_stage_streaks == {"stage:1": 2}


def test_a_streak_spanning_three_plan_versions_fires_at_the_third():
    state = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE")
    for version, sha in enumerate(("sha-1", "sha-2", "sha-3"), start=1):
        cli._review_cycle_arrive(state, sha)
        review(state, "revise", stage="stage:1")
        fired = gates.review_trigger(state, thr(**{ACTS_KEY: "50", MINUTES_KEY: "500"}))
        assert (fired is not None) == (version == 3)
    assert fired["trigger"] == "stage_streak" and fired["stage"] == "stage:1"


# --- cycles open and close ---------------------------------------------------------------


def test_a_new_sha_opens_a_cycle_and_resets_acts_and_the_minutes_anchor():
    state = cycle(review_cycle_acts=3, review_trigger_spent={"cycle_acts": 3, "cycle_minutes": 1})
    book(state, 12.0)
    assert cli._review_cycle_arrive(state, "sha-b") is True
    assert (state.review_cycle_sha, state.review_cycle_acts) == ("sha-b", 0)
    assert state.review_cycle_minutes_anchor == 12.0
    assert state.review_trigger_spent == {}


def test_the_same_sha_leaves_the_open_cycle_alone():
    state = cycle(review_cycle_acts=3, review_cycle_minutes_anchor=2.0)
    book(state, 12.0)
    assert cli._review_cycle_arrive(state, "sha-a") is False
    assert (state.review_cycle_acts, state.review_cycle_minutes_anchor) == (3, 2.0)


def test_a_new_sha_does_not_reset_a_streak_but_approve_does():
    state = cycle(review_stage_streaks={"stage:1": 2}, review_trigger_spent={"stage_streak:stage:1": 2})
    cli._review_cycle_arrive(state, "sha-b")
    assert state.review_stage_streaks == {"stage:1": 2}
    assert state.review_trigger_spent == {"stage_streak:stage:1": 2}
    cli._review_cycle_close(state)
    assert state.review_stage_streaks == {}
    assert (state.review_cycle_open, state.review_cycle_acts, state.review_trigger_spent) == (False, 0, {})
    assert state.review_trigger_fire is None


def test_a_re_arrival_after_close_opens_a_new_cycle_even_on_the_same_sha():
    state = cycle(review_cycle_acts=3)
    cli._review_cycle_close(state)
    assert cli._review_cycle_arrive(state, "sha-a") is True
    assert state.review_cycle_open is True


# --- the state file -----------------------------------------------------------------------


def test_an_old_state_file_without_the_fields_loads_with_defaults():
    state = SessionState(session_id="s", task_id="t")
    data = json.loads(state.to_json())
    for name in ("review_cycle_sha", "review_cycle_open", "review_cycle_acts",
                 "review_cycle_minutes_anchor", "review_stage_streaks",
                 "review_trigger_fire", "review_trigger_spent"):
        data.pop(name)
    loaded = SessionState.from_json(json.dumps(data))
    assert (loaded.review_cycle_open, loaded.review_cycle_acts, loaded.review_cycle_sha) == (False, 0, "")
    assert loaded.review_stage_streaks == {} and loaded.review_trigger_spent == {}
    assert loaded.review_trigger_fire is None and loaded.review_cycle_minutes_anchor is None
