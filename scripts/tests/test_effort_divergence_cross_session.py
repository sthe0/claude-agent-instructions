"""Stage 6 / item B integration test: a session restarted on the same task_id
inherits the prior session's accumulated replan count through the
cross-session task accumulator, so the effort-divergence trigger fires on the
COMBINED total even though neither session alone reached the absolute
threshold -- the exact symptom of the `hook-guard-permission-self-grant`
incident (task_accumulator.py's module docstring), reproduced end-to-end
through effort.divergence's real `cross_session_totals` parameter and the
same `task_accumulator.add` calls cli.py's cmd_replan wiring makes."""
from __future__ import annotations

from agentctl import effort
from agentctl import task_accumulator as ta
from agentctl.config import Thresholds
from agentctl.state import Actor, Criterion, Means, SessionState, Stage, Subject, WeightClass

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


def _substantive_state(session_id: str, task_id: str) -> SessionState:
    state = SessionState(session_id=session_id, task_id=task_id, goal="g", overall_done_criterion="dc")
    state.weight_class = WeightClass.SUBSTANTIVE.value
    state.stages = [
        Stage(
            index=0,
            title="stage 0",
            subject=Subject(material="m", result="r"),
            means=Means(means="means", method="method"),
            actor=Actor(executor="spawn:developer", cost_tier=None),
            criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
        )
    ]
    return state


def test_session_restart_inherits_prior_replans_and_fires_on_the_combined_total(tmp_path):
    task_id = "hook-guard-permission-self-grant"

    # --- Session 1: two replans, then closes. Each replan is folded into the
    # accumulator exactly as cli.py's cmd_replan does at its three logging sites. ---
    session1 = _substantive_state("18fb6860", task_id)
    effort.arm(session1, THR)
    for _ in range(2):
        session1.log("replan", kind="substantive")
        ta.add(task_id, "replan_count", 1, session_id=session1.session_id, root=tmp_path)

    cross_after_session1 = ta.get(task_id, root=tmp_path)["per_axis_totals"]
    assert cross_after_session1["replan_count"] == 2
    # Session 1 alone never reaches the threshold of 3, session-local or cross-session.
    assert effort.divergence(session1, THR, cross_session_totals=cross_after_session1) is None

    # --- Session 2: a brand-new SessionState, no in-memory link to session 1
    # (models an actual process restart), same task_id, one more replan. ---
    session2 = _substantive_state("2442a5ac", task_id)
    effort.arm(session2, THR)
    session2.log("replan", kind="substantive")
    ta.add(task_id, "replan_count", 1, session_id=session2.session_id, root=tmp_path)

    # Session 2's OWN delta is only 1 -- a session-local-only check would never fire.
    assert effort.deltas(session2)[effort.SCALE_REPLANS] == 1
    assert effort.divergence(session2, THR) is None

    full_state_after_session2 = ta.get(task_id, root=tmp_path)
    assert full_state_after_session2["session_ids_contributing"] == ["18fb6860", "2442a5ac"]
    cross_after_session2 = full_state_after_session2["per_axis_totals"]
    assert cross_after_session2["replan_count"] == 3

    fired = effort.divergence(session2, THR, cross_session_totals=cross_after_session2)
    assert fired is not None
    assert fired.scale == effort.SCALE_REPLANS
    assert fired.kind == "absolute"
    assert fired.actual == 3
    assert "prior sessions" in fired.framing
