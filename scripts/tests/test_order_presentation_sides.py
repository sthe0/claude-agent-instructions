"""The two sides of a new-form order as the user is shown them, and the presentation gate that
holds the registered rendering to that shape.

Claims, each pinned in the direction that could fail:

* `render_order_md` of a new-form order prints the customer side first under a reconstruction
  marker, the executor side second under a marker that names it the agent's professional
  understanding, shown and not put to agreement, and the planning cost last, outside both;
  method ideas and conditions on means are not printed;
* `present-plan --kind order` stamps a new-form order's rendering only when each marker occurs
  once with the customer side first, each side's content sits under its own marker, the cost lies
  outside the order's text and equals the budget record (a draft of the understanding already
  agreed may not carry another figure than the agreed one), and the binding-conditions question
  is recorded under its kind with the engine's own text, answered by the user and consistently
  with the draft's binding_condition records;
* the same structure is re-checked at `order-agree`, so a verb sequence that changes the
  question, the figure or the rendering after the stamp does not slip past the stamp;
* an old-form order is presented as before.
"""
from __future__ import annotations

import time
from argparse import Namespace

from agentctl import cli, cli_order, effort_segments, gates, plugins, premise
from agentctl.plan import (
    ORDER_COST_MARKER,
    ORDER_CUSTOMER_MARKER,
    ORDER_EXECUTOR_MARKER,
    PLANNING_COST_LABEL,
    load_order_draft,
    planning_cost_line,
)
from agentctl.render import render_order_md, render_order_understanding_md
from order_agreement_mutants import mutant  # noqa: F401  (autouse fixture)
from test_order_agreement import (  # noqa: F401  (home and ostore are fixtures)
    SESSION, _agree, _agreed, _classify, _confirm_delivery, _marker_of, _presented, _start,
    _user_answers, armed_env, home, ostore,
)
from test_order_phase import (
    BUDGET, COST_LINE, NO_BUDGET_LINE, OLD_RENDERING, RENDERING, RENDERING_NO_BUDGET, SIDES, _draft, _present,
    _ready, _review, _submit,
)
from test_order_form_migration import _agreed_earlier
from test_order_two_sides import BESIDE


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _rendered(tmp_path, **draft_kw) -> list[str]:
    return render_order_md(load_order_draft(_draft(tmp_path, **draft_kw))[0])


def _line_index(lines: list[str], marker: str) -> int:
    return [line.strip() for line in lines].index(marker)


def _without(text: str, line: str) -> str:
    assert line in text
    return text.replace(line + "\n", "")


# --- the rendering -----------------------------------------------------------------------


def test_reconstruction_marker(ostore, tmp_path):
    lines = _rendered(tmp_path)
    assert _line_index(lines, ORDER_CUSTOMER_MARKER) >= 0
    assert "reconstruction" in ORDER_CUSTOMER_MARKER
    _ready(ostore, tmp_path)
    refused = _present(ostore, tmp_path, _without(RENDERING, ORDER_CUSTOMER_MARKER))
    assert refused.ok is False and "customer-side reconstruction marker" in refused.detail
    assert _present(ostore, tmp_path, bind=False).ok


def test_customer_side_first(ostore, tmp_path):
    lines = _rendered(tmp_path)
    customer = _line_index(lines, ORDER_CUSTOMER_MARKER)
    executor = _line_index(lines, ORDER_EXECUTOR_MARKER)
    assert customer < executor
    side = load_order_draft(_draft(tmp_path))[0].meta.order.customer_side
    text = "\n".join(lines)
    assert text.index(side.step) < text.index(ORDER_EXECUTOR_MARKER)
    _ready(ostore, tmp_path)
    swapped = RENDERING.replace(ORDER_CUSTOMER_MARKER, "@@").replace(
        ORDER_EXECUTOR_MARKER, ORDER_CUSTOMER_MARKER).replace("@@", ORDER_EXECUTOR_MARKER)
    refused = _present(ostore, tmp_path, swapped)
    assert refused.ok is False and "customer side must come before the executor side" in refused.detail


