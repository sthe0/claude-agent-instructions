"""A replan that changes the root product goes back to the user; every other change follows the
approval rule of what it changes.

The classes of a replan's diff (`gates.classify_replan`): (a) the root product (goal, done criterion,
the understanding the order agreed), (a') the executor side of a two-sided order, (b) a stage's
description inside the root order, (c) the executor's method. Only (a) is the user's, so only (a) is
refused until `order-agree` has recorded the change with its four grounds. The three over-routing
cases (description, method, executor side) are regression guards of the base tree — a replan of
those classes was never routed to the user — and each also asserts, in its own fixture, that a root
product change is refused, so a router that sends everything to the user (or nothing) fails.
"""

from __future__ import annotations

import time
from pathlib import Path
import pytest

from agentctl import cli, cli_common, cli_condition, gates, plan as plan_mod, sub_plan
from agentctl.gates import ReplanClasses, classify_replan
from agentctl.machine import Node
from agentctl.state import GateRecord, Partition
from agentctl.plan import load_order_draft, load_plan, product_digest
from agentctl.render import reagreement_lines
from conftest import SUBSTANTIVE_ORDER, declare_venue
from order_agreement_mutants import MUTANTS, mutant  # noqa: F401  (autouse fixture)
from test_meta_order import _COVERAGE, _FINAL_CHECK, _PLAN
from test_order_agreement import (  # noqa: F401  (home, ostore and armed_env are fixtures)
    SESSION, _blockers, _classify, _confirm_delivery, _marker_of, _plan, _plan_ready, _start, _user_answers,
    armed_env, home, ns, ostore,
)
from test_effort_fire_diagnosing import (  # noqa: F401  (real is a fixture)
    _marker_of as _correction_marker, _overspent_agreed_stage_in_its_cycle, real,
)
from test_order_phase import BUDGET, _LINES, _draft, _present, _review, _submit
from test_order_two_sides import _replacing
from test_stage_estimates import _user_choice
from test_stage_order_subplan import _HOW_KEPT, _RECORD_HEAD, _walk_to_submit
from test_subplan_presentation_dispatch import CHILD, PARENT, contour, home_store  # noqa: F401  (fixtures)

@pytest.fixture(autouse=True)
def _no_replan_authorization(monkeypatch):
    """The routing under test is the order's; the replan-diff presentation gate is another
    stage's and is walked by its own tests."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


GROUNDS = {"reagree_changes": "the goal", "reagree_why": "the user changed what they want",
           "reagree_earlier_plan": "none yet", "reagree_difficulty": "the first goal missed the need"}


def _edit(path: str | Path, *pairs: tuple[str, str]) -> str:
    """`path` with each (old, new) applied once; an `old` that is absent fails the test, so the
    edit cannot silently leave the file as it was."""
    text = Path(path).read_text(encoding="utf-8")
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new, 1)
    Path(path).write_text(text, encoding="utf-8")
    return str(path)


def _proposal(tmp_path: Path, *edits: tuple[str, str], name="proposal.toml", **plan_kw) -> str:
    return _edit(_plan(tmp_path, name=name, **plan_kw), *edits)


def _replan(store, proposal: str):
    return cli.cmd_replan(ns(session=SESSION, plan=proposal), store=store)


def _submit_second(store, tmp_path: Path, *edits: tuple[str, str], grounds=None, name="d2.toml",
                   draft_edits=(("goal = \"g\"", "goal = \"g2\""),)):
    """order-submit of a changed order draft with the four grounds (`grounds`: the flags given, by
    default all four). Returns (directive, draft path)."""
    path = _draft(tmp_path, name=name, **({"replace": dict(edits)} if edits else {}))
    _edit(path, *draft_edits)
    given = GROUNDS if grounds is None else grounds
    return cli.cmd_order_submit(ns(session=SESSION, plan=str(path), **given), store=store), path


def _reagree(store, tmp_path: Path, *edits: tuple[str, str], name="d2.toml",
             draft_edits=(("goal = \"g\"", "goal = \"g2\""),)) -> object:
    """The user's second agreement: a changed order draft submitted with its grounds, reviewed,
    presented (the rendering carries the grounds), answered and agreed through the real verbs.
    Returns the order-agree directive."""
    submitted, path = _submit_second(store, tmp_path, *edits, name=name, draft_edits=draft_edits)
    assert submitted.ok, submitted.detail
    assert _review(store).ok
    assert _present(store, tmp_path).ok
    _confirm_delivery(store)
    _user_answers(_marker_of(path), at=time.time() + 5)
    return cli.cmd_order_agree(ns(session=SESSION, note=""), store=store)


GOAL2 = (('goal = "g"', 'goal = "g2"'),)


def _past_routing(d) -> None:
    """The replan was not stopped by the order routing. A later gate (premises, review) may still
    stop it; that is another stage's refusal and is named by its own action."""
    assert d.action != "replan", (d.detail, d.data.get("blockers"))
    text = _blockers(d)
    assert "order-agree" not in text and "order-executor-revise" not in text, text


def _cover_order_element(store, plan: str) -> None:
    """The premise gate's order element, raised and covered by stage 1 through the real verbs."""
    cli.cmd_order_raise(ns(session=SESSION, id="O1", element="the release is one command"), store=store)
    d = cli.cmd_order_dispose(ns(session=SESSION, id="O1", as_="covered", stage=1, reason="", plan=plan),
                              store=store)
    assert d.ok, d.detail


def _approved(store, home: Path, tmp_path: Path) -> str:
    plan = _plan_ready(store, home, tmp_path)
    _cover_order_element(store, plan)
    d = cli.cmd_approve(ns(session=SESSION, by="user"), store=store)
    assert d.ok, (d.detail, d.data.get("blockers"))
    return plan


def _redispose_goal_premise(store, proposal: str) -> None:
    """The premise `BC` is bound to the goal; a changed goal makes it stale, and the user's answer
    stands against the new plan through the real verb."""
    answer = next(q["answer"] for q in store.load(SESSION).plugins["premise"]["questions"] if q["id"] == "BC")
    redisposed = cli.cmd_question_dispose(
        ns(session=SESSION, id="BC", to="escalated", answer=answer, source="", derivation="",
           basis="", risk="", plan=proposal), store=store)
    assert redisposed.ok, redisposed.detail


# --- the root product is the user's ------------------------------------------------------------


def test_root_product_replan_refused_until_reagreement(ostore, home, armed_env, tmp_path):
    """The refusal names the way on (order-submit, order-agree with the four grounds) and leaves
    the session at its plan."""
    plan = _plan_ready(ostore, home, tmp_path)

    d = _replan(ostore, _proposal(tmp_path, *GOAL2))

    assert d.ok is False and d.action == "replan", (d.action, d.detail)
    text = _blockers(d)
    assert "order-submit" in text and "order-agree" in text and "goal" in text, text
    for flag in ("--reagree-changes", "--reagree-why", "--reagree-earlier-plan", "--reagree-difficulty"):
        assert flag in text, text
    assert ostore.load(SESSION).plan_path == plan


def test_root_product_replan_passes_with_reagreement_record(ostore, home, armed_env, tmp_path):
    """Regression guard of the release node: with the record the same replan is not refused by
    the routing (it is the way on the refusal names)."""
    _approved(ostore, home, tmp_path)
    proposal = _proposal(tmp_path, *GOAL2)

    before = _replan(ostore, proposal)
    assert before.ok is False and before.action == "replan" and "order-agree" in _blockers(before)
    agreed = _reagree(ostore, tmp_path)
    assert agreed.ok, agreed.detail
    d = _replan(ostore, proposal)

    _past_routing(d)
    assert "question 'BC' is bound to plan.goal" in _blockers(d), (d.detail, _blockers(d))
    _redispose_goal_premise(ostore, proposal)
    d = _replan(ostore, proposal)
    assert d.ok, (d.detail, d.data.get("blockers"))
    assert ostore.load(SESSION).plan_path == proposal


def test_pre_approval_replan_routes_only_root_product(ostore, home, armed_env, tmp_path):
    """Before approval a replan of a stage description is accepted without a re-agreement; the
    same fixture refuses a changed root product."""
    _plan_ready(ostore, home, tmp_path)
    titled = _proposal(tmp_path, ('title = "the stage under test"', 'title = "the renamed stage"'))
    assert ostore.load(SESSION).node == "PLAN_READY"

    ok = _replan(ostore, titled)
    refused = _replan(ostore, _proposal(tmp_path, *GOAL2, name="product.toml"))

    _past_routing(ok)
    assert refused.ok is False and "order-agree" in _blockers(refused)


def test_second_draft_needs_grounds_at_order_submit(ostore, home, armed_env, tmp_path):
    """A session that already agreed an order does not put a new-form draft to the user again
    without the four grounds: order-submit refuses and stores nothing. The way on the refusal
    names (the same draft with the four flags) is then walked to a recorded agreement."""
    _plan_ready(ostore, home, tmp_path)
    first = ostore.load(SESSION).order_agreement
    draft = ostore.load(SESSION).order_draft_path

    d, path = _submit_second(ostore, tmp_path, grounds={})

    assert d.ok is False and "four grounds" in d.detail and "--reagree-changes" in d.detail, d.detail
    state = ostore.load(SESSION)
    assert state.order_agreement == first and state.order_draft_path == draft
    assert state.order_draft_reagreement == {}
    again = cli.cmd_order_submit(ns(session=SESSION, plan=str(path), **GROUNDS), store=ostore)
    assert again.ok, again.detail
    assert ostore.load(SESSION).order_draft_reagreement["changes"] == GROUNDS["reagree_changes"]


