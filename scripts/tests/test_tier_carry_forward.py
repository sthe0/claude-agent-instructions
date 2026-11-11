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
    eng.run("classify", session="t1", architectural=True, changed_lines=200, wall_clock_min=60)
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
    change, report = _revise(env, _set(0, output_artifacts=["a-new-product.py"]))

    assert change.tier == 3 and report["carried"] is False and report["records"] == []
    assert env.status("unit:1") == "stale:unit"
    assert env.status("3-1").startswith("stale:")
    assert env.status("3-2") == "current"


def test_meta_edit_still_owes_a_whole_plan_review(env):
    env.record_every_id()
    d = _review(env.store, SID, env.plan)
    assert d.ok, d.detail
    assert gates.plan_review_blockers(env.state(), str(env.plan)) == []

    change, report = _revise(env, lambda data: data["meta"].update(goal="a different goal"))

    assert change.tier == 4 and report["carried"] is False and report["records"] == []
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
    blockers = d.data["blockers"]
    assert any("thinker review is stale" in b and "re-run plan-review" in b for b in blockers), blockers
    assert store.load(sid).plan_review.plan_path == str(plan)


def test_derived_grant_growth_still_leaves_the_boundary(eng):
    approved_order(eng)
    rule = "Bash(git push origin HEAD:ticket/x)"
    d = eng.open("g2", eng.write(plan_text(stage2_extra=grant_block(rule))))
    assert action(d) == "await_user_approval"


# --- an agreed order change in the middle of a plan ------------------------------------
#
# A replan writes a new plan file, so every record bound to the old path is `missing` on
# the new one unless something re-binds it. After an agreed order change only the rows of
# settled stages (PASSED, own digest unmoved, currency hash equal) are re-bound.


def _settled_plan() -> dict:
    """1 <- 2 <- 3, and 4 relies on 1 and 3. R1 is covered by stage 4, R2 by stage 1.
    Units: base, 1-4. Pairs: 2-1, 3-2, 4-1, 4-3, base-1, base-4."""
    from test_norm_staleness_pair_currency import _stage

    return {
        "meta": {
            "task_id": "t", "goal": "G", "done_criterion": "DC",
            "order": {
                "requirements": [{"id": "R1", "text": "R1 text"}, {"id": "R2", "text": "R2 text"}],
                "coverage": {"R1": ["stage 4 verify_command"], "R2": ["stage 1 verify_command"]},
            },
        },
        "final_check": [{"command": "true", "expected_exit": 0, "label": "fc1"}],
        "stage": [
            _stage(1, depends_on=[]),
            _stage(2, depends_on=[1], supplies=[{"on": 1, "element": "method"}]),
            _stage(3, depends_on=[2], supplies=[{"on": 2, "element": "method"}]),
            _stage(4, depends_on=[1, 3], supplies=[{"on": 1, "element": "method"},
                                                   {"on": 3, "element": "method"}]),
        ],
    }


#: What the default order change carries on `_settled_plan` with stages 1-2 PASSED.
SETTLED_ROWS = ["2-1", "base-1", "unit:1", "unit:2"]


def _order_change(data) -> None:
    data["meta"].update(goal="G, after the order change", done_criterion="DC, after the order change")
    data["meta"]["order"]["requirements"][0]["text"] = "R1 text, restated"


def _requirement(index: int, text: str):
    return lambda data: data["meta"]["order"]["requirements"][index].update(text=text)


def _both(*mutations):
    def apply(data):
        for mutate in mutations:
            mutate(data)
    return apply


class _OrderChange:
    """The old version recorded in full on `env`, the edited version at its own path."""

    def __init__(self, env, new_path, report, state):
        self.env, self.path, self.report, self.state = env, new_path, report, state
        self.doc = load_plan(new_path)

    def status(self, rid: str) -> str:
        return gates.pair_status(self.state, self.doc, str(self.path), rid)

    def satisfied(self) -> list[str]:
        from agentctl.plan import review_ids

        return sorted(r for r in review_ids(self.doc) if self.status(r) in gates.PAIR_SATISFIED)


def _passed(state, doc, indices) -> None:
    from agentctl.state import StageStatus

    state.stages = load_plan(doc).stages
    for stage in state.stages:
        if stage.index in indices:
            stage.outcome.status = StageStatus.PASSED.value


