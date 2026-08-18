"""Stage 7: the pre-approval review-round budget. Counts PLAN_READY resubmission
rounds (the revise_plan self-loop) on the session, reused against the SAME
Rule-of-Three threshold as effort-replan-absolute (config.md, no new key) rather
than a fresh one. Below the threshold, and before any review has been recorded at
all, every plan_review_blockers verdict is byte-identical to before this stage; at
the threshold every blocking sub-reason collapses into one routing message naming
the two decisions only the customer can make — record an override to go ahead as it
stands, or cut scope and resubmit. The blockers list stays non-empty, so approve
remains structurally refused; the release itself is recorded (state.history, deduped
per round count) and surfaced in cmd_approve's failing Directive payload rather than
happening silently; and the override the message names must actually reach APPROVED,
since a routing whose named exits all bounce is a livelock. Approval resets the count.

Group 1 locks gates.plan_review_round_release_active / plan_review_blockers
directly (mirrors test_plan_review_gate.py's Group 1 style). Group 2 drives the
real CLI (cmd_submit_plan increment, cmd_approve surface/record/reset)."""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli, gates
from agentctl.config import Thresholds
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
    s = _subst(plan_review_rounds=3,
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert len(blockers) == 1
    assert "no further thinker review is required" in blockers[0]
    assert "round budget exhausted at round 3" in blockers[0]
    assert "override" in blockers[0] and "cut scope" in blockers[0]
    # Pinned because this clause has been wrong twice: it must not claim the override is
    # the ONLY act that opens the gate — a fresh passing review clears it at any count,
    # as test_a_recorded_pass_still_clears_regardless_of_rounds shows.
    assert "cutting scope does not by itself open this gate" in blockers[0]
    assert "nothing else" not in blockers[0]


def test_release_does_not_re_derive_that_a_review_happened(gate_on):
    """"A review happened" is carried by the count, not re-read off the records still on
    file. The two diverge whenever a review is staled by the edit that answers it, and
    re-deriving would then read three spent rounds as none — so the release fires on the
    count alone, with no review record present."""
    s = _subst(plan_review_rounds=3)
    assert gates.plan_review_round_release_active(s) is True
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert len(blockers) == 1 and "round budget exhausted" in blockers[0]


def test_release_wraps_every_blocking_sub_reason_uniformly(gate_on):
    """Whichever sub-reason a recorded review produced — a `revise` verdict here, a
    staleness blocker on the same record — collapses to the one routing message once
    the round threshold is met."""
    s = _subst(plan_review_rounds=3,
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    assert len(gates.plan_review_blockers(s, s.plan_path)) == 1
    stale = _subst(plan_review_rounds=3,
                   plan_review=PlanReview("/other.toml", "pass", "thinker", plan_sha256="ab12"))
    assert len(gates.plan_review_blockers(stale, stale.plan_path)) == 1


def test_release_never_empties_the_blockers_list(gate_on):
    """Never auto-approve: the release replaces the WORDING, not the fact that
    approve is still refused — a scope/risk question is the customer's to answer,
    so the gate must stay structurally blocking."""
    s = _subst(plan_review_rounds=5,  # well past the threshold
               plan_review=PlanReview("/plan.toml", "revise", "thinker"))
    blockers = gates.plan_review_blockers(s, s.plan_path)
    assert blockers != []
    assert "at round 5" in blockers[0]  # the live count, not a threshold-shaped literal


def test_round_release_inactive_below_threshold(gate_on):
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=0)) is False
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=2)) is False


def test_round_release_active_at_and_past_threshold(gate_on):
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=3)) is True
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=4)) is True


def test_the_threshold_comes_from_config_not_a_literal(gate_on):
    """The stage's whole claim is that it REUSES effort-replan-absolute rather than
    minting a key, so the predicate must read the row. Every other test asserts against
    the row's shipped value of 3, which a hardcoded 3 satisfies just as well."""
    retuned = Thresholds({"effort-replan-absolute": "2"})
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=2), retuned) is True
    assert gates.plan_review_round_release_active(_subst(plan_review_rounds=1), retuned) is False


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


def _record_revise(store, sid, plan):
    cli.cmd_plan_review(ns(session=sid, verdict="revise", reviewer="thinker",
                           concerns=["the migration drops a column"], note="",
                           target=plan, plan_digest=_sha256_file(plan)), store=store)


def test_each_resubmission_after_a_review_counts_one_round(store, fixtures_dir, gate_on):
    sid = "rb-resub"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    _record_revise(store, sid, plan)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 1
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 2


