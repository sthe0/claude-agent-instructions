"""Partition step: the pure M1–M4 truth-table + the cmd_partition transition
gating EXECUTING behind an assessment on the spawn route."""
from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.partition import render_section, render_units, unit_delivery_order, verdict
from agentctl.state import Node, PartitionUnit


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


# --- per-unit delivery routing (units) -----------------------------------

def _part(sid, store, fixtures_dir, **kw):
    """Run cmd_partition with default markers off unless overridden."""
    base = dict(m1=False, m2=False, m3=False, m4=False, m3_severe=False,
                m4_severe=False, unit=None)
    base.update(kw)
    return cli.cmd_partition(ns(session=sid, **base), store=store)


def test_unit_parsing_full_spec_incl_optional_ref():
    known = {1, 2}
    units, errors = cli._parse_partition_units(
        ["inline|1|Core work", "subtask|2|Follow-up|ABC-42"], known)
    assert errors == []
    assert units[0] == PartitionUnit(title="Core work", stages=[1], mode="inline", ref=None)
    assert units[1] == PartitionUnit(title="Follow-up", stages=[2], mode="subtask", ref="ABC-42")


def test_unit_unknown_mode_rejected():
    _, errors = cli._parse_partition_units(["bogus|1|X"], {1})
    assert any("unknown mode" in e for e in errors)


def test_unit_missing_stage_index_rejected():
    _, errors = cli._parse_partition_units(["inline|99|X"], {1, 2})
    assert any("does not exist" in e for e in errors)


def test_unit_overlapping_stages_rejected():
    _, errors = cli._parse_partition_units(["inline|1|A", "spawn|1|B"], {1, 2})
    assert any("disjoint" in e for e in errors)


def test_unit_malformed_spec_rejected():
    _, errors = cli._parse_partition_units(["inline|1"], {1})  # missing title field
    assert any("expected" in e for e in errors)


def test_partition_records_units_and_renders_delivery_order(store, fixtures_dir):
    sid = "u1"
    _to_approved(store, sid, fixtures_dir)
    # stage 2 depends_on [1] -> unit 2 must be delivered "after unit 1"
    d = _part(sid, store, fixtures_dir, m1=True, m2=True,
              unit=["inline|1|Core", "spawn|2|Tests"])
    assert d.ok
    section = d.data["section"]
    assert "Units:" in section
    assert "[inline] Core (stages: 1)" in section
    assert "[spawn] Tests (stages: 2)" in section
    assert "after unit 1" in section  # derived cross-unit delivery order

    state = store.load(sid)
    assert [u.mode for u in state.partition.units] == ["inline", "spawn"]
    assert state.partition.units[1].stages == [2]


def test_partition_invalid_units_fails_and_records_nothing(store, fixtures_dir):
    sid = "u2"
    _to_approved(store, sid, fixtures_dir)
    d = _part(sid, store, fixtures_dir, m1=True, unit=["nope|1|Bad"])
    assert d.ok is False
    assert d.action == "fix_units"
    state = store.load(sid)
    assert state.node == Node.APPROVED.value  # no transition on invalid units
    assert state.partition is None            # nothing recorded


def test_partition_no_units_renders_byte_identical(store, fixtures_dir):
    sid = "u3"
    _to_approved(store, sid, fixtures_dir)
    d = _part(sid, store, fixtures_dir, m1=True, m2=True)
    assert "Units:" not in d.data["section"]
    assert d.data["section"] == render_section(True, True, False, False, False, False,
                                               "recommended")


def test_partition_units_node_gating(store, fixtures_dir):
    sid = "u4"
    _to_approved(store, sid, fixtures_dir)
    # before partition (node APPROVED) partition-units is refused
    d = cli.cmd_partition_units(ns(session=sid, unit=["inline|1|A"]), store=store)
    assert d.ok is False and d.action == "noop"
    # after partition (node PARTITIONED) it is allowed and replaces the list
    _part(sid, store, fixtures_dir, m1=True, m2=True, unit=["inline|1|Old"])
    d2 = cli.cmd_partition_units(
        ns(session=sid, unit=["inline|1|Core", "subtask|2|Split|ABC-9"]), store=store)
    assert d2.ok
    state = store.load(sid)
    assert [u.title for u in state.partition.units] == ["Core", "Split"]
    assert state.partition.units[1].ref == "ABC-9"


def test_partition_units_rejects_invalid_and_keeps_prior(store, fixtures_dir):
    sid = "u5"
    _to_approved(store, sid, fixtures_dir)
    _part(sid, store, fixtures_dir, m1=True, m2=True, unit=["inline|1|Keep"])
    d = cli.cmd_partition_units(ns(session=sid, unit=["inline|99|Bad"]), store=store)
    assert d.ok is False and d.action == "fix_units"
    state = store.load(sid)
    assert [u.title for u in state.partition.units] == ["Keep"]  # unchanged


def test_unit_delivery_order_pure():
    units = [PartitionUnit(title="a", stages=[1]), PartitionUnit(title="b", stages=[2])]
    order = unit_delivery_order(units, {1: [], 2: [1]})
    assert order == [[], [1]]  # unit 2 depends on unit 1


def test_render_units_empty_is_blank():
    assert render_units([], {}) == ""
