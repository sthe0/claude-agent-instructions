"""Tests for the PlanPresentation receipt + delivery-stamp gate:
state.py's PlanPresentation dataclass/round-trip, cli.py's `present-plan` /
`confirm-delivery` commands, gates.py's plan_presentation_blockers (receipt
side and delivery side), and the cmd_approve refusal that closes the
ask-less-bypass residual this stage exists to remove."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery, gates
from agentctl.delivery import DeliveryStamp
from agentctl.state import (
    Node,
    PLAN_PRESENTATION_RENDERING_CAP_BYTES,
    PlanPresentation,
    SessionState,
    SHOW_FULL_PLAN_MARKER,
)
from agentctl.store import FileStateStore

_DELIVERY_GATE_HOOK = Path(__file__).resolve().parent.parent / "hook-plan-delivery-gate.py"


def _load_delivery_gate_hook():
    """Load the hyphenated hook module by path — same recipe as
    test_plan_delivery_gate_hook.py's _load_module, reused rather than
    re-implemented."""
    spec = importlib.util.spec_from_file_location("hook_plan_delivery_gate", _DELIVERY_GATE_HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")


@pytest.fixture
def home_store(tmp_path, monkeypatch):
    """A store whose root agrees with lib.config_root.resolve_agentctl_state_file,
    needed for any test that exercises the DELIVERY-side half of
    plan_presentation_blockers (or cmd_confirm_delivery/cmd_present_plan's
    interaction with the sidecar) — see the class summary for why the plain
    `store` fixture's tmp_path root does not agree with config_root's
    resolution."""
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return FileStateStore(home / "agentctl" / "state")


def _to_plan_ready(store, sid, plan) -> None:
    cli.cmd_start(
        ns(session=sid, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _write_rendering(tmp_path, text, name="rendering.txt") -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# --- state round trip ----------------------------------------------------------

def test_state_round_trip_plan_presentation():
    pp = PlanPresentation(
        plan_path="/plan.toml", kind="essence", plan_sha256="a" * 64,
        rendering_sha256="b" * 64, rendering_text="hello", presented_ts=123.0,
    )
    s = SessionState(session_id="s", task_id="t", plan_presentations=[pp])
    assert s.schema_version == 21
    round_tripped = SessionState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert round_tripped == s


def test_legacy_state_with_no_plan_presentations_key_loads_empty():
    raw = SessionState(session_id="s", task_id="t").to_dict()
    raw.pop("plan_presentations", None)
    s = SessionState.from_dict(raw)
    assert s.plan_presentations == []


def test_plan_presentation_requires_presented_ts():
    with pytest.raises(TypeError):
        PlanPresentation(
            plan_path="/plan.toml", kind="essence", plan_sha256="a",
            rendering_sha256="b", rendering_text="x",
        )


# --- cmd_present_plan: essence / full / skeleton / supersede / cap ------------

def test_present_plan_essence_stamps_receipt(store, fixtures_dir, tmp_path, gate_on):
    sid = "pp1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Summary of the plan.")

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    s = store.load(sid)
    assert len(s.plan_presentations) == 1
    p = s.plan_presentations[0]
    assert p.kind == "essence"
    assert p.plan_sha256 == _sha256_file(plan)
    assert p.rendering_sha256 == _sha256_file(rendering)
    assert p.presented_ts > 0

    # The essence directive must hand the coordinator the full choreography —
    # the marker literal and the three ordered steps — at the one point it is
    # guaranteed to read it, rather than leaving that to forgettable prose.
    assert SHOW_FULL_PLAN_MARKER in d.detail
    assert "sleep 2" in d.detail
    assert "FINAL text" in d.detail
    assert "next turn" in d.detail
    assert d.data["show_full_plan_marker"] == SHOW_FULL_PLAN_MARKER
    assert isinstance(d.data["next_steps"], list) and len(d.data["next_steps"]) == 3


def test_present_plan_full_directive_detail_unchanged(store, fixtures_dir, tmp_path, gate_on):
    """Regression guard: only the essence branch gained the choreography —
    the full-kind directive's detail text is byte-identical to before."""
    sid = "pp1f"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(
        tmp_path, "[stage 1] Scaffold module\nbody1\n[stage 2] Add tests\nbody2\n"
    )

    d = cli.cmd_present_plan(
        ns(session=sid, kind="full", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    assert d.detail == (
        "presentation receipt recorded (kind=full); emit this exact rendering "
        "as the turn's FINAL text message so the delivery hook can verify it "
        "actually reached the user"
    )
    assert set(d.data.keys()) == {"rendering_sha256", "plan_sha256"}


def test_emitted_essence_marker_satisfies_hook(store, fixtures_dir, tmp_path, gate_on):
    """Cross-component regression test for the exact fixable problem: the
    marker the ENGINE EMITS, dropped into an ask option, must actually clear
    the hook's own marker check — and an ask with no marker at all must still
    be denied with _NO_MARKER_REASON."""
    sid = "pp1x"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Summary of the plan.")

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    marker = d.data["show_full_plan_marker"]

    hook = _load_delivery_gate_hook()
    tool_input_with_marker = {
        "questions": [{"options": [{"label": f"show the full plan {marker}"}]}]
    }
    assert hook._has_show_full_plan_option(tool_input_with_marker) is True

    decision, reason, _delivery_verified = hook.gate_decision(
        "PLAN_READY", 100.0, 90.0, turn_start_ts=110.0,
        presentation_active=True,
        receipt=PlanPresentation(
            plan_path=plan, kind="essence", plan_sha256="a" * 64,
            rendering_sha256="b" * 64, rendering_text="Summary of the plan.",
            presented_ts=100.0,
        ),
        receipt_stale_reason=None,
        delivered_texts=[("Summary of the plan.", 105.0)],
        has_show_full_plan_option=False,
    )
    assert decision == "deny"
    assert reason == hook._NO_MARKER_REASON


def test_present_plan_full_missing_stage_anchor_rejected_nothing_stamped(
    store, fixtures_dir, tmp_path, gate_on
):
    sid = "pp2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "[stage 1] only one stage here\nbody")

    d = cli.cmd_present_plan(
        ns(session=sid, kind="full", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert d.data.get("missing") == [2]
    assert store.load(sid).plan_presentations == []


def test_present_plan_full_complete_stamps(store, fixtures_dir, tmp_path, gate_on):
    sid = "pp3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(
        tmp_path, "[stage 1] Scaffold module\nbody1\n[stage 2] Add tests\nbody2\n"
    )

    d = cli.cmd_present_plan(
        ns(session=sid, kind="full", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    assert [p.kind for p in store.load(sid).plan_presentations] == ["full"]


def test_emit_skeleton_enumerates_stages_in_order_and_stamps_nothing(
    store, fixtures_dir, gate_on
):
    sid = "sk1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)

    d = cli.cmd_present_plan(
        ns(session=sid, emit_skeleton=True, kind="essence", rendering_file=None),
        store=store,
    )
    assert d.ok is True
    assert d.data["skeleton"] == "[stage 1] Scaffold module\n[stage 2] Add tests\n"
    assert d.data["stage_count"] == 2
    assert store.load(sid).plan_presentations == []


def test_present_plan_missing_rendering_file_non_ok_nothing_stamped(
    store, fixtures_dir, gate_on
):
    sid = "pp4"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file="/no/such/file", emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert store.load(sid).plan_presentations == []


def test_supersede_essence_leaves_one_receipt_the_newest(store, fixtures_dir, tmp_path, gate_on):
    sid = "sup1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    r1 = _write_rendering(tmp_path, "first", "r1.txt")
    r2 = _write_rendering(tmp_path, "second", "r2.txt")

    cli.cmd_present_plan(ns(session=sid, kind="essence", rendering_file=r1, emit_skeleton=False), store=store)
    cli.cmd_present_plan(ns(session=sid, kind="essence", rendering_file=r2, emit_skeleton=False), store=store)

    essence = [p for p in store.load(sid).plan_presentations if p.kind == "essence"]
    assert len(essence) == 1
    assert essence[0].rendering_text == "second"


def test_essence_and_full_presentations_leave_two_receipts(store, fixtures_dir, tmp_path, gate_on):
    sid = "sup2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    r1 = _write_rendering(tmp_path, "essence text", "e.txt")
    r2 = _write_rendering(
        tmp_path, "[stage 1] Scaffold module\nb1\n[stage 2] Add tests\nb2\n", "f.txt"
    )

    cli.cmd_present_plan(ns(session=sid, kind="essence", rendering_file=r1, emit_skeleton=False), store=store)
    cli.cmd_present_plan(ns(session=sid, kind="full", rendering_file=r2, emit_skeleton=False), store=store)

    assert sorted(p.kind for p in store.load(sid).plan_presentations) == ["essence", "full"]


# --- cmd_present_plan essence gated on plan_review_blockers -------------------

@pytest.fixture
def review_gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


def test_present_plan_essence_refused_without_review_nothing_stamped(
    store, fixtures_dir, tmp_path, gate_on, review_gate_on
):
    sid = "prg1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Summary of the plan.")

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert d.data["blockers"] == gates.plan_review_blockers(store.load(sid), plan)
    assert store.load(sid).plan_presentations == []


def test_present_plan_essence_allowed_after_passing_review(
    store, fixtures_dir, tmp_path, gate_on, review_gate_on
):
    sid = "prg2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_plan_review(
        ns(session=sid, verdict="pass", reviewer="thinker", concerns=None, note="", target=None),
        store=store,
    )
    rendering = _write_rendering(tmp_path, "Summary of the plan.")

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    assert [p.kind for p in store.load(sid).plan_presentations] == ["essence"]


def test_present_plan_full_not_gated_by_plan_review(
    store, fixtures_dir, tmp_path, gate_on, review_gate_on
):
    sid = "prg3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(
        tmp_path, "[stage 1] Scaffold module\nbody1\n[stage 2] Add tests\nbody2\n"
    )

    d = cli.cmd_present_plan(
        ns(session=sid, kind="full", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is True
    assert [p.kind for p in store.load(sid).plan_presentations] == ["full"]


def test_present_plan_over_cap_rejected_not_truncated(store, fixtures_dir, tmp_path, gate_on):
    sid = "cap1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "x" * (PLAN_PRESENTATION_RENDERING_CAP_BYTES + 1))

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert str(PLAN_PRESENTATION_RENDERING_CAP_BYTES) in d.detail
    assert store.load(sid).plan_presentations == []


# --- receipt-side blockers (unit level) ----------------------------------------

def _subst(**kw) -> SessionState:
    kw.setdefault("plan_path", "/plan.toml")
    return SessionState(
        session_id="s", task_id="t", weight_class="SUBSTANTIVE",
        plan_verified=True, **kw
    )


def test_no_receipt_blocks(gate_on):
    blockers = gates.plan_presentation_blockers(_subst(), "/plan.toml")
    assert blockers and "no plan presentation recorded" in blockers[0]


def test_receipt_for_different_plan_path_blocks_as_stale(gate_on):
    pp = PlanPresentation(
        plan_path="/OTHER.toml", kind="essence", plan_sha256="", rendering_sha256="",
        rendering_text="", presented_ts=1.0,
    )
    blockers = gates.plan_presentation_blockers(_subst(plan_presentations=[pp]), "/plan.toml")
    assert blockers and "stale" in blockers[0]


def test_full_only_receipt_without_essence_blocks(gate_on):
    pp = PlanPresentation(
        plan_path="/plan.toml", kind="full", plan_sha256="", rendering_sha256="",
        rendering_text="", presented_ts=1.0,
    )
    blockers = gates.plan_presentation_blockers(_subst(plan_presentations=[pp]), "/plan.toml")
    assert blockers and "no plan presentation recorded" in blockers[0]


def test_inactive_session_clears(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "0")
    s = SessionState(session_id="s", task_id="t", weight_class="SMALL_CHANGE", plan_path="/plan.toml")
    assert gates.plan_presentation_blockers(s, "/plan.toml") == []


def test_env_zero_clears_even_for_substantive_session(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "0")
    assert gates.plan_presentation_blockers(_subst(), "/plan.toml") == []


def test_content_hash_mismatch_after_inplace_rewrite_blocks_as_stale(gate_on, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("index = 1\n")
    pp = PlanPresentation(
        plan_path=str(plan), kind="essence", plan_sha256=_sha256_file(plan),
        rendering_sha256="r", rendering_text="t", presented_ts=1.0,
    )
    s = _subst(plan_path=str(plan), plan_presentations=[pp])
    plan.write_text("index = 2\n")  # in-place rewrite after presentation
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "changed since it was presented" in blockers[0]


def test_content_hash_unreadable_target_degrades_to_path_binding_not_a_wedge(gate_on, tmp_path):
    missing = tmp_path / "gone.toml"
    pp = PlanPresentation(
        plan_path=str(missing), kind="essence", plan_sha256="deadbeef",
        rendering_sha256="r", rendering_text="t", presented_ts=1.0,
    )
    s = _subst(plan_path=str(missing), plan_presentations=[pp])
    blockers = gates.plan_presentation_blockers(s, str(missing))
    # receipt-side clears (degrades to path-only binding, per doubt-your-own
    # -snapshot fail-open discipline); falls through to the delivery check,
    # which blocks for its own, separate reason (no stamp exists at all).
    assert blockers and "delivery proof" in blockers[0]


def test_content_hash_empty_stored_hash_is_path_only(gate_on, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("x")
    pp = PlanPresentation(
        plan_path=str(plan), kind="essence", plan_sha256="",
        rendering_sha256="r", rendering_text="t", presented_ts=1.0,
    )
    s = _subst(plan_path=str(plan), plan_presentations=[pp])
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "delivery proof" in blockers[0]


# --- delivery-side blockers (unit level, real sidecar via home_store) ---------

def _bound_state(store, sid, plan) -> tuple[SessionState, PlanPresentation]:
    plan.write_text("index = 1\n")
    receipt = PlanPresentation(
        plan_path=str(plan), kind="essence", plan_sha256=_sha256_file(plan),
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
    s, _ = _bound_state(home_store, "d1", plan)
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "no delivery proof recorded" in blockers[0]
    assert "confirm-delivery" in blockers[0]


def test_delivery_hook_stamp_matching_clears(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d2", plan)
    state_file = home_store.path("d2")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    assert gates.plan_presentation_blockers(s, str(plan)) == []


def test_delivery_stale_plan_sha_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d3", plan)
    state_file = home_store.path("d3")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256="stale" * 16,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "delivery proof is stale" in blockers[0]


def test_delivery_mismatched_rendering_sha_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d4", plan)
    state_file = home_store.path("d4")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256="other-rendering-superseded", verified_ts=2.0, source=delivery.SOURCE_HOOK,
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "delivery proof is stale" in blockers[0]


def test_delivery_corrupt_sidecar_blocks_not_fail_open(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, _ = _bound_state(home_store, "d5", plan)
    state_file = home_store.path("d5")
    sidecar = delivery.stamp_path_for(state_file)
    sidecar.write_text("{not valid json", encoding="utf-8")
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "no delivery proof recorded" in blockers[0]


def test_delivery_override_with_by_and_note_clears(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d6", plan)
    state_file = home_store.path("d6")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0,
        source=delivery.SOURCE_OVERRIDE, by="fedor", note="hook not installed",
    )
    delivery.write_stamp(state_file, stamp)
    assert gates.plan_presentation_blockers(s, str(plan)) == []


def test_delivery_override_missing_note_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d7", plan)
    state_file = home_store.path("d7")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0,
        source=delivery.SOURCE_OVERRIDE, by="fedor", note="",
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "requires a non-empty" in blockers[0] and "note" in blockers[0]


def test_delivery_override_missing_by_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d8", plan)
    state_file = home_store.path("d8")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0,
        source=delivery.SOURCE_OVERRIDE, by="", note="hook not installed",
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "requires a non-empty" in blockers[0] and "by" in blockers[0]


def test_delivery_unknown_source_blocks(gate_on, home_store, tmp_path):
    plan = tmp_path / "plan.toml"
    s, receipt = _bound_state(home_store, "d9", plan)
    state_file = home_store.path("d9")
    stamp = DeliveryStamp(
        plan_path=receipt.plan_path, plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256, verified_ts=2.0, source="carrier-pigeon",
    )
    delivery.write_stamp(state_file, stamp)
    blockers = gates.plan_presentation_blockers(s, str(plan))
    assert blockers and "delivery stamp source is" in blockers[0]


# --- cmd_confirm_delivery -------------------------------------------------------

def test_confirm_delivery_rejects_by_hook_case_insensitively(home_store, fixtures_dir, tmp_path, gate_on):
    sid = "cd1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(home_store, sid, plan)
    rendering = _write_rendering(tmp_path, "essence text")
    cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=home_store,
    )
    d = cli.cmd_confirm_delivery(ns(session=sid, by="HOOK", note="x"), store=home_store)
    assert d.ok is False


def test_confirm_delivery_requires_essence_receipt_first(home_store, fixtures_dir, gate_on):
    sid = "cd2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(home_store, sid, plan)
    d = cli.cmd_confirm_delivery(ns(session=sid, by="fedor", note="x"), store=home_store)
    assert d.ok is False


def test_confirm_delivery_missing_note_refuses(home_store, fixtures_dir, tmp_path, gate_on):
    sid = "cd3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(home_store, sid, plan)
    rendering = _write_rendering(tmp_path, "essence text")
    cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=home_store,
    )
    d = cli.cmd_confirm_delivery(ns(session=sid, by="fedor", note=""), store=home_store)
    assert d.ok is False


# --- cmd_approve integration: the ask-less-bypass fence ------------------------

def test_approve_blocked_without_essence_presentation(home_store, fixtures_dir, gate_on):
    sid = "apr1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(home_store, sid, plan)
    cli.cmd_plan_review(
        ns(session=sid, verdict="pass", reviewer="thinker", concerns=None, note="", target=None),
        store=home_store,
    )
    d = cli.cmd_approve(ns(session=sid, by="user"), store=home_store)
    assert d.ok is False
    assert d.node == Node.PLAN_READY.value
    assert any("presentation" in b or "present-plan" in b for b in d.data["blockers"])


def test_approve_proceeds_with_receipt_and_matching_delivery_stamp(
    home_store, fixtures_dir, tmp_path, gate_on
):
    sid = "apr2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(home_store, sid, plan)
    cli.cmd_plan_review(
        ns(session=sid, verdict="pass", reviewer="thinker", concerns=None, note="", target=None),
        store=home_store,
    )
    rendering = _write_rendering(tmp_path, "essence text")
    cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=home_store,
    )
    cli.cmd_confirm_delivery(
        ns(session=sid, by="fedor", note="hook not installed in test"), store=home_store,
    )
    d = cli.cmd_approve(ns(session=sid, by="user"), store=home_store)
    assert d.node == Node.APPROVED.value


def test_essence_receipt_without_delivery_stamp_still_blocks_approve(
    home_store, fixtures_dir, tmp_path, gate_on
):
    """The residual this stage exists to close: registering a presentation
    receipt alone — with no delivery proof — must never let approve through,
    even though present-plan itself always returns ok=True (it only stamps a
    RECEIPT, never delivery). If this test ever passes approve, the ask-less
    bypass is re-opened."""
    sid = "apr3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(home_store, sid, plan)
    cli.cmd_plan_review(
        ns(session=sid, verdict="pass", reviewer="thinker", concerns=None, note="", target=None),
        store=home_store,
    )
    rendering = _write_rendering(tmp_path, "essence text")
    d_present = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False),
        store=home_store,
    )
    assert d_present.ok is True  # presentation itself succeeds...

    d = cli.cmd_approve(ns(session=sid, by="user"), store=home_store)
    assert d.ok is False
    assert d.node == Node.PLAN_READY.value  # ...but approve still refuses
    assert any("delivery" in b for b in d.data["blockers"])
