"""#present-plan refuses an essence while premise blockers stand (incident
2026-09-28: an essence was stamped with 12 raised enumeration candidates, `approve`
then refused, and the coordinator had to obtain a second approval for the same
plan). `cmd_present_plan` (kind=essence) now computes the SAME blocker set `approve`
would refuse on — via `plugins_premise.premise_blockers` — and stamps NOTHING when
any stand, reversing the #60 "present, then discover at approve" ordering.

The essence-coverage half of `premise_blockers` (the block-vs-live-order-bag
comparison) is deliberately EXCLUDED here (`include_essence_coverage=False`): it
compares the essence receipt this very call is about to stamp against itself, which
is circular before the receipt exists. `approve`'s own gating is unfiltered and
therefore unchanged — proven by leaving test_plugins_premise.py and
test_premise_gate_e2e.py untouched.

Covers: refusal on a raised enumeration candidate; refusal when the enumeration
cross-check has not run; a stamp when every check is clear; `full` staying
ungated (it never was); silence with no premise bag at all; and the exclusion
of the essence-coverage half not being merely permissive but load-bearing —
re-presenting after the order bag moved past an EARLIER receipt is not blocked
by that stale receipt.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plugins_premise
from agentctl.plan import load_plan
from agentctl.render import render_plan_grants
from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE, PLAN_PRESENTATION_KIND_FULL

_PLAN = str(Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml")


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def armed(monkeypatch):
    """Both knobs on: the premise plugin (which owns the order bag) and the
    plan-presentation gate. conftest forces both off suite-wide."""
    monkeypatch.setenv("AGENTCTL_PREMISE", "1")
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")


def _plan_ready(store, sid="e", plan=_PLAN):
    cli.cmd_start(
        ns(session=sid, task="demo", goal="g", done_criterion="dc",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    return sid


def _order(store, sid, *, id, element, as_=None, stage=None, reason=""):
    cli.cmd_order_raise(ns(session=sid, id=id, element=element), store=store)
    if as_:
        cli.cmd_order_dispose(
            ns(session=sid, id=id, as_=as_, stage=stage, reason=reason), store=store)


def _mark_enumerated(store, sid) -> None:
    """Lands the enumeration half at the CURRENT plan digest — not stale, no
    escape needed — so a test can isolate the ONE blocker it means to exercise."""
    state = store.load(sid)
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = plugins_premise._plan_content_digest(
        load_plan(_PLAN))
    store.save(state)


def _raise_candidate(store, sid, *, id="qenum-1", statement="which mode is out of scope?") -> None:
    state = store.load(sid)
    state.plugins["premise"]["candidates"].append({
        "id": id, "statement": statement, "disposition": "raised",
        "reason": "", "question": "", "target": "",
    })
    store.save(state)


def _grants_block() -> str:
    return render_plan_grants(load_plan(_PLAN), fmt="compact").strip()


def _essence_text(block: str = "") -> str:
    parts = ["## Essence\n\nprose about the plan."]
    if block:
        parts.append(block)
    parts.append(_grants_block())
    return "\n\n".join(parts)


def _present_essence(store, sid, tmp_path, text=None, name="essence.md"):
    if text is None:
        text = _essence_text(_block(store, sid))
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session=sid, kind=PLAN_PRESENTATION_KIND_ESSENCE,
           rendering_file=str(p), emit_skeleton=False),
        store=store,
    )


def _block(store, sid) -> str:
    state = store.load(sid)
    return plugins_premise.coverage_block(state, state.plugins["premise"])


def _premise_clear(store, sid) -> None:
    """Covers the order, lands the enumeration, leaves no open question/candidate —
    every named blocker below is exercised by moving state AWAY from this baseline."""
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    _mark_enumerated(store, sid)


# --- refusal cases ---------------------------------------------------------------

def test_essence_refused_while_candidate_raised(store, tmp_path, armed):
    sid = _plan_ready(store)
    _premise_clear(store, sid)
    _raise_candidate(store, sid)

    d = _present_essence(store, sid, tmp_path)
    assert d.ok is False and d.action == "noop"
    assert store.load(sid).plan_presentations == []
    assert any("qenum-1" in b for b in d.data["blockers"])


def test_essence_refused_when_enumeration_not_run(store, tmp_path, armed):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    # enumeration deliberately left un-landed

    d = _present_essence(store, sid, tmp_path)
    assert d.ok is False and d.action == "noop"
    assert store.load(sid).plan_presentations == []
    assert any("enumeration" in b for b in d.data["blockers"])


# --- the clean pass ----------------------------------------------------------------

def test_essence_stamped_when_premise_clear(store, tmp_path, armed):
    sid = _plan_ready(store)
    _premise_clear(store, sid)

    d = _present_essence(store, sid, tmp_path)
    assert d.ok is True, d.detail
    [receipt] = store.load(sid).plan_presentations
    assert receipt.kind == PLAN_PRESENTATION_KIND_ESSENCE


# --- full stays ungated -------------------------------------------------------------

def test_full_kind_not_premise_gated(store, tmp_path, armed):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    _raise_candidate(store, sid)  # would refuse an essence; full never checks this

    doc = load_plan(_PLAN)
    stages_text = "\n\n".join(f"[stage {s.index}] {s.title}\n" for s in doc.stages)
    full_grants_block = render_plan_grants(doc, fmt="full")
    p = tmp_path / "full.md"
    p.write_text(f"## Full plan\n\n{stages_text}\n\n{full_grants_block}", encoding="utf-8")
    d = cli.cmd_present_plan(
        ns(session=sid, kind=PLAN_PRESENTATION_KIND_FULL,
           rendering_file=str(p), emit_skeleton=False),
        store=store,
    )
    assert d.ok is True, d.detail


# --- silence guards ----------------------------------------------------------------

def test_no_premise_bag_unchanged(store, tmp_path, monkeypatch):
    """Premise plugin never armed => no bag => the new check is silent, exactly the
    pre-existing behaviour every other present-plan test relies on."""
    monkeypatch.setenv("AGENTCTL_PREMISE", "0")
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    sid = _plan_ready(store)
    assert "premise" not in store.load(sid).plugins

    p = tmp_path / "essence.md"
    p.write_text(_essence_text(), encoding="utf-8")
    d = cli.cmd_present_plan(
        ns(session=sid, kind=PLAN_PRESENTATION_KIND_ESSENCE,
           rendering_file=str(p), emit_skeleton=False),
        store=store,
    )
    assert d.ok is True, d.detail


# --- the essence-coverage half is not circular --------------------------------------

def test_essence_coverage_half_is_not_circular(store, tmp_path, armed):
    """A first essence stamps; the order bag then moves PAST that receipt (an
    element is cut), which makes the OLD receipt's embedded block stale. Re-
    presenting with a freshly-derived block must still succeed — the new gate
    excludes the essence-coverage half, so it never judges the new stamp attempt
    against the very receipt it is about to replace."""
    sid = _plan_ready(store)
    _premise_clear(store, sid)

    assert _present_essence(store, sid, tmp_path, name="essence1.md").ok is True

    cli.cmd_order_dispose(
        ns(session=sid, id="O1", as_="cut", reason="dropped after the essence was shown"),
        store=store)
    # Unfiltered premise_blockers (what `approve` uses) DOES see this as a
    # blocker — proving the scenario is real, not vacuous.
    state = store.load(sid)
    unfiltered = plugins_premise.premise_blockers(state, state.plugins["premise"])
    assert any("scope-coverage block" in b for b in unfiltered)

    d = _present_essence(store, sid, tmp_path, name="essence2.md")
    assert d.ok is True, d.detail
