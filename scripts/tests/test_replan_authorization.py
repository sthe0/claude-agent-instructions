"""Tests for gates.replan_authorization_active / replan_authorization_blockers
— the write-side twin of plan_presentation_blockers (see that module's
docstring): a non-substantive `replan` edit to an ALREADY APPROVED plan,
outside the DIAGNOSING difficulty cycle, must be presented to the user as a
`replan_diff` receipt AND proven delivered before it may take effect."""
from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery, gates
from agentctl.delivery import DeliveryStamp
from agentctl.state import (
    AUTHORIZE_REPLAN_MARKER,
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    PlanPresentation,
    SessionState,
)
from agentctl.store import FileStateStore
from test_plan_delivery_gate_presentation import (
    _is_deny,
    _stamp,
    run_hook,
    text_only_entry,
    user_prompt_entry,
    write_full_state,
    write_transcript,
)


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


# --- numbered cases from the stage-6 expected result image -----------------------
#
# Each `test_caseN_*` below is named after the numbered case it covers, in order,
# so the mapping to the brief's list is legible without cross-referencing this
# file against it. Some cases need more than one test to cover both halves of
# their claim (e.g. case 10's two `--kind` behaviors, case 11's three hook
# variants, case 12's two coexistence claims) — every such split is noted inline.


def ns(**kw):
    return Namespace(**kw)


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                      criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                         wall_clock_min=60, tracker_key=None, architectural=True,
                         external_effect=False, new_dependency=False,
                         public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                          m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _present_and_stamp_replan_diff(store, sid, target_plan, tmp_path, note="test"):
    rendering = tmp_path / f"rendering-{sid}.txt"
    rendering.write_text("## Replan diff\n...proposed change...\n", encoding="utf-8")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=target_plan, rendering_file=str(rendering)),
        store=store,
    )
    assert d.ok is True, d.detail
    d2 = cli.cmd_confirm_delivery(
        ns(session=sid, kind="replan_diff", by="fedor", note=note,
           escape_reason=delivery.ESCAPE_DELIVERED_OUT_OF_BAND),
        store=store,
    )
    assert d2.ok is True, d2.detail
    return d


# --- case 1 & 2: DIAGNOSING carve-out at the cmd_replan call site -----------------

def test_case1_diagnosing_complete_difficulty_replan_succeeds(store, fixtures_dir, gate_on):
    sid = "case1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)
    state = store.load(sid)
    assert gates.replan_authorization_blockers(state, refined, diff_kind="refinement") == []
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok is True


def test_case2_incomplete_difficulty_refuses_before_replan_authorization(store, fixtures_dir, gate_on):
    sid = "case2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok is False
    assert d.action == "declare"


# --- case 3: refusal leaves state untouched ---------------------------------------

def test_case3_no_receipt_refuses_without_mutating_state(store, fixtures_dir, gate_on):
    sid = "case3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)
    state_path = store.path(sid)
    before = state_path.read_bytes()
    digest_before = store.load(sid).accepted_plan_digest

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    assert d.ok is False
    assert state_path.read_bytes() == before
    assert store.load(sid).accepted_plan_digest == digest_before


# --- case 4: receipt + valid stamp applies the refinement -------------------------

def test_case4_receipt_and_stamp_succeeds_and_applies_refinement(home_store, fixtures_dir, gate_on, tmp_path):
    sid = "case4"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(home_store, sid, plan)
    _present_and_stamp_replan_diff(home_store, sid, refined, tmp_path)

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=home_store)

    assert d.ok is True
    state = home_store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert state.stage(1).title == "Scaffold the module skeleton"


# --- case 5: receipt bound to bytes the plan has since moved past -----------------

def test_case5_cli_stale_receipt_refuses(home_store, fixtures_dir, gate_on, tmp_path):
    sid = "case5"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = tmp_path / "refined.toml"
    refined.write_text((fixtures_dir / "plan_two_stage_refined.toml").read_text())
    _to_executing_stage1(home_store, sid, plan)
    _present_and_stamp_replan_diff(home_store, sid, str(refined), tmp_path)

    refined.write_text(refined.read_text() + "\n# touched after presentation\n")

    d = cli.cmd_replan(ns(session=sid, plan=str(refined)), store=home_store)
    assert d.ok is False
    assert any("changed since it was presented" in b for b in d.data["blockers"])


