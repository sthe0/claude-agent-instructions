"""Tests for gates.replan_authorization_active / replan_authorization_blockers
— the write-side twin of plan_presentation_blockers (see that module's
docstring): a non-substantive `replan` edit to an ALREADY APPROVED plan,
outside the DIAGNOSING difficulty cycle, must be presented to the user as a
`replan_diff` receipt AND proven delivered before it may take effect."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agentctl import delivery, gates
from agentctl.delivery import DeliveryStamp
from agentctl.state import (
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    PlanPresentation,
    SessionState,
)
from agentctl.store import FileStateStore


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "1")


@pytest.fixture
def home_store(tmp_path, monkeypatch):
    """A store whose root agrees with lib.config_root.resolve_agentctl_state_file
    — same recipe as test_plan_presentation.py's fixture of the same name,
    needed for any test exercising the delivery-stamp half of the gate."""
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return FileStateStore(home / "agentctl" / "state")


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _subst(**kw) -> SessionState:
    kw.setdefault("plan_path", "/plan.toml")
    return SessionState(
        session_id="s", task_id="t", weight_class="SUBSTANTIVE",
        plan_verified=True, **kw
    )


def _complete_difficulty() -> Difficulty:
    return Difficulty(
        declaration=Declaration("e", "a", "m"),
        investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
        critique=Critique("fg", "rt"),
    )


# --- replan_authorization_active ------------------------------------------------

def test_active_default_env_substantive_session():
    s = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE")
    assert gates.replan_authorization_active(s) is True


def test_active_default_env_small_change_session_is_inactive():
    s = SessionState(session_id="s", task_id="t", weight_class="SMALL_CHANGE")
    assert gates.replan_authorization_active(s) is False


def test_active_env_zero_forces_off_even_for_substantive(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    s = SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE")
    assert gates.replan_authorization_active(s) is False


def test_active_env_one_forces_on_even_for_small_change(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "1")
    s = SessionState(session_id="s", task_id="t", weight_class="SMALL_CHANGE")
    assert gates.replan_authorization_active(s) is True


# --- replan_authorization_blockers: the four unconditional []-conditions -------

def test_inactive_gate_clears(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    s = _subst()
    assert gates.replan_authorization_blockers(s, "/plan.toml", diff_kind="refinement") == []


def test_diagnosing_with_complete_difficulty_clears(gate_on):
    s = _subst(node=Node.DIAGNOSING.value, difficulty=_complete_difficulty())
    assert gates.replan_authorization_blockers(s, "/plan.toml", diff_kind="refinement") == []


def test_diagnosing_with_incomplete_difficulty_not_exempted(gate_on):
    """The DIAGNOSING carve-out requires a COMPLETE cycle — an incomplete one
    (or none at all) must fall through to the ordinary receipt/delivery check,
    not be waved through just because the node is DIAGNOSING."""
    s = _subst(node=Node.DIAGNOSING.value, difficulty=Difficulty(declaration=Declaration("e", "a", "m")))
    blockers = gates.replan_authorization_blockers(s, "/plan.toml", diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


def test_diagnosing_with_no_difficulty_record_not_exempted(gate_on):
    s = _subst(node=Node.DIAGNOSING.value)
    blockers = gates.replan_authorization_blockers(s, "/plan.toml", diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


def test_substantive_diff_kind_clears(gate_on):
    s = _subst()
    assert gates.replan_authorization_blockers(s, "/plan.toml", diff_kind="substantive") == []


def test_no_change_diff_kind_not_exempted(gate_on):
    """Only 'substantive' is named as an exemption — 'no_change' must still be
    checked (it is expected to clear via the byte-identical digest condition
    instead, not via diff_kind alone)."""
    s = _subst()
    blockers = gates.replan_authorization_blockers(s, "/plan.toml", diff_kind="no_change")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


def test_byte_identical_to_accepted_digest_clears(gate_on, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    s = _subst(plan_path=str(plan), accepted_plan_digest=_sha256_file(plan))
    assert gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement") == []


def test_digest_mismatch_not_exempted(gate_on, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    s = _subst(plan_path=str(plan), accepted_plan_digest="deadbeef" * 8)
    blockers = gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


def test_unreadable_target_plan_not_exempted_by_digest_check(gate_on, tmp_path):
    """An OSError reading target_plan must NOT take the byte-identical
    exemption — the stage's own charter names this explicitly."""
    missing = tmp_path / "gone.toml"
    s = _subst(plan_path=str(missing), accepted_plan_digest="deadbeef" * 8)
    blockers = gates.replan_authorization_blockers(s, str(missing), diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


# --- receipt-side blockers -------------------------------------------------------

def test_no_receipt_blocks_naming_present_plan_and_target(gate_on):
    blockers = gates.replan_authorization_blockers(_subst(), "/plan.toml", diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]
    assert "present-plan --kind replan_diff" in blockers[0]
    assert "/plan.toml" in blockers[0]


def test_essence_only_receipt_does_not_satisfy_replan_diff(gate_on):
    """An essence/full receipt is a DIFFERENT kind — it must not satisfy the
    replan_diff check (the two charters stay independent)."""
    pp = PlanPresentation(
        plan_path="/plan.toml", kind="essence", plan_sha256="", rendering_sha256="",
        rendering_text="", presented_ts=1.0,
    )
    blockers = gates.replan_authorization_blockers(
        _subst(plan_presentations=[pp]), "/plan.toml", diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


def test_receipt_for_different_plan_path_blocks_as_stale_never_says_delivery(gate_on):
    pp = PlanPresentation(
        plan_path="/OTHER.toml", kind="replan_diff", plan_sha256="", rendering_sha256="",
        rendering_text="", presented_ts=1.0,
    )
    blockers = gates.replan_authorization_blockers(
        _subst(plan_presentations=[pp]), "/plan.toml", diff_kind="refinement")
    assert blockers and "stale" in blockers[0]
    assert "replan-diff presentation" in blockers[0]
    # hook-plan-delivery-gate.py's _receipt_stale_reason partitions gates'
    # messages by the substring "delivery" — this label must never contain it.
    assert "delivery" not in blockers[0]


def test_content_hash_mismatch_after_inplace_rewrite_blocks_as_stale(gate_on, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    pp = PlanPresentation(
        plan_path=str(plan), kind="replan_diff", plan_sha256=_sha256_file(plan),
        rendering_sha256="r", rendering_text="t", presented_ts=1.0,
    )
    s = _subst(plan_path=str(plan), plan_presentations=[pp])
    plan.write_text("index = 2\n")  # in-place rewrite after presentation
    blockers = gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement")
    assert blockers and "changed since it was presented" in blockers[0]


# --- delivery-side blockers (real sidecar via home_store) -----------------------

def _bound_state(store, sid, plan) -> tuple[SessionState, PlanPresentation]:
    plan.write_text("index = 1\n")
    receipt = PlanPresentation(
        plan_path=str(plan), kind="replan_diff", plan_sha256=_sha256_file(plan),
        rendering_sha256="rend-sha", rendering_text="t", presented_ts=1.0,
    )
    s = SessionState(
        session_id=sid, task_id="t", weight_class="SUBSTANTIVE",
        plan_path=str(plan), plan_verified=True, plan_presentations=[receipt],
    )
    store.save(s)
    return s, receipt


def test_delivery_missing_stamp_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, _ = _bound_state(home_store, "ra1", plan)
    blockers = gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement")
    assert blockers and "no delivery proof recorded" in blockers[0]
    assert "confirm-delivery" in blockers[0]


def test_delivery_hook_stamp_matching_clears(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "ra2", plan)
    state_file = home_store.path("ra2")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement") == []


def test_delivery_stale_plan_sha_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "ra3", plan)
    state_file = home_store.path("ra3")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256="stale" * 16,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement")
    assert blockers and "delivery proof is stale" in blockers[0]


def test_delivery_override_with_by_and_note_clears(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "ra4", plan)
    state_file = home_store.path("ra4")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0,
        source=delivery.SOURCE_OVERRIDE, by="fedor", note="hook not installed",
    )
    delivery.write_stamp(state_file, stamp)
    assert gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement") == []


def test_delivery_override_missing_note_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "ra5", plan)
    state_file = home_store.path("ra5")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0,
        source=delivery.SOURCE_OVERRIDE, by="fedor", note="",
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement")
    assert blockers and "requires a non-empty" in blockers[0] and "note" in blockers[0]
