"""Regression pins for the review fixes on tier-proportional carry-forward.

`test_tier_carry_forward.py` pins what the carry delivers; this file pins what it must never
do: write a digest no reviewer gave, carry a verdict that asked for another round, let one
stage's reviewer vouch for another stage's command, owe a presentation for an edit that
changes nothing the user approved, or leave a premise stamp stale on a refused replan.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.plan import WHOLE_STAGE_ELEMENT, change_tier, stage_norm_keys
from agentctl.state import SessionState

from test_change_tier import _plan_file, _stage1
from test_disposition_invalidation_on_replan import (
    _setup_session_with_disposed_question, _write_plan,
)
from test_norm_staleness_pair_currency import SID, _fan_in, _set, env, make_env  # noqa: F401
from test_replan_autonomy_boundary import (  # noqa: F401
    _no_ambient_harness_session, action, approved_order, autonomy, cmd_plan, eng, thinker_review,
    venue,
)
from test_tier_carry_forward import (
    _open_rows, _review, _revise, _says, _two_stage, _whole_pass, ns,
)
from test_tier_carry_forward import _premise_state, _question  # noqa: F401


# --- carry never invents an attestation, never carries a revise -------------------------


def test_a_carry_never_stamps_a_digest_no_reviewer_gave(env):
    env.record_every_id()
    _whole_pass(env)

    def unattest(state: SessionState) -> None:
        for record in (state.plan_review, *state.plan_pair_reviews.values()):
            record.plan_sha256 = ""

    env.mutate_state(unattest)

    _, report = _revise(env, _set(2, method="a different construction"))

    assert report["carried"] and "unit:1" in report["records"], report
    state = env.state()
    assert state.plan_review.plan_sha256 == ""
    assert {r.plan_sha256 for r in state.plan_pair_reviews.values()} == {""}


def test_a_revise_record_is_not_carried_or_held(env):
    from agentctl.plan import review_ids

    assert env.record("unit:2", "revise").ok
    assert env.record("3-1", "revise").ok
    env.record_ok(*[rid for rid in review_ids(env.doc()) if rid not in ("unit:2", "3-1")])
    recorded = {rid: env.state().plan_pair_reviews[rid].plan_sha256 for rid in ("unit:2", "3-1")}

    _, report = _revise(env, _set(0, method="a different construction"))

    assert "unit:2" not in report["records"] and "3-1" not in report["records"], report
    assert "3-1" not in report["held"]
    state = env.state()
    for rid, sha in recorded.items():
        record = state.plan_pair_reviews[rid]
        assert record.plan_sha256 == sha and record.carried_tier is None and not record.held
    assert {"unit:2", "3-1"} <= set(_open_rows(env)), env.walk_rows()


# --- --escalate-pairs belongs to a unit review and nothing else ------------------------


@pytest.mark.parametrize("scope", [None, "stage:1", "topo:3-1"])
def test_escalate_pairs_is_refused_before_anything_is_recorded(env, scope):
    d = _review(env.store, SID, env.plan, scope=scope, escalate_pairs=True)

    assert not d.ok and "--escalate-pairs" in d.detail, d.detail
    state = env.state()
    assert state.plan_review is None and not state.plan_stage_reviews
    assert "3-1" not in state.plan_pair_reviews


# --- the first thinker verdict is per stage --------------------------------------------


def _two_cmd_plan(venue: Path, first: str, second: str) -> str:
    return cmd_plan(venue, first).replace(
        "depends_on = [1]\n", f'depends_on = [1]\nverify_command = "{second}"\n', 1)


def _scoped(eng, sid: str, plan: str, scope: str, verdict: str = "pass"):
    digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    return eng.run("plan_review", session=sid, verdict=verdict, reviewer="thinker", target=plan,
                   scope=scope, plan_digest=digest)


def _two_changed_commands(eng, venue):
    fast = approved_order(eng, _two_cmd_plan(venue, "frobnicate --fast", "grind --fast"))
    assert thinker_review(eng, "u1", fast)["ok"] is True
    eng.run("start", session="t1", task="task-t1", goal="g", done_criterion="dc")
    eng.run("classify", session="t1", architectural=True, files=5, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="t1")
    eng.run("submit_plan", session="t1", plan=fast)
    assert thinker_review(eng, "t1", fast)["ok"] is True
    slow = eng.write(_two_cmd_plan(venue, "frobnicate --slow", "grind --slow"))
    d = eng.run("submit_plan", session="t1", plan=slow)
    assert len(autonomy(d)["unresolved_changed_commands"]) == 2, autonomy(d)
    return slow


@pytest.mark.parametrize("first,second", [
    ("stage:1", "stage:2"), ("topo:unit:1", "topo:unit:2"), ("stage:1", "topo:unit:2"),
])
def test_one_stages_pass_does_not_vouch_the_other_stages_command(eng, venue, first, second):
    slow = _two_changed_commands(eng, venue)

    assert _scoped(eng, "t1", slow, first)["ok"] is True

    half = eng.open("t2", slow)
    assert autonomy(half)["thinker_pass"] is False, autonomy(half)
    assert action(half) == "await_user_approval"

    assert _scoped(eng, "t1", slow, second)["ok"] is True

    whole = eng.open("t3", slow)
    assert autonomy(whole)["thinker_pass"] is True, autonomy(whole)
    assert action(whole) == "self_approve"


def test_a_unit_pass_vouches_a_check_only_edit(eng, venue):
    fast = approved_order(eng, cmd_plan(venue))
    eng.open("u1", fast)
    eng.run("approve", session="u1", by="agent")
    eng.fail_stage("u1")
    eng.diagnose("u1", difference=False)
    assert thinker_review(eng, "u1", fast)["ok"] is True
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))

    def kind() -> str:
        from agentctl.plan import load_plan
        return cli._kind_within_boundary(eng.state("u1"), "refinement",
                                         load_plan(slow, strict=False), ledgered=True)

    assert kind() == "substantive"
    assert _scoped(eng, "u1", slow, "topo:unit:1")["ok"] is True

    assert kind() == "refinement"


# --- a tier 0-1 replan owes no replan_diff presentation -------------------------------


def test_tier_1_replan_needs_no_replan_diff_presentation(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_REPLAN_AUTHORIZATION", raising=False)
    sid = "auth-tier1"
    plan = _two_stage(tmp_path, store, monkeypatch, venued_fixtures_dir, sid)
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'cost_tier = "large"'), "edited.toml")
    state = store.load(sid)
    assert gates.replan_authorization_active(state)
    assert gates.replan_authorization_blockers(state, edited, diff_kind="refinement"), \
        "without the waiver a presentation is owed"

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert d.ok, _says(d)
    assert any(h.get("event") == "replan_authorization_waived" for h in store.load(sid).history)
    assert "replan authorization waived" in _says(d)


def test_tier_2_edit_still_owes_the_presentation(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_REPLAN_AUTHORIZATION", raising=False)
    sid = "auth-tier2"
    plan = _two_stage(tmp_path, store, monkeypatch, venued_fixtures_dir, sid)
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'method = "a different construction"'),
                        "edited.toml")
    state = store.load(sid)

    blockers = gates.replan_authorization_blockers(state, edited, diff_kind="refinement", change_tier=2)

    assert blockers and "replan-diff" in blockers[0], blockers
    assert gates.replan_authorization_blockers(
        state, edited, diff_kind="refinement", change_tier=1) == []


# --- the premise carry reaches the real state on a refused replan ---------------------


def test_refused_replan_marks_only_the_dispositions_the_edit_made_stale(
        store, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    original = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    sid = "refused-premise"
    _setup_session_with_disposed_question(store, sid, str(original))
    assert _review(store, sid, original).ok

    from agentctl.plan import load_plan
    keys = stage_norm_keys(load_plan(str(original)).stages[0])

    def legacy_stamp(state: SessionState) -> None:
        bag = state.plugins["premise"]
        bag["questions"][0]["target"] = "stage:1.result"
        bag["questions"][0]["disposed_at_key"] = keys[WHOLE_STAGE_ELEMENT]

    state = store.load(sid)
    legacy_stamp(state)
    store.save(state)
    cli.cmd_question_raise(
        ns(session=sid, id="Q2", target="stage:2.result", question="an open one?",
           own_research="", control=None), store=store)

    edited = tmp_path / "edited.toml"
    edited.write_text(original.read_text().replace("stage 1 done", "stage 1 done differently"),
                      encoding="utf-8")
    assert change_tier(load_plan(str(original)), load_plan(str(edited))).tier == 2
    _review(store, sid, edited, scope="stage:1")
    cli.cmd_declare(ns(session=sid, symptom="criterion moved", diagnosis="criterion"), store=store)
    cli.cmd_critique(ns(session=sid, similarities="same approach", differences="criterion",
                        failure_address="not_applicable"), store=store)

    d = cli.cmd_replan(ns(session=sid, plan=str(edited), normalization_waiver="one-off",
                          coverage_waiver=None), store=store)

    assert not d.ok and d.action == "close_questions", _says(d)
    question = next(q for q in store.load(sid).plugins["premise"]["questions"] if q["id"] == "Q1")
    assert not question.get("stale_note"), question