def test_drafting_resubmissions_before_any_review_are_not_rounds(store, fixtures_dir, gate_on):
    """A round is one turn of review-then-revise. Redrafting a plan nobody has looked
    at yet spends none of the budget — counting it would exhaust a review budget on
    rounds of review that never took place."""
    sid = "rb-draft"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    for _ in range(4):
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 0


def _to_round_budget_exhausted(store, sid, plan):
    """One whole-plan revise verdict, then 3 resubmissions of the same bytes — the
    cheapest state that sits exactly at the threshold. It is NOT the three-turn
    negotiation the budget is named for; the review-edit-resubmit shape is exercised by
    test_a_stage_scoped_review_staled_by_its_own_answer_still_spends_a_round."""
    _to_plan_ready(store, sid, plan)
    _record_revise(store, sid, plan)
    for _ in range(3):
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 3


def test_below_threshold_approve_payload_carries_no_release(store, fixtures_dir, gate_on):
    sid = "rb-none"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    _record_revise(store, sid, plan)
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

    # ...but a release at a NEW count is a new fact: the dedup is per round, so the
    # history stays countable. Keying it on the event name alone would silently drop
    # every release after the first.
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    d3 = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d3.data["plan_review_round_release"] == {"rounds": 4}
    events = [e for e in store.load(sid).history if e.get("event") == "plan_review_round_release"]
    assert [e["rounds"] for e in events] == [3, 4]


def test_the_released_directive_names_an_act_that_actually_opens_the_gate(store, fixtures_dir,
                                                                          gate_on):
    """The release is a routing, and a routing that names no reachable exit is a
    livelock, not a route. Following the directive literally must end at APPROVED —
    otherwise the user re-runs `approve`, gets this same message, and the plan gate
    loops exactly as the machinery this stage belongs to exists to prevent."""
    sid = "rb-exit"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_round_budget_exhausted(store, sid, plan)
    released = gates.plan_review_blockers(store.load(sid), plan)[0]
    assert "plan-review --verdict override" in released

    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).node == Node.PLAN_READY.value
    cli.cmd_plan_review(ns(session=sid, verdict="override", reviewer="user",
                           concerns=None, note="the residual risk is acceptable",
                           target=plan, plan_digest=None), store=store)
    assert cli.cmd_approve(ns(session=sid, by="user"), store=store).node == Node.APPROVED.value


def _retitle_stage(plan, index, text):
    from pathlib import Path
    before = _sha256_file(plan)
    lines = Path(plan).read_text().splitlines()
    seen = 0
    for i, line in enumerate(lines):
        if line.strip() == "[[stage]]":
            seen += 1
        elif seen == index and line.startswith("title = "):
            lines[i] = f'title = "{text}"'
            break
    else:
        raise AssertionError(f"no title line for stage {index}")
    Path(plan).write_text("\n".join(lines) + "\n")
    assert _sha256_file(plan) != before


def test_a_stage_scoped_review_staled_by_its_own_answer_still_spends_a_round(
        store, fixtures_dir, tmp_path, gate_on):
    """The realistic stage-scoped negotiation: review stage 2, edit stage 2 to answer it,
    resubmit — which stales that very review, so no record survives the cycle. Three such
    cycles are three spent rounds and must release. Reading "did a review happen" off the
    surviving records instead of off the count reported none of them, leaving the release
    silent in exactly the workflow the stage-scoped verdict exists to support."""
    import shutil
    sid = "rb-stage-scoped"
    plan = str(tmp_path / "p.toml")
    shutil.copy(fixtures_dir / "plan_two_stage.toml", plan)
    _to_plan_ready(store, sid, plan)
    for n in range(3):
        cli.cmd_plan_review(ns(session=sid, verdict="revise", reviewer="thinker",
                               concerns=[f"stage 2 concern {n}"], note="", target=plan,
                               plan_digest=_sha256_file(plan), scope="stage:2"), store=store)
        _retitle_stage(plan, 2, f"Add tests, revision {n}")
        cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)

    s = store.load(sid)
    assert s.plan_review_rounds == 3
    assert not s.plan_stage_reviews  # the reviews staled away, the spent rounds did not
    assert gates.plan_review_round_release_active(s) is True
    blockers = gates.plan_review_blockers(s, plan)
    assert len(blockers) == 1 and "round budget exhausted at round 3" in blockers[0]

    # A bare resubmission from THIS state — rounds already spent, every record staled —
    # is still a redraft nobody has reviewed, so it must not advance the count. Reached
    # only here: the before-any-review test cannot, its count is zero by construction.
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert store.load(sid).plan_review_rounds == 3


def test_approval_resets_the_round_count(store, fixtures_dir, gate_on):
    sid = "rb-reset"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    _record_revise(store, sid, plan)
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
