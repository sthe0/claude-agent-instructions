"""order-raise / -dispose / -list and the order-coverage half of the premise gate.

`approve` today reads the plan's bytes but never the ORDER the plan answers, so a
narrowing chosen at plan-authoring time passes unnamed. premise.validate_order_elements
is the closure check that closes it and plugins_premise.premise_blockers wires it to
the plan_approval gate; these three commands only read/write the bag.

Covers, in both directions: the pure validator's rule table (including the EMPTY-bag
rule, which is the OPPOSITE of the question bag's — argued in validate_order_elements);
render_coverage_block's exact text and its deterministic ordering; the CLI round-trip
(upsert, the two disposition fast-fails, the plugin-inactive guard, `--format md` being
byte-identical to the generator); backward compatibility with a bag minted before the
order half existed; and a gate-level e2e through the real `cli.main` dispatch — approve
REFUSED while an element is undispositioned or the bag is empty, ALLOWED once every
element is covered or cut.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import advisor, cli, plugins, plugins_premise, premise
from agentctl.state import SessionState
from agentctl.store import FileStateStore


# --- helpers -------------------------------------------------------------------

def _el(id, element="an element of the order", disposition="raised", stage=None, reason=""):
    return premise.OrderElement(
        id=id, element=element, disposition=disposition, stage=stage, reason=reason)


def _validate(elements, *, stage_indices=frozenset({1, 2}), plan_present=True):
    return premise.validate_order_elements(
        elements, stage_indices=set(stage_indices), plan_present=plan_present)


def _state(store, sid="s", *, plan_path=None):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    if plan_path is not None:
        state.plan_path = str(plan_path)
    store.save(state)
    return state


def _raise(store, sid, *, id, element="an element of the order"):
    return cli.cmd_order_raise(Namespace(session=sid, id=id, element=element), store=store)


def _dispose(store, sid, *, id, as_, stage=None, reason=""):
    return cli.cmd_order_dispose(
        Namespace(session=sid, id=id, as_=as_, stage=stage, reason=reason), store=store)


def _list(store, sid, *, format=""):
    return cli.cmd_order_list(Namespace(session=sid, format=format), store=store)


def _bag(store, sid):
    return store.load(sid).plugins["premise"]["order_elements"]


_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "img-{i}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
"""


def _write_plan(path, indices):
    body = [
        "[meta]",
        'task_id = "demo-order"',
        'goal = "exercise the order-coverage CLI"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        "",
    ]
    prev = None
    for i in indices:
        body.append(_STAGE_TMPL.format(i=i, deps="[]" if prev is None else f"[{prev}]"))
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


# --- the pure validator: the empty-set rule, both directions --------------------

def test_empty_bag_blocks_once_a_plan_exists():
    blockers = _validate([], plan_present=True)
    assert len(blockers) == 1
    # the blocker must name its own way out: a session already in flight when this
    # lands meets it with a submitted plan and an empty bag.
    assert "order-raise" in blockers[0]
    assert "order-dispose" in blockers[0]
    assert "--as covered --stage" in blockers[0] and "--as cut --reason" in blockers[0]


def test_empty_bag_is_silent_before_a_plan_exists():
    assert _validate([], stage_indices=set(), plan_present=False) == []


# --- the pure validator: per-element rules, both directions ---------------------

def test_raised_element_always_blocks():
    blockers = _validate([_el("O1")])
    assert any("'O1'" in b and "raised (undispositioned)" in b for b in blockers)


def test_covered_element_with_an_existing_stage_is_clean():
    assert _validate([_el("O1", disposition="covered", stage=2)]) == []


def test_covered_element_without_a_stage_blocks():
    blockers = _validate([_el("O1", disposition="covered")])
    assert any("covered order element 'O1' names no stage" in b for b in blockers)


def test_covered_element_pointing_at_a_missing_stage_blocks():
    blockers = _validate([_el("O1", disposition="covered", stage=9)])
    assert any("'O1'" in b and "dangling edge" in b for b in blockers)


def test_dangling_stage_is_not_checked_when_no_plan_is_loaded():
    """Mirrors validate_questions: an empty `stage_indices` means the caller cannot
    yet compute stage indices, so the containment check is skipped rather than
    blocking every element."""
    assert _validate([_el("O1", disposition="covered", stage=9)],
                     stage_indices=set(), plan_present=False) == []


def test_cut_element_with_a_reason_is_clean():
    assert _validate([_el("O1", disposition="cut", reason="out of scope by the order")]) == []


def test_cut_element_without_a_reason_blocks():
    blockers = _validate([_el("O1", disposition="cut")])
    assert any("cut order element 'O1' has no reason" in b for b in blockers)


