"""Review depth follows the plan's shape: `gates.pairwise_review_route` on a SPAWN route.

Pairs review reliance edges, so a one-stage plan, an all-small plan, or a plan with
fewer reliance edges than config.md's `review-pairwise-min-edges` gets one whole-plan
review. Every test sets its own row through a temporary config.md (value >= 2), never
the live value, and calls the predicate through the `gates` module attribute so the
mutation control reaches it.
"""
from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

import pytest

from agentctl import cli
from agentctl import config as agentctl_config
from agentctl import gates, plugins
from agentctl.directive import Directive
from agentctl.render import dispatch_ceiling_chars, render_plan_md
from agentctl.plan import load_plan
from agentctl.state import Node, Route, SessionState, WeightClass
from lib.spawn_prompt_files import composed_prompt_files, skill_path

from test_plan_review_topo import _blockers, _compose, _exit, _record_pairs, make_env  # noqa: F401

ROW = "review-pairwise-min-edges"
THRESHOLD = 3


def _stage(index: int, depends_on: list[int], tier: str = "medium", extra: str = "", prose: str = "") -> str:
    return (
        "[[stage]]\n"
        f"index = {index}\n"
        f'title = "Stage {index}"\n'
        'executor = "spawn:developer"\n'
        f'expected_result_image = "stage {index} output exists{prose}"\n'
        'criterion_type = "measurable"\n'
        f'done_criterion = "check {index} exits 0"\n'
        f"depends_on = {depends_on}\n"
        f'output_artifacts = ["out{index}.py"]\n'
        f'cost_tier = "{tier}"\n'
        f"{extra}\n"
    )