@pytest.mark.parametrize("missing", sorted(GROUNDS))
def test_reagreement_missing_ground_refused(ostore, home, armed_env, tmp_path, missing):
    _plan_ready(ostore, home, tmp_path)
    flag = "--" + missing.replace("_", "-")

    d, _ = _submit_second(ostore, tmp_path, grounds={k: v for k, v in GROUNDS.items() if k != missing})

    assert d.ok is False and flag in d.detail, d.detail
    assert ostore.load(SESSION).order_draft_reagreement == {}
    assert ostore.load(SESSION).order_agreement.reagreement is None


def test_first_agreement_is_asked_for_no_grounds(ostore, home, armed_env, tmp_path):
    """The first agreement of a session has nothing it changes: order-submit takes no grounds, the
    agreement is recorded without any, and no re-agreement record exists."""
    _start(ostore)
    assert _classify(ostore).ok
    first = _draft(tmp_path)

    submitted = cli.cmd_order_submit(ns(session=SESSION, plan=str(first)), store=ostore)

    assert submitted.ok, submitted.detail
    assert "order_reagreement_block" not in submitted.data
    assert ostore.load(SESSION).order_draft_reagreement == {}
    assert _review(ostore).ok and _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    _user_answers(_marker_of(first))
    agreed = cli.cmd_order_agree(ns(session=SESSION, note=""), store=ostore)
    assert agreed.ok, agreed.detail
    assert ostore.load(SESSION).order_agreement.reagreement is None


def test_old_form_order_is_asked_for_no_grounds(ostore, home, armed_env, tmp_path):
    """An old-form draft put again unchanged (an order agreed before the split, no customer side to
    re-agree) takes no grounds, though the session already agreed it."""
    _old_form_plan_ready(ostore, home, tmp_path)
    assert ostore.load(SESSION).order_agreement is not None
    again = cli.cmd_order_submit(
        ns(session=SESSION, plan=str(_draft(tmp_path, name="old-again.toml", omit=("customer_side",)))),
        store=ostore)
    assert "four grounds" not in again.detail, again.detail


def test_order_agree_refuses_a_second_draft_submitted_without_grounds(ostore, home, armed_env, tmp_path):
    """A draft already submitted before order-submit took the grounds (an order caught mid-flight)
    reaches order-agree with no record: it is refused, and the way on named (submit again with the
    four grounds, present the rendering that carries them) ends in a recorded agreement."""
    _plan_ready(ostore, home, tmp_path)
    submitted, path = _submit_second(ostore, tmp_path)
    assert submitted.ok and _review(ostore).ok and _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    _user_answers(_marker_of(path), at=time.time() + 5)
    state = ostore.load(SESSION)
    state.order_draft_reagreement = {}
    ostore.save(state)

    d = cli.cmd_order_agree(ns(session=SESSION, note=""), store=ostore)

    assert d.ok is False and "four grounds" in d.detail and "submit it again" in d.detail, d.detail
    assert ostore.load(SESSION).order_agreement.reagreement is None
    walked = _reagree(ostore, tmp_path, name="d3.toml")
    assert walked.ok, walked.detail
    assert ostore.load(SESSION).order_agreement.reagreement["why"] == GROUNDS["reagree_why"]


def test_rendering_without_the_grounds_is_not_presented(ostore, home, armed_env, tmp_path, monkeypatch):
    """What the user answers carries the grounds order-submit recorded: a rendering without them is
    refused at present-plan naming the lines to add, and with them it is registered."""
    import test_order_phase

    _plan_ready(ostore, home, tmp_path)
    submitted, _ = _submit_second(ostore, tmp_path)
    assert submitted.ok and _review(ostore).ok
    with monkeypatch.context() as m:
        m.setattr(test_order_phase, "reagreement_lines", lambda record, doc=None: [])
        refused = _present(ostore, tmp_path)

    assert refused.ok is False and "re-agreement grounds" in _text_of(refused), _text_of(refused)
    assert GROUNDS["reagree_why"] in _text_of(refused)
    assert _present(ostore, tmp_path).ok


def test_rendering_without_the_goal_and_done_criterion_is_not_presented(
        ostore, home, armed_env, tmp_path, monkeypatch):
    """The product the agreement releases a replan against is the draft's goal and done criterion
    beside the understanding. A rendering that carries the four grounds but not them leaves the user
    agreeing to a goal they were never shown: it is refused at present-plan naming both lines to add,
    and with them it is registered, agreed and the replan to that goal is released."""
    import test_order_phase

    _approved(ostore, home, tmp_path)
    submitted, path = _submit_second(ostore, tmp_path)
    assert submitted.ok and _review(ostore).ok
    assert "- **Goal:** g2" in submitted.data["order_reagreement_block"]
    with monkeypatch.context() as m:
        m.setattr(test_order_phase, "reagreement_lines",
                  lambda record, doc=None: reagreement_lines(record))
        refused = _present(ostore, tmp_path)

    text = _text_of(refused)
    assert refused.ok is False and "re-agreement grounds" in text, text
    assert "- **Goal:** g2" in text and "- **Done criterion:** d" in text, text
    assert GROUNDS["reagree_why"] not in text
    assert _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    _user_answers(_marker_of(path), at=time.time() + 5)
    agreed = cli.cmd_order_agree(ns(session=SESSION, note=""), store=ostore)
    assert agreed.ok, agreed.detail
    proposal = _proposal(tmp_path, *GOAL2)
    _redispose_goal_premise(ostore, proposal)
    assert _replan(ostore, proposal).ok


def test_reagreement_lines_inside_the_executor_section_are_not_shown_to_the_user(
        ostore, home, armed_env, tmp_path):
    """The grounds and the product lines count only where the user reads what they answer: a
    rendering that puts them under the executor marker is refused (the message says 'outside the
    executor section'), and the same lines before the customer marker are accepted."""
    from test_order_phase import ORDER_EXECUTOR_MARKER, _rendering_naming

    _plan_ready(ostore, home, tmp_path)
    submitted, path = _submit_second(ostore, tmp_path)
    assert submitted.ok and _review(ostore).ok
    block = reagreement_lines(ostore.load(SESSION).order_draft_reagreement, load_order_draft(path)[0])
    hidden = _rendering_naming(load_order_draft(path)[0]).replace(
        ORDER_EXECUTOR_MARKER, ORDER_EXECUTOR_MARKER + "\n\n" + "\n".join(block), 1)

    refused = _present(ostore, tmp_path, hidden)

    assert refused.ok is False and "outside the executor section" in _text_of(refused), _text_of(refused)
    assert "- **Goal:** g2" in _text_of(refused)
    assert _present(ostore, tmp_path).ok


def test_grounds_changed_after_presentation_make_the_presentation_stale(
        ostore, home, armed_env, tmp_path):
    """The agreement stores the grounds the rendering carried: a record that no longer matches the
    presented lines leaves the receipt stale, so order-agree records nothing."""
    _plan_ready(ostore, home, tmp_path)
    submitted, path = _submit_second(ostore, tmp_path)
    assert submitted.ok and _review(ostore).ok and _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    _user_answers(_marker_of(path), at=time.time() + 5)
    state = ostore.load(SESSION)
    state.order_draft_reagreement = {**state.order_draft_reagreement, "why": "something else entirely"}
    ostore.save(state)

    d = cli.cmd_order_agree(ns(session=SESSION, note=""), store=ostore)

    assert d.ok is False and "re-agreement grounds" in _text_of(d), _text_of(d)
    assert ostore.load(SESSION).order_agreement.reagreement is None


def test_reagreement_record_outside_order_digest(ostore, home, armed_env, tmp_path):
    """The record sits beside the order: the understanding digest is the draft's alone, so the
    grounds neither move it nor sit among the agreed parts."""
    _plan_ready(ostore, home, tmp_path)
    first = ostore.load(SESSION).order_agreement
    path = _draft(tmp_path, name="d2.toml")
    digest = plan_mod.understanding_digest(load_order_draft(path)[0])

    agreed = _reagree(ostore, tmp_path)

    state = ostore.load(SESSION)
    assert agreed.ok, agreed.detail
    assert state.order_agreement.reagreement["changes"] == GROUNDS["reagree_changes"]
    assert state.order_agreement.understanding_sha256 == first.understanding_sha256 == digest
    assert not any("reagree" in part for part in state.order_agreement.parts)
    assert agreed.data["order_reagreement_block"] == reagreement_lines(
        state.order_agreement.reagreement, load_order_draft(path)[0])


def test_reagreement_record_rendered_beside_order():
    record = {"changes": "the goal", "why": "the user asked", "earlier_plan": "none yet",
              "difficulty": "the need was missed", "product_sha256": "x"}

    lines = reagreement_lines(record)

    assert lines[0] == "## Re-agreement"
    for part in ("the goal", "the user asked", "none yet", "the need was missed"):
        assert any(part in line for line in lines)
    assert reagreement_lines(None) == []