# --- case 6: stamp bound to a different rendering ---------------------------------

def test_case6_delivery_stamp_wrong_rendering_sha_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "ra6", plan)
    state_file = home_store.path("ra6")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256="different-rendering-sha", verified_ts=2.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.replan_authorization_blockers(s, str(plan), diff_kind="refinement")
    assert blockers and "delivery proof is stale" in blockers[0]


# --- case 7: a substantive diff needs no receipt and re-arms approval ------------

def test_case7_substantive_diff_no_receipt_succeeds_and_rearms(store, fixtures_dir, gate_on):
    sid = "case7"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=bigger), store=store)

    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert not state.approval.passed


# --- case 8: byte-identical proposed plan takes the exemption --------------------

def test_case8_cli_byte_identical_plan_succeeds(store, fixtures_dir, gate_on):
    sid = "case8"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    d = cli.cmd_replan(ns(session=sid, plan=plan), store=store)
    assert d.ok is True


# --- case 9: small_change + env override deactivates the gate ---------------------

def test_case9_small_change_with_env_zero_is_inactive(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    s = SessionState(session_id="s", task_id="t", weight_class="SMALL_CHANGE")
    assert gates.replan_authorization_active(s) is False


# --- case 10: confirm-delivery --kind binds to the matching receipt --------------

def test_case10_confirm_delivery_kind_replan_diff_binds_to_replan_receipt(home_store, tmp_path, gate_on):
    sid = "case10a"
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    state = SessionState(session_id=sid, task_id="t", weight_class="SUBSTANTIVE",
                          plan_path=str(plan), plan_verified=True)
    home_store.save(state)
    _present_and_stamp_replan_diff(home_store, sid, str(plan), tmp_path)

    stamp = delivery.read_stamp(home_store.path(sid))
    assert stamp is not None
    assert stamp.source == delivery.SOURCE_OVERRIDE
    assert stamp.plan_sha256 == _sha256_file(plan)


def test_case10_confirm_delivery_default_kind_binds_to_essence(home_store, tmp_path):
    sid = "case10b"
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    receipt = PlanPresentation(
        plan_path=str(plan), kind="essence", plan_sha256=_sha256_file(plan),
        rendering_sha256="e" * 64, rendering_text="essence text", presented_ts=1.0,
    )
    state = SessionState(session_id=sid, task_id="t", weight_class="SUBSTANTIVE",
                          plan_path=str(plan), plan_verified=True, plan_presentations=[receipt])
    home_store.save(state)

    d = cli.cmd_confirm_delivery(
        ns(session=sid, kind=None, by="fedor", note="hook not installed",
           escape_reason=delivery.ESCAPE_HOOK_NOT_INSTALLED),
        store=home_store,
    )

    assert d.ok is True
    stamp = delivery.read_stamp(home_store.path(sid))
    assert stamp is not None
    assert stamp.rendering_sha256 == "e" * 64


# --- case 11: hook stamps the replan_diff receipt only on the marked, delivered ---
# ask — three variants covering the marker, the same-turn/open-turn exclusion, and
# the no-marker case.

def _replan_ask_payload(session_id, transcript_path, *, with_marker=True):
    option = {"label": f"Yes, authorize {AUTHORIZE_REPLAN_MARKER}" if with_marker else "Yes, authorize"}
    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": session_id,
        "tool_input": {"questions": [{"question": "Authorize this replan?",
                                       "options": [option, {"label": "No"}]}]},
    }
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    return payload


def _replan_receipt(plan_path, rendering_text, presented_ts, *, plan_sha256="a" * 64, rendering_sha256="b" * 64):
    return PlanPresentation(
        plan_path=plan_path, kind="replan_diff", plan_sha256=plan_sha256,
        rendering_sha256=rendering_sha256, rendering_text=rendering_text, presented_ts=presented_ts,
    )


def test_case11_hook_stamps_replan_diff_only_with_marker(tmp_path):
    rendering = "## Replan diff\n...proposed change...\n"
    receipt = _replan_receipt("/plan.toml", rendering, 100.0)
    write_full_state(tmp_path, "hk1", node="EXECUTING", plan_presentations=[receipt],
                      approval_passed=True)
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, rendering),
        user_prompt_entry(110.0),
    ])

    proc = run_hook(_replan_ask_payload("hk1", t, with_marker=True), tmp_path)

    assert proc.returncode == 0
    assert not _is_deny(proc)
    stamp = _stamp(tmp_path, "hk1")
    assert stamp is not None
    assert stamp.plan_sha256 == "a" * 64 and stamp.rendering_sha256 == "b" * 64