def _agree(state, doc) -> None:
    from agentctl.plan import understanding_parts
    from agentctl.state import OrderAgreement

    state.order_agreement_armed = True
    state.order_agreement = OrderAgreement(
        understanding_sha256="u" * 64, parts=understanding_parts(doc), draft_path="", at=0.0)


def _write_version(env, mutate, name: str) -> Path:
    """`env`'s plan edited by `mutate`, written to `name`; the trailing comment keeps its
    bytes distinct from the old version's even when `mutate` moves nothing."""
    from test_norm_staleness_pair_currency import _write

    data = json.loads(json.dumps(env._data))
    mutate(data)
    path = _write(Path(env.plan).with_name(name), data)
    path.write_text(path.read_text() + f"# {name}\n")
    return path


def _change_order(env, mutate=_order_change, *, passed=(1, 2), agreement="new", seed=None,
                  name="plan.r2.toml", change=None, record=True, reopened=frozenset()):
    """Record every row of `env`'s plan (unless `record` is False), write the edited plan
    to `name`, mark `passed` PASSED, arm the agreement on the new understanding
    (`agreement`: new / old / none / unarmed) and carry with `reopened` reopened.
    `seed(state)` edits the state before the carry; `change` replaces the computed
    `PlanChange`."""
    from agentctl import carry_forward

    if record:
        env.record_every_id()
    old_path = Path(env.plan)
    new_path = _write_version(env, mutate, name)
    old, new = load_plan(old_path), load_plan(new_path)
    state = env.state()
    _passed(state, old_path, passed)
    if agreement != "none":
        _agree(state, new if agreement != "old" else old)
    if agreement == "unarmed":
        state.order_agreement_armed = False
    if seed is not None:
        seed(state)
    report = carry_forward.apply(
        state, old, new, new_path=str(new_path), new_sha=_sha(new_path),
        change=change or change_tier(old, new), old_path=str(old_path), old_sha=_sha(old_path),
        reopened=reopened)
    return _OrderChange(env, new_path, report, state)


@pytest.fixture
def order_env(make_env):
    return make_env(_settled_plan())


def test_settled_rows_carry_across_an_agreed_order_change(order_env):
    result = _change_order(order_env)

    assert change_tier(order_env.doc(), result.doc).tier == 4
    assert result.satisfied() == SETTLED_ROWS
    assert result.report["settled"] == SETTLED_ROWS
    assert result.report["carried"] is False and result.report["records"] == []
    record = result.state.plan_pair_reviews["2-1"]
    assert record.plan_path == str(result.path) and record.carried_tier == 4
    assert record.carried_from_sha == _sha(order_env.plan)


def test_a_settled_stage_unit_carries_and_a_pending_one_stays_owed(order_env):
    result = _change_order(order_env)

    assert result.status("unit:1") == "current" and result.status("unit:2") == "current"
    assert result.status("unit:3") == "missing" and result.status("unit:4") == "missing"


def test_a_pair_with_a_pending_stage_stays_owed(order_env):
    from agentctl.plan import pair_currency_hash

    result = _change_order(order_env)

    assert pair_currency_hash(order_env.doc(), "3-2") == pair_currency_hash(result.doc, "3-2")
    assert result.status("3-2") == "missing", "stage 3 is pending, so the pair is not settled"
    assert result.status("4-1") == "missing" and result.status("4-3") == "missing"


def test_a_pair_with_a_passed_but_moved_stage_stays_owed(order_env):
    from agentctl.plan import pair_currency_hash

    result = _change_order(order_env, _both(_order_change, _set(1, title="Stage 2, retitled")))

    assert pair_currency_hash(order_env.doc(), "2-1") == pair_currency_hash(result.doc, "2-1"), \
        "the pair's own content is equal, so only the stage digest keeps it owed"
    assert result.status("2-1") == "missing" and result.status("unit:2") == "missing"
    assert result.satisfied() == ["base-1", "unit:1"]


def test_a_changed_stage_set_carries_nothing(order_env):
    from test_norm_staleness_pair_currency import _stage

    result = _change_order(order_env, _both(_order_change, lambda d: d["stage"].append(_stage(5))))

    assert "stages" in change_tier(order_env.doc(), result.doc).meta
    assert result.satisfied() == [] and result.report["settled"] == []


def _move_final_check(data) -> None:
    data["final_check"][0]["command"] = "true --moved"


