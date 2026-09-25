"""R1 (convergence-levers): once a whole-plan or stage-scoped `pass` is recorded
in the current approval cycle, a later `revise` at that scope is terminal — it
only reopens the plan-review gate when it carries a `--regression-command` the
engine actually runs (and which exits non-zero) AND a `--concern` naming a part
(`meta`/`order` or `stage:<n>`) that `plan.changed_parts` reports as changed
since the pass. Without both, the `revise` is recorded as a non-blocking note
(`plan_review_post_pass_unevidenced`), surfacing the concern verbatim and
routing to a user decision (override, or edit the plan) instead of another
thinker round. The round-release message also stops offering "run a fresh
whole-plan thinker review" once a pass stands. See `gates.plan_review_prior_pass`
/ `gates._plan_review_regression_evidence` / `gates._remedy_tag_for_concern` and
`cli.cmd_plan_review`'s post-pass-revise branch.

Imports only symbols present on both the pre-lever and post-lever trees (the
new fields/functions are referenced inside test bodies only) so this module
collects cleanly on the pre-lever commit too — the negative control the
verify_command runs requires every test below to FAIL there via AssertionError
or AttributeError, not via a collection error.
"""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

from agentctl import cli, gates
from agentctl.config import Thresholds
from agentctl.dispatch import RunResult
from agentctl.state import PlanReview, SessionState


def ns(**kw):
    return Namespace(**kw)


def _subst(**kw) -> SessionState:
    kw.setdefault("plan_path", "/plan.toml")
    return SessionState(session_id="s", task_id="t", weight_class="SUBSTANTIVE",
                        plan_verified=True, **kw)


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _to_plan_ready(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


import pytest  # noqa: E402


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


# --- 1. an unevidenced revise after a pass does not block ---------------------

def test_revise_after_pass_without_regression_evidence_does_not_block(store, fixtures_dir, gate_on):
    sid = "r1-1"
    plan = fixtures_dir / "plan_two_stage_substantive.toml"
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan), regression_command=None),
                        store=store)

    d = cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="revise",
                               reviewer="thinker", concerns=["reconsider stage 1"], note="",
                               plan_digest=None, regression_command=None),
                            store=store)

    assert d.ok is True
    assert d.data.get("plan_review_post_pass_unevidenced") is True
    assert "reconsider stage 1" in d.detail
    state = store.load(sid)
    assert state.plan_review.verdict == "pass"   # the prior pass stays authoritative
    assert gates.plan_review_blockers(state, str(plan)) == []


# --- 2. an evidenced revise (red regression command + concern naming the
#         changed part) overturns the pass and blocks -------------------------

def test_revise_after_pass_with_red_regression_command_blocks(store, fixtures_dir, tmp_path, gate_on):
    sid = "r1-2"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan), regression_command=None),
                        store=store)

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    d = cli.cmd_plan_review(
        ns(session=sid, target=None, scope=None, verdict="revise", reviewer="thinker",
           concerns=["stage:1 needs another look"], note="",
           plan_digest=None, regression_command="repro-regression"),
        store=store, runner=lambda argv: RunResult(1, stdout="", stderr="reproduced"),
    )

    assert d.data.get("plan_review_post_pass_unevidenced") is not True
    state = store.load(sid)
    assert state.plan_review.verdict == "revise"
    assert state.plan_review.regression_command == "repro-regression"
    assert state.plan_review.regression_exit == 1
    # the pass was genuinely overturned -- the gate is reopened, not still "in
    # force" behind a stale record.
    assert gates.plan_review_blockers(state, str(plan)) != []


# --- 3. a green (exit-0) regression command is refused as evidence ------------

def test_green_regression_command_is_not_evidence(store, fixtures_dir, tmp_path, gate_on):
    sid = "r1-3"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan), regression_command=None),
                        store=store)

    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    d = cli.cmd_plan_review(
        ns(session=sid, target=None, scope=None, verdict="revise", reviewer="thinker",
           concerns=["stage:1 needs another look"], note="",
           plan_digest=None, regression_command="repro-regression"),
        store=store, runner=lambda argv: RunResult(0, stdout="", stderr=""),
    )

    assert d.data.get("plan_review_post_pass_unevidenced") is True
    state = store.load(sid)
    assert state.plan_review.verdict == "pass"   # a green command never overturns the pass
    # the pass stays authoritative, but the plan's CONTENT moved (the retitled
    # fixture) independently of this revise -- the gate must not report a stale
    # pass as still "in force" (d.ok reflects the same recomputed blockers).
    assert d.ok is False
    assert gates.plan_review_blockers(state, str(plan)) != []


# --- 4. a red regression command whose concern names no changed part is
#         also refused as evidence ---------------------------------------------