def test_case11_same_turn_rendering_not_stamped(tmp_path):
    rendering = "## Replan diff\n...proposed change...\n"
    receipt = _replan_receipt("/plan.toml", rendering, 100.0)
    write_full_state(tmp_path, "hk2", node="EXECUTING", plan_presentations=[receipt],
                      approval_passed=True)
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, rendering),  # no closing boundary -> still the open turn
    ])

    proc = run_hook(_replan_ask_payload("hk2", t, with_marker=True), tmp_path)

    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "hk2") is None


def test_case11_no_marker_ask_not_stamped(tmp_path):
    rendering = "## Replan diff\n...proposed change...\n"
    receipt = _replan_receipt("/plan.toml", rendering, 100.0)
    write_full_state(tmp_path, "hk3", node="EXECUTING", plan_presentations=[receipt],
                      approval_passed=True)
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, rendering),
        user_prompt_entry(110.0),
    ])

    proc = run_hook(_replan_ask_payload("hk3", t, with_marker=False), tmp_path)

    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "hk3") is None


# --- case 12: essence and replan_diff receipts coexist; two replan_diff
# presentations against different --plan targets collapse to one -----------------

def test_case12_essence_and_replan_diff_coexist_without_superseding(store, tmp_path):
    sid = "case12a"
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    state = SessionState(session_id=sid, task_id="t", weight_class="SUBSTANTIVE",
                          plan_path=str(plan), plan_verified=True)
    store.save(state)

    essence_rendering = tmp_path / "essence.txt"
    essence_rendering.write_text("## essence\n", encoding="utf-8")
    cli.cmd_present_plan(ns(session=sid, kind="essence", plan=None,
                            rendering_file=str(essence_rendering)), store=store)
    diff_rendering = tmp_path / "diff.txt"
    diff_rendering.write_text("## replan diff\n", encoding="utf-8")
    cli.cmd_present_plan(ns(session=sid, kind="replan_diff", plan=str(plan),
                            rendering_file=str(diff_rendering)), store=store)

    state = store.load(sid)
    kinds = sorted(p.kind for p in state.plan_presentations)
    assert kinds == ["essence", "replan_diff"]


def test_case12_two_replan_diff_presentations_different_paths_leave_one_record(store, tmp_path):
    sid = "case12b"
    plan_a = tmp_path / "a.toml"
    plan_a.write_text("index = 1\n")
    plan_b = tmp_path / "b.toml"
    plan_b.write_text("index = 2\n")
    state = SessionState(session_id=sid, task_id="t", weight_class="SUBSTANTIVE",
                          plan_path=str(plan_a), plan_verified=True)
    store.save(state)

    r1 = tmp_path / "r1.txt"
    r1.write_text("## diff A\n", encoding="utf-8")
    cli.cmd_present_plan(ns(session=sid, kind="replan_diff", plan=str(plan_a),
                            rendering_file=str(r1)), store=store)
    r2 = tmp_path / "r2.txt"
    r2.write_text("## diff B\n", encoding="utf-8")
    cli.cmd_present_plan(ns(session=sid, kind="replan_diff", plan=str(plan_b),
                            rendering_file=str(r2)), store=store)

    state = store.load(sid)
    replan_receipts = [p for p in state.plan_presentations if p.kind == "replan_diff"]
    assert len(replan_receipts) == 1
    assert replan_receipts[0].plan_path == str(plan_b)


# --- case 13: path-mismatch staleness, made reachable by real --plan usage -------

def test_case13_receipt_path_mismatch_reachable_via_present_then_replan(store, fixtures_dir, gate_on, tmp_path):
    sid = "case13"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)

    rendering = tmp_path / "rendering.txt"
    rendering.write_text("## diff\n", encoding="utf-8")
    d = cli.cmd_present_plan(ns(session=sid, kind="replan_diff", plan=refined,
                                rendering_file=str(rendering)), store=store)
    assert d.ok is True

    # A DIFFERENT refinement target than the one just presented — classifies as
    # "refinement", not "substantive", so it cannot take the diff_kind exemption
    # and must actually reach the receipt's path-mismatch/staleness branch.
    other = tmp_path / "other_refinement.toml"
    other.write_text(Path(plan).read_text().replace("Add tests", "Add more tests"))

    d2 = cli.cmd_replan(ns(session=sid, plan=str(other)), store=store)
    assert d2.ok is False
    assert any("stale" in b and "replan-diff presentation" in b for b in d2.data["blockers"])


