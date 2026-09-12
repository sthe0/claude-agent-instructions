"""The required observation shape (GitHub issue #95) is stated at every point an
observation is authored, not only where a bad one is rejected: `record-result
--observation` help, `close --observation` help, both observation-refusal
Directives in `cmd_record_result`, and the developer specialization's guidance
for what a `COMPLETED:` observation must look like. All four must use the same
canonical wording (`cli.OBSERVATION_CONTRACT`) so an author who reads any one of
them learns the real contract, not a paraphrase of it."""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    GateRecord,
    Means,
    Node,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
    Outcome,
)

# The two phrases that distinguish the contract from the old, insufficient
# "what you actually observed, distinct from the expected image" wording:
# present-tense attestation, and the explicit ban on narrating the defect
# history. Matched case-sensitively against the canonical constant.
_DISTINGUISHING_PHRASES = (
    "present tense",
    "Do not narrate what had been wrong",
)


def _help_text(capsys, *argv: str) -> str:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([*argv, "--help"])
    return capsys.readouterr().out


def test_record_result_help_carries_the_contract(capsys):
    out = _help_text(capsys, "record-result")
    for phrase in _DISTINGUISHING_PHRASES:
        assert phrase in out, f"record-result --help missing: {phrase!r}"


def test_close_help_carries_the_contract(capsys):
    out = _help_text(capsys, "close")
    for phrase in _DISTINGUISHING_PHRASES:
        assert phrase in out, f"close --help missing: {phrase!r}"


def _measurable_substantive_session(store, sid: str) -> SessionState:
    """A MEASURABLE-criterion stage on a SUBSTANTIVE session, active and ready
    for record-result — the shape the observation gate applies to via Defect 2
    (control compares result with goal at every stage of a substantive session),
    mirroring test_stage_review_scope.py's `_measurable_session`."""
    state = SessionState(
        session_id=sid,
        task_id="observation-contract-test",
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
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=CriterionType.MEASURABLE.value,
                    done_criterion="pytest exits 0",
                ),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    store.save(state)
    return state


def test_empty_observation_refusal_carries_the_contract(store, monkeypatch):
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "0")
    _measurable_substantive_session(store, "s-empty")
    directive = cli.cmd_record_result(
        Namespace(session="s-empty", status="passed", actual="ran the suite",
                   control=None, observation=""),
        store=store,
    )
    assert directive.ok is False
    for phrase in _DISTINGUISHING_PHRASES:
        assert phrase in directive.detail, f"empty-observation refusal missing: {phrase!r}"


def test_echo_observation_refusal_carries_the_contract(store, monkeypatch):
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "0")
    _measurable_substantive_session(store, "s-echo")
    directive = cli.cmd_record_result(
        Namespace(session="s-echo", status="passed", actual="ran the suite",
                   control=None, observation="tests pass with no failures"),
        store=store,
    )
    assert directive.ok is False
    for phrase in _DISTINGUISHING_PHRASES:
        assert phrase in directive.detail, f"echo-observation refusal missing: {phrase!r}"


def test_developer_skill_states_the_contract():
    text = (cli.REPO_ROOT / "skills" / "specializations" / "developer" / "SKILL.md").read_text()
    for phrase in _DISTINGUISHING_PHRASES:
        assert phrase in text, f"developer SKILL.md missing: {phrase!r}"


def test_acceptance_judge_leaf_matches_the_canonical_wording():
    leaf = (
        cli.REPO_ROOT / "memory-global" / "leaves" / "system-knowledge"
        / "agentctl-acceptance-judge-gate.md"
    ).read_text()
    for phrase in _DISTINGUISHING_PHRASES:
        assert phrase in leaf, f"acceptance-judge leaf missing: {phrase!r}"