def _move_repo_root(data) -> None:
    data["meta"]["repo_root"] = "/elsewhere"


def _move_assumptions(data) -> None:
    data["meta"]["assumption"] = [{"id": "A1", "text": "t", "basis": "b", "risk": "r"}]


@pytest.mark.parametrize("move,field", [
    (_move_final_check, "final_check"), (_move_repo_root, "repo_root"),
    (_move_assumptions, "assumptions"),
])
def test_non_order_meta_move_carries_nothing(order_env, move, field):
    result = _change_order(order_env, _both(_order_change, move))

    assert field in change_tier(order_env.doc(), result.doc).meta
    assert result.satisfied() == [] and result.report["settled"] == []


@pytest.mark.parametrize("move", [
    lambda d: d["meta"].update(goal="another goal"),
    lambda d: d["meta"].update(done_criterion="another done criterion"),
    _requirement(0, "R1 text, restated"),
], ids=["goal", "done_criterion", "order"])
def test_order_change_meta_set_is_goal_done_order(order_env, move):
    from agentctl import carry_forward

    assert carry_forward.ORDER_CHANGE_META == {"goal", "done_criterion", "order"}
    result = _change_order(order_env, move)

    assert set(change_tier(order_env.doc(), result.doc).meta) <= carry_forward.ORDER_CHANGE_META
    assert result.satisfied() == SETTLED_ROWS


@pytest.mark.parametrize("agreement", ["none", "old", "unarmed"])
def test_no_standing_agreement_carries_nothing(order_env, agreement):
    result = _change_order(order_env, agreement=agreement)

    assert result.satisfied() == [] and result.report["settled"] == []


def test_an_open_challenge_to_the_agreement_carries_nothing(order_env):
    from agentctl.state import UnderstandingChallenge

    def challenge(state):
        state.order_challenges = [UnderstandingChallenge(
            id="C1", text="the order is wrong", reviewer="thinker", plan_sha256="",
            understanding_sha256="u" * 64, at=0.0)]

    result = _change_order(order_env, seed=challenge)

    assert result.satisfied() == []


def test_unit_base_never_carries_even_when_its_content_is_equal(order_env):
    from agentctl.plan import PlanChange, unit_currency_hash

    result = _change_order(order_env, lambda d: None, change=PlanChange(4, {}, ("goal",)))

    assert unit_currency_hash(order_env.doc(), "unit:base") == unit_currency_hash(result.doc, "unit:base")
    assert result.status("unit:base") == "missing"
    assert result.satisfied() == SETTLED_ROWS


def test_whole_plan_stage_scoped_pass_and_premise_records_are_not_carried(order_env):
    order_env.record_every_id()
    assert _review(order_env.store, SID, order_env.plan).ok
    assert _review(order_env.store, SID, order_env.plan, scope="stage:1").ok
    question = _question("Q1", "stage:1.method", "k" * 64)

    def seed(state):
        state.plugins["premise"] = {"questions": [dict(question)], "order_elements": []}

    result = _change_order(order_env, seed=seed, record=False)

    state = result.state
    assert state.plan_review is not None and state.plan_review.plan_path == str(order_env.plan)
    assert all(r.plan_path == str(order_env.plan) for r in state.plan_stage_reviews.values())
    assert all(r.plan_path == str(order_env.plan) for r in state.plan_review_passes.values())
    assert state.plugins["premise"]["questions"] == [question]
    assert result.report["records"] == [] and result.satisfied() == SETTLED_ROWS
    assert gates.plan_review_blockers(state, str(result.path)), "the plan's own review is owed"


def test_a_revise_record_is_not_revived(order_env):
    def revise(state):
        state.plan_pair_reviews["2-1"].verdict = "revise"

    result = _change_order(order_env, seed=revise)

    assert result.state.plan_pair_reviews["2-1"].plan_path == str(order_env.plan)
    assert result.status("2-1") == "missing"
    assert "2-1" not in result.report["settled"]


def test_an_override_record_of_a_settled_pair_carries(order_env):
    def override(state):
        state.plan_pair_reviews["2-1"].verdict = "override"

    result = _change_order(order_env, seed=override)

    assert result.status("2-1") == "override"
    assert "2-1" in result.report["settled"]