def test_reagreement_keeps_understanding_closed(ostore, home, armed_env, tmp_path):
    """A re-agreement opens no new understanding arming and reopens none; and a rebuild replan that
    carries new figures afterwards is not held by the effort ledger."""
    from agentctl import effort_segments

    def understanding(store):
        ledger = store.load(SESSION).effort_segments
        return ledger, [a for a in ledger.get("armings") or () if a["segment"] == effort_segments.UNDERSTANDING]

    _approved(ostore, home, tmp_path)
    ledger_before, before = understanding(ostore)
    assert len(before) == 1 and before[0].get("closed_at"), before
    snapshot = [dict(a) for a in before]

    agreed = _reagree(ostore, tmp_path)
    assert agreed.ok, agreed.detail

    ledger_after, after = understanding(ostore)
    assert after == snapshot, (before, after)
    assert effort_segments.open_arming_of(ledger_after, effort_segments.UNDERSTANDING) is None

    rebuilt = _proposal(tmp_path, *GOAL2, ('negative_control = "false"', 'negative_control = "false"\nexpected_minutes = 20'),
                        name="rebuilt.toml")
    assert load_plan(rebuilt).stages[0].expected_minutes == 20
    _redispose_goal_premise(ostore, rebuilt)
    d = _replan(ostore, rebuilt)
    assert d.ok, (d.detail, d.data.get("blockers"))
    assert ostore.load(SESSION).plan_path == rebuilt
    assert understanding(ostore)[1] == snapshot


# --- the other classes are not the user's -----------------------------------------------------


@pytest.mark.parametrize("edit, leaf", [
    (('title = "the stage under test"', 'title = "the stage, renamed"'), "title"),
    (('expected_result_image = "The seam requires a typed order of a substantive plan."',
      'expected_result_image = "The seam requires a typed order."'), "subject.result"),
    (('done_criterion = "d1"', 'done_criterion = "d1, restated"'), "criterion.done_criterion"),
])
def test_stage_description_edit_needs_no_reagreement(ostore, home, armed_env, tmp_path, edit, leaf):
    """Class (b), a regression guard of the base: a changed stage description (its title, its
    result image, its done criterion) passes the routing. The same fixture refuses a changed goal."""
    _plan_ready(ostore, home, tmp_path)
    old = load_plan(ostore.load(SESSION).plan_path)
    new_path = _proposal(tmp_path, edit, name="described.toml")
    new = load_plan(new_path)

    classes = classify_replan(old, new)
    d = _replan(ostore, new_path)
    product = _replan(ostore, _proposal(tmp_path, edit, *GOAL2, name="both.toml"))

    assert classes.description and not classes.product and not classes.method, classes
    assert any(leaf in name for name in classes.description), (leaf, classes)
    _past_routing(d)
    assert product.ok is False and "order-agree" in _blockers(product)


def test_method_only_replan_not_routed_to_order(ostore, home, armed_env, tmp_path):
    """Class (c), a regression guard of the base: an old-form plan's changed procedure is not
    the order's business; the same fixture refuses a changed goal."""
    plan = _old_form_plan_ready(ostore, home, tmp_path)
    _cover_order_element(ostore, plan)
    procedure = ("3. re-check the seam", "3. re-check the seam twice")
    d_path = _old_form_proposal(tmp_path, procedure, name="method.toml")

    classes = classify_replan(load_plan(plan), load_plan(d_path))
    d = _replan(ostore, d_path)
    product = _replan(ostore, _old_form_proposal(tmp_path, procedure, *GOAL2, name="both.toml"))

    assert classes.method and not classes.product, classes
    assert d.ok, (d.data.get("blockers"),)
    assert ostore.load(SESSION).plan_path == d_path
    assert product.ok is False and "order-agree" in _blockers(product)


EXECUTOR_FIELDS = [
    pytest.param("requirements", "releases are tagged by a command", "releases are tagged by a signed command",
                 id="how-built-requirement"),
    pytest.param("requirements", "removes the hand-work", "removes the hand-work and the wait",
                 id="derivation"),
    pytest.param("proposals", 'verdict = "adopt"', 'verdict = "adapt"', id="proposal-verdict"),
    pytest.param("constraints", "no new dependency", "no new dependency and no new service",
                 id="condition-on-the-product"),
]


@pytest.mark.parametrize("field, old, new", EXECUTOR_FIELDS)
def test_executor_side_change_needs_no_reagreement(ostore, home, armed_env, tmp_path, field, old, new):
    """Class (a'), a regression guard of the base: an executor-side change recorded by the real
    `order-executor-revise` (the method-statement judge a stub) passes the routing without a
    re-agreement and leaves the agreed digest where it was; the same fixture refuses a changed
    goal, and refuses the executor-side change until it is recorded."""
    from test_method_statement_rule import Judge

    _approved(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    old_plan = load_plan(state.plan_path)
    agreed = state.order_agreement.understanding_sha256
    replace = _replacing(field, old, new)
    new_path = _plan(tmp_path, replace=replace, name="executor.toml")
    classes = classify_replan(old_plan, load_plan(new_path))
    assert classes.executor_side and not classes.product, classes
    unrecorded = _replan(ostore, new_path)
    assert unrecorded.ok is False and "order-executor-revise" in _blockers(unrecorded)

    revised = cli.cmd_order_executor_revise(
        ns(session=SESSION, plan=str(_draft(tmp_path, name="revised.toml", replace=replace)),
           why="the way it is built changed", by="agent"), store=ostore, method_runner=Judge())
    assert revised.ok, revised.detail
    state = ostore.load(SESSION)
    assert state.order_agreement.understanding_sha256 == agreed
    assert state.executor_side_revisions[-1]["executor_side_sha256"] == plan_mod.executor_side_digest(
        load_plan(new_path))

    d = _replan(ostore, new_path)
    product = _replan(ostore, _edit(_plan(tmp_path, replace=replace, name="both.toml"), *GOAL2))
    _past_routing(d)
    assert product.ok is False and "order-agree" in _blockers(product)
    assert ostore.load(SESSION).order_agreement.understanding_sha256 == agreed


def _revise_executor_side(store, tmp_path: Path, replace, name: str, *draft_edits):
    from test_method_statement_rule import Judge

    path = _draft(tmp_path, name=name, replace=replace)
    _edit(path, *draft_edits)
    revised = cli.cmd_order_executor_revise(
        ns(session=SESSION, plan=str(path), why="the way it is built changed", by="agent"),
        store=store, method_runner=Judge())
    assert revised.ok, revised.detail


def test_executor_side_must_match_the_agreed_or_the_latest_revision_also_after_a_reagreement(
        ostore, home, armed_env, tmp_path):
    """A replan whose executor side is neither the one agreed nor the latest recorded revision is
    refused naming `order-executor-revise`, when the product is unchanged and when the product was
    re-agreed alike; an earlier revision is no way back. Each way on is the real verb."""
    _approved(ostore, home, tmp_path)
    first = _replacing("requirements", "releases are tagged by a command", "releases are tagged by a signed command")
    second = _replacing("requirements", "removes the hand-work", "removes the hand-work and the wait")

    agreed = _reagree(ostore, tmp_path)
    assert agreed.ok, agreed.detail
    both_first = _edit(_plan(tmp_path, replace=first, name="both-first.toml"), *GOAL2)
    refused = _replan(ostore, both_first)
    assert refused.ok is False and "order-executor-revise" in _blockers(refused), _blockers(refused)

    _revise_executor_side(ostore, tmp_path, first, "rev-first.toml", *GOAL2)
    _past_routing(_replan(ostore, both_first))

    _revise_executor_side(ostore, tmp_path, {**first, **second}, "rev-second.toml", *GOAL2)
    back = _replan(ostore, both_first)
    assert back.ok is False and "order-executor-revise" in _blockers(back), "an earlier revision is no way back"
    latest = _edit(_plan(tmp_path, replace={**first, **second}, name="both-latest.toml"), *GOAL2)
    _past_routing(_replan(ostore, latest))


def test_executor_side_change_unrecorded_refused(ostore, home, armed_env, tmp_path):
    """An executor-side change no record covers names `order-executor-revise` as the way on."""
    _approved(ostore, home, tmp_path)
    side = ('specifics = "releases here are tagged from the main line only"',
            'specifics = "releases here are tagged from the release branch only"')

    d = _replan(ostore, _proposal(tmp_path, side, name="executor.toml"))

    assert d.ok is False and "order-executor-revise" in _blockers(d), _blockers(d)
    assert "order-agree" not in _blockers(d)


# --- what the classes are, as a pure function of the two plans ---------------------------------


def _old_form_order(replace=None) -> str:
    lines = {**_LINES, **(replace or {})}
    coverage = "".join(f"{k} = {v!r}\n".replace("'", '"') for k, v in _COVERAGE.items())
    return ("[meta.order]\n" + "\n".join(lines.values()) + "\n\n[meta.order.coverage]\n" + coverage + "\n")


def _old_form_text(tmp_path: Path, *, replace=None, goal='"g"') -> str:
    return declare_venue(
        _PLAN.format(goal=goal, done_criterion='"d"', weight_class='weight_class = "substantive"\n',
                     order=_old_form_order(replace), final_check=_FINAL_CHECK), tmp_path)


def _old_form_proposal(tmp_path: Path, *edits, name="old-proposal.toml", replace=None) -> str:
    path = tmp_path / name
    path.write_text(_old_form_text(tmp_path, replace=replace), encoding="utf-8")
    return _edit(path, *edits)


def _old_form_plan_ready(store, home: Path, tmp_path: Path) -> str:
    from agentctl.state import OrderAgreement

    _start(store)
    assert _classify(store).ok
    path = _draft(tmp_path, omit=("customer_side",))
    doc = load_order_draft(path)[0]
    state = store.load(SESSION)
    state.order_agreement_history.append(OrderAgreement(
        understanding_sha256=plan_mod.understanding_digest(doc), parts=plan_mod.understanding_parts(doc),
        draft_path=str(path), at=1.0))
    store.save(state)
    assert _submit(store, path).ok and _review(store).ok and _present(store, tmp_path).ok
    _confirm_delivery(store)
    assert _user_answers(_marker_of(path)) == 1
    assert cli.cmd_order_agree(ns(session=SESSION, note="", correction="", binding_decision=[]),
                               store=store).ok
    assert cli.cmd_plan(ns(session=SESSION), store=store).ok
    plan = _old_form_proposal(tmp_path, name="old-plan.toml")
    d = cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=store)
    assert d.ok, (d.detail, d.data)
    return plan