def test_executor_side_shown_not_agreed(ostore, tmp_path):
    lines = _rendered(tmp_path)
    doc = load_order_draft(_draft(tmp_path))[0]
    text = "\n".join(lines)
    assert "not put to agreement" in ORDER_EXECUTOR_MARKER
    assert "professional understanding" in ORDER_EXECUTOR_MARKER
    assert text.index(ORDER_EXECUTOR_MARKER) < text.index(doc.meta.order.executor_side.specifics)
    _ready(ostore, tmp_path)
    refused = _present(ostore, tmp_path, _without(RENDERING, ORDER_EXECUTOR_MARKER))
    assert refused.ok is False and "executor-side marker" in refused.detail


CUSTOMER_TEXT = "cutting a release needs a command that tags it (check A1).\n"
EXECUTOR_TEXT = ("[R1] releases are tagged by a command.\n[R2] a tag names its commit.\n"
                 "[P1] tag releases automatically (adopted).\n[K1] no new dependency.\n")


def _sections(customer: str, executor: str) -> str:
    return ("The order, as I understand it.\n\n"
            f"{ORDER_CUSTOMER_MARKER}\n\n{customer}\n"
            f"{ORDER_EXECUTOR_MARKER}\n\n{executor}\n"
            f"{ORDER_COST_MARKER}\n\n{COST_LINE}\n")


def test_each_side_carries_its_own_content(ostore, tmp_path):
    _ready(ostore, tmp_path)
    cases = {
        "the executor's anchors under the customer marker":
            (CUSTOMER_TEXT + EXECUTOR_TEXT, "executor text with no anchor",
             "anchors sit outside the executor section"),
        "an anchor repeated under the customer marker":
            (CUSTOMER_TEXT + "[R1] again.\n", EXECUTOR_TEXT, "anchors sit outside the executor section"),
        "an empty customer section": ("", EXECUTOR_TEXT, "the customer section is empty"),
        "a customer section that names no acceptance check":
            ("cutting a release needs a command.\n", EXECUTOR_TEXT, "does not name its acceptance check(s): A1"),
    }
    for name, (customer, executor, expected) in cases.items():
        refused = _present(ostore, tmp_path, _sections(customer, executor), bind=False)
        assert refused.ok is False and expected in refused.detail, (name, refused.detail)
    in_the_preamble = _sections(CUSTOMER_TEXT, EXECUTOR_TEXT.replace("[K1] no new dependency.\n", "")).replace(
        "as I understand it.", "as I understand it. [K1]", 1)
    refused = _present(ostore, tmp_path, in_the_preamble, bind=False)
    assert refused.ok is False and "does not carry the anchor(s): K1" in refused.detail
    also_in_the_preamble = _sections(CUSTOMER_TEXT, EXECUTOR_TEXT).replace(
        "as I understand it.", "as I understand it. [K1]", 1)
    refused = _present(ostore, tmp_path, also_in_the_preamble, bind=False)
    assert refused.ok is False and "anchors sit outside the executor section: K1" in refused.detail
    assert _present(ostore, tmp_path, _sections(CUSTOMER_TEXT, EXECUTOR_TEXT)).ok


WITH_CONDITION = _sections(CUSTOMER_TEXT + "B1 no change to the release branch.\n", EXECUTOR_TEXT)


def test_a_binding_condition_left_out_of_the_customer_section_is_refused(ostore, tmp_path):
    _ready(ostore, tmp_path, sides=SIDES + BESIDE)
    refused = _present(ostore, tmp_path, _sections(CUSTOMER_TEXT, EXECUTOR_TEXT), bind=False)
    assert refused.ok is False and "does not name its binding condition(s): B1" in refused.detail
    named_elsewhere = _sections(CUSTOMER_TEXT, EXECUTOR_TEXT + "B1 no change to the release branch.\n")
    refused = _present(ostore, tmp_path, named_elsewhere, bind=False)
    assert refused.ok is False and "does not name its binding condition(s): B1" in refused.detail
    assert _present(ostore, tmp_path, WITH_CONDITION).ok