def test_a_record_stale_on_the_old_version_is_not_revived(order_env):
    """unit:1 and 2-1 attest an earlier stage-1 text, so they are stale on the old version:
    stage 1 being settled does not make them current on the new one."""
    from agentctl.plan import pair_content, unit_currency_hash

    earlier = load_plan(_write_version(order_env, _set(0, method="an earlier method"), "plan.r0.toml"))

    def stale(state):
        state.plan_pair_reviews["unit:1"].unit_norm = unit_currency_hash(earlier, "unit:1")
        record = state.plan_pair_reviews["2-1"]
        for key, value in pair_content(earlier, "2-1").items():
            setattr(record, key, value)

    result = _change_order(order_env, seed=stale)

    assert result.status("unit:1") == "missing" and result.status("2-1") == "missing"
    assert result.state.plan_pair_reviews["unit:1"].plan_path == str(order_env.plan)
    assert result.report["settled"] == ["base-1", "unit:2"]


def test_a_record_already_bound_to_the_new_version_is_not_rebound(order_env):
    import dataclasses

    new_path = Path(order_env.plan).with_name("plan.r2.toml")
    bound_as = {}

    def bind_to_new(state):
        record = state.plan_pair_reviews["2-1"]
        record.plan_path, record.plan_sha256 = str(new_path), _sha(new_path)
        bound_as.update(dataclasses.asdict(record))

    result = _change_order(order_env, seed=bind_to_new)

    assert "2-1" not in result.report["settled"]
    assert dataclasses.asdict(result.state.plan_pair_reviews["2-1"]) == bound_as


def test_a_reopened_stage_is_not_settled(order_env):
    result = _change_order(order_env, reopened={2})

    assert result.status("unit:2") == "missing" and result.status("2-1") == "missing"
    assert result.satisfied() == ["base-1", "unit:1"]


def test_the_replan_reopens_the_passed_dependents_of_its_roots(order_env):
    from agentctl.cli_replan import _reopened_stages

    state = order_env.state()
    _passed(state, order_env.plan, (1, 2))
    stages = load_plan(order_env.plan).stages

    assert _reopened_stages(state, stages, [1]) == {1, 2}
    assert _reopened_stages(state, stages, [2]) == {2}
    assert _reopened_stages(state, stages, []) == set()


def test_a_settled_row_whose_content_hash_moved_stays_owed(order_env):
    """A pending stage's edit adds an inbound edge on settled stage 1, and the order change
    restates R2, the requirement covering stage 1: neither stage digest moves, the unit's
    and the base pair's currency do."""
    def inbound(data):
        data["stage"][2]["supplies"].append({"on": 1, "element": "method"})
        data["stage"][2]["depends_on"] = [1, 2]

    result = _change_order(order_env, _both(_order_change, inbound, _requirement(1, "R2, restated")))

    assert result.status("unit:1") == "missing" and result.status("base-1") == "missing"
    assert result.satisfied() == ["2-1", "unit:2"]


def test_base_pair_of_a_settled_stage_follows_its_covering_requirement(order_env):
    moved = _change_order(order_env, _both(_order_change, _requirement(1, "R2, restated")))
    assert moved.status("base-1") == "missing"

    goal_only = _change_order(order_env, lambda d: d["meta"].update(goal="another goal"),
                              name="plan.r3.toml", record=False)
    assert goal_only.status("base-1") == "current"


def test_requirement_text_does_not_decide_stage_stage_pairs(order_env):
    first = _change_order(order_env, _both(_order_change, _requirement(0, "R1, one wording")))
    second = _change_order(order_env, _both(_order_change, _requirement(0, "R1, quite another")),
                           name="plan.r3.toml", record=False)

    stage_pairs = lambda result: [r for r in result.report["settled"] if r[0].isdigit()]
    assert stage_pairs(first) == stage_pairs(second) == ["2-1"]


def test_a_pending_pair_bundle_shows_the_new_order(order_env, tmp_path):
    from agentctl.render import render_pair_review_bundle

    result = _change_order(order_env)

    bundle = render_pair_review_bundle(result.doc, "3-2", plan_sha256=_sha(result.path),
                                       view_dir=tmp_path / "view")
    assert "## Order context" in bundle and "R1 text, restated" in bundle


def _topo_record(store, sid, target, rid):
    return cli.cmd_plan_review(
        ns(session=sid, target=str(target), scope=f"topo:{rid}", verdict="pass",
           reviewer="thinker", concerns=None, note="", plan_digest=_sha(Path(target)),
           regression_command=None),
        store=store)


