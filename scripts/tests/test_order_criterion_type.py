"""Where a new-form order's criterion type comes from.

The customer side of the order draft names the criterion type of the task; the session's own
field starts as whatever `start` or `classify` was given. Claims:

* the order review reads the type from the draft's customer side, never from the session;
* `order-agree` copies the agreed type into the session, where the closing ask and `verify-final`
  read it;
* an order agreed in the old form (no customer side) leaves the session's type alone;
* `reset` of the same task keeps the type the order agreed, a reset to another task does not.

Calls into code a mutant replaces go through their module.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_order_agreement import (  # noqa: F401  (home and ostore are fixtures)
    SESSION, _agree, _classify, _confirm_delivery, _marker_of, _user_answers, armed_env, home, ostore,
)
from test_order_form_migration import _agreed_earlier
from test_order_phase import SIDES, _draft, _present, _review, _submit

ACCEPTANCE = SIDES.replace('criterion_type = "measurable"', 'criterion_type = "acceptance-review"')


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _started_as(store, session_type: str):
    cli.cmd_start(ns(session=SESSION, task="od", goal="g", done_criterion="dc",
                     criterion_type=session_type, recursion_depth=0), store=store)
    assert _classify(store).ok


def _reviewed(store, tmp_path, *, sides=ACCEPTANCE, session_type="measurable"):
    _started_as(store, session_type)
    path = _draft(tmp_path, sides=sides)
    assert _submit(store, path).ok
    assert _review(store).ok
    return path


def _agreed(store, tmp_path, **kw):
    path = _reviewed(store, tmp_path, **kw)
    assert _present(store, tmp_path).ok
    _confirm_delivery(store)
    assert _user_answers(_marker_of(path)) == 1
    d = _agree(store)
    assert d.ok, d.detail
    return path


def test_review_reads_draft_type(ostore, tmp_path):
    _reviewed(ostore, tmp_path)
    state = ostore.load(SESSION)
    assert state.overall_criterion_type == "measurable"
    assert state.order_reviews[-1].criterion_type == "acceptance-review"


def test_review_of_a_measurable_draft_in_an_acceptance_session(ostore, tmp_path):
    _reviewed(ostore, tmp_path, sides=SIDES, session_type="acceptance-review")
    assert ostore.load(SESSION).order_reviews[-1].criterion_type == "measurable"


def test_agree_adopts_type(ostore, home, armed_env, tmp_path):
    _agreed(ostore, tmp_path)
    state = ostore.load(SESSION)
    assert state.order_agreement.criterion_type == "acceptance-review"
    assert state.overall_criterion_type == "acceptance-review"


def test_old_form_keeps_session_type(ostore, tmp_path):
    cli.cmd_start(ns(session=SESSION, task="od", goal="g", done_criterion="dc",
                     criterion_type="acceptance-review", recursion_depth=0), store=ostore)
    state = ostore.load(SESSION)
    assert state.overall_criterion_type == "acceptance-review"
    _agreed_earlier_in_place(ostore, tmp_path)
    assert _agree(ostore).ok
    state = ostore.load(SESSION)
    assert state.order_agreement.criterion_type == ""
    assert state.overall_criterion_type == "acceptance-review"


def _agreed_earlier_in_place(store, tmp_path):
    """`_agreed_earlier` for a session started with the given type, not a fresh measurable one."""
    from agentctl import plan
    from agentctl.state import OrderAgreement
    from agentctl.plan import load_order_draft
    assert _classify(store).ok
    path = _draft(tmp_path, omit=("customer_side",))
    doc = load_order_draft(path)[0]
    state = store.load(SESSION)
    state.order_agreement_history.append(OrderAgreement(
        understanding_sha256=plan.understanding_digest(doc), parts=plan.understanding_parts(doc),
        draft_path=str(path), at=1.0))
    store.save(state)
    assert _submit(store, path).ok
    assert _review(store).ok
    assert _present(store, tmp_path).ok
    _confirm_delivery(store)
    assert _user_answers(_marker_of(path)) == 1


def test_reset_keeps_order_type(ostore, home, armed_env, tmp_path):
    _agreed(ostore, tmp_path)

    kept = cli.cmd_reset(ns(session=SESSION, task="od", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0, force=True), store=ostore)
    assert kept.ok, kept.detail
    assert ostore.load(SESSION).overall_criterion_type == "acceptance-review"


def test_reset_to_another_task_keeps_the_default(ostore, home, armed_env, tmp_path):
    _agreed(ostore, tmp_path)

    other = cli.cmd_reset(ns(session=SESSION, task="another", goal="g", done_criterion="dc",
                             criterion_type="measurable", recursion_depth=0, force=True), store=ostore)
    assert other.ok, other.detail
    assert ostore.load(SESSION).overall_criterion_type == "measurable"