def test_a_check_id_is_a_whole_token_in_the_customer_section(ostore, tmp_path):
    _ready(ostore, tmp_path)
    refused = _present(ostore, tmp_path, _sections("cutting a release (check A10).\n", EXECUTOR_TEXT), bind=False)
    assert refused.ok is False and "does not name its acceptance check(s): A1" in refused.detail


def test_a_repeated_marker_is_refused(ostore, tmp_path):
    _ready(ostore, tmp_path)
    for marker in (ORDER_CUSTOMER_MARKER, ORDER_EXECUTOR_MARKER, ORDER_COST_MARKER):
        twice = RENDERING.replace(f"{marker}\n", f"{marker}\n\nmore.\n\n{marker}\n", 1)
        refused = _present(ostore, tmp_path, twice, bind=False)
        assert refused.ok is False and "occurs 2 times" in refused.detail, marker


def test_cost_figure_outside_and_kept(ostore, tmp_path):
    lines = _rendered(tmp_path)
    cost = _line_index(lines, ORDER_COST_MARKER)
    assert cost > _line_index(lines, ORDER_EXECUTOR_MARKER)
    assert [line for line in lines[:cost] if "active minutes" in line] == []
    assert lines[cost + 2] == COST_LINE
    _ready(ostore, tmp_path)
    inside = RENDERING.replace(
        f"{ORDER_EXECUTOR_MARKER}\n", f"{ORDER_EXECUTOR_MARKER}\n{COST_LINE}\n")
    refused = _present(ostore, tmp_path, inside)
    assert refused.ok is False and "inside the order's text" in refused.detail
    wrong = RENDERING.replace(COST_LINE, planning_cost_line(PLANNING_COST_LABEL, 30, 9))
    refused = _present(ostore, tmp_path, wrong, bind=False)
    assert refused.ok is False and "does not equal the budget record" in refused.detail
    assert _present(ostore, tmp_path, bind=False).ok


BIGGER_BUDGET = planning_cost_line(PLANNING_COST_LABEL, 30, 9)


def _edit_budget(path):
    text = path.read_text(encoding="utf-8")
    assert "active_minutes = 12\nusd = 4.5" in text
    path.write_text(text.replace("active_minutes = 12\nusd = 4.5", "active_minutes = 30\nusd = 9.0"),
                    encoding="utf-8")


def _bound_budget(store):
    return store.load(SESSION).order_agreement.planning_budget


def test_a_budget_edited_after_agreement_is_never_bound_unseen(ostore, home, armed_env, tmp_path):
    """Agree with one budget, edit the draft's budget (it lies outside the digest), put the same
    understanding again: whichever figure the user is shown is the one that stays bound."""
    path = _presented(ostore, tmp_path)
    assert _user_answers(_marker_of(path)) == 1
    assert _agree(ostore).ok
    agreed = _bound_budget(ostore)
    _edit_budget(path)
    assert _submit(ostore, path).ok
    assert _review(ostore).ok

    for shown in (RENDERING, RENDERING.replace(COST_LINE, BIGGER_BUDGET)):
        refused = _present(ostore, tmp_path, shown, bind=False)
        assert refused.ok is False and "differs from the one agreed" in refused.detail
    assert _user_answers(_marker_of(path)) == 1
    assert _agree(ostore).ok is False
    assert _bound_budget(ostore) == agreed
    assert ostore.load(SESSION).effort_segments["agreed_estimate"]["usd"] == 4.5

    path.write_text(path.read_text(encoding="utf-8").replace("active_minutes = 30\nusd = 9.0",
                                                            "active_minutes = 12\nusd = 4.5"), encoding="utf-8")
    assert _submit(ostore, path).ok
    assert _review(ostore).ok
    assert _present(ostore, tmp_path, bind=False).ok
    _confirm_delivery(ostore)
    assert _user_answers(_marker_of(path)) == 1
    assert _agree(ostore).ok
    assert _bound_budget(ostore) == agreed