def _walk_status(store, sid, target) -> dict[str, str]:
    d = cli.cmd_plan_review_walk(ns(session=sid, target=str(target), format="json"), store=store)
    assert d.ok, d.detail
    return {row["pair"]: row["status"] for level in d.data["levels"] for row in level}


def _order_changed_session(store, sid, venued_fixtures_dir, tmp_path, monkeypatch,
                           independent=False):
    """A session whose stage 1 PASSED with every row of the two-stage plan reviewed, and an
    agreed goal-only order change of that plan, not yet applied. Returns both plan paths.
    `independent` drops stage 2's reliance on stage 1, so the plan has unit rows only."""
    from agentctl.plan import review_ids
    from test_change_tier import _passed_session

    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    plan = venued_fixtures_dir / "plan_two_stage.toml"
    if independent:
        text = plan.read_text()
        assert text.count("depends_on = [1]") == 1, "fixture no longer names stage 2's reliance"
        plan.write_text(text.replace("depends_on = [1]", "depends_on = []"))
        assert not [r for r in review_ids(load_plan(plan)) if not r.startswith("unit:")]
    _passed_session(store, sid, str(plan), stages=1)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    for rid in review_ids(load_plan(plan)):
        assert _topo_record(store, sid, plan, rid).ok, rid
    text = plan.read_text()
    goal = 'goal = "Demonstrate the full two-stage coordination cycle"'
    assert text.count(goal) == 1, "fixture no longer states its goal as expected"
    edited = Path(_plan_file(tmp_path, text.replace(goal, 'goal = "Demonstrate it, restated"'),
                             "order-changed.toml"))
    assert set(change_tier(load_plan(plan), load_plan(edited)).meta) == {"goal"}
    state = store.load(sid)
    _agree(state, load_plan(edited))
    store.save(state)
    return plan, edited


