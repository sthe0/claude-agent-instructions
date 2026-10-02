"""An optional `guard_exempt_paths` stage field (TOML flat key -> Actor) that
dispatch forwards to spawn-specialist.py as repeated `--guard-exempt` flags.

Covers the declaration side: the Actor default, both Stage.from_dict shapes, the
TOML parser, and the engine paths that would otherwise silently drop an
exemption added to an already-approved plan (the refinement diff, the live-stage
refresh, and the renormalization residual)."""
from __future__ import annotations

from agentctl.cli import _apply_refined_stage_fields
from agentctl.gates import _renorm_stage_residual
from agentctl.plan import diff_plans, parse_plan
from agentctl.state import Actor, Stage


def _stage_dict(index=1, **overrides):
    base = {
        "index": index, "title": "s", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": "do",
    }
    base.update(overrides)
    return base


def _doc(stages):
    return parse_plan({"meta": {"task_id": "t"}, "stage": stages})


def test_actor_default_is_an_empty_list_not_shared_between_instances():
    a, b = Actor(executor="in_thread"), Actor(executor="in_thread")
    assert a.guard_exempt_paths == []
    assert a.guard_exempt_paths is not b.guard_exempt_paths


def test_from_dict_flat_shape_reads_the_field_and_defaults_to_empty():
    flat = {
        "index": 1, "title": "t", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
    }
    assert Stage.from_dict(dict(flat)).actor.guard_exempt_paths == []
    declared = Stage.from_dict({**flat, "guard_exempt_paths": ["a/b.json"]})
    assert declared.actor.guard_exempt_paths == ["a/b.json"]


def test_from_dict_nested_shape_carries_the_field_through_the_actor_splat():
    nested = Stage.from_dict({
        "index": 1, "title": "t",
        "subject": {"material": "m", "result": "r"},
        "means": {"means": "Edit", "method": "do"},
        "actor": {"executor": "in_thread", "guard_exempt_paths": ["a/b.json"]},
        "criterion": {"criterion_type": "measurable", "done_criterion": "dc"},
    })
    assert nested.actor.guard_exempt_paths == ["a/b.json"]


def test_toml_parser_reads_the_array_and_defaults_to_empty():
    declared = _doc([_stage_dict(guard_exempt_paths=["x/y.json", "x/z.json"])]).stages[0]
    assert declared.actor.guard_exempt_paths == ["x/y.json", "x/z.json"]
    assert _doc([_stage_dict()]).stages[0].actor.guard_exempt_paths == []


def test_an_exemption_only_edit_is_a_refinement_not_a_no_change():
    old = _doc([_stage_dict()])
    new = _doc([_stage_dict(guard_exempt_paths=["x/y.json"])])
    assert diff_plans(old, new) == "refinement"
    assert diff_plans(old, old) == "no_change"


def test_refresh_copies_declared_and_cleared_exemptions_onto_the_live_stage():
    cur = _doc([_stage_dict()]).stages[0]
    declared = _doc([_stage_dict(guard_exempt_paths=["x/y.json"])]).stages[0]
    _apply_refined_stage_fields(cur, declared)
    assert cur.actor.guard_exempt_paths == ["x/y.json"]
    assert cur.actor.guard_exempt_paths is not declared.actor.guard_exempt_paths
    _apply_refined_stage_fields(cur, _doc([_stage_dict()]).stages[0])
    assert cur.actor.guard_exempt_paths == []


def test_the_renormalization_residual_sees_an_exemption():
    bare = _doc([_stage_dict()]).stages[0]
    declared = _doc([_stage_dict(guard_exempt_paths=["x/y.json"])]).stages[0]
    assert _renorm_stage_residual(bare) != _renorm_stage_residual(declared)
