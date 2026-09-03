"""The scope-coverage block bound to the PRESENTED essence (order element A's second
half, carrying element C on its header line).

Stage 1 made the order's elements records the gate can read; that alone never reaches
the user. Two halves close the remaining distance, and they are not interchangeable:
`present-plan --kind essence` fast-fails an essence that omits the block (stamping
NOTHING and handing it back to paste, because the essence is emitted as a turn's FINAL
text and discovering the omission at `approve` would cost the whole present -> ask
cycle), while plugins_premise.premise_blockers re-derives the block from LIVE state at
gate time — catching an element cut AFTER presentation, which the receipt's plan_sha256
binding structurally cannot see, the order bag not being plan bytes.

Covers, in both directions: the pure containment helper; the fast-fail refusing and
stamping nothing vs. stamping an essence that embeds the block; the gate going red on a
post-presentation cut and green again after re-presenting; and both silence guards —
plan presentation inactive, and no essence receipt at all (that absence is
gates.plan_presentation_blockers' own refusal, with its own route out).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plugins_premise, premise
from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE

_PLAN = str(Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml")


def ns(**kw) -> Namespace:
    return Namespace(**kw)


# --- helpers -------------------------------------------------------------------

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


def _block(store, sid) -> str:
    state = store.load(sid)
    return plugins_premise.coverage_block(state, state.plugins["premise"])


def _present(store, sid, text, tmp_path, name="essence.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session=sid, kind=PLAN_PRESENTATION_KIND_ESSENCE,
           rendering_file=str(p), emit_skeleton=False),
        store=store,
    )


def _blockers(store, sid) -> list[str]:
    state = store.load(sid)
    return plugins_premise.premise_blockers(state, state.plugins["premise"])


def _coverage_blockers(store, sid) -> list[str]:
    return [b for b in _blockers(store, sid) if "scope-coverage block" in b]


# --- the pure containment helper, both directions -------------------------------

def test_missing_lines_empty_when_every_line_is_carried():
    block = "[scope] plan has 2 stage(s)\n- covered: a -> stage 1"
    rendering = (
        "## Plan essence\n\nSome prose about the plan.\n\n"
        "    [scope] plan has 2 stage(s)\n"
        "  - covered: a -> stage 1\n\nmore prose\n"
    )
    assert plugins_premise.coverage_block_missing_lines(block, rendering) == []


def test_missing_lines_reports_each_absent_line():
    block = "[scope] header\n- covered: a -> stage 1\n- cut: b — no honest source"
    missing = plugins_premise.coverage_block_missing_lines(
        block, "[scope] header\n- covered: a -> stage 1\n")
    assert missing == ["- cut: b — no honest source"]


def test_missing_lines_requires_the_line_intact_not_merely_mentioned():
    """A paraphrase is not the block. The check is containment of the generated
    LINE, so an essence that merely alludes to a cut still fails."""
    block = "- cut: automating the escape-hatch inventory — named in the order"
    rendering = "We also cut automating the escape-hatch inventory, as the order said.\n"
    assert plugins_premise.coverage_block_missing_lines(block, rendering) == [block]


def test_coverage_block_is_none_before_a_plan_is_submitted(store):
    cli.cmd_start(
        ns(session="s", task="t", goal="g", done_criterion="dc",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    state = store.load("s")
    assert plugins_premise.coverage_block(state, {"order_elements": []}) is None


# --- the present-plan fast-fail -------------------------------------------------

def test_present_plan_refuses_an_essence_without_the_block_and_stamps_nothing(
    store, tmp_path, armed
):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)

    d = _present(store, sid, "A summary of the plan with no scope block.\n", tmp_path)
    assert d.ok is False and d.action == "noop"
    assert store.load(sid).plan_presentations == []
    # the block is handed back verbatim, so the coordinator pastes rather than
    # composes a second rendering of its own
    assert d.data["coverage_block"] == _block(store, sid)
    assert d.data["missing_lines"] == _block(store, sid).splitlines()


def test_present_plan_stamps_an_essence_that_embeds_the_block(store, tmp_path, armed):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    _order(store, sid, id="O2", element="automating the inventory", as_="cut",
           reason="named in the order brief as out of scope")

    block = _block(store, sid)
    d = _present(store, sid, f"## Essence\n\nprose first.\n\n{block}\n\nand after.\n", tmp_path)
    assert d.ok is True
    [receipt] = store.load(sid).plan_presentations
    assert receipt.kind == PLAN_PRESENTATION_KIND_ESSENCE
    assert _coverage_blockers(store, sid) == []


def test_the_stage_count_rides_the_block_header(store, tmp_path, armed):
    """Element C: the plan's SIZE reaches the user through the same block, not a
    second mechanism — and the fast-fail therefore enforces it too."""
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    block_lines = _block(store, sid).splitlines()
    header = block_lines[0]
    assert header.startswith("[scope] plan has 2 stage(s);")

    d = _present(store, sid, "- covered: the gate -> stage 1\n", tmp_path)
    assert d.ok is False
    # Every block line the rendering doesn't already carry — the header, plus (when
    # a background enumeration launch is still outstanding, cmd_submit_plan's own
    # detached launch) the in-flight disclosure line coverage_block appends.
    assert d.data["missing_lines"] == [
        line for line in block_lines
        if line.strip() and line.strip() != "- covered: the gate -> stage 1"
    ]


def test_present_plan_refuses_an_essence_when_the_order_bag_is_empty(store, tmp_path, armed):
    """An empty bag still has a block (the header), so the essence still has to
    carry it — and the header then says 0 elements, which is exactly the state
    `validate_order_elements` blocks approve on."""
    sid = _plan_ready(store)
    d = _present(store, sid, "no scope block here\n", tmp_path)
    assert d.ok is False
    # Only the header's own content is this test's concern; a background
    # enumeration launch may still add its own disclosure line as a later one.
    assert d.data["coverage_block"].splitlines()[0] == (
        "[scope] plan has 2 stage(s); order: 0 element(s) — 0 covered, 0 cut")


# --- the gate half: the post-presentation window --------------------------------

def test_gate_goes_red_when_an_element_is_cut_after_presenting(store, tmp_path, armed):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    assert _present(store, sid, _block(store, sid), tmp_path).ok is True
    assert _coverage_blockers(store, sid) == []

    # The plan bytes are untouched, so the receipt's plan_sha256 binding still
    # holds — only the live order bag moved. This is the window the fast-fail
    # cannot see and the gate exists for.
    cli.cmd_order_dispose(
        ns(session=sid, id="O1", as_="cut", reason="dropped after the essence was shown"),
        store=store)
    [blocker] = _coverage_blockers(store, sid)
    assert "present-plan --kind essence" in blocker
    assert "- cut: the gate — dropped after the essence was shown" in blocker

    assert _present(store, sid, _block(store, sid), tmp_path, name="essence2.md").ok is True
    assert _coverage_blockers(store, sid) == []


def test_gate_goes_red_when_a_new_element_is_raised_after_presenting(store, tmp_path, armed):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    assert _present(store, sid, _block(store, sid), tmp_path).ok is True

    _order(store, sid, id="O2", element="the norm sentence", as_="covered", stage=2)
    [blocker] = _coverage_blockers(store, sid)
    assert "order: 2 element(s)" in blocker


# --- both silence guards --------------------------------------------------------

def test_silent_when_plan_presentation_is_inactive(store, tmp_path, monkeypatch):
    """chat / small-change / AGENTCTL_PLAN_PRESENTATION=0: no receipt is required at
    all, so neither half may demand one."""
    monkeypatch.setenv("AGENTCTL_PREMISE", "1")
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "0")
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)

    assert _present(store, sid, "an essence with no scope block\n", tmp_path).ok is True
    assert _coverage_blockers(store, sid) == []


def test_silent_when_no_essence_receipt_exists(store, armed):
    """The absent receipt is gates.plan_presentation_blockers' own refusal, with its
    own route out; double-blocking it here would leave a refusal whose route belongs
    to another gate."""
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    assert store.load(sid).plan_presentations == []
    assert _coverage_blockers(store, sid) == []


def test_silent_when_the_premise_plugin_is_not_armed(store, tmp_path, monkeypatch):
    """No order bag exists, so an arbitrary essence still stamps — the pre-existing
    behaviour every other present-plan test relies on."""
    monkeypatch.setenv("AGENTCTL_PREMISE", "0")
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    sid = _plan_ready(store)
    assert store.load(sid).plugins.get("premise") is None
    assert _present(store, sid, "Summary of the plan.\n", tmp_path).ok is True


# --- the block the gate checks IS the one order-list prints ---------------------

def test_order_list_md_is_what_the_essence_must_carry(store, armed):
    sid = _plan_ready(store)
    _order(store, sid, id="O1", element="the gate", as_="covered", stage=1)
    d = cli.cmd_order_list(ns(session=sid, format="md"), store=store)
    assert d.detail == _block(store, sid)
    # _block (plugins_premise.coverage_block) is render_coverage_block plus, when a
    # background enumeration launch is still outstanding, an appended in-flight
    # disclosure line render_coverage_block itself has no access to (see
    # coverage_block's own docstring) -- so the two agree only on the part
    # render_coverage_block actually produces.
    state = store.load(sid)
    rendered = premise.render_coverage_block(
        premise.order_elements_from_dicts(state.plugins["premise"]["order_elements"]), 2)
    assert d.detail.startswith(rendered)