def test_mark_follows_order_form(ostore, home, armed_env, tmp_path):
    """A two-sided order's plan has stage descriptions; the same title change is a description
    change there and none in an old-form plan. The agreement the real verbs recorded carries the
    same mark as the plan's order (stage 3's `customer_side_agreed` and this stage's
    `is_two_sided`), also right after a re-agreement."""
    _approved(ostore, home, tmp_path)
    first = ostore.load(SESSION).order_agreement
    assert first.customer_side_agreed is True
    assert _reagree(ostore, tmp_path).ok
    state = ostore.load(SESSION)
    agreed_doc = load_order_draft(state.order_agreement.draft_path)[0]
    assert state.order_agreement is not first and state.order_agreement.customer_side_agreed is True
    assert plan_mod.is_two_sided(agreed_doc.meta.order) is True
    assert plan_mod.is_two_sided(load_plan(state.plan_path).meta.order) is True

    two = load_plan(_plan(tmp_path, name="two.toml"))
    two_new = load_plan(_proposal(tmp_path, ('title = "the stage under test"', 'title = "renamed"'),
                                  name="two-new.toml"))
    old = load_plan(_old_form_proposal(tmp_path, name="old.toml"))
    old_new = load_plan(_old_form_proposal(
        tmp_path, ('title = "the stage under test"', 'title = "renamed"'), name="old-new.toml"))

    assert classify_replan(two, two_new).description
    assert classify_replan(old, old_new).description == ()


def test_old_form_replan_keeps_old_form_routing(tmp_path):
    """An old-form plan's requirement change is a root product change (every understanding part
    counts in the old form), a procedure change is method only, and it has no executor side."""
    old = load_plan(_old_form_proposal(tmp_path, name="old.toml"))
    requirement = load_plan(_old_form_proposal(
        tmp_path, name="req.toml",
        replace={"requirements": _LINES["requirements"].replace("tagged by a command", "tagged by hand")}))
    procedure = load_plan(_old_form_proposal(
        tmp_path, ("3. re-check the seam", "3. re-check twice"), name="proc.toml"))

    moved = classify_replan(old, requirement)
    assert any("R:R1" in name for name in moved.product), moved
    assert moved.executor_side is False
    method = classify_replan(old, procedure)
    assert method.method and not method.product and not method.description, method


def test_old_form_replan_keeps_old_form_review_scope(tmp_path):
    """Regression guard of the base: an old-form plan's change tier is the stage-leaf change the
    plan's own diff shows, and the classes add no description move to it."""
    old = load_plan(_old_form_proposal(tmp_path, name="old.toml"))
    new = load_plan(_old_form_proposal(
        tmp_path, ("3. re-check the seam", "3. re-check twice"), name="proc.toml"))

    change = plan_mod.change_tier(old, new)

    assert change.stages and classify_replan(old, new, change).description == ()


def test_classes_of_an_identical_plan_are_empty(tmp_path):
    same = load_plan(_plan(tmp_path))

    assert classify_replan(same, same) == ReplanClasses()
    assert classify_replan(None, same) == ReplanClasses()


# --- the mutants -------------------------------------------------------------------------------


def test_product_replan_unrouted_registered():
    assert "product_replan_unrouted" in MUTANTS
    assert "old_form_mark_ignored" in MUTANTS


# --- the executor's method and estimate: the sub-plan record a replan carries ------------------

FRAME = "w"
METHOD2 = ("split the module and keep the seam", "split it twice")


def _frame_plan(store, tmp_path: Path) -> Path:
    """A frame at PLAN_READY whose plan carries the record of the parent's stage 1."""
    submitted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT)
    assert submitted.ok, submitted.data
    return tmp_path / "plan.toml"


def _frame_replan(store, tmp_path: Path, *pairs: tuple[str, str], append="", name="again.toml"):
    path = tmp_path / name
    path.write_text((tmp_path / "plan.toml").read_text(encoding="utf-8") + append, encoding="utf-8")
    _edit(path, *pairs)
    return cli.cmd_replan(ns(session=FRAME, plan=str(path)), store=store)


def _stored(store) -> sub_plan.SubPlanRecord:
    return sub_plan.stored_record(store.load(FRAME), 1)


def _text_of(d) -> str:
    return d.detail + " ".join(d.data.get("blockers", []) + d.data.get("problems", []))


def _approve_in_frame(store) -> str:
    """The user's approval of the sub-plan as the record stands (what approve-subplan writes)."""
    state = store.load(FRAME)
    record = sub_plan.stored_record(state, 1)
    state.stage_approvals.setdefault("0:1", {}).update({
        "user_subplan_approval": sub_plan.approval_digest(record),
        "user_subplan_request": sub_plan.resource_request_digest(record.resource_request)})
    store.save(state)
    return sub_plan.approval_digest(record)


def _agree_figure_in_frame(store, minutes=40.0, corrections=()) -> None:
    """The parent's agreement of the figure; the frame keeps it at the level of the plan whose
    stage the record describes. `corrections`: the entries `estimate-correct` already wrote."""
    state = store.load(FRAME)
    sub_plan._copy_slot(state, 1, 0).update(
        minutes=minutes, usd=None, basis="one module split, one suite run", author=FRAME, agreer="root",
        authority=sub_plan.AUTHORITY_USER, agreed=True, held=None, corrections=[dict(c) for c in corrections])
    store.save(state)


REQUEST = '\n[[meta.sub_plan.resource_request]]\nstage = 1\nallow = ["Edit", "Write"]\n'
NARROWER = ('allow = ["Edit", "Write"]', 'allow = ["Edit"]')


def _resolve_and_pop(store):
    """Settle every stage of the frame and pop it through the real `pop-subplan`, back to the
    level of the plan whose stage the record describes."""
    state = store.load(FRAME)
    for stage in state.stages:
        stage.outcome.status = "PASSED"
    state.node = Node.RESOLVED.value
    state.partition = Partition(verdict="single")
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    store.save(state)
    popped = cli.cmd_pop_subplan(ns(session=FRAME), store=store)
    assert popped.ok, popped.detail
    return popped


def _start_blockers(store, stage: int, sid: str) -> list[str]:
    """What stops the stage starting, except the method review and the estimate confirmation a
    decided-later stage also needs (other gates, with their own verbs and tests): the approvals
    and the request are what these tests move."""
    state = store.load(sid)
    return [b for b in gates.stage_start_blockers(state, state.stage(stage), store=store)
            if "method review" not in b and "confirmed an estimate" not in b]


def _push_second_frame(store, tmp_path: Path) -> None:
    """A second frame for stage 1 whose plan is the first one's file, classified, planned and
    submitted through the real verbs: its replans run at depth 1, where the user's approval of the
    sub-plan lives one level up."""
    pushed = cli.cmd_push_subplan(ns(session=FRAME, plan=str(tmp_path / "plan.toml"), task=None,
                                     originating_stage=1), store=store)
    assert pushed.ok, pushed.detail
    assert cli.cmd_classify(ns(session=FRAME, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                               tracker_key=None, architectural=True, external_effect=False,
                               new_dependency=False, public_api_change=False), store=store).ok
    assert cli.cmd_plan(ns(session=FRAME), store=store).ok
    submitted = cli.cmd_submit_plan(ns(session=FRAME, plan=str(tmp_path / "plan.toml")), store=store)
    assert submitted.ok, (submitted.detail, submitted.data)


def _approved_frame_at_dispatch(store, tmp_path: Path, *, lazy: bool) -> str:
    """The user approved the sub-plan and its resources through `approve-subplan` /
    `approve-resources` at the parent's level (stage 1 whole-plan or decided later), then a second
    frame for the stage is at its submitted plan. Returns the approval's digest."""
    from test_subplan_presentation_dispatch import (
        _agent_approves_stage_resources, _present_and_approve, _user_approves_resources)

    submitted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT + REQUEST)
    assert submitted.ok, submitted.data
    _resolve_and_pop(store)
    state = store.load(FRAME)
    state.stage(1).lazy = lazy
    store.save(state)
    approved = _present_and_approve(store, tmp_path, 1, FRAME)
    assert approved.ok, approved.detail
    if lazy:
        assert _agent_approves_stage_resources(store, 1, FRAME).ok
    else:
        assert _user_approves_resources(store, by="fedor", stage=1, sid=FRAME).ok
    assert _start_blockers(store, 1, FRAME) == []
    _push_second_frame(store, tmp_path)
    return store.load(FRAME).stage_approvals["0:1"]["user_subplan_approval"]


