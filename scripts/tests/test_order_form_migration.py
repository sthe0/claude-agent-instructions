"""The two-sided order beside the old-form order: what an order agreed before the split keeps.

An order is new-form exactly when its `customer_side` is present. An old-form order keeps the
digest it was agreed under, requirements, proposals, constraints and planning estimate included;
a new-form order counts those in its executor side instead. `submit-plan` still reads an old-form
`[meta.order]`; `order-submit` and `order-agree` do not take one that was not already agreed.

Calls into code a mutant replaces go through their module (`cli_order.`, `plan.`).
"""
from __future__ import annotations

import hashlib
from argparse import Namespace

from agentctl import cli, cli_order, plan
from agentctl.plan import load_order_draft
from agentctl.state import OrderAgreement
from conftest import SUBSTANTIVE_ORDER, declare_venue
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_meta_order import _FINAL_CHECK, _PLAN
from test_order_agreement import (  # noqa: F401  (home and ostore are fixtures)
    SESSION, _classify, _confirm_delivery, _marker_of, _presented, _start, _user_answers, home, ostore,
)
from test_order_phase import _draft, _present, _review, _submit

OLD = {"omit": ("customer_side",)}

OLD_FORM_PART_NAMES = {
    "customer", "activity", "difficulty", "functional_place", "R:R1", "R:R2", "P:P1", "K:K1",
    "planning_estimate",
}
OLD_FORM_DIGEST = "45f5eb6ef17fb94e4331ef3b53b5c8d2ad5344511ab4a4fdfc9b51c74932bad2"


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _doc(path):
    return load_order_draft(path)[0]


def test_an_old_form_order_keeps_its_agreed_digest_parts(tmp_path):
    doc = _doc(_draft(tmp_path, **OLD))

    parts = plan.understanding_parts(doc)

    assert set(parts) == OLD_FORM_PART_NAMES
    assert plan.executor_side_parts(doc) == {} and plan.executor_side_digest(doc) == ""
    assert plan.understanding_digest(doc) == hashlib.sha256(
        repr(tuple(sorted(parts.items()))).encode("utf-8")).hexdigest()
    assert plan.understanding_digest(doc) == OLD_FORM_DIGEST


def test_an_old_form_planning_estimate_stays_a_part_of_the_agreed_digest(tmp_path):
    with_estimate = plan.understanding_digest(_doc(_draft(tmp_path, **OLD)))
    other = _draft(tmp_path, name="other.toml", omit=("customer_side",), replace={
        "planning_estimate": 'planning_estimate = { active_minutes = 30, usd = 9.0, derivation = "bigger" }'})
    without = _draft(tmp_path, name="without.toml", omit=("customer_side", "planning_estimate"))

    assert plan.understanding_digest(_doc(other)) != with_estimate
    assert plan.understanding_digest(_doc(without)) != with_estimate


def test_a_new_form_order_counts_the_customer_side_and_no_executor_part(tmp_path):
    parts = plan.understanding_parts(_doc(_draft(tmp_path)))

    assert {"S:step", "C:A1"} <= set(parts)
    assert not {"R:R1", "P:P1", "K:K1", "planning_estimate"} & set(parts)


def test_order_submit_refuses_a_draft_without_a_customer_side(ostore, tmp_path):
    _start(ostore)

    d = cli.cmd_order_submit(ns(session=SESSION, plan=str(_draft(tmp_path, **OLD))), store=ostore)

    assert d.ok is False and "customer_side" in d.detail
    assert not ostore.load(SESSION).order_draft_path


def test_old_form_plan_still_submits(ostore, tmp_path):
    """submit-plan reads the old-form `[meta.order]` as before: the split touches order-submit."""
    _start(ostore, session="mo")
    cli.cmd_classify(ns(session="mo", chat=False, changed_lines=200, files=5, wall_clock_min=60,
                        tracker_key=None, architectural=True, external_effect=False,
                        new_dependency=False, public_api_change=False), store=ostore)
    assert cli.cmd_plan(ns(session="mo"), store=ostore).ok
    text = _PLAN.format(
        goal='"g"', done_criterion='"d"', weight_class='weight_class = "substantive"\n',
        order=SUBSTANTIVE_ORDER, final_check=_FINAL_CHECK)
    path = tmp_path / "old-plan.toml"
    path.write_text(declare_venue(text, tmp_path), encoding="utf-8")

    d = cli.cmd_submit_plan(ns(session="mo", plan=str(path)), store=ostore)

    assert d.ok, (d.detail, d.data)


def _agreed_earlier(ostore, tmp_path):
    """An old-form draft the user agreed in an earlier turn of this session, put again unchanged:
    no monkeypatch, the engine as it is now accepts it at order-submit and present-plan."""
    _start(ostore)
    assert _classify(ostore).ok
    path = _draft(tmp_path, **OLD)
    doc = _doc(path)
    state = ostore.load(SESSION)
    state.order_agreement_history.append(OrderAgreement(
        understanding_sha256=plan.understanding_digest(doc), parts=plan.understanding_parts(doc),
        draft_path=str(path), at=1.0))
    ostore.save(state)
    assert _submit(ostore, path).ok
    assert _review(ostore).ok
    assert _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    assert _user_answers(_marker_of(path)) == 1
    return path


def _agree(store):
    return cli_order.cmd_order_agree(ns(session=SESSION, note="", correction="", binding_decision=[]),
                                     store=store)


def test_an_unagreed_old_form_draft_is_refused_at_agreement(ostore, tmp_path, monkeypatch):
    """A draft an earlier version of the engine accepted, put to the user, never agreed."""
    with monkeypatch.context() as m:
        m.setattr(cli_order, "order_customer_side_missing", lambda meta: [])
        path = _presented(ostore, tmp_path, **OLD)
    assert _user_answers(_marker_of(path)) == 1

    d = _agree(ostore)

    assert d.ok is False and "no customer side" in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_an_unagreed_old_form_draft_is_refused_at_present_plan(ostore, tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(cli_order, "order_customer_side_missing", lambda meta: [])
        _start(ostore)
        assert _classify(ostore).ok
        assert _submit(ostore, _draft(tmp_path, **OLD)).ok
        assert _review(ostore).ok

    d = _present(ostore, tmp_path)

    assert d.ok is False and "customer_side" in d.detail


def test_an_old_form_order_agreed_before_the_change_is_put_again_and_renewed(ostore, tmp_path):
    path = _agreed_earlier(ostore, tmp_path)
    earlier = ostore.load(SESSION).order_agreement_history[0]

    d = _agree(ostore)

    assert d.ok, d.detail
    agreement = ostore.load(SESSION).order_agreement
    assert agreement.understanding_sha256 == earlier.understanding_sha256
    assert agreement.customer_side_agreed is False
    assert "planning_estimate" in agreement.parts and agreement.executor_side_parts == {}
    assert agreement.planning_budget is None


def test_an_old_form_draft_changed_since_its_agreement_is_refused_at_submit(ostore, tmp_path):
    _agreed_earlier(ostore, tmp_path)
    changed = _draft(tmp_path, name="changed.toml", omit=("customer_side",),
                     replace={"difficulty": 'difficulty = "a release takes a week"'})

    d = _submit(ostore, changed)

    assert d.ok is False and "customer_side" in d.detail


def test_the_agreement_of_an_old_form_order_still_opens_the_arming_from_its_estimate(
        ostore, tmp_path):
    _agreed_earlier(ostore, tmp_path)

    assert _agree(ostore).ok

    estimate = ostore.load(SESSION).effort_segments["agreed_estimate"]
    assert estimate["active_minutes"] == 12 and estimate["usd"] == 4.5