@pytest.mark.parametrize("placeholder", ["TODO", " tbd ", "n/a", "..."])
def test_cut_element_with_a_placeholder_reason_blocks(placeholder):
    blockers = _validate([_el("O1", disposition="cut", reason=placeholder)])
    assert any("'O1'" in b and "placeholder value" in b for b in blockers)


def test_unknown_disposition_blocks():
    blockers = _validate([_el("O1", disposition="deferred")])
    assert any("'O1'" in b and "unknown disposition" in b for b in blockers)


def test_every_element_is_reported_not_just_the_first():
    blockers = _validate([_el("O1"), _el("O2", disposition="cut")])
    assert any("'O1'" in b for b in blockers) and any("'O2'" in b for b in blockers)


# --- the round-trip is tolerant of a dict written before this change ------------

def test_order_elements_round_trip():
    elements = [_el("O1", disposition="covered", stage=1),
                _el("O2", disposition="cut", reason="named cut")]
    assert premise.order_elements_from_dicts(
        premise.order_elements_to_dicts(elements)) == elements


def test_order_elements_from_dicts_tolerates_missing_fields():
    [e] = premise.order_elements_from_dicts([{"id": "O1"}])
    assert (e.element, e.disposition, e.stage, e.reason) == ("", "raised", None, "")


# --- render_coverage_block: exact text, deterministic ordering ------------------

def test_render_coverage_block_text_and_ordering():
    elements = [
        _el("O9", "third element", disposition="covered", stage=2),
        _el("O2", "second element", disposition="cut", reason="the engine has no honest source"),
        _el("O1", "first element", disposition="covered", stage=1),
        _el("O0", "a cut one", disposition="cut", reason="named in the order, not folded in"),
    ]
    assert premise.render_coverage_block(elements, 2) == (
        "[scope] plan has 2 stage(s); order: 4 element(s) — 2 covered, 2 cut\n"
        "- covered: first element -> stage 1\n"
        "- covered: third element -> stage 2\n"
        "- cut: a cut one — named in the order, not folded in\n"
        "- cut: second element — the engine has no honest source"
    )


def test_render_coverage_block_is_stable_across_input_order():
    elements = [_el("O1", "a", disposition="covered", stage=1),
                _el("O2", "b", disposition="cut", reason="r")]
    assert (premise.render_coverage_block(elements, 3)
            == premise.render_coverage_block(list(reversed(elements)), 3))


def test_render_coverage_block_on_an_empty_bag():
    assert premise.render_coverage_block([], 0) == (
        "[scope] plan has 0 stage(s); order: 0 element(s) — 0 covered, 0 cut")


# --- the CLI: the plugin-inactive guard ----------------------------------------

def test_order_raise_refused_when_plugin_inactive(store):
    store.save(SessionState(session_id="s", task_id="t"))
    d = _raise(store, "s", id="O1")
    assert d.ok is False and d.action == "noop" and "not active" in d.detail


# --- the CLI: raise upserts, dispose writes both dispositions -------------------

def test_order_raise_records_an_undispositioned_element(store):
    _state(store)
    assert _raise(store, "s", id="O1", element="the norm sentence").ok is True
    [stored] = _bag(store, "s")
    assert stored["element"] == "the norm sentence"
    assert stored["disposition"] == "raised"


def test_order_raise_upserts_by_id_and_resets_disposition(store):
    _state(store)
    _raise(store, "s", id="O1", element="first wording")
    _dispose(store, "s", id="O1", as_="cut", reason="not in this delivery")
    _raise(store, "s", id="O1", element="second wording")
    [stored] = _bag(store, "s")
    assert stored["element"] == "second wording"
    assert stored["disposition"] == "raised"
    assert stored["reason"] == ""


def test_order_dispose_covered_and_cut(store):
    _state(store)
    _raise(store, "s", id="O1")
    _raise(store, "s", id="O2")
    assert _dispose(store, "s", id="O1", as_="covered", stage=2).ok is True
    assert _dispose(store, "s", id="O2", as_="cut", reason="named in the order, not folded in").ok is True
    by_id = {e["id"]: e for e in _bag(store, "s")}
    assert (by_id["O1"]["disposition"], by_id["O1"]["stage"]) == ("covered", 2)
    assert (by_id["O2"]["disposition"], by_id["O2"]["reason"]) == (
        "cut", "named in the order, not folded in")
    # re-dispositioning the other way clears the field that no longer applies
    _dispose(store, "s", id="O1", as_="cut", reason="dropped after all")
    assert {e["id"]: e for e in _bag(store, "s")}["O1"]["stage"] is None