# --- case 14: --renormalize legitimately diverges accepted_plan_digest from
# plan_snapshot_path; the authorization gate is bound by the DIGEST, and a
# genuinely different refinement is still gated despite the divergence ----------

def test_case14_renormalize_digest_diverges_from_snapshot_but_still_gates_refinement(
    store, fixtures_dir, gate_on, tmp_path
):
    sid = "case14"
    base_text = (fixtures_dir / "plan_two_stage_means.toml").read_text()
    # Two variants differing ONLY in stage 1's `procedure` — the one field
    # --renormalize is licensed to move without a fresh review/approval.
    variant_a = base_text.replace(
        'method = "re-run the same import unchanged"',
        'method = "re-run the same import unchanged"\nprocedure = "install then import"',
    )
    variant_b = base_text.replace(
        'method = "re-run the same import unchanged"',
        'method = "re-run the same import unchanged"\nprocedure = "reload then import"',
    )
    plan_a = tmp_path / "variant_a.toml"
    plan_a.write_text(variant_a)
    plan_b = tmp_path / "variant_b.toml"
    plan_b.write_text(variant_b)

    _to_executing_stage1(store, sid, str(plan_a))
    before = store.load(sid)
    digest_a = before.accepted_plan_digest
    snapshot_before = before.plan_snapshot_path

    d = cli.cmd_replan(ns(session=sid, plan=str(plan_b), renormalize=True), store=store)
    assert d.ok is True, d.detail

    state = store.load(sid)
    assert state.accepted_plan_digest == _sha256_file(plan_b)
    assert state.accepted_plan_digest != digest_a
    # the snapshot legitimately does NOT move — it still names the ORIGINALLY
    # approved bytes, not the renormalized ones, per _renormalize_replan's own
    # contract; this is the divergence the case exists to pin.
    assert state.plan_snapshot_path == snapshot_before

    # byte-identical exemption: a replan against the SAME (renormalized) bytes
    # still clears, because accepted_plan_digest now names them.
    assert gates.replan_authorization_blockers(state, str(plan_b), diff_kind="refinement") == []

    # but a genuinely different refinement is still gated — bounded to exactly
    # the bytes gates.renormalization_blockers verified as means.procedure-only,
    # not opened wider by the digest/snapshot divergence above.
    blockers = gates.replan_authorization_blockers(state, str(plan_a), diff_kind="refinement")
    assert blockers and "no replan-diff presentation recorded" in blockers[0]


# --- case 15: the cost invariant — no marker means no state load, no scan -------

def test_case15_no_marker_ask_never_loads_state_or_scans_transcript(tmp_path, monkeypatch):
    import importlib.util

    hook_path = Path(__file__).resolve().parent.parent / "hook-plan-delivery-gate.py"
    spec = importlib.util.spec_from_file_location("hook_case15", hook_path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    calls = {"state": 0, "transcript": 0}

    def _count_state(*_a, **_kw):
        calls["state"] += 1
        return None

    def _count_transcript(*_a, **_kw):
        calls["transcript"] += 1
        return []

    monkeypatch.setattr(hook, "_load_session_state", _count_state)
    monkeypatch.setattr(hook, "delivered_final_texts", _count_transcript)

    config_dir = tmp_path
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    from agentctl.state import GateRecord

    state = SessionState(
        session_id="c15", task_id="t", node="EXECUTING", weight_class="SUBSTANTIVE",
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
    )
    (state_dir / "c15.json").write_text(json.dumps(state.to_dict()))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")

    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": "c15",
        "transcript_path": str(transcript),
        "tool_input": {"questions": [{"options": [{"label": "Yes"}, {"label": "No"}]}]},
    }

    decision, reason, sp, receipt = hook.decide(payload)

    assert decision == "allow"
    assert receipt is None
    assert calls["state"] == 0
    assert calls["transcript"] == 0
