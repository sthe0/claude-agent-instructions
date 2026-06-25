"""Partition step: the pure M1–M4 truth-table + the cmd_partition transition
gating EXECUTING behind an assessment on the spawn route."""
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.partition import render_section, verdict
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


# --- pure truth-table ----------------------------------------------------

@pytest.mark.parametrize("m1,m2,m3,m4,expected", [
    (False, False, False, False, "not_required"),  # no marker -> one PR
    (True, False, False, False, "possible"),        # M1 alone, no other -> possible
    (False, True, False, False, "possible"),        # a marker but not the M1 combo
    (False, False, True, False, "possible"),
    (False, False, False, True, "possible"),
    (True, True, False, False, "recommended"),      # M1 ∧ other -> recommended
    (True, False, True, False, "recommended"),
    (True, False, False, True, "recommended"),
    (True, True, True, True, "recommended"),
    (False, True, True, True, "possible"),          # several others but no M1
])
def test_verdict_truth_table(m1, m2, m3, m4, expected):
    assert verdict(m1, m2, m3, m4) == expected


def test_verdict_covers_all_sixteen_combos():
    seen = {verdict(bool(i & 1), bool(i & 2), bool(i & 4), bool(i & 8)) for i in range(16)}
    assert seen == {"not_required", "possible", "recommended"}


@pytest.mark.parametrize("m3_severe,m4_severe", [(True, False), (False, True), (True, True)])
def test_severity_forces_recommended_even_without_m1(m3_severe, m4_severe):
    # severe M3/M4 overrides the combo rule — recommended even with all base markers off
    assert verdict(False, False, False, False, m3_severe, m4_severe) == "recommended"


def test_render_section_shape():
    section = render_section(True, False, True, False, verdict_value="recommended")
    assert section.startswith("## Partition")
    assert "Verdict: recommended" in section
    assert "M1 independent deliverables: yes" in section
    assert "M3 blocking deps: yes (severe: no)" in section


# --- cmd_partition transition --------------------------------------------

def _to_approved(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)


def test_partition_records_state_and_advances(store, fixtures_dir):
    sid = "dc1"
    _to_approved(store, sid, fixtures_dir)
    d = cli.cmd_partition(ns(session=sid, m1=True, m2=True, m3=False, m4=False,
                             m3_severe=False, m4_severe=False), store=store)
    assert d.ok
    assert d.node == Node.PARTITIONED.value
    assert d.data["verdict"] == "recommended"
    assert "## Partition" in d.data["section"]

    state = store.load(sid)
    assert state.partition is not None
    assert state.partition.verdict == "recommended"
    assert state.partition.m1 is True


def test_partition_not_required_advances_to_next_stage(store, fixtures_dir):
    sid = "dc2"
    _to_approved(store, sid, fixtures_dir)
    d = cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                             m3_severe=False, m4_severe=False), store=store)
    assert d.data["verdict"] == "not_required"
    assert d.action == "next_stage"


def test_partition_recommended_surfaces_to_user(store, fixtures_dir):
    sid = "dc3"
    _to_approved(store, sid, fixtures_dir)
    d = cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                             m3_severe=True, m4_severe=False), store=store)
    assert d.data["verdict"] == "recommended"
    assert d.action == "surface_partition"


def test_partition_refused_off_approved(store, fixtures_dir):
    sid = "dc4"
    cli.cmd_start(ns(session=sid, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    d = cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                             m3_severe=False, m4_severe=False), store=store)
    assert d.ok is False
    assert d.action == "noop"
    assert store.load(sid).node == Node.CLASSIFIED.value  # node unchanged
