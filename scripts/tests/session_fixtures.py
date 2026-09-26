"""Shared SessionState builder for tests that drive `cli.cmd_record_result`
directly against a single MEASURABLE, SUBSTANTIVE stage.

test_venue_tree_identity.py, test_convergence_r4_judge_after_check.py and
test_acceptance_gate_precedence_and_attempts.py each hand-built the identical
fixture; kept here once, the same way `ast_purity.py`/`dataclass_domain.py`
are shared rather than copied.
"""
from __future__ import annotations

from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    Means,
    Node,
    Outcome,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def measurable_session(store, sid, *, verify_command=None, expected_exit=0,
                        executor="in_thread"):
    """A MEASURABLE-criterion, SUBSTANTIVE, single-stage session, saved to
    `store` and returned. `executor` defaults to in_thread; a caller exercising
    the code-review gate passes `executor="spawn:developer"`."""
    state = SessionState(
        session_id=sid,
        task_id="test",
        goal="fix the bug",
        overall_done_criterion="the test suite passes",
        overall_criterion_type=CriterionType.MEASURABLE.value,
        weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True, by="test-setup"),
        stages=[
            Stage(
                index=1,
                title="Fix the bug",
                subject=Subject(material="the module", result="tests pass with no failures"),
                means=Means(means="pytest", method="run the test suite"),
                actor=Actor(executor=executor),
                criterion=Criterion(
                    criterion_type=CriterionType.MEASURABLE.value,
                    done_criterion="the check passes",
                    verify_command=verify_command,
                    expected_exit=expected_exit,
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)
    return state