def test_no_budget_record_refused(ostore, home, armed_env, tmp_path):
    _start(ostore)
    assert _classify(ostore).ok
    path = _draft(tmp_path, budget="")
    assert _submit(ostore, path).ok and _review(ostore).ok
    refused = _present(ostore, tmp_path)
    assert refused.ok is False and "no usable planning estimate" in refused.detail
    assert ostore.load(SESSION).plan_presentations == []


def _close_planning(store):
    state = store.load(SESSION)
    arming = effort_segments.open_arming_of(state.effort_segments, effort_segments.PLANNING)
    effort_segments.close_arming(state.effort_segments, arming["arming_id"], "approve-by-user", time.time(), 5.0)
    store.save(state)
    assert cli_order._planning_open(store.load(SESSION)) is False


def test_a_draft_that_drops_the_agreed_budget_record_is_refused(ostore, home, armed_env, tmp_path):
    """The budget lies outside the understanding digest: a draft of the very understanding agreed
    with a figure may not drop its record, or the user is shown `(none)` where a figure was bound."""
    path = _agreed(ostore, home, tmp_path)
    assert _bound_budget(ostore) is not None
    _close_planning(ostore)
    text = path.read_text(encoding="utf-8")
    assert BUDGET.strip() in text
    path.write_text(text.replace(BUDGET.strip(), ""), encoding="utf-8")
    assert _submit(ostore, path).ok and _review(ostore).ok
    refused = _present(ostore, tmp_path, RENDERING_NO_BUDGET, bind=False)
    assert refused.ok is False and "missing or differs from the one agreed" in refused.detail


def _renew(store, tmp_path, difficulty: str, *, shown=RENDERING_NO_BUDGET):
    """Put and agree, with no budget record, an order whose understanding differs from the
    standing agreement's."""
    path = _draft(tmp_path, budget="", replace={"difficulty": f'difficulty = "{difficulty}"'},
                  name=f"{difficulty.replace(' ', '-')}.toml")
    assert _submit(store, path).ok and _review(store).ok
    presented = _present(store, tmp_path, shown, bind=False)
    if not presented.ok:
        return presented
    _confirm_delivery(store)
    assert _user_answers(_marker_of(path)) == 1
    return _agree(store)


def test_renewals_after_the_first_approval_carry_no_budget(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    assert _bound_budget(ostore) is not None
    _close_planning(ostore)
    first = _renew(ostore, tmp_path, "release by hand")
    assert first.ok, first.detail
    assert _bound_budget(ostore) is None
    second = _renew(ostore, tmp_path, "release by hand twice")
    assert second.ok, second.detail
    assert _bound_budget(ostore) is None


def test_a_standing_agreement_without_a_budget_renews_after_the_first_approval(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    state.order_agreement.planning_budget = None
    ostore.save(state)
    _close_planning(ostore)
    renewed = _renew(ostore, tmp_path, "release by hand")
    assert renewed.ok, renewed.detail


def test_a_renewal_shows_what_the_engine_renders_for_it(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    _close_planning(ostore)
    refused = _renew(ostore, tmp_path, "release by hand", shown=RENDERING)
    assert refused.ok is False and "does not equal the budget record" in refused.detail
    assert NO_BUDGET_LINE in render_order_md(load_order_draft(tmp_path / "release-by-hand.toml")[0])


# --- the binding-conditions question -----------------------------------------------------


def _activate_premise(store):
    state = store.load(SESSION)
    if "premise" not in state.plugins:
        plugins.activate(state, "premise")
        store.save(state)


def _raise_kind(store, qid="BC", question=""):
    _activate_premise(store)
    d = cli.cmd_question_raise(
        ns(session=SESSION, id=qid, target="plan.goal", question=question, control=None, plan=None,
           kind=gates.QUESTION_KIND_BINDING_CONDITIONS), store=store)
    assert d.ok, d.detail


def _dispose(store, answer, qid="BC"):
    cli.cmd_question_research(ns(session=SESSION, id=qid, attempted="asked the user"), store=store)
    d = cli.cmd_question_dispose(
        ns(session=SESSION, id=qid, to="escalated", answer=answer, source="", derivation="",
           basis="", risk="", plan=None), store=store)
    assert d.ok, d.detail


def test_binding_conditions_question(ostore, tmp_path):
    _ready(ostore, tmp_path, sides=SIDES + BESIDE)
    _activate_premise(ostore)
    refused = _present(ostore, tmp_path, WITH_CONDITION, bind=False)
    assert refused.ok is False and "binding-conditions question is not recorded" in refused.detail

    _raise_kind(ostore, "Q1")
    refused = _present(ostore, tmp_path, WITH_CONDITION, bind=False)
    assert refused.ok is False and "not answered" in refused.detail
    assert "question-research" in refused.detail

    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "Q1")
    refused = _present(ostore, tmp_path, WITH_CONDITION, bind=False)
    assert refused.ok is False and "disagrees with the draft" in refused.detail

    _raise_kind(ostore, "Q2")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "Q2")
    assert _present(ostore, tmp_path, WITH_CONDITION, bind=False).ok


def test_binding_conditions_answer_has_conditions_needs_a_record(ostore, tmp_path):
    _ready(ostore, tmp_path)
    _raise_kind(ostore, "Q1")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "Q1")
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "disagrees with the draft" in refused.detail
    _raise_kind(ostore, "Q2")
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "Q2")
    assert _present(ostore, tmp_path, bind=False).ok


