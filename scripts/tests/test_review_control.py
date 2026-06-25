"""record-result --control: general control-criterion attestation, required for
spawn:developer stages when recording passed.

Covers the done-criterion scenarios:
  - spawn:developer + passed + no --control  -> REFUSED, message names record-result --control
  - spawn:developer + passed + --control     -> ALLOWED -> VERIFYING
  - spawn:developer + passed + trivial waiver in --control -> ALLOWED
  - in_thread + passed + no --control        -> ALLOWED
  - spawn:developer + failed + no --control  -> ALLOWED
  - --control accepted on a non-developer spawn stage without error
  - Stage.needs_control() / has_control() helpers
  - Stage round-trip preserves control field
"""
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _stage(index=1, executor="spawn:developer", control=None):
    return Stage(
        index=index, title=f"stage-{index}",
        subject=Subject(material="m", result="img"),
        means=Means(means="Edit", method="do"),
        actor=Actor(executor=executor),
        criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
        control=control,
    )


def _executing_state(*stages):
    return SessionState(
        session_id="rrc", task_id="rrc-task",
        node=Node.EXECUTING.value,
        weight_class="SUBSTANTIVE",
        route="SPAWN",
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
        partition=Partition(verdict="not-recommended"),
        stages=list(stages),
        current_stage=stages[0].index,
    )


class _Mem:
    def __init__(self, state):
        self.s = state
    def load(self, _):
        return self.s
    def save(self, s):
        self.s = s


def _store(*stages):
    return _Mem(_executing_state(*stages))


def _rr(store, status, actual="done", control=None):
    return cli.cmd_record_result(
        Namespace(session="rrc", status=status, actual=actual, control=control),
        store=store,
    )


# ---------------------------------------------------------------------------
# Stage helper tests
# ---------------------------------------------------------------------------

def test_needs_control_true_for_spawn_developer():
    assert _stage(executor="spawn:developer").needs_control() is True


def test_needs_control_false_for_in_thread():
    assert _stage(executor="in_thread").needs_control() is False


def test_needs_control_false_for_spawn_planner():
    assert _stage(executor="spawn:planner").needs_control() is False


def test_has_control_false_when_none():
    assert _stage().has_control() is False


def test_has_control_false_when_whitespace_only():
    s = _stage(control="   ")
    assert s.has_control() is False


def test_has_control_true_when_set():
    assert _stage(control="reviewed: ok").has_control() is True


def test_stage_roundtrip_preserves_control():
    s = _stage(control="reviewed: self-review ok")
    rt = Stage.from_dict(s.__class__.__dataclass_fields__  # noqa: just import check
                         and __import__("dataclasses").asdict(s))
    assert rt.control == "reviewed: self-review ok"


# ---------------------------------------------------------------------------
# record-result precondition: spawn:developer + passed
# ---------------------------------------------------------------------------

def test_refused_without_control_developer_passed():
    store = _store(_stage(executor="spawn:developer"))
    d = _rr(store, "passed", control=None)
    assert d.ok is False
    assert "record-result" in d.detail
    assert "--control" in d.detail


def test_refused_node_unchanged():
    """Refusal must not transition the node (still EXECUTING)."""
    store = _store(_stage(executor="spawn:developer"))
    _rr(store, "passed", control=None)
    assert store.s.node == Node.EXECUTING.value


def test_allowed_with_control_developer_passed():
    store = _store(_stage(executor="spawn:developer"))
    d = _rr(store, "passed", control="reviewed: self-review complete, no issues")
    assert d.ok is True
    assert store.s.node == Node.VERIFYING.value


def test_allowed_with_trivial_waiver():
    """A conscious trivial waiver ('trivial one-line, no review: <reason>') is allowed."""
    store = _store(_stage(executor="spawn:developer"))
    d = _rr(store, "passed", control="trivial one-liner, no review needed: mechanical rename only")
    assert d.ok is True


def test_control_stored_on_stage():
    store = _store(_stage(executor="spawn:developer"))
    _rr(store, "passed", control="reviewed: ok")
    assert store.s.stage(1).control == "reviewed: ok"


# ---------------------------------------------------------------------------
# record-result precondition: failed and non-developer are unaffected
# ---------------------------------------------------------------------------

def test_developer_failed_without_control_allowed():
    store = _store(_stage(executor="spawn:developer"))
    d = _rr(store, "failed", actual="boom", control=None)
    assert d.ok is False  # failed -> diagnose cycle (ok=False), but NOT a control refusal
    assert d.action == "declare"   # DIAGNOSING sub-spine, not attest_control


def test_in_thread_passed_without_control_allowed():
    store = _store(_stage(executor="in_thread"))
    d = _rr(store, "passed", control=None)
    assert d.ok is True
    assert store.s.node == Node.VERIFYING.value


def test_non_developer_spawn_passed_without_control_allowed():
    """A spawn:planner stage does not need a control attestation."""
    store = _store(_stage(executor="spawn:planner"))
    d = _rr(store, "passed", control=None)
    assert d.ok is True


# ---------------------------------------------------------------------------
# --control accepted (stored) on non-developer stages without error
# ---------------------------------------------------------------------------

def test_control_accepted_on_in_thread_stage():
    store = _store(_stage(executor="in_thread"))
    d = _rr(store, "passed", control="manager self-reviewed in-thread change")
    assert d.ok is True
    assert store.s.stage(1).control == "manager self-reviewed in-thread change"


def test_control_accepted_on_non_developer_spawn():
    store = _store(_stage(executor="spawn:planner"))
    d = _rr(store, "passed", control="manager reviewed planner output")
    assert d.ok is True
    assert store.s.stage(1).control == "manager reviewed planner output"


# ---------------------------------------------------------------------------
# no new command was added to COMMANDS for this feature
# ---------------------------------------------------------------------------

def test_no_new_review_command_in_commands():
    forbidden = {"attest-control", "record-review", "review-result", "review"}
    found = forbidden & set(cli.COMMANDS)
    assert not found, (
        f"control obligation must ride record-result, not a new verb; found: {found}"
    )


# ---------------------------------------------------------------------------
# CLI parser accepts --control on record-result
# ---------------------------------------------------------------------------

def test_parser_accepts_control_on_record_result():
    p = cli.build_parser()
    args = p.parse_args(["record-result", "--session", "s", "--status", "passed", "--control", "ok"])
    assert args.control == "ok"


def test_parser_control_defaults_to_none():
    p = cli.build_parser()
    args = p.parse_args(["record-result", "--session", "s", "--status", "passed"])
    assert args.control is None