@pytest.mark.parametrize("lazy", [False, True], ids=["whole-plan-stage", "decided-later-stage"])
def test_in_boundary_method_change_restamps_digest(home_store, tmp_path, lazy):
    """The user approved the sub-plan through `approve-subplan`; a replan of the executor's method
    inside the second frame carries that approval to the new method, so the stage is dispatchable
    after the pop with no approval verb run (the executor's act inside the boundary). A replan that
    also changes the resource request carries none: `parent_resource_dispatch_blockers` refuses the
    stage with its digest reason until `approve-stage-resources` is run, which lifts it."""
    from test_subplan_presentation_dispatch import _agent_approves_stage_resources, _present_and_approve

    store = home_store
    old = _approved_frame_at_dispatch(store, tmp_path, lazy=lazy)

    moved_plan = _frame_replan(store, tmp_path, METHOD2)

    assert moved_plan.ok, (moved_plan.detail, moved_plan.data)
    moved = sub_plan.approval_digest(_stored(store))
    assert moved != old
    assert store.load(FRAME).stage_approvals["0:1"]["user_subplan_approval"] == moved
    _resolve_and_pop(store)
    assert _start_blockers(store, 1, FRAME) == []

    _push_second_frame(store, tmp_path)
    changed = _frame_replan(store, tmp_path, METHOD2, ("split it twice", "a third way"), NARROWER,
                            name="third.toml")

    assert changed.ok, (changed.detail, changed.data)
    assert sub_plan.approval_digest(_stored(store)) != moved
    _resolve_and_pop(store)
    refused = " ".join(_start_blockers(store, 1, FRAME))
    if lazy:
        assert "changed since the user approved it" in refused, refused
        assert _present_and_approve(store, tmp_path, 1, FRAME).ok
    else:
        assert "resource" in refused and "approve-stage-resources" in refused, refused
    assert "approve-stage-resources" in " ".join(_start_blockers(store, 1, FRAME))
    assert _agent_approves_stage_resources(store, 1, FRAME).ok
    assert _start_blockers(store, 1, FRAME) == []


def test_replan_file_is_no_writer_of_an_approval(store, tmp_path):
    """An approval the file states that differs from the engine's is refused and nothing is stored,
    also after the user's approval (written into `stage_approvals` as `approve-subplan` writes it, by
    this module's helper; the verb itself is walked in `test_in_boundary_method_change_restamps_digest`):
    the approval the gates read stays as it was. The record's own copy of the two approvals is
    written by no verb (the gates read `stage_approvals`), so the last half sets it by hand to show
    what the carry does with it: it survives a replan file that leaves it out, which is all it
    proves; it is not evidence that an approval is held anywhere a gate looks."""
    _frame_plan(store, tmp_path)
    means = 'means = "Edit and Write"'
    forged = (means, means + '\nuser_subplan_approval = "forged"')

    refused = _frame_replan(store, tmp_path, forged)

    assert refused.ok is False and "leave the field out" in _text_of(refused), _text_of(refused)
    assert _stored(store).user_subplan_approval == ""
    approved = _approve_in_frame(store)
    refused_again = _frame_replan(store, tmp_path, forged, name="forged-approved.toml")
    assert refused_again.ok is False and "leave the field out" in _text_of(refused_again)
    assert store.load(FRAME).stage_approvals["0:1"]["user_subplan_approval"] == approved
    state = store.load(FRAME)
    state.sub_plan_records["0:1"]["user_subplan_approval"] = "held"
    state.sub_plan_records["0:1"]["parent_resource_approval"] = "held-too"
    store.save(state)

    left_out = _frame_replan(store, tmp_path, name="left-out.toml")

    assert left_out.ok, (left_out.detail, left_out.data)
    assert _stored(store).user_subplan_approval == "held"
    assert _stored(store).parent_resource_approval == "held-too"


def test_method_replan_restamp_leaves_an_unapproved_subplan_unapproved(store, tmp_path):
    _frame_plan(store, tmp_path)

    d = _frame_replan(store, tmp_path, METHOD2)

    assert d.ok, (d.detail, d.data)
    assert not store.load(FRAME).stage_approvals.get("0:1", {}).get("user_subplan_approval")


def test_estimate_only_replan_needs_correction(store, tmp_path):
    """A figure the parent agreed changes only through the agreed correction: a replan that moves
    it is refused and names estimate-correct; the same replan at the agreed figure is taken. A
    figure nobody agreed is the executor's own to move, so the same replan passes before the parent
    has agreed one."""
    _frame_plan(store, tmp_path)
    assert sub_plan.estimate_copy(store.load(FRAME), 1, 0).get("agreed") in (None, False)
    unagreed = _frame_replan(store, tmp_path, ("minutes = 40", "minutes = 55"), name="unagreed.toml")
    assert unagreed.ok, (unagreed.detail, unagreed.data)
    assert _stored(store).estimate.minutes == 55.0
    assert _frame_replan(store, tmp_path, name="back.toml").ok
    assert _stored(store).estimate.minutes == 40.0
    _agree_figure_in_frame(store)

    refused = _frame_replan(store, tmp_path, ("minutes = 40", "minutes = 55"))

    assert refused.ok is False
    assert "estimate-correct" in _text_of(refused), _text_of(refused)
    assert _stored(store).estimate.minutes == 40.0
    kept = _frame_replan(store, tmp_path, METHOD2, name="kept.toml")
    assert kept.ok, (kept.detail, kept.data)
    assert _stored(store).estimate.minutes == 40.0 and _stored(store).method == METHOD2[1]


def test_replan_forged_correction_refused(store, tmp_path):
    """A replan is no writer of a correction: a plan that adds one, one that claims the parent's
    agreement for it, and one that changes an entry `estimate-correct` wrote are each refused with
    the record's own reason, with or without an agreed figure, and nothing is stored."""
    _frame_plan(store, tmp_path)
    reason = "carries a correction the record cannot write"
    forged = ("\n[[meta.sub_plan.estimate.corrections]]\nbasis = \"mine\"\n"
              "[meta.sub_plan.estimate.corrections.to]\nminutes = 99.0\n")
    claiming = ("\n[[meta.sub_plan.estimate.corrections]]\nbasis = \"agreed by root\"\nagreer = \"root\"\n"
                "agreed = true\n[meta.sub_plan.estimate.corrections.to]\nminutes = 99.0\n")
    for agreed in (False, True):
        if agreed:
            _agree_figure_in_frame(store)
        for label, text in (("added", forged), ("claiming", claiming)):
            refused = _frame_replan(store, tmp_path, append=text, name=f"forged-{label}-{agreed}.toml")
            assert refused.ok is False and reason in _text_of(refused), (label, agreed, _text_of(refused))
            assert _stored(store).estimate.corrections == []
    assert sub_plan.estimate_copy(store.load(FRAME), 1, 0)["corrections"] == []

    written = {"basis": "the suite is slower than thought", "to": {"minutes": 45.0}}
    _agree_figure_in_frame(store, minutes=45.0, corrections=[written])
    changed = ("\n[[meta.sub_plan.estimate.corrections]]\nbasis = \"rewritten\"\n"
               "[meta.sub_plan.estimate.corrections.to]\nminutes = 45.0\n")
    refused = _frame_replan(store, tmp_path, append=changed, name="forged-changed.toml")
    assert refused.ok is False and reason in _text_of(refused), _text_of(refused)
    assert sub_plan.estimate_copy(store.load(FRAME), 1, 0)["corrections"] == [written]


def test_unlisted_subplan_field_refused(store, tmp_path, monkeypatch):
    """A field the replan does not list is not edited by a replan: the refusal names the field and
    the ones a replan may change. The record's fields are all listed today, so the how-kept answer
    is taken off the list here to show that the list, and nothing else, decides."""
    _frame_plan(store, tmp_path)
    monkeypatch.setattr(sub_plan, "REPLAN_ANSWER_FIELDS", ("compared_plans",))

    refused = _frame_replan(store, tmp_path, ("no push verb is run", "a different way"))

    assert refused.ok is False
    text = _text_of(refused)
    assert "how_kept" in text and "method" in text, text
    assert _stored(store).how_kept[0].how == "no push verb is run"


def test_method_replan_changing_how_a_condition_is_kept_carries_the_new_answer(store, tmp_path):
    """A method replan that changes how a binding condition is kept states the new answer in the
    record, and is accepted: the answer is the executor's (the approval digest carries it, so the
    user's approval is restamped to the new answer like any method move)."""
    _frame_plan(store, tmp_path)
    old = _approve_in_frame(store)

    d = _frame_replan(store, tmp_path, ("no push verb is run", "the push verb is run on a dry-run"))

    assert d.ok, (d.detail, d.data)
    assert _stored(store).how_kept[0].how == "the push verb is run on a dry-run"
    moved = sub_plan.approval_digest(_stored(store))
    assert moved != old
    assert store.load(FRAME).stage_approvals["0:1"]["user_subplan_approval"] == moved