def test_order_dispose_fast_fails_on_the_missing_field(store):
    _state(store)
    _raise(store, "s", id="O1")
    assert _dispose(store, "s", id="O1", as_="covered").ok is False
    assert _dispose(store, "s", id="O1", as_="cut").ok is False
    assert _bag(store, "s")[0]["disposition"] == "raised"


def test_order_dispose_refuses_an_unknown_element(store):
    _state(store)
    d = _dispose(store, "s", id="O404", as_="cut", reason="whatever")
    assert d.ok is False and "no such order element" in d.detail


# --- the CLI: order-list --format md IS the generator ---------------------------

def test_order_list_md_is_the_coverage_block(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [1, 2])
    _state(store, plan_path=plan_path)
    _raise(store, "s", id="O1", element="the gate")
    _dispose(store, "s", id="O1", as_="covered", stage=1)

    d = _list(store, "s", format="md")
    assert d.ok is True
    assert d.detail == premise.render_coverage_block(
        premise.order_elements_from_dicts(_bag(store, "s")), 2)
    assert d.data["stage_count"] == 2


def test_order_list_compact_default(store):
    _state(store)
    assert _list(store, "s").detail == "no order elements"
    _raise(store, "s", id="O1")
    assert _list(store, "s").detail == "O1=raised"


# --- backward compatibility: a bag minted before the order half existed ---------

def test_premise_blockers_tolerates_a_bag_without_order_elements(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [1])
    _state(store, plan_path=plan_path)
    live = store.load("s")
    del live.plugins["premise"]["order_elements"]
    store.save(live)

    live = store.load("s")
    blockers = plugins_premise.premise_blockers(live, live.plugins["premise"])
    # no KeyError, and the empty-bag rule still applies to the absent key
    assert any("no element of the order is recorded" in b for b in blockers)


# --- gate-level e2e: the REAL cli.main dispatch --------------------------------

@pytest.fixture
def _premise_armed(monkeypatch):
    """Drop conftest's suite-wide AGENTCTL_PREMISE=0 so the real weight_class-alone
    arming predicate runs, exactly as a production session arms it."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)


@pytest.fixture
def _stub_advisor_runner(monkeypatch):
    """`question-enumerate` through cli.main has no runner= seam; stub the
    module-level fallback to a healthy, question-less pass so the mandatory
    cross-check discharges without a live subprocess."""
    monkeypatch.setattr(
        advisor, "subprocess_runner",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""))


_PLAN = str(Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml")


def _run(capsys, root, *argv):
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def _build_substantive(capsys, root, sid="e2e"):
    _run(capsys, root, "start", "--session", sid, "--task", "t", "--goal", "g",
         "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--architectural")
    _run(capsys, root, "plan", "--session", sid)
    _run(capsys, root, "submit-plan", "--session", sid, "--plan", _PLAN)
    _run(capsys, root, "question-enumerate", "--session", sid)
    return sid


def _blockers(directive):
    return (directive.get("data") or {}).get("blockers") or []


@pytest.mark.usefixtures("_premise_armed", "_stub_advisor_runner")
def test_approve_refused_with_an_empty_order_bag(capsys, tmp_path):
    root = str(tmp_path / "state")
    sid = _build_substantive(capsys, root)
    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 1 and d["ok"] is False
    assert any("[premise]" in b and "no element of the order is recorded" in b
               for b in _blockers(d))


@pytest.mark.usefixtures("_premise_armed", "_stub_advisor_runner")
def test_approve_refused_with_an_undispositioned_order_element(capsys, tmp_path):
    root = str(tmp_path / "state")
    sid = _build_substantive(capsys, root)
    _run(capsys, root, "order-raise", "--session", sid, "--id", "O1",
         "--element", "make the plan's size visible before approval")

    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 1 and d["ok"] is False
    assert any("[premise]" in b and "'O1'" in b and "raised (undispositioned)" in b
               for b in _blockers(d))


@pytest.mark.usefixtures("_premise_armed", "_stub_advisor_runner")
def test_approve_allowed_once_every_order_element_is_covered_or_cut(capsys, tmp_path):
    root = str(tmp_path / "state")
    sid = _build_substantive(capsys, root)
    _run(capsys, root, "order-raise", "--session", sid, "--id", "O1",
         "--element", "make the plan's size visible before approval")
    _run(capsys, root, "order-raise", "--session", sid, "--id", "O2",
         "--element", "automate the escape-hatch inventory")
    _run(capsys, root, "order-dispose", "--session", sid, "--id", "O1",
         "--as", "covered", "--stage", "1")
    _run(capsys, root, "order-dispose", "--session", sid, "--id", "O2",
         "--as", "cut", "--reason", "named in the order brief as out of scope")

    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 0 and d["ok"] is True
    assert not _blockers(d)