def test_regression_concern_must_name_part_changed_since_pass(store, fixtures_dir, tmp_path, gate_on):
    sid = "r1-4"
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan), regression_command=None),
                        store=store)

    # stage 1 is the part that actually moved -- the concern below names none
    # of the changed parts at all, so even a genuinely red regression command
    # must not be accepted as evidence for it.
    plan.write_text((fixtures_dir / "plan_two_stage_substantive_stage1_retitled.toml").read_text())
    d = cli.cmd_plan_review(
        ns(session=sid, target=None, scope=None, verdict="revise", reviewer="thinker",
           concerns=["the tone of the plan feels off"], note="",
           plan_digest=None, regression_command="repro-regression"),
        store=store, runner=lambda argv: RunResult(1, stdout="", stderr="reproduced"),
    )

    assert d.data.get("plan_review_post_pass_unevidenced") is True
    state = store.load(sid)
    assert state.plan_review.verdict == "pass"
    # the pass stays authoritative, but the plan's CONTENT moved (the retitled
    # fixture) independently of this revise -- the gate must not report a stale
    # pass as still "in force".
    assert gates.plan_review_blockers(state, str(plan)) != []


# --- 5. round-release, once a pass has been recorded, stops offering a fresh
#         whole-plan thinker review as an exit ---------------------------------

def test_round_release_after_pass_offers_no_fresh_review_exit(gate_on):
    thr = Thresholds()
    pr = PlanReview("/plan.toml", "revise", "thinker", concerns=["still not right"])
    prior_pass = PlanReview("/plan.toml", "pass", "thinker", plan_sha256="deadbeef")
    s = _subst(plan_review=pr, plan_review_rounds=thr.effort_replan_absolute(),
               plan_review_passes={"": prior_pass})

    blockers = gates.plan_review_blockers(s, "/plan.toml")

    assert blockers
    assert "is no longer an exit" in blockers[0]
    assert "record plan-review --verdict pass — this clears the gate" not in blockers[0]


# --- 6. a concern's leading cut:/add: remedy tag is parsed and logged ---------

def test_concern_remedy_tags_are_logged(store, fixtures_dir, gate_on):
    sid = "r1-6"
    plan = fixtures_dir / "plan_two_stage_substantive.toml"
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan), regression_command=None),
                        store=store)

    cli.cmd_plan_review(
        ns(session=sid, target=None, scope=None, verdict="revise", reviewer="thinker",
           concerns=["cut: drop stage 2 entirely", "add: a rollback step"], note="",
           plan_digest=None, regression_command=None),
        store=store,
    )

    event = store.load(sid).history[-1]
    assert event["event"] == "plan_review_post_pass_unevidenced"
    assert event["remedy_cut"] == 1
    assert event["remedy_add"] == 1


# --- 7. a stage-scoped revise after a WHOLE-PLAN pass is bound by the same
#         terminal rule -- the scope fallback is not a loophole around it -----

def test_stage_scoped_revise_after_whole_plan_pass_is_terminal(store, fixtures_dir, gate_on):
    sid = "r1-7"
    plan = fixtures_dir / "plan_two_stage_substantive.toml"
    _to_plan_ready(store, sid, str(plan))
    cli.cmd_plan_review(ns(session=sid, target=None, scope=None, verdict="pass",
                           reviewer="thinker", concerns=None, note="",
                           plan_digest=_sha256_file(plan), regression_command=None),
                        store=store)

    # No pass has ever been recorded at "stage:1" itself -- only the whole-plan
    # scope ("") carries one. plan_review_prior_pass's scope-fallback must still
    # find it, so this unevidenced stage-scoped revise is bound by the same
    # terminal rule a whole-plan revise would be, not a loophole around it.
    d = cli.cmd_plan_review(ns(session=sid, target=None, scope="stage:1", verdict="revise",
                               reviewer="thinker", concerns=["stage:1 reconsider"], note="",
                               plan_digest=None, regression_command=None),
                            store=store)

    assert d.ok is True
    assert d.data.get("plan_review_post_pass_unevidenced") is True
    state = store.load(sid)
    assert state.plan_stage_reviews.get("stage:1") is None   # never overwritten
    assert gates.plan_review_blockers(state, str(plan)) == []


# --- 8. remedy tags are counted on the ORDINARY (non-post-pass) plan_review
#         event too, not just the unevidenced-post-pass one --------------------

def test_concern_remedy_tags_are_logged_on_ordinary_review(store, fixtures_dir, gate_on):
    sid = "r1-8"
    plan = fixtures_dir / "plan_two_stage_substantive.toml"
    _to_plan_ready(store, sid, str(plan))

    cli.cmd_plan_review(
        ns(session=sid, target=None, scope=None, verdict="revise", reviewer="thinker",
           concerns=["cut: drop stage 2 entirely", "add: a rollback step"], note="",
           plan_digest=None, regression_command=None),
        store=store,
    )

    event = store.load(sid).history[-1]
    assert event["event"] == "plan_review"
    assert event["remedy_cut"] == 1
    assert event["remedy_add"] == 1
