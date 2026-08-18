"""Stage 7: the pre-approval review-round budget. Counts PLAN_READY resubmission
rounds (the revise_plan self-loop) on the session, reused against the SAME
Rule-of-Three threshold as effort-replan-absolute (config.md, no new key) rather
than a fresh one. Below the threshold every plan_review_blockers verdict is
byte-identical to before this stage; at the threshold every blocking sub-reason
collapses into one routing message naming the two decisions only the customer can
make (approve with the recorded accepted risks, or cut scope) — the blockers list
stays non-empty, so approve remains structurally refused, and the release itself is
recorded (state.history, deduped per round count) and surfaced in cmd_approve's
failing Directive payload rather than happening silently. Approval resets the count.

Group 1 locks gates.plan_review_round_release_active / plan_review_blockers
directly (mirrors test_plan_review_gate.py's Group 1 style). Group 2 drives the
real CLI (cmd_submit_plan increment, cmd_approve surface/record/reset)."""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli, gates
from agentctl.state import Node, PlanReview, SessionState


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


def _subst(**kw) -> SessionState:
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_path="/plan.toml", plan_verified=True, **kw)


# --- 1. gates.py: the threshold predicate and the wrap -------------------------

def test_below_threshold_review_still_required(gate_on):
    s = _subst(plan_review_rounds=2)  # threshold (effort-replan-absolute) is 3
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert blockers and "no thinker review" in blockers[0]


def test_at_threshold_requirement_released_with_recorded_reason(gate_on):
    s = _subst(plan_review_rounds=3)
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert len(blockers) == 1
    assert "released" in blockers[0]
    assert "round 3" in blockers[0] or "round budget exhausted at round 3" in blockers[0]
    assert "approve" in blockers[0] and "cut scope" in blockers[0]


def test_release_wraps_every_blocking_sub_reason_uniformly(gate_on):
    """Not just the no-review case: a `revise` verdict's blockers collapse to the
    same single routing message once the round threshold is met."""
    s = _subst(plan_review_rounds=3,
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert len(blockers) == 1
    assert "released" in blockers[0]


def test_release_never_empties_the_blockers_list(gate_on):
    """Never auto-approve: the release replaces the WORDING, not the fact that
    approve is still refused — a scope/risk question is the customer's to answer,
    so the gate must stay structurally blocking."""
    s = _subst(plan_review_rounds=5)  # well past the threshold
    assert gates.plan_review_blockers(s, s.plan_path) != []


def test_round_release_inactive_below_threshold(gate_on):
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=0)) is False
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=2)) is False


def test_round_release_active_at_and_past_threshold(gate_on):
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=3)) is True
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=4)) is True


def test_a_recorded_pass_still_clears_regardless_of_rounds(gate_on):
    """The round count only matters while there IS a blocker; a clean pass at any
    round count clears exactly as before."""
    s = _subst(plan_review_rounds=3,
               plan_review=PlanReview("/plan.toml", "pass", "thinker", plan_sha256="ab12"))
    assert gates.plan_review_blockers(s, "/plan.toml") == []


# --- 2. cmd_submit_plan / cmd_approve: counting, reset, and the surfaced payload -

def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def test_first_submission_does_not_count_as_a_round(store, fixtures_dir, gate_on):
    sid = "rb-first"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    assert store.load(sid).plan_review_rounds == 0


def test_each_resubmission_at_plan_ready_counts_one_round(store, fixtures_dir, gate_on):
    sid = "rb-resub"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 1
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 2


def _to_round_budget_exhausted(store, sid, plan):
    """3 resubmissions past the initial PLAN_READY submission — plan_review_rounds
    lands exactly at the threshold, no review ever recorded."""
    _to_plan_ready(store, sid, plan)
    for _ in range(3):
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 3


def test_below_threshold_approve_payload_carries_no_release(store, fixtures_dir, gate_on):
    sid = "rb-none"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)  # rounds=1, still < 3
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.PLAN_READY.value
    assert d.data["plan_review_round_release"] is None
    assert not any(e.get("event") == "plan_review_round_release" for e in store.load(sid).history)


def test_release_present_in_surfaced_payload_and_recorded(store, fixtures_dir, gate_on):
    sid = "rb-release"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_round_budget_exhausted(store, sid, plan)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.PLAN_READY.value  # still refused — never auto-approves
    assert d.data["plan_review_round_release"] == {"rounds": 3}
    events = [e for e in store.load(sid).history if e.get("event") == "plan_review_round_release"]
    assert len(events) == 1
    assert events[0]["rounds"] == 3
    # a second blocked approve at the SAME round count must not duplicate the record
    d2 = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d2.data["plan_review_round_release"] == {"rounds": 3}
    events = [e for e in store.load(sid).history if e.get("event") == "plan_review_round_release"]
    assert len(events) == 1


def test_approval_resets_the_round_count(store, fixtures_dir, gate_on):
    sid = "rb-reset"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)  # rounds=1
    assert store.load(sid).plan_review_rounds == 1
    cli.cmd_plan_review(ns(session=sid, verdict="pass", reviewer="thinker",
                           concerns=None, note="", target=None,
                           plan_digest=_sha256_file(plan)), store=store)
    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value
    assert store.load(sid).plan_review_rounds == 0


def _sha256_file(p) -> str:
    import hashlib
    from pathlib import Path
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()
