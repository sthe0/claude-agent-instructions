"""Tier-proportional review owed, and carry-forward of review, premise and order records.

A replan or a PLAN_READY re-submission replaces the plan version every review record is
bound to. How much of that review is owed again follows the change tier of the edit
(`plan.change_tier`): a tier 0-1 edit (bookkeeping, final venue) cannot change what any
reviewer judged, so every record carries onto the new version; a tier 2 edit (check-only
or operative) owes the edited stage its own review while the pairs touching it are held;
a tier 3-4 edit (interface, meta) and an edit of unknown tier leave every record exactly
as stale as before.

`agentctl.carry_forward` is reached inside the test bodies, so a test fails on a tree
that lacks it instead of failing the whole module at collection.
"""
from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.plan import change_tier, pair_binding, stage_norm_keys
from agentctl.plan import load_plan
from agentctl.state import PlanPairReview, SessionState
from conftest import STAGE_OBSERVATIONS  # noqa: F401  (keeps the shared fixtures importable)

from test_change_tier import _plan_file, _stage1, _to_executing_stage1
from test_norm_staleness_pair_currency import (  # noqa: F401
    SID, _change_render_code, _covered_stage_one, _fan_in, _set, _sha, env, make_env,
)
from test_replan_autonomy_boundary import (  # noqa: F401
    _no_ambient_harness_session, action, approved_order, autonomy, cmd_plan, eng, grant_block,
    plan_text, thinker_review, venue,
)