def test_only_binding_conditions_make_the_answer_has_conditions(ostore, tmp_path):
    others = SIDES + "\n[[meta.order.method_ideas]]" + BESIDE.split("\n[[meta.order.method_ideas]]")[1]
    assert "binding_conditions" not in others and "method_ideas" in others
    _ready(ostore, tmp_path, sides=others)
    _raise_kind(ostore, "Q1")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "Q1")
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "disagrees with the draft" in refused.detail
    _raise_kind(ostore, "Q2")
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "Q2")
    assert _present(ostore, tmp_path, bind=False).ok


def test_a_record_counts_whatever_its_decision_says(ostore, tmp_path):
    agent_set = (SIDES + BESIDE).replace('set_by = "user"', 'set_by = "agent"')
    _ready(ostore, tmp_path, sides=agent_set)
    _raise_kind(ostore, "Q1")
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "Q1")
    assert _present(ostore, tmp_path, WITH_CONDITION, bind=False).ok is False
    _raise_kind(ostore, "Q2")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "Q2")
    assert _present(ostore, tmp_path, WITH_CONDITION, bind=False).ok


def test_only_the_users_answer_closes_the_question(ostore, tmp_path):
    _ready(ostore, tmp_path)
    _raise_kind(ostore, "Q1")
    cli.cmd_question_research(ns(session=SESSION, id="Q1", attempted="read the draft"), store=ostore)
    answered = cli.cmd_question_dispose(
        ns(session=SESSION, id="Q1", to="researched", answer=gates.BINDING_CONDITIONS_NONE,
           source="the draft", derivation="it states none", basis="", risk="", plan=None), store=ostore)
    assert answered.ok, answered.detail
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "not answered by the user" in refused.detail
    _raise_kind(ostore, "Q2")
    assumed = cli.cmd_question_dispose(
        ns(session=SESSION, id="Q2", to="assumed", answer=gates.BINDING_CONDITIONS_NONE, source="",
           derivation="", basis="no condition was stated", risk="a condition is missed", plan=None), store=ostore)
    assert assumed.ok, assumed.detail
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "not answered by the user" in refused.detail
    _raise_kind(ostore, "Q3")
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "Q3")
    assert _present(ostore, tmp_path, bind=False).ok