def test_an_agreed_order_change_replan_owes_only_the_pending_rows(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    """The whole review loop across an agreed order change after stage 1 PASSED: the
    replan is refused for review, walk and compose read the rows the gate will count
    (stage 1's carried, the rest owed), the owed rows alone are recorded, compose passes,
    and the replan is accepted with the settled rows re-bound in the real state."""
    sid = "order-change"
    plan, edited = _order_changed_session(store, sid, venued_fixtures_dir, tmp_path, monkeypatch)
    state = store.load(sid)
    state.plan_review_rounds = 3
    store.save(state)

    refused = cli.cmd_replan(ns(session=sid, plan=str(edited)), store=store)
    assert not refused.ok and "review" in _says(refused), _says(refused)

    owed = ["2-1", "unit:2", "unit:base"]
    walked = _walk_status(store, sid, edited)
    assert walked["unit:1"] == "current"
    assert sorted(r for r, s in walked.items() if s not in gates.PAIR_SATISFIED) == owed
    assert store.load(sid).plan_pair_reviews["unit:1"].plan_path == str(plan), \
        "walk reads the carried view and writes nothing"
    for rid in owed:
        assert _topo_record(store, sid, edited, rid).ok, rid

    composed = cli.cmd_plan_review_compose(ns(session=sid, target=str(edited)), store=store)
    assert composed.ok, composed.detail
    assert store.load(sid).plan_review_rounds == 4, "a composed whole-plan review is a round"

    accepted = cli.cmd_replan(ns(session=sid, plan=str(edited)), store=store)
    assert accepted.ok, _says(accepted)
    state = store.load(sid)
    assert state.plan_pair_reviews["unit:1"].plan_path == str(edited)
    assert state.plan_pair_reviews["unit:1"].carried_tier == 4
    assert state.plan_review_rounds == 0 and state.plan_review_passes == {}, \
        "a rows-only carry is no carried review: the replan resets the round count"
    carried = [e for e in state.history if e.get("event") == "review_rows_carried"]
    assert carried and carried[-1]["rows"] == ["unit:1"]


@pytest.mark.parametrize("independent", [False, True], ids=["with-pairs", "units-only"])
def test_a_reopening_replan_owes_the_reopened_stages_rows_after_compose(
        store, venued_fixtures_dir, tmp_path, monkeypatch, independent):
    """Walk and compose read stage 1's carried rows as current, not knowing a later
    `--reopen 1` re-arms it; the replan gate still owes those rows a fresh verdict, also
    for a plan whose rows are all unit rows (no pair record ever binds to it)."""
    from agentctl import carry_forward
    from agentctl.plan import review_ids

    sid = "order-change-reopen"
    _, edited = _order_changed_session(store, sid, venued_fixtures_dir, tmp_path, monkeypatch,
                                       independent=independent)
    walked = _walk_status(store, sid, edited)
    assert walked["unit:1"] == "current"
    for rid, status in walked.items():
        if status not in gates.PAIR_SATISFIED:
            assert _topo_record(store, sid, edited, rid).ok, rid
    composed = cli.cmd_plan_review_compose(ns(session=sid, target=str(edited)), store=store)
    assert composed.ok, composed.detail

    refused = cli.cmd_replan(ns(session=sid, plan=str(edited), reopen=[1]), store=store)
    assert not refused.ok and "unit:1" in _says(refused), _says(refused)

    covering = [r for r in review_ids(load_plan(edited)) if 1 in carry_forward.covered_stages(r)]
    assert "unit:1" in covering
    for rid in covering:
        assert _topo_record(store, sid, edited, rid).ok, rid
    accepted = cli.cmd_replan(ns(session=sid, plan=str(edited), reopen=[1]), store=store)
    assert accepted.ok, _says(accepted)


def test_a_revise_of_a_carried_current_row_needs_regression_evidence(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    sid = "order-change-revise"
    _, edited = _order_changed_session(store, sid, venued_fixtures_dir, tmp_path, monkeypatch)
    assert _walk_status(store, sid, edited)["unit:1"] == "current"

    refused = cli.cmd_plan_review(
        ns(session=sid, target=str(edited), scope="topo:unit:1", verdict="revise",
           reviewer="thinker", concerns=["blocking: C1: regressed"], note="",
           plan_digest=_sha(edited), regression_command=None),
        store=store)

    assert not refused.ok and "--regression-command" in refused.detail, refused.detail


def test_a_chained_order_change_keeps_the_first_version(order_env):
    from agentctl import carry_forward

    first = _change_order(order_env)
    third = _write_version(
        order_env, _both(_order_change, lambda d: d["meta"].update(goal="G, changed twice")),
        "plan.r3.toml")
    state = first.state
    _agree(state, load_plan(third))
    report = carry_forward.apply(
        state, first.doc, load_plan(third), new_path=str(third), new_sha=_sha(third),
        change=change_tier(first.doc, load_plan(third)), old_path=str(first.path),
        old_sha=_sha(first.path))

    assert report["settled"] == SETTLED_ROWS
    assert state.plan_pair_reviews["2-1"].carried_from_sha == _sha(order_env.plan)
    assert gates.pair_status(state, load_plan(third), str(third), "2-1") == "current"


def _dashboard_shaped(data) -> None:
    """The shape of the real r24 -> r25 edit: the goal and done criterion restated, one
    requirement's text and its coverage entry moved, the final checks untouched."""
    _order_change(data)
    data["meta"]["order"]["coverage"]["R1"] = ["stage 4 verify_command", "stage 4 done_criterion"]


def test_dashboard_shaped_order_change_carries(order_env):
    result = _change_order(order_env, _dashboard_shaped)

    assert set(change_tier(order_env.doc(), result.doc).meta) == {"goal", "done_criterion", "order"}
    assert result.satisfied() == SETTLED_ROWS

    with_check = _change_order(order_env, _both(_dashboard_shaped, _move_final_check),
                               name="plan.r3.toml", record=False)
    assert with_check.satisfied() == []


def test_the_seam_returns_the_carried_copy_for_a_rows_only_carry(order_env):
    from agentctl import carry_forward
    from agentctl.cli_common import _carry_records

    order_env.record_every_id()
    new_path = _write_version(order_env, _order_change, "plan.r2.toml")
    state = order_env.state()
    _passed(state, order_env.plan, (1, 2))
    _agree(state, load_plan(new_path))

    carried, report, _ = _carry_records(order_env.store, state, str(new_path), in_place=False)

    assert carried is not state and report["carried"] is False
    assert report["settled"] == SETTLED_ROWS
    assert carried.plan_pair_reviews["2-1"].plan_path == str(new_path)
    assert state.plan_pair_reviews["2-1"].plan_path == str(order_env.plan)
    line = carry_forward.describe(report)
    assert "settled stages" in line and "2-1" in line