def test_replan_dropping_a_required_how_kept_answer_refused(store, tmp_path):
    """Check 13 holds at replan: a replan whose record no longer says how it keeps a binding
    condition is refused naming the condition, and the stored record keeps its answer."""
    _frame_plan(store, tmp_path)

    refused = _frame_replan(
        store, tmp_path, ('[[meta.how_kept]]\ncondition = "B1"\nhow = "no push verb is run"\n', ""))

    assert refused.ok is False
    assert "does not say how it keeps binding condition 'B1'" in _text_of(refused), _text_of(refused)
    assert [h.how for h in _stored(store).how_kept] == ["no push verb is run"]


def test_replan_changing_compared_plans_is_a_writable_answer(store, tmp_path):
    """`compared_plans` is the executor's answer too: a replan that changes it is taken."""
    _frame_plan(store, tmp_path)

    d = _frame_replan(store, tmp_path, (
        '[meta.compared_plans]\nnone = true\nsearch_id = "S1"',
        '[meta.compared_plans]\nnone = false\nsearch_id = "S1"\n\n[[meta.compared_plans.entries]]\n'
        'ref = "the seam-keeping split"\ndeparture = "splits one more module"'))

    assert d.ok, (d.detail, d.data)
    assert _stored(store).compared_plans.entries[0]["ref"] == "the seam-keeping split"


@pytest.mark.parametrize("edit, field", [
    (('method = "run"\n', 'method = "run twice"\n'), "means.method"),
    (('means = "bash"\n', 'means = "bash and git"\n'), "means.means"),
])
def test_renormalize_method_edit_still_refused(ostore, home, armed_env, tmp_path, edit, field):
    """`--renormalize` stays procedure-only: a stage's own `method` (or `means`), which the
    executor's replan may change, is no way round through it, and a changed goal is not either."""
    plan = _old_form_plan_ready(ostore, home, tmp_path)
    path = _old_form_proposal(tmp_path, edit, name="renorm.toml")
    assert classify_replan(load_plan(plan), load_plan(path)).method

    d = cli.cmd_replan(ns(session=SESSION, plan=path, renormalize=True), store=ostore)

    assert d.ok is False and f"a renormalization may not edit `{field}`" in _blockers(d), _blockers(d)
    state = ostore.load(SESSION)
    assert state.plan_path == plan and state.order_agreement.reagreement is None
    by_goal = cli.cmd_replan(
        ns(session=SESSION, plan=_old_form_proposal(tmp_path, *GOAL2, name="renorm-goal.toml"),
           renormalize=True), store=ostore)
    assert by_goal.ok is False and "may not edit `goal`" in _blockers(by_goal)


def test_product_and_method_together_classified_product(tmp_path):
    """A change of the goal that also moves a procedure is a product change (its route is the
    user's), and the method in it is never carried over."""
    old = load_plan(_old_form_proposal(tmp_path, name="old.toml"))
    both = load_plan(_old_form_proposal(
        tmp_path, ("3. re-check the seam", "3. re-check twice"), *GOAL2, name="both.toml"))

    classes = classify_replan(old, both)

    assert classes.product == ("goal",) and classes.method
    assert classes.restamps is False
    assert ReplanClasses(method=("x",)).restamps is True


def test_method_replan_of_planning_subplan(contour):
    """A child contour still planning its stage replans the method of its record: no re-agreement
    is asked, and the parent reads the new method with the figure it had."""
    assert contour.submit()["ok"] is True
    before = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    plan = contour.write(contour.text(method="split it twice"))

    with contour.as_child():
        d = contour.child.run("replan", session=CHILD, plan=plan)

    assert d["ok"] is True, d
    assert "order-agree" not in str(d)
    read = sub_plan.read(contour.store.load(PARENT), 1, contour.store)
    assert read.method == "split it twice" and read.estimate.minutes == before.estimate.minutes


def test_mixed_description_method_edit_is_not_a_product_change(ostore, home, armed_env, tmp_path):
    """A stage description beside a method change is class (b) and (c) together: neither is the
    user's product, so neither goes to order-agree, and nothing is re-stamped (`restamps` is False).
    The same fixture is refused, naming order-agree, once the goal moves too. What this does not
    prove is the sub-plan approval at the stage's start: that is walked in
    `test_mixed_method_and_goal_edit_in_a_frame_is_not_carried`."""
    _plan_ready(ostore, home, tmp_path)
    old = load_plan(ostore.load(SESSION).plan_path)
    title = ('title = "the stage under test"', 'title = "the stage, renamed"')
    executor = ('executor = "in_thread"', 'executor = "spawn:developer"')
    new_path = _proposal(tmp_path, title, executor, name="mixed.toml")

    classes = classify_replan(old, load_plan(new_path))
    d = _replan(ostore, new_path)

    assert classes.description and classes.method and not classes.product, classes
    assert classes.restamps is False
    _past_routing(d)
    refused = _replan(ostore, _proposal(tmp_path, title, executor, *GOAL2, name="mixed-product.toml"))
    assert refused.ok is False and "order-agree" in _blockers(refused)


def test_mixed_method_and_goal_edit_in_a_frame_is_not_carried(home_store, tmp_path):
    """A replan inside a second frame that moves the method and also the plan's own goal restamps
    nothing: the user's approval stays at the old digest, so the stage is refused at its start
    after the pop until the sub-plan's own approver approves it; the parent's approval of the
    resources does not lift it (r4-18)."""
    from test_subplan_presentation_dispatch import (
        _agent_approves_stage_resources, _present_and_approve)

    store = home_store
    old = _approved_frame_at_dispatch(store, tmp_path, lazy=True)

    d = _frame_replan(store, tmp_path, METHOD2, ('goal = "g"', 'goal = "g elsewhere"'))

    assert d.ok, (d.detail, d.data)
    assert store.load(FRAME).stage_approvals["0:1"]["user_subplan_approval"] == old
    assert sub_plan.approval_digest(_stored(store)) != old
    _resolve_and_pop(store)
    refused = " ".join(_start_blockers(store, 1, FRAME))
    assert "sub-plan changed since the user approved it" in refused, refused
    lifted = _agent_approves_stage_resources(store, 1, FRAME)
    assert lifted.ok is False and "changed since the user approved it" in lifted.detail, lifted.detail
    assert "sub-plan changed since the user approved it" in " ".join(_start_blockers(store, 1, FRAME))
    approved = _present_and_approve(store, tmp_path, 1, FRAME)
    assert approved.ok, approved.detail
    assert _start_blockers(store, 1, FRAME) == []


def test_order_agreed_before_the_change_with_no_plan_takes_the_old_plan_form(
        ostore, home, armed_env, tmp_path):
    """The mark is read from the order the plan follows: an old-form order agreed before this
    change, with no plan yet, gets a plan that classifies by the old form. The engine reads the
    mark when it routes the session's own replans: a title change is no description change there,
    and a requirement change is a root product change that goes back to the user."""
    plan = _old_form_plan_ready(ostore, home, tmp_path)
    title = ('title = "the stage under test"', 'title = "the stage, renamed"')
    requirement = {"requirements": _LINES["requirements"].replace("tagged by a command", "tagged by hand")}

    assert plan_mod.is_two_sided(load_plan(plan).meta.order) is False
    titled = _old_form_proposal(tmp_path, title, name="titled.toml")
    assert classify_replan(load_plan(plan), load_plan(titled)).description == ()
    d = _replan(ostore, titled)
    _past_routing(d)
    product = _replan(ostore, _old_form_proposal(tmp_path, name="req.toml", replace=requirement))
    assert product.ok is False and "order-agree" in _blockers(product), _blockers(product)
    assert ostore.load(SESSION).plan_path == plan


def test_old_form_product_replan_way_on_is_walked(ostore, home, armed_env, tmp_path):
    """An old-form session whose plan changes the root product is refused, and the way on the
    refusal names exists: an old-form changed draft is refused at order-submit for its missing
    customer side, a two-sided draft goes through review, presentation, the user's answer and
    order-agree with the four grounds, and the replan that carries it is then past the routing."""
    _old_form_plan_ready(ostore, home, tmp_path)
    old_form_goal = _old_form_proposal(tmp_path, *GOAL2, name="old-product.toml")
    assert _replan(ostore, old_form_goal).ok is False

    stuck = _submit(ostore, _edit(_draft(tmp_path, name="old-draft.toml", omit=("customer_side",)),
                                  ("a release takes a day of hand-work", "a release takes two days")))
    assert stuck.ok is False and "customer" in (stuck.detail + _blockers(stuck)).lower()

    agreed = _reagree(ostore, tmp_path)
    assert agreed.ok, (agreed.detail, agreed.data)
    d = _replan(ostore, _proposal(tmp_path, *GOAL2, name="new-product.toml"))

    _past_routing(d)


