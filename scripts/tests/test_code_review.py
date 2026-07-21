"""The code-review gate (gates.code_review_blockers): a record-result --status
passed on a needs_control() (spawn:developer) stage is blocked until a recorded
CodeReview with a passing (or user-override) verdict exists. The fourth instance
of the PlanReview/StageReview charter — same shape, same override escape, same
weight/kill-switch scoping — but over a stage's produced code instead of a plan
file or an acceptance observation, and bound via a CALLER-SUPPLIED digest (no
subprocess/git reach — see the module Q1 discussion in gates.code_review_blockers)
rather than a hash the gate recomputes itself.

Also covers the state layer: the CodeReview dataclass + SessionState.code_reviews
field (schema 21), including round-trip and legacy-load (grandfathering).

Deliberately ABSENT from gates.GUARDIANS (an internal record-result precondition,
like acceptance_review_blockers/plan_review_blockers), so verify-agentctl requires
no new hook. PURE: never a subprocess/socket/network reach."""
from __future__ import annotations

import json
from argparse import Namespace

from agentctl import cli, gates
from agentctl.state import (
    Actor,
    CodeReview,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    SCHEMA_VERSION,
    SessionState,
    Stage,
    StageStatus,
    Subject,
)
from ast_purity import impure_names


def _dev_stage(index=1):
    return Stage(
        index=index, title="s1",
        subject=Subject(material="m", result="the expected image"),
        means=Means(means="Edit", method="implement"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _subst(stage, *, reviews=(), weight="SUBSTANTIVE"):
    return SessionState(session_id="s", task_id="t", weight_class=weight,
                        stages=[stage], code_reviews=list(reviews))


def _review(verdict, reviewer="code-reviewer", note="", code_sha256=""):
    return CodeReview(stage_index=1, verdict=verdict, reviewer=reviewer, note=note,
                       code_sha256=code_sha256)


# --- cli / record-result: the CodeReview recorder + the gate fold-in ---------

class _Mem:
    def __init__(self, state):
        self.s = state

    def load(self, _):
        return self.s

    def save(self, s):
        self.s = s


def _executing_state(*stages, weight="SUBSTANTIVE"):
    return SessionState(
        session_id="cr", task_id="cr-task",
        node=Node.EXECUTING.value,
        weight_class=weight,
        route="SPAWN" if weight == "SUBSTANTIVE" else "INLINE",
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
        partition=Partition(verdict="not-recommended"),
        stages=list(stages),
        current_stage=stages[0].index,
    )


def _store(*stages, weight="SUBSTANTIVE"):
    return _Mem(_executing_state(*stages, weight=weight))


def _code_review(store, verdict, reviewer="code-reviewer", note="", concerns=None, code_ref=None):
    return cli.cmd_code_review(
        Namespace(session="cr", verdict=verdict, reviewer=reviewer, note=note,
                   concerns=concerns, code_ref=code_ref),
        store=store,
    )


def _rr(store, status, actual="done", control=None, code_ref=None):
    return cli.cmd_record_result(
        Namespace(session="cr", status=status, actual=actual, control=control,
                   observation="", code_ref=code_ref),
        store=store,
    )


def test_cli_code_review_recorder_writes_bound_review():
    store = _store(_dev_stage())
    d = _code_review(store, "pass", code_ref="rev1")
    assert d.ok is True
    reviews = store.s.code_reviews
    assert len(reviews) == 1
    assert reviews[0].verdict == "pass"
    assert reviews[0].stage_index == 1
    assert reviews[0].code_sha256  # bound from --code-ref, non-empty


def test_cli_code_review_recorder_rejects_non_developer_stage():
    non_dev = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="Read", method="plan"),
        actor=Actor(executor="spawn:planner"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )
    store = _store(non_dev)
    d = _code_review(store, "pass")
    assert d.ok is False
    assert d.action == "noop"
    assert store.s.code_reviews == []


def test_record_result_substantive_developer_with_control_but_no_code_review_is_blocked(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    store = _store(_dev_stage())
    d = _rr(store, "passed", control="reviewed: self-review complete")
    assert d.ok is False
    assert d.action == "spawn_code_review"
    assert store.s.node == Node.EXECUTING.value  # blocked pass never advances the node


def test_record_result_passes_after_a_passing_code_review_is_recorded(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    store = _store(_dev_stage())
    _code_review(store, "pass")
    d = _rr(store, "passed", control="reviewed: self-review complete")
    assert d.ok is True
    assert store.s.node == Node.VERIFYING.value


def test_record_result_re_recording_after_pass_still_passes(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    store = _store(_dev_stage())
    _code_review(store, "revise", note="needs work")
    blocked = _rr(store, "passed", control="reviewed")
    assert blocked.ok is False
    _code_review(store, "pass")
    d = _rr(store, "passed", control="reviewed")
    assert d.ok is True


def test_record_result_non_substantive_developer_passes_with_only_control(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    store = _store(_dev_stage(), weight="SMALL_CHANGE")
    d = _rr(store, "passed", control="reviewed: self-review complete")
    assert d.ok is True
    assert store.s.node == Node.VERIFYING.value
    assert store.s.code_reviews == []  # gate never consulted -> byte-identical path


def test_record_result_kill_switch_off_passes_with_only_control(monkeypatch):
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "0")
    store = _store(_dev_stage())
    d = _rr(store, "passed", control="reviewed: self-review complete")
    assert d.ok is True
    assert store.s.node == Node.VERIFYING.value


def test_record_result_non_developer_stage_unaffected_by_code_review_gate():
    non_dev = Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="img"),
        means=Means(means="Read", method="plan"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )
    store = _store(non_dev)
    d = _rr(store, "passed", control=None)
    assert d.ok is True
    assert store.s.node == Node.VERIFYING.value


def test_record_result_stale_code_ref_blocks_even_with_prior_pass(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    store = _store(_dev_stage())
    _code_review(store, "pass", code_ref="rev1")
    d = _rr(store, "passed", control="reviewed", code_ref="rev2")
    assert d.ok is False
    assert d.action == "spawn_code_review"


def test_cli_code_review_registered_in_parser():
    p = cli.build_parser()
    args = p.parse_args(["code-review", "--session", "s", "--verdict", "pass"])
    assert args.verdict == "pass"
    assert args.reviewer == ""
    assert args.code_ref is None


# --- state: CodeReview + SessionState.code_reviews --------------------------

def test_state_schema_version_bumped_for_code_review():
    assert SCHEMA_VERSION >= 21


def test_state_code_review_roundtrip_preserves_fields():
    s = SessionState(
        session_id="s", task_id="t",
        code_reviews=[
            CodeReview(stage_index=1, verdict="pass", reviewer="code-reviewer",
                       concerns=["c1"], note="clean diff", code_sha256="deadbeef"),
            CodeReview(stage_index=2, verdict="override", reviewer="fedor",
                       note="deadlock escape", code_sha256="cafe"),
        ],
    )
    back = SessionState.from_json(s.to_json())
    assert back == s
    assert back.code_reviews[0].code_sha256 == "deadbeef"
    assert back.code_reviews[1].verdict == "override"


def test_state_legacy_session_without_code_reviews_key_loads_with_default():
    """A pre-schema-21 state dict (no code_reviews key at all) loads with an
    empty list, so old state.json remains readable."""
    s = SessionState(
        session_id="s", task_id="t",
        code_reviews=[CodeReview(stage_index=1, verdict="pass", reviewer="code-reviewer")],
    )
    raw = json.loads(s.to_json())
    raw.pop("code_reviews", None)
    loaded = SessionState.from_dict(raw)
    assert loaded.code_reviews == []


# --- gate: activation scoping (weight + kill switch) -------------------------

def test_gate_inactive_on_small_change_is_vacuous(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), weight="SMALL_CHANGE")
    assert gates.code_review_active(s) is False
    assert gates.code_review_blockers(s, s.stages[0]) == []


def test_gate_force_off_env_makes_gate_vacuous(monkeypatch):
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "0")
    s = _subst(_dev_stage())  # substantive, no review recorded
    assert gates.code_review_active(s) is False
    assert gates.code_review_blockers(s, s.stages[0]) == []


def test_gate_force_on_env_activates_small_change(monkeypatch):
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "1")
    s = _subst(_dev_stage(), weight="SMALL_CHANGE")
    assert gates.code_review_active(s) is True
    assert gates.code_review_blockers(s, s.stages[0])  # no review -> blocks


def test_gate_advisor_kill_switch_does_not_disable_gate(monkeypatch):
    # The advisor's own cost knob must not silently defeat the mandatory gate.
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    s = _subst(_dev_stage())
    assert gates.code_review_active(s) is True
    assert gates.code_review_blockers(s, s.stages[0])  # blocks despite advisor off


# --- gate: the verdict matrix (gate active) ----------------------------------

def test_gate_missing_review_blocks(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage())
    b = gates.code_review_blockers(s, s.stages[0])
    assert b and "no code-reviewer verdict" in b[0]


def test_gate_pass_clears(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("pass")])
    assert gates.code_review_blockers(s, s.stages[0]) == []


