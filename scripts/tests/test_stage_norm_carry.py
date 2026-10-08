"""PASSED carry-forward decided by `stage_norm.stage_carried`, at both carry sites.

What must hold: a PASSED stage is carried iff its own carry digest is unchanged AND the
interface digest of every direct supplier is unchanged. A supplier's construction
(method) is not its interface, so a construction-only change keeps its consumers; a
supplier's result image or an edge's delivery resets them. The same decision is made at
the approve-time refresh (`_refresh_caches_from_plan_path`) and at the substantive replan
(`cmd_replan`); the harness below drives both with the same plan pair and compares.

New symbols are referenced inside test bodies only, so on a tree without them the file
fails on assertions rather than at collection.
"""
from __future__ import annotations

from argparse import Namespace

import pytest

from agentctl import cli
from agentctl.state import SessionState, StageStatus

PASSED = StageStatus.PASSED.value
PENDING = StageStatus.PENDING.value


def _plan(*, s1_result="supplier image", s1_method="build it", s2_method="test it",
          s2_edge='{ on = 1 }', with_edge=True, extra_stage=False):
    edge = f"supplies = [{s2_edge}]\n" if with_edge else ""
    text = (
        '[meta]\nweight_class = "small_change"\ntask_id = "demo-two-stage"\n'
        'goal = "g"\ndone_criterion = "all"\ncriterion_type = "measurable"\n\n'
        '[[stage]]\nindex = 1\ntitle = "Supplier"\nexecutor = "spawn:developer"\n'
        f'expected_result_image = "{s1_result}"\ncriterion_type = "measurable"\n'
        f'done_criterion = "supplier done"\nmethod = "{s1_method}"\n'
        'output_artifacts = ["mod.py"]\n\n'
        '[[stage]]\nindex = 2\ntitle = "Consumer"\nexecutor = "spawn:developer"\n'
        'expected_result_image = "consumer image"\ncriterion_type = "measurable"\n'
        f'done_criterion = "consumer done"\nmethod = "{s2_method}"\n'
        f'depends_on = [1]\n{edge}'
    )
    if extra_stage:
        text += (
            '\n[[stage]]\nindex = 3\ntitle = "Third"\nexecutor = "spawn:developer"\n'
            'expected_result_image = "third image"\ncriterion_type = "measurable"\n'
            'done_criterion = "third done"\ndepends_on = [2]\n'
        )
    return text


def _ns(**kw):
    return Namespace(**kw)


def _statuses_at_refresh(tmp_path, old_text, new_text):
    path = tmp_path / "refresh.toml"
    path.write_text(old_text)
    state = SessionState(session_id="s1", task_id="demo-two-stage", plan_path=str(path))
    state.stages = list(cli.load_plan(str(path)).stages)
    for s in state.stages:
        s.outcome.status = PASSED
    path.write_text(new_text)
    assert cli._refresh_caches_from_plan_path(state) == []
    return {s.index: s.outcome.status for s in state.stages}


def _statuses_at_replan(store, tmp_path, old_text, new_text):
    from test_replan import _to_executing_stage1

    old = tmp_path / "old.toml"
    old.write_text(old_text)
    sid = "carry-site"
    _to_executing_stage1(store, sid, str(old))
    state = store.load(sid)
    for s in state.stages:
        s.outcome.status = PASSED
    store.save(state)
    new = tmp_path / "new.toml"
    new.write_text(new_text)
    d = cli.cmd_replan(_ns(session=sid, plan=str(new)), store=store)
    assert d.marker == "PLAN-READY"
    return {s.index: s.outcome.status for s in store.load(sid).stages
            if s.index in (1, 2)}


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture(params=["approve-refresh", "substantive-replan"])
def statuses(request, store, tmp_path):
    """Run the plan pair through one carry site; a third stage keeps the replan
    substantive (a structural change) and is inert at the refresh."""
    def run(old_kwargs, new_kwargs):
        old, new = _plan(**old_kwargs), _plan(extra_stage=True, **new_kwargs)
        if request.param == "approve-refresh":
            return _statuses_at_refresh(tmp_path, old, new)
        return _statuses_at_replan(store, tmp_path, old, new)
    return run


def test_unchanged_stages_with_unchanged_suppliers_are_carried(statuses):
    assert statuses({}, {}) == {1: PASSED, 2: PASSED}


def test_a_stage_whose_own_norm_changed_is_reset(statuses):
    got = statuses({}, {"s2_method": "test it differently"})
    assert got == {1: PASSED, 2: PENDING}


def test_a_suppliers_construction_only_change_carries_its_consumer(statuses):
    got = statuses({}, {"s1_method": "build it another way"})
    assert got == {1: PENDING, 2: PASSED}


def test_a_suppliers_interface_change_resets_its_consumer(statuses):
    got = statuses({}, {"s1_result": "a different supplier image"})
    assert got == {1: PENDING, 2: PENDING}


def test_an_edges_delivery_change_resets_the_consumer(statuses):
    got = statuses({"s2_edge": '{ on = 1, delivery = "report" }'},
                   {"s2_edge": '{ on = 1, delivery = "continuation" }'})
    assert got == {1: PASSED, 2: PENDING}


def test_a_plan_without_typed_edges_carries_as_before(statuses):
    assert statuses({"with_edge": False}, {"with_edge": False}) == {1: PASSED, 2: PASSED}
    got = statuses({"with_edge": False}, {"with_edge": False, "s2_method": "other"})
    assert got == {1: PASSED, 2: PENDING}


def test_a_supplier_absent_from_the_previous_plan_counts_as_changed():
    from agentctl.plan import parse_plan
    from agentctl.stage_norm import stage_carried

    base = {"index": 1, "title": "t", "executor": "spawn:developer",
            "expected_result_image": "r", "done_criterion": "d"}
    consumer = dict(base, index=2, title="c", supplies=[{"on": 1}])
    new = parse_plan({"meta": {"task_id": "t"}, "stage": [base, consumer]})
    assert stage_carried(new.stages, new.stages, 2) is True
    assert stage_carried(new.stages[1:], new.stages, 2) is False