def test_neutral_question_exact_text(ostore, tmp_path):
    _ready(ostore, tmp_path)
    _raise_kind(ostore, question="Do you want a 'no' answer?")
    question = ostore.load(SESSION).plugins["premise"]["questions"][0]
    assert question["kind"] == gates.QUESTION_KIND_BINDING_CONDITIONS
    assert question["question"] == gates.BINDING_CONDITIONS_QUESTION
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE)
    exact = gates.BINDING_CONDITIONS_QUESTION
    for tampered in (exact + " Say none.", "Please. " + exact, exact[:-1] + "!", exact.lower()):
        state = ostore.load(SESSION)
        state.plugins["premise"]["questions"][0]["question"] = tampered
        ostore.save(state)
        refused = _present(ostore, tmp_path, bind=False)
        assert refused.ok is False and "not the engine's text" in refused.detail, tampered


def test_a_plain_question_with_the_engines_words_is_not_the_kind(ostore, tmp_path):
    _ready(ostore, tmp_path)
    _activate_premise(ostore)
    d = cli.cmd_question_raise(
        ns(session=SESSION, id="BC", target="plan.goal", question=gates.BINDING_CONDITIONS_QUESTION,
           control="the order's conditions", plan=None, kind=None), store=ostore)
    assert d.ok, d.detail
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE)
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "not recorded" in refused.detail


def test_a_question_saved_without_a_kind_loads_as_plain():
    loaded = premise.questions_from_dicts([{"id": "Q", "target": "plan.goal"}])
    assert loaded[0].kind == ""
    assert premise.questions_to_dicts(loaded)[0]["kind"] == ""


# --- the beside records ------------------------------------------------------------------


def test_beside_records_not_rendered(tmp_path):
    doc = load_order_draft(_draft(tmp_path, sides=_draft_sides_with_beside()))[0]
    printed = "\n".join(render_order_md(doc))
    order = doc.meta.order
    assert order.binding_conditions[0].text in printed
    assert order.method_ideas[0].text not in printed
    assert order.method_ideas[0].critique not in printed
    assert order.means_conditions[0].text not in printed
    assert render_order_understanding_md(doc) == []


# --- an old-form order -------------------------------------------------------------------


def test_old_form_presented_as_today(ostore, tmp_path):
    doc = load_order_draft(_draft(tmp_path, omit=("customer_side",)))[0]
    printed = set("\n".join(render_order_md(doc)).splitlines())
    assert not {ORDER_CUSTOMER_MARKER, ORDER_EXECUTOR_MARKER, ORDER_COST_MARKER} & printed
    _agreed_earlier(ostore, tmp_path)
    assert _present(ostore, tmp_path, OLD_RENDERING, bind=False).ok
    assert ostore.load(SESSION).plan_presentations[-1].rendering_text == OLD_RENDERING


# --- a verb sequence around the stamp ----------------------------------------------------


def test_a_question_changed_after_the_stamp_is_caught_by_order_agree(ostore, home, armed_env, tmp_path):
    """The binding-conditions question lives in the session, not in the stamped bytes: put a
    conforming rendering, then change the question; only the re-check at order-agree sees it."""
    path = _presented(ostore, tmp_path)
    assert _user_answers(_marker_of(path)) == 1

    _raise_kind(ostore, "LATER", "")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "LATER")
    refused = _agree(ostore)
    assert refused.ok is False and "disagrees with the draft" in refused.detail


def test_a_draft_edited_after_the_stamp_is_stale_at_order_agree(ostore, home, armed_env, tmp_path):
    """The receipt binds the draft's bytes, so an edit outside the digest (the budget) is caught
    as a stale presentation, by the byte binding rather than by the re-check of the structure."""
    path = _presented(ostore, tmp_path)
    assert _user_answers(_marker_of(path)) == 1
    _edit_budget(path)
    refused = _agree(ostore)
    assert refused.ok is False and "stale" in refused.detail.lower()


# --- the receipt ------------------------------------------------------------------------


def test_a_receipt_keeps_its_rendering(ostore, tmp_path):
    _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok
    receipt = ostore.load(SESSION).plan_presentations[-1]
    assert receipt.rendering_text == RENDERING


def _draft_sides_with_beside() -> str:
    return SIDES + BESIDE