def test_gate_pass_bound_to_matching_expected_sha_clears(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("pass", code_sha256="abc123")])
    assert gates.code_review_blockers(s, s.stages[0], expected_code_sha256="abc123") == []


def test_gate_pass_bound_to_a_different_expected_sha_is_stale(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("pass", code_sha256="abc123")])
    b = gates.code_review_blockers(s, s.stages[0], expected_code_sha256="def456")
    assert b and "stale" in b[0]


def test_gate_empty_stored_sha_degrades_to_verdict_only(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("pass", code_sha256="")])
    assert gates.code_review_blockers(s, s.stages[0], expected_code_sha256="def456") == []


def test_gate_empty_expected_sha_degrades_to_verdict_only(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("pass", code_sha256="abc123")])
    assert gates.code_review_blockers(s, s.stages[0], expected_code_sha256=None) == []


def test_gate_revise_blocks(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("revise")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert b and "revise" in b[0]


def test_gate_unknown_verdict_blocks(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("maybe")])
    assert gates.code_review_blockers(s, s.stages[0])


def test_gate_override_with_reviewer_and_note_clears(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("override", reviewer="fedor", note="deadlock")])
    assert gates.code_review_blockers(s, s.stages[0]) == []


def test_gate_override_missing_reviewer_blocks(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("override", reviewer="", note="x")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert b and "requires a non-empty reviewer" in b[0]


def test_gate_override_missing_note_blocks(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("override", reviewer="fedor", note="")])
    b = gates.code_review_blockers(s, s.stages[0])
    assert b and "requires a non-empty note" in b[0]


def test_gate_last_recorded_review_wins(monkeypatch):
    monkeypatch.delenv("AGENTCTL_CODE_REVIEW", raising=False)
    s = _subst(_dev_stage(), reviews=[_review("revise"), _review("pass")])
    assert gates.code_review_blockers(s, s.stages[0]) == []


# --- structural contract: pure, and NOT a registered guardian ----------------

def test_gate_is_pure():
    assert impure_names(gates.code_review_blockers) == set()


def test_gate_absent_from_guardians():
    assert "code_review" not in gates.GUARDIANS
    assert set(gates.GUARDIANS) == {"plan_approval", "resolution"}
