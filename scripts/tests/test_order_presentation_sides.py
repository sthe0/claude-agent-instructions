"""The two sides of a new-form order as the user is shown them, and the presentation gate that
holds the registered rendering to that shape.

Claims, each pinned in the direction that could fail:

* `render_order_md` of a new-form order prints the customer side first under a reconstruction
  marker, the executor side second under a marker that names it the agent's professional
  understanding, shown and not put to agreement, and the planning cost last, outside both;
  method ideas and conditions on means are not printed;
* `present-plan --kind order` stamps a new-form order's rendering only when both markers are
  present with the customer side first, the cost lies outside the order's text and equals the
  budget record (the bound figure once the draft is what the user agreed), and the
  binding-conditions question is recorded under its kind with the engine's own text and answered
  consistently with the draft's binding_condition records;
* the same structure is re-checked at `order-agree`, so a verb sequence that changes the
  question, the figure or the rendering after the stamp does not slip past the stamp;
* an old-form order is presented as before.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, gates, plugins, premise
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
    SESSION, _agree, _classify, _confirm_delivery, _marker_of, _presented, _user_answers, armed_env, home, ostore,
)
from test_order_phase import (
    COST_LINE, OLD_RENDERING, RENDERING, SIDES, _draft, _present, _ready, _submit,
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


def test_cost_figure_after_agreement_is_the_bound_one(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    assert _user_answers(_marker_of(path)) == 1
    assert _agree(ostore).ok
    _edit_budget(path)
    assert _submit(ostore, path).ok
    refused = _present(ostore, tmp_path, RENDERING.replace(COST_LINE, BIGGER_BUDGET), bind=False)
    assert refused.ok is False and "does not equal the budget record" in refused.detail
    assert _present(ostore, tmp_path, bind=False).ok


def test_no_budget_record_refused(ostore, tmp_path):
    _ready(ostore, tmp_path, budget="")
    refused = _present(ostore, tmp_path)
    assert refused.ok is False and "no planning budget record" in refused.detail
    assert ostore.load(SESSION).plan_presentations == []


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
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "binding-conditions question is not recorded" in refused.detail

    _raise_kind(ostore, "Q1")
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "not answered" in refused.detail

    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "Q1")
    refused = _present(ostore, tmp_path, bind=False)
    assert refused.ok is False and "disagrees with the draft" in refused.detail

    _raise_kind(ostore, "Q2")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "Q2")
    assert _present(ostore, tmp_path, bind=False).ok


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
    assert _present(ostore, tmp_path, bind=False).ok is False
    _raise_kind(ostore, "Q2")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "Q2")
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


def test_nothing_after_the_stamp_slips_past_order_agree(ostore, home, armed_env, tmp_path):
    """Present a conforming rendering, then take every verb a user could take to change what the
    presentation checks stand on; each must leave order-agree refused."""
    path = _presented(ostore, tmp_path)
    assert _user_answers(_marker_of(path)) == 1

    _raise_kind(ostore, "LATER", "")
    _dispose(ostore, gates.BINDING_CONDITIONS_YES, "LATER")
    refused = _agree(ostore)
    assert refused.ok is False and "disagrees with the draft" in refused.detail

    _raise_kind(ostore, "LATEST")
    _dispose(ostore, gates.BINDING_CONDITIONS_NONE, "LATEST")
    _edit_budget(path)
    refused = _agree(ostore)
    assert refused.ok is False and "stale" in refused.detail.lower()


# --- the criterion type the presentation shows no longer comes from the session ---------


def test_a_receipt_keeps_its_rendering(ostore, tmp_path):
    _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok
    receipt = ostore.load(SESSION).plan_presentations[-1]
    assert receipt.rendering_text == RENDERING


def _draft_sides_with_beside() -> str:
    return SIDES + BESIDE