def test_d1_users_revise_after_a_fire_is_one_decision_and_a_raise_past_it_is_held(real):
    """The user's decision on a corrected figure is made once, through the verbs: an overspent
    stage fires into DIAGNOSING, `fire-acknowledge --by user --decision revise` at a figure past the
    agent's margin is refused until the user's marked answer exists, then writes the one user
    correction, closes the fire and restarts the since-user-decision counters; a later raise above
    that acknowledged figure is the agent's and is held, writing nothing."""
    from agentctl import task_accumulator

    _overspent_agreed_stage_in_its_cycle(real)
    state = real.state("s")
    assert state.node == "DIAGNOSING" and gates.open_fire_record(state) is not None
    task_accumulator.add(state.task_id, "replans_since_user_decision", 2)

    asked = real.ack(by="user", decision="revise", minutes=25, usd=40)
    assert asked["ok"] is False
    marker = _correction_marker(asked)
    _user_choice(marker, session="s")
    done = real.ack(by="user", decision="revise", minutes=25, usd=40)

    assert done["ok"], done
    state = real.state("s")
    [only] = real.corrections()
    assert (only["by"], only["authority"], only["to"]["usd"]) == ("user", "user", 40.0)
    assert gates.open_fire_record(state) is None
    assert task_accumulator.get(state.task_id)["per_axis_totals"]["replans_since_user_decision"] == 0

    raised = real.correct(usd=400, basis="grew again")

    assert raised["ok"] is False and raised["action"] == "await_user_approval", raised
    assert len(real.corrections()) == 1
    assert sub_plan.held_figure(real.state("s"), 1)["usd"] == 400.0
    assert sub_plan.user_figure(real.state("s"), 1)["usd"] == 40.0


def test_estimate_correct_does_not_reach_a_frames_parent_stage(home_store, tmp_path):
    """Why the frame half of the D1 walk below writes the parent's result by hand: in a plan-stack
    frame `estimate-correct --stage 1` addresses the frame's own stage 1, an old-form stage, and
    refuses with that message for the agent and for the user alike; the parent's stage the record
    describes is corrected only by the parent session, which holds that stage in its own plan."""
    store = home_store
    _frame_plan(store, tmp_path)
    _agree_figure_in_frame(store)

    for by in ("agent", "fedor"):
        d = cli.cmd_estimate_correct(
            ns(session=FRAME, stage=1, minutes=80.0, usd=None, basis="b", by=by), store=store)
        assert d.ok is False and "stage 1 is an old-form stage: it keeps its tier price" in d.detail, d.detail


def test_d1_frame_replan_carries_the_figure_the_parent_agreed(home_store, tmp_path):
    """The replan that carries the corrected figure asks the user for nothing more: before the
    parent's correction the figure is refused (naming estimate-correct), after it the same replan is
    taken. The correction entry and the figure it agrees are written into the frame's copy, as the
    parent session's `estimate-correct` writes them (the verb walk is the test above)."""
    store = home_store
    _frame_plan(store, tmp_path)
    _agree_figure_in_frame(store)
    basis = "the user decided"
    carrying = (("minutes = 40", "minutes = 80"),
                ('basis = "one module split, one suite run"', f'basis = "{basis}"'))
    assert _frame_replan(store, tmp_path, *carrying).ok is False

    state = store.load(FRAME)
    figure = sub_plan.figure_of(80.0, None)
    slot = sub_plan._copy_slot(state, 1, 0)
    slot["corrections"].append({
        "from": sub_plan.figure_of(40.0, None), "to": figure, "basis": basis, "by": "fedor",
        "agreer": FRAME, "authority": sub_plan.AUTHORITY_USER, "at": 1.0})
    slot.update(figure, basis=basis, authority=sub_plan.AUTHORITY_USER)
    store.save(state)

    d = _frame_replan(store, tmp_path, *carrying, name="corrected.toml")

    assert d.ok, (d.detail, d.data)
    assert _stored(store).estimate.minutes == 80.0
    stale = _frame_replan(store, tmp_path, name="stale.toml")
    assert stale.ok is False and "estimate-correct" in _text_of(stale)


# --- a re-agreement released one product only; the method classes; the judge's silence -----------


def test_an_earlier_reagreement_of_a_product_releases_nothing_once_replaced(ostore, home, armed_env, tmp_path):
    """The record that releases a replan is the CURRENT agreement's: the user re-agreed goal g2,
    then goal g3; a replan to g2 is refused again, a replan to g3 is past the routing."""
    _approved(ostore, home, tmp_path)
    assert _reagree(ostore, tmp_path).ok
    to_g2 = _proposal(tmp_path, *GOAL2, name="to-g2.toml")
    _past_routing(_replan(ostore, to_g2))

    again = _reagree(ostore, tmp_path, name="d3.toml", draft_edits=(('goal = "g"', 'goal = "g3"'),))
    assert again.ok, again.detail
    refused = _replan(ostore, to_g2)
    released = _replan(ostore, _proposal(tmp_path, ('goal = "g"', 'goal = "g3"'), name="to-g3.toml"))

    assert refused.ok is False and "order-agree" in _blockers(refused), _blockers(refused)
    _past_routing(released)


@pytest.mark.parametrize("old, new", [
    pytest.param('executor = "in_thread"', 'executor = "spawn:developer"', id="executor"),
    pytest.param('conditions = "The corpus fixture directory is present."',
                 'conditions = "The corpus fixture directory is present and writable."', id="conditions"),
])
def test_a_stage_executor_or_conditions_edit_is_the_method_not_the_product(tmp_path, old, new):
    before = load_plan(_old_form_proposal(tmp_path, name="before.toml"))
    after = load_plan(_old_form_proposal(tmp_path, (old, new), name="after.toml"))

    classes = classify_replan(before, after)

    assert classes.method and not classes.product and not classes.description, classes


def test_order_executor_revise_is_refused_while_the_method_judge_is_silent(ostore, home, armed_env, tmp_path):
    """`order-executor-revise` runs the method-statement judge over the executor side; with no
    answer the revision is refused naming the judge's silence and records nothing, and the same
    call once the judge answers records it. The judge is a stub, never a live model."""
    from test_method_statement_rule import Judge
    from test_order_two_sides import _agree_two_sided, _replacing, _sides

    _agree_two_sided(ostore, tmp_path)
    revised = _sides(tmp_path, "revised.toml",
                     replace=_replacing("proposals", "tag releases automatically", "tag on every merge"))

    def revise(judge):
        return cli.cmd_order_executor_revise(
            ns(session=SESSION, plan=str(revised), why="the way it is built changed", by="agent"),
            store=ostore, method_runner=judge)

    silent = revise(Judge(fail=True))

    assert silent.ok is False and "judge did not answer" in silent.detail, silent.detail
    assert ostore.load(SESSION).executor_side_revisions == []
    assert revise(Judge()).ok and len(ostore.load(SESSION).executor_side_revisions) == 1


# --- binding-condition objections and `condition-decide` ---------------------------------------

from agentctl.state import condition_decide_marker  # noqa: E402
from test_stage_order_subplan import TWO_CONDITIONS  # noqa: E402

_HOW_KEPT_B1 = '[[meta.how_kept]]\ncondition = "B1"\nhow = "no push verb is run"\n'
_HOW_KEPT_B2_ONLY = '[[meta.how_kept]]\ncondition = "B2"\nhow = "nothing is deleted"\n'
_HOW_KEPT_B2 = ('\n' + _HOW_KEPT_B2_ONLY + '\n[[meta.sub_plan.setter_decisions]]\ncondition = "B2"\n'
                'value = "kept"\nsetter = "user"\ndecided_by = "user"\n')


def _two_condition_frame(store, tmp_path: Path) -> None:
    submitted = _walk_to_submit(store, tmp_path, _RECORD_HEAD + _HOW_KEPT + _HOW_KEPT_B2,
                                conditions=TWO_CONDITIONS)
    assert submitted.ok, submitted.data


def _break(store, tmp_path: Path, *ids: str, drop=(_HOW_KEPT_B1,), name="break.toml", sid=FRAME):
    """A replan whose plan no longer says how it keeps the conditions in `drop`, declaring that it
    breaks `ids`."""
    path = tmp_path / name
    path.write_text((tmp_path / "plan.toml").read_text(encoding="utf-8"), encoding="utf-8")
    _edit(path, *[(text, "") for text in drop])
    return cli.cmd_replan(ns(session=sid, plan=str(path), breaks_condition=list(ids)), store=store)


def _decide(store, objection: str, value: str, *, by="agent", new_text="", sid=FRAME):
    return cli_condition.cmd_condition_decide(
        ns(session=sid, objection=objection, value=value, new_text=new_text, by=by), store=store)


def _users_decision(store, objection: str, value: str, new_text=""):
    """The user's marked answer to the ask for this decision, then the decision by their name."""
    _user_choice(condition_decide_marker(objection, value), session=FRAME)
    return _decide(store, objection, value, by="fedor", new_text=new_text)


def _objection_ids(refused) -> list[str]:
    return list(refused.data["objections"])


def test_binding_condition_break_refused_until_setter_decides(home_store, tmp_path):
    """A replan that no longer keeps a binding condition is refused by check 13 as it was; one that
    names the condition it breaks mints an objection with its own id and is refused until the level
    that set the condition has decided it. Each way on is walked through the verbs: the agent is
    refused at the root level, the user's marked answer decides, and the same replan then passes."""
    store = home_store
    _frame_plan(store, tmp_path)
    plain = _break(store, tmp_path, name="plain.toml")
    assert plain.ok is False and "does not say how it keeps binding condition 'B1'" in _text_of(plain)
    assert _stored(store).objections == []

    refused = _break(store, tmp_path, "B1")
    assert refused.ok is False and "condition-decide" in _text_of(refused), _text_of(refused)
    (objection_id,) = _objection_ids(refused)
    (raised,) = _stored(store).objections
    assert raised["id"] == objection_id and raised["condition"] == "B1" and raised["status"] == "open"
    assert raised["raised_by"] == FRAME and raised["site"]["key"] is None

    again = _break(store, tmp_path, "B1", name="again.toml")
    assert _objection_ids(again) == [objection_id] and len(_stored(store).objections) == 1

    by_agent = _decide(store, objection_id, "withdrawn")
    assert by_agent.ok is False and "only the user decides" in by_agent.detail
    unanswered = _decide(store, objection_id, "withdrawn", by="fedor")
    assert unanswered.ok is False and condition_decide_marker(objection_id, "withdrawn") in unanswered.detail

    assert _users_decision(store, objection_id, "withdrawn").ok
    passed = _break(store, tmp_path, "B1", name="passes.toml")
    assert passed.ok, (passed.detail, passed.data)
    assert _stored(store).objections[0]["status"] == "consumed"