def _plan(tmp_path: Path, deps: list[list[int]], tiers=None, extra: str = "", prose: str = "", name="plan.toml") -> Path:
    head = (
        "[meta]\n"
        'weight_class = "small_change"\n'
        'task_id = "shape"\n'
        'goal = "shape demo"\n'
        'done_criterion = "all stages pass"\n'
        'criterion_type = "measurable"\n\n'
    )
    tiers = tiers or ["medium"] * len(deps)
    body = "".join(_stage(i + 1, d, tiers[i], extra, prose) for i, d in enumerate(deps))
    path = tmp_path / name
    path.write_text(head + body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def row(config_row):
    config_row(ROW, THRESHOLD)


def _state(plan: Path | None, route: str = Route.SPAWN.value) -> SessionState:
    return SessionState(
        session_id="shape1", task_id="t", node=Node.PLAN_READY.value,
        weight_class=WeightClass.SUBSTANTIVE.value, route=route,
        plan_path=str(plan) if plan else None,
    )


def _route(plan: Path, route: str = Route.SPAWN.value) -> bool:
    return gates.pairwise_review_route(_state(plan, route))


CHAIN = [[], [1], [2], [3], [4]]  # n stages -> n-1 reliance edges


def test_one_stage_gets_whole_plan_review(tmp_path):
    assert _route(_plan(tmp_path, [[]])) is False


def test_all_small_gets_whole_plan_review(tmp_path):
    assert _route(_plan(tmp_path, CHAIN, tiers=["small"] * 5)) is False


def test_one_non_small_stage_keeps_pairs(tmp_path):
    assert _route(_plan(tmp_path, CHAIN, tiers=["small"] * 4 + ["medium"])) is True


def test_below_threshold_gives_no_pairs(tmp_path):
    plan = _plan(tmp_path, CHAIN[:THRESHOLD])  # THRESHOLD - 1 edges
    assert gates.plan_shape(load_plan(plan))[1] == THRESHOLD - 1
    assert _route(plan) is False


def test_threshold_edges_gives_pairs(tmp_path):
    plan = _plan(tmp_path, CHAIN[: THRESHOLD + 1])  # THRESHOLD edges
    assert gates.plan_shape(load_plan(plan))[1] == THRESHOLD
    assert _route(plan) is True


def test_prose_length_does_not_move_the_decision(tmp_path):
    prose = " lorem ipsum" * 50
    below = _plan(tmp_path, CHAIN[:THRESHOLD], prose=prose, name="b.toml")
    at = _plan(tmp_path, CHAIN[: THRESHOLD + 1], prose=prose, name="a.toml")
    for plan in (below, at):
        assert len(render_plan_md(load_plan(plan))) < dispatch_ceiling_chars()
    assert (_route(below), _route(at)) == (False, True)


def test_unrecognised_stage_field_does_not_move_the_decision(tmp_path):
    extra = 'kind = "coarse"\nmystery = "x"'
    below = _plan(tmp_path, CHAIN[:THRESHOLD], extra=extra, name="b.toml")
    at = _plan(tmp_path, CHAIN[: THRESHOLD + 1], extra=extra, name="a.toml")
    assert (_route(below), _route(at)) == (False, True)


def test_ceiling_forces_pairs_on_spawn_only(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [[]])
    monkeypatch.setattr("agentctl.render.dispatch_ceiling_chars", lambda model=None: 10)
    assert _route(plan) is True
    assert _route(plan, route=Route.IN_THREAD.value) is False


def test_non_spawn_route_never_pairs(tmp_path):
    assert _route(_plan(tmp_path, CHAIN), route=Route.IN_THREAD.value) is False


def test_altered_row_moves_the_boundary(tmp_path, config_row):
    plan = _plan(tmp_path, CHAIN[:3])  # 2 edges
    assert _route(plan) is False
    config_row(ROW, 2)
    assert _route(plan) is True


@pytest.mark.parametrize("line", [None, f"| `{ROW}` | `many` | test |"])
def test_missing_or_non_integer_row_gives_pairs(tmp_path, config_row, line):
    plan = _plan(tmp_path, CHAIN[:2])
    config_row(ROW, line)
    assert _route(plan) is True


def test_unloadable_plan_gives_pairs(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("not [valid", encoding="utf-8")
    assert _route(bad) is True
    assert gates.pairwise_review_route(_state(tmp_path / "missing.toml")) is True


def test_one_stage_wins_over_a_missing_row(tmp_path, config_row):
    config_row(ROW, None)
    assert _route(_plan(tmp_path, [[]])) is False


def test_ceiling_wins_over_one_stage(tmp_path, monkeypatch):
    monkeypatch.setattr("agentctl.render.dispatch_ceiling_chars", lambda model=None: 10)
    assert _route(_plan(tmp_path, [[]])) is True


def test_below_threshold_directive_names_whole_plan_review(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REVIEW_DISPATCH", "1")
    state = _state(_plan(tmp_path, CHAIN[:THRESHOLD]))
    plugins.activate(state, "review_dispatch")
    fired = plugins.fire("submit_plan", state, Directive(True, state.node, "noop"))
    (directive,) = [p for p in fired if p["plugin"] == "review_dispatch"]
    assert directive["data"]["mode"] == "whole"


def _coverage_calls(monkeypatch) -> list:
    calls: list = []
    real = gates._plan_review_blockers_coverage

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(gates, "_plan_review_blockers_coverage", spy)
    return calls


def test_non_spawn_session_with_pair_records_takes_the_coverage_path(make_env, monkeypatch):
    env = make_env()
    _record_pairs(env)
    assert gates.has_pair_records_for(env.state(), str(env.plan))
    assert env.state().route != Route.SPAWN.value
    calls = _coverage_calls(monkeypatch)
    _blockers(env)
    assert calls


def test_below_threshold_plan_with_pair_records_clears_with_one_whole_plan_pass(make_env, monkeypatch):
    env = make_env()
    env.mutate_state(lambda s: setattr(s, "route", Route.SPAWN.value))
    _record_pairs(env)
    assert gates.has_pair_records_for(env.state(), str(env.plan))
    assert gates.whole_plan_by_shape(env.doc())
    assert env.record("", "pass").ok
    calls = _coverage_calls(monkeypatch)
    assert _blockers(env) == []
    assert not calls


def _spawn_env_with_later_pair_revise(make_env, *, compose=False, monkeypatch=None) -> list[str]:
    """Whole-by-shape spawn plan with every pair recorded but 3-1, then a whole-plan pass
    (a composed one when `compose`), then a pair revise on 3-1 recorded after it; returns
    the gate's blockers. Real records throughout."""
    env = make_env()
    env.mutate_state(lambda s: setattr(s, "route", Route.SPAWN.value))
    assert gates.whole_plan_by_shape(env.doc())
    if compose:
        env.record_all()
        monkeypatch.setattr(cli, "_record_first_thinker_verdict", lambda *a, **k: None)
        assert _compose(env).ok
    else:
        _record_pairs(env, skip=("3-1",))
        assert env.record("", "pass").ok
    assert env.record(
        "topo:3-1", "revise", concerns=["blocking: needs work"],
        regression_command="check", runner=_exit(1),
    ).ok
    return _blockers(env)


def test_pair_revise_after_a_whole_plan_pass_blocks(make_env):
    assert any("3-1" in blocker for blocker in _spawn_env_with_later_pair_revise(make_env))


def test_pair_revise_after_compose_blocks_when_ceiling_makes_the_predicate_say_pairs(make_env, monkeypatch):
    monkeypatch.setattr("agentctl.render.dispatch_ceiling_chars", lambda model=None: 10)
    blockers = _spawn_env_with_later_pair_revise(make_env, compose=True, monkeypatch=monkeypatch)
    assert any("3-1" in blocker for blocker in blockers)


def test_old_pair_revise_is_superseded_by_a_fresh_whole_plan_pass(make_env):
    env = make_env()
    env.mutate_state(lambda s: setattr(s, "route", Route.SPAWN.value))
    _record_pairs(env, skip=("3-1",))
    assert env.record("topo:3-1", "revise", concerns=["blocking: needs work"]).ok
    assert _blockers(env)
    assert env.record("", "pass").ok
    assert _blockers(env) == []


def test_mutation_gate_discarding_newer_pair_records_is_caught(make_env):
    source = textwrap.dedent(inspect.getsource(gates._honours_pair_records))
    assert "record.record_seq > whole.record_seq" in source
    namespace = dict(vars(gates))
    exec(source.replace("record.record_seq > whole.record_seq", "False"), namespace)
    real = gates._honours_pair_records
    try:
        gates._honours_pair_records = namespace["_honours_pair_records"]
        blockers = _spawn_env_with_later_pair_revise(make_env)
    finally:
        gates._honours_pair_records = real
    assert not any("3-1" in blocker for blocker in blockers), "the mutant must lose the pair-3-1 blocker"


def test_unreadable_reader_context_overhead_reads_pairs(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [[]])
    assert _route(plan) is False
    monkeypatch.setattr(gates, "composed_prompt_files", lambda skill: [tmp_path / "missing.md"])
    assert gates._whole_plan_reader_context_overhead_chars() is None
    assert _route(plan) is True


def test_ceiling_counts_the_reader_context_overhead(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [[]])
    size = len(render_plan_md(load_plan(plan)))
    overhead = gates._whole_plan_reader_context_overhead_chars()
    assert overhead
    assert overhead == sum(p.stat().st_size for p in composed_prompt_files(skill_path("thinker")))
    monkeypatch.setattr("agentctl.render.dispatch_ceiling_chars", lambda model=None: size + overhead)
    assert _route(plan) is False
    monkeypatch.setattr("agentctl.render.dispatch_ceiling_chars", lambda model=None: size + overhead - 1)
    assert _route(plan) is True


def test_mutation_route_only_is_caught(tmp_path, monkeypatch):
    spawn = Route.SPAWN.value
    monkeypatch.setattr(
        gates, "pairwise_review_route",
        lambda state, doc=None, **kw: state is not None and state.route == spawn,
    )
    with pytest.raises(AssertionError):
        test_below_threshold_gives_no_pairs(tmp_path)