@pytest.fixture(autouse=True)
def _review_on_authorization_off(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def ns(**kw):
    return Namespace(**kw)


def _says(directive) -> str:
    return f"{directive.detail} {json.dumps(directive.data, default=str)}"


def _review(store, sid, target, *, scope=None, escalate_pairs=False):
    return cli.cmd_plan_review(
        ns(session=sid, target=str(target), scope=scope, verdict="pass", reviewer="thinker",
           concerns=None, note="", plan_digest=hashlib.sha256(Path(target).read_bytes()).hexdigest(),
           regression_command=None, escalate_pairs=escalate_pairs),
        store=store)


# --- the carry primitive at the gate level ---------------------------------------------


def _whole_pass(env) -> None:
    d = _review(env.store, SID, env.plan)
    assert d.ok, d.detail


def _revise(env, mutate):
    """Edit the plan file in place and carry the session's records onto the new bytes,
    the way an applied replan does. Returns (change, report)."""
    from agentctl import carry_forward

    old = env.doc()
    env.edit(mutate)
    new = env.doc()
    change = change_tier(old, new)
    state = env.state()
    report = carry_forward.apply(state, old, new, new_path=str(env.plan),
                                 new_sha=_sha(env.plan), change=change)
    env.store.save(state)
    return change, report


def _open_rows(env) -> list[str]:
    return [r["pair"] for r in env.walk_rows() if r["status"] not in gates.PAIR_SATISFIED]


def test_stage_local_edit_owes_only_its_unit(env):
    env.record_every_id()
    _whole_pass(env)
    assert gates.plan_review_blockers(env.state(), str(env.plan)) == []

    change, report = _revise(env, _set(2, method="a different construction"))

    assert change.tier == 2
    assert _open_rows(env) == ["unit:3"], env.walk_rows()
    held = {r["pair"] for r in env.walk_rows() if r.get("held")}
    assert held == {"3-1", "3-2"}
    assert env.status("unit:1") == "current" and env.status("unit:2") == "current"
    assert gates.plan_review_blockers(env.state(), str(env.plan)), "unit:3 is still owed"

    env.record_ok("unit:3")
    assert gates.plan_review_blockers(env.state(), str(env.plan)) == []
    assert report["tier"] == 2


def test_unit_reviewer_can_escalate_to_the_pairs(env):
    env.record_every_id()
    _whole_pass(env)
    _revise(env, _set(2, method="a different construction"))

    d = _review(env.store, SID, env.plan, scope="topo:unit:3", escalate_pairs=True)

    assert d.ok, d.detail
    assert set(_open_rows(env)) >= {"3-1", "3-2"}, env.walk_rows()
    assert not [r for r in env.walk_rows() if r.get("held")]


def test_legacy_pair_record_stays_current_while_plan_content_is_unchanged(env, monkeypatch):
    doc = env.doc()
    legacy = PlanPairReview(
        pair="3-1", base="3", service="1", verdict="pass", reviewer="thinker",
        plan_path=str(env.plan), plan_sha256=_sha(env.plan), **pair_binding(doc, "3-1"))
    env.mutate_state(lambda s: s.plan_pair_reviews.update({"3-1": legacy}))
    assert env.status("3-1") == "current"

    _change_render_code(monkeypatch)

    assert env.status("3-1") == "current", "render code moved, plan content did not"

    from agentctl import carry_forward

    old, old_sha = env.doc(), _sha(env.plan)
    env.edit(_set(1, method="an unrelated stage's method"))
    state = env.state()
    carry_forward.apply(state, old, env.doc(), new_path=str(env.plan), new_sha=_sha(env.plan),
                        change=change_tier(old, env.doc()), old_sha=old_sha)
    env.store.save(state)
    assert env.status("3-1") == "current", "stages 3 and 1 are unchanged, so is the legacy record"


def _premise_state(questions=(), order_elements=()) -> SessionState:
    return SessionState(session_id="p", task_id="t", weight_class="SUBSTANTIVE",
                        plugins={"premise": {"questions": list(questions),
                                             "order_elements": list(order_elements)}})


def _question(qid, target, key) -> dict:
    return {"id": qid, "target": target, "question": "q?", "own_research": "r",
            "disposition": "assumed", "basis": "b", "risk": "k", "disposed_at_key": key}


def _premise_blockers(state, doc) -> list[str]:
    from agentctl import premise

    bag = state.plugins["premise"]
    keys = {s.index: stage_norm_keys(s) for s in doc.stages}
    return premise.validate_questions(premise.questions_from_dicts(bag["questions"]), stage_keys=keys)


def _order_blockers(state, doc) -> list[str]:
    from agentctl import premise

    bag = state.plugins["premise"]
    keys = {s.index: stage_norm_keys(s) for s in doc.stages}
    return premise.validate_order_elements(
        premise.order_elements_from_dicts(bag["order_elements"]),
        stage_indices={s.index for s in doc.stages}, plan_present=True, stage_keys=keys)


def _carry_premise(state, old, new):
    from agentctl import carry_forward

    carry_forward.apply(state, old, new, new_path="/new.toml", new_sha="n" * 64,
                        change=change_tier(old, new))


def test_legacy_whole_stage_premise_stamp_survives_a_method_edit(make_env):
    env = make_env()
    old = env.doc()
    keys = stage_norm_keys(old.stages[2])
    state = _premise_state(
        questions=[_question("Q1", "stage:3.criterion", _whole_key(keys)),
                   _question("Q2", "stage:3.method", keys["method"])],
        order_elements=[{"id": "O1", "element": "R1", "disposition": "covered", "stage": 3,
                         "content_digest": _whole_key(keys), "stale_note": ""}])
    assert _premise_blockers(state, old) == [] and _order_blockers(state, old) == []

    env.edit(_set(2, method="a different construction"))
    new = env.doc()
    assert len(_premise_blockers(state, new)) == 2, "base: both stamps are stale after the edit"

    _carry_premise(state, old, new)

    blockers = _premise_blockers(state, new)
    assert len(blockers) == 1 and "stage 3's method" in blockers[0], blockers
    assert _order_blockers(state, new) == []


def _whole_key(keys) -> str:
    from agentctl.plan import WHOLE_STAGE_ELEMENT

    return keys[WHOLE_STAGE_ELEMENT]


def test_blank_interface_stage_premise_survives_a_method_edit(make_env):
    from agentctl.plan import INTERFACE_ELEMENT
    from agentctl.premise import order_binding_key

    env = make_env(_covered_stage_one(blank_interface=True))
    old = env.doc()
    blank = [s for s in old.stages if stage_norm_keys(s)[INTERFACE_ELEMENT].startswith("full:")]
    assert blank, "the fixture needs a stage with no declared product"
    index = blank[-1].index
    keys = stage_norm_keys(old.stages[index - 1])
    state = _premise_state(
        questions=[_question("Q1", f"stage:{index}.method", keys["method"])],
        order_elements=[{"id": "O1", "element": "R1", "disposition": "covered", "stage": index,
                         "content_digest": order_binding_key(keys), "stale_note": ""}])
    assert _premise_blockers(state, old) == [] and _order_blockers(state, old) == []

    env.edit(_set(index - 1, method="a different construction"))
    new = env.doc()
    assert _premise_blockers(state, new) and _order_blockers(state, new)

    _carry_premise(state, old, new)

    blockers = _premise_blockers(state, new)
    assert len(blockers) == 1 and "method" in blockers[0], blockers
    assert _order_blockers(state, new) == []


# --- the seams: an applied replan and a PLAN_READY re-submission -----------------------


def _two_stage(tmp_path, store, monkeypatch, venued_fixtures_dir, sid):
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    _to_executing_stage1(store, sid, str(plan))
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    d = _review(store, sid, plan)
    assert d.ok, d.detail
    return plan


def test_cost_tier_only_replan_owes_no_review(store, venued_fixtures_dir, tmp_path, monkeypatch):
    sid = "tier1"
    plan = _two_stage(tmp_path, store, monkeypatch, venued_fixtures_dir, sid)
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'cost_tier = "large"'), "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert d.ok, _says(d)
    state = store.load(sid)
    assert state.plan_review.plan_path == edited
    assert state.plan_review.plan_sha256 == hashlib.sha256(Path(edited).read_bytes()).hexdigest()
    assert state.plan_review.carried_tier == 1
    assert gates.plan_review_blockers(state, edited) == []
    assert "carried" in _says(d) and "tier 1" in _says(d)