def test_condition_decide_only_from_setter_level(home_store, tmp_path):
    """The raising session cannot decide for itself, a decision names a value and a text that go
    together, and an objection nobody raised is refused naming those open."""
    store = home_store
    _frame_plan(store, tmp_path)
    (objection_id,) = _objection_ids(_break(store, tmp_path, "B1"))

    assert "no objection 'ghost'" in _decide(store, "ghost", "withdrawn").detail
    assert "--new-text" in _decide(store, objection_id, "changed", by="fedor").detail
    assert "`changed` decision only" in _decide(store, objection_id, "withdrawn", new_text="x").detail
    assert "--value is one of" in _decide(store, objection_id, "ignored").detail
    refused = _decide(store, objection_id, "changed", new_text="no push to main")
    assert refused.ok is False and "only the user decides" in refused.detail
    assert store.load(FRAME).root_conditions.get("decisions") in (None, [])


_BREAK_BOTH = (_HOW_KEPT_B1, _HOW_KEPT_B2_ONLY)


def test_decision_on_one_objection_does_not_lift_another(home_store, tmp_path):
    """Two conditions, two objections: a decision on B1 leaves B2 refused, and a decision that
    keeps B2 lifts nothing."""
    store = home_store
    _two_condition_frame(store, tmp_path)
    both = _break(store, tmp_path, "B1", "B2", drop=_BREAK_BOTH)
    first, second = sorted(_objection_ids(both), key=lambda i: "B2" in i)
    assert len(_stored(store).objections) == 2 and "B1" in first and "B2" in second

    assert _users_decision(store, first, "withdrawn").ok
    still = _break(store, tmp_path, "B1", "B2", drop=_BREAK_BOTH, name="still.toml")
    assert still.ok is False and _objection_ids(still) == [second]

    assert _users_decision(store, second, "kept").ok
    kept = _break(store, tmp_path, "B1", "B2", drop=_BREAK_BOTH, name="kept.toml")
    assert kept.ok is False and "setter level kept condition B2" in _text_of(kept)

    assert _users_decision(store, second, "withdrawn").ok
    done = _break(store, tmp_path, "B1", "B2", drop=_BREAK_BOTH, name="done.toml")
    assert done.ok, (done.detail, done.data)


def test_condition_decide_registered_in_every_verb_set():
    from agentctl import own_contour
    from lib import widening_targets

    assert "condition-decide" in cli.COMMANDS
    assert "condition-decide" in own_contour.OWN_CONTOUR_VERBS
    assert "condition-decide" in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS


def test_any_setter_decision_unblocks_registered():
    assert "any_setter_decision_unblocks" in MUTANTS
    decisions = [sub_plan.SetterDecision.from_dict(
        {"condition": "B2", "value": "kept", "setter": "u", "decided_by": "u", "objection": "o-2"})]
    assert sub_plan.objection_lifted({"id": "o-1"}, decisions) is False


def test_condition_text_readers_use_the_resolver(tmp_path, monkeypatch):
    """The root level's `changed` decision is the text of the condition wherever the order is shown
    or judged: the rendering the user answers and the statements the method judge reads."""
    from agentctl import method_statement, render
    from test_order_two_sides import BESIDE, _sides

    doc, _ = load_order_draft(str(_sides(tmp_path, "d.toml", extra=BESIDE)))
    old, new = "no change to the release branch", "no change to any branch"
    decision = {"condition": "B1", "value": "changed", "setter": "user", "decided_by": "user",
                "objection": "o-1", "new_text": new, "version": 1}
    plain = "\n".join(render.render_order_md(doc))
    decided = "\n".join(render.render_order_md(doc, [decision]))
    assert old in plain and new not in plain
    assert new in decided and old not in decided

    monkeypatch.setattr(method_statement, "binding_exempt", lambda: False)
    texts = [s.text for s in method_statement.order_statements(doc.meta.order, [decision])
             if s.label == "binding condition B1"]
    assert texts and new in texts[0] and old not in texts[0]


_PLAN_FILE_FIELDS = [
    ("setter_decisions", '\n[[meta.sub_plan.setter_decisions]]\ncondition = "B1"\nvalue = "withdrawn"\n'
                         'setter = "user"\ndecided_by = "user"\n', "condition-decide"),
    ("objections", '\n[[meta.sub_plan.objections]]\nid = "forged"\ncondition = "B1"\nstatus = "open"\n',
     "replan --breaks-condition"),
    ("reviews", '\n[[meta.sub_plan.reviews]]\nkind = "x"\nverdict = "pass"\n', "plan-review-compose"),
]


@pytest.mark.parametrize("field, toml, verb", _PLAN_FILE_FIELDS, ids=[f[0] for f in _PLAN_FILE_FIELDS])
def test_replan_file_is_no_writer_of_an_engine_field(home_store, tmp_path, field, toml, verb):
    """A replan file carrying a value for a field the engine writes is refused naming the verb that
    writes it and stores nothing; the same replan without the field is taken."""
    store = home_store
    _frame_plan(store, tmp_path)
    before = getattr(_stored(store), field)

    refused = _frame_replan(store, tmp_path, append=toml)

    assert refused.ok is False, (refused.detail, refused.data)
    assert f"carries `{field}`" in _text_of(refused) and f"`{verb}`" in _text_of(refused), _text_of(refused)
    assert getattr(_stored(store), field) == before
    assert _frame_replan(store, tmp_path, name="without.toml").ok


def test_decision_moves_neither_agreed_digest_nor_priced_figures(home_store, tmp_path):
    """An objection and its decision touch the objection list and the decision list only: the
    digest of the sub-plan the user approved, the approval itself and the figure the parent agreed
    stay as they were."""
    store = home_store
    _frame_plan(store, tmp_path)
    _agree_figure_in_frame(store)
    approved = _approve_in_frame(store)
    figure = dict(sub_plan.estimate_copy(store.load(FRAME), 1, 0))

    (objection_id,) = _objection_ids(_break(store, tmp_path, "B1"))
    assert _users_decision(store, objection_id, "changed", "no push to main").ok

    assert sub_plan.approval_digest(_stored(store)) == approved
    assert store.load(FRAME).stage_approvals["0:1"]["user_subplan_approval"] == approved
    assert sub_plan.estimate_copy(store.load(FRAME), 1, 0) == figure


def _middle_level_contour(store, tmp_path: Path):
    """Parent `p` is itself a contour (linked at stage 4 of its own parent) and set B1 on its stage
    1; the child contour `c` builds that stage and has submitted its plan."""
    from test_stage_order_subplan import _RECORD_HEAD as head, _walk_to_submit_in_contour

    submitted = _walk_to_submit_in_contour(store, tmp_path, head + _HOW_KEPT)
    assert submitted.ok, submitted.data
    parent = store.load("p")
    parent.parent_link = {"parent_session": "g", "stage_index": 4, "norm": {}}
    store.save(parent)


def test_below_root_decision_lands_where_check_13_reads_it(home_store, tmp_path):
    """The condition was set by a middle level that is a contour of its own: the agent of that
    level decides (the raising session cannot), the decision is written into the middle level's
    record of the stage it was linked at, and the child's replan then passes check 13, which
    reads it there. The decided text is what the readers of the condition show."""
    store = home_store
    _middle_level_contour(store, tmp_path)
    refused = _break(store, tmp_path, "B1", sid="c")
    assert refused.ok is False, (refused.detail, refused.data)
    (objection_id,) = _objection_ids(refused)
    raised = sub_plan.stored_record(store.load("c"), 1).objections[0]
    assert raised["site"] == {"owner": "p", "key": "4", "depth": 0}

    below = _decide(store, objection_id, "changed", new_text="no push to main", sid="c")
    assert below.ok is False and "keeps its decisions in session 'p'" in below.detail
    assert _break(store, tmp_path, "B1", sid="c", name="before.toml").ok is False

    decided = _decide(store, objection_id, "changed", new_text="no push to main", sid="p")
    assert decided.ok, decided.detail
    (written,) = store.load("p").sub_plan_records["4"]["setter_decisions"]
    assert written["objection"] == objection_id and written["value"] == "changed"
    assert written["new_text"] == "no push to main" and written["version"] == 1
    assert store.load("c").sub_plan_records.get("0:1", {}).get("setter_decisions") in (None, [])

    seen = cli_common._stage_order_context(store.load("c"), store=store).binding_conditions
    assert [c["text"] for c in seen] == ["no push to main"]
    passed = _break(store, tmp_path, "B1", sid="c", name="after.toml", drop=())
    assert passed.ok, (passed.detail, passed.data)
    assert sub_plan.stored_record(store.load("c"), 1).objections[0]["status"] == "consumed"