def test_review_passes_survive_a_tier_1_replan(store, venued_fixtures_dir, tmp_path, monkeypatch):
    sid = "tier1-passes"
    plan = _two_stage(tmp_path, store, monkeypatch, venued_fixtures_dir, sid)
    state = store.load(sid)
    state.plan_review_rounds = 2
    state.plan_review_counted_digest = "counted"
    store.save(state)
    passes_before = set(store.load(sid).plan_review_passes)
    assert passes_before
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'cost_tier = "large"'), "edited.toml")

    assert cli.cmd_replan(ns(session=sid, plan=edited), store=store).ok

    state = store.load(sid)
    assert set(state.plan_review_passes) == passes_before
    assert all(p.plan_path == edited for p in state.plan_review_passes.values())
    assert state.plan_review_rounds == 2 and state.plan_review_counted_digest == "counted"


def test_tier_1_resubmission_carries_review_records(store, venued_fixtures_dir, tmp_path, monkeypatch):
    sid = "tier1-resubmit"
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                        tracker_key=None, architectural=True, external_effect=False,
                        new_dependency=False, public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    assert cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store).ok
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    assert _review(store, sid, plan).ok
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'cost_tier = "large"'), "edited.toml")

    d = cli.cmd_submit_plan(ns(session=sid, plan=edited), store=store)

    assert d.ok, _says(d)
    state = store.load(sid)
    assert state.plan_review is not None and state.plan_review.carried_tier == 1
    assert state.plan_review_rounds == 0, "a carried review is not a counted round"
    assert gates.plan_review_blockers(state, edited) == []


# --- the autonomy boundary reads one verdict -------------------------------------------


def _scoped_stage1_pass(eng, sid, plan):
    digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    return eng.run("plan_review", session=sid, verdict="pass", reviewer="thinker", target=plan,
                   scope="stage:1", plan_digest=digest)


def test_scoped_pass_vouches_the_autonomy_boundary(eng, venue):
    fast = approved_order(eng, cmd_plan(venue))
    assert thinker_review(eng, "u1", fast)["ok"] is True
    eng.run("start", session="t1", task="task-t1", goal="g", done_criterion="dc")
    eng.run("classify", session="t1", architectural=True, files=5, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="t1")
    eng.run("submit_plan", session="t1", plan=fast)
    assert thinker_review(eng, "t1", fast)["ok"] is True
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    d = eng.run("submit_plan", session="t1", plan=slow)
    assert autonomy(d)["thinker_pass"] is False

    assert _scoped_stage1_pass(eng, "t1", slow)["ok"] is True

    later = eng.open("t2", slow)
    assert action(later) == "self_approve", autonomy(later)
    assert autonomy(later)["thinker_pass"] is True


def test_check_only_edit_vouched_by_its_unit_pass_stays_within_boundary(eng, venue):
    fast = approved_order(eng, cmd_plan(venue))
    eng.open("u1", fast)
    eng.run("approve", session="u1", by="agent")
    eng.fail_stage("u1")
    eng.diagnose("u1", difference=False)
    assert thinker_review(eng, "u1", fast)["ok"] is True
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))

    def kind() -> str:
        return cli._kind_within_boundary(eng.state("u1"), "refinement",
                                         load_plan(slow, strict=False), ledgered=True)

    assert kind() == "substantive", "no review has vouched the changed command yet"
    assert _scoped_stage1_pass(eng, "u1", slow)["ok"] is True

    assert kind() == "refinement"


# --- what must stay as it was ----------------------------------------------------------


def test_interface_edit_still_fans_out_to_pairs(env):
    env.record_every_id()
    env.edit(_set(0, output_artifacts=["a-new-product.py"]))

    assert env.status("unit:1") == "stale:unit"
    assert env.status("3-1").startswith("stale:")
    assert env.status("3-2") == "current"


def test_meta_edit_still_owes_a_whole_plan_review(env):
    env.record_every_id()
    d = _review(env.store, SID, env.plan)
    assert d.ok, d.detail
    assert gates.plan_review_blockers(env.state(), str(env.plan)) == []

    env.edit(lambda data: data["meta"].update(goal="a different goal"))

    assert gates.plan_review_blockers(env.state(), str(env.plan))
    assert gates.plan_review_delta(env.state(), env.doc())[0] is True


def test_tier_3_edit_leaves_records_as_stale_as_on_the_base(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    sid = "tier3"
    plan = _two_stage(tmp_path, store, monkeypatch, venued_fixtures_dir, sid)
    edited = _plan_file(tmp_path, _stage1(plan.read_text(), 'output_artifacts = ["other.py"]'),
                        "edited.toml")

    d = cli.cmd_replan(ns(session=sid, plan=edited), store=store)

    assert not d.ok, "an interface edit still owes its review"
    assert store.load(sid).plan_review.plan_path == str(plan)


def test_derived_grant_growth_still_leaves_the_boundary(eng):
    approved_order(eng)
    rule = "Bash(git push origin HEAD:ticket/x)"
    d = eng.open("g2", eng.write(plan_text(stage2_extra=grant_block(rule))))
    assert action(d) == "await_user_approval"
