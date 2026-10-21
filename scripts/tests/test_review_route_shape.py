"""Review depth follows the plan's shape: `gates.pairwise_review_route` on a SPAWN route.

Pairs review reliance edges, so a one-stage plan, an all-small plan, or a plan with
fewer reliance edges than config.md's `review-pairwise-min-edges` gets one whole-plan
review. Every test sets its own row through a temporary config.md (value >= 2), never
the live value, and calls the predicate through the `gates` module attribute so the
mutation control reaches it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentctl import config as agentctl_config
from agentctl import gates, plugins
from agentctl.directive import Directive
from agentctl.render import dispatch_ceiling_chars, render_plan_md
from agentctl.plan import load_plan
from agentctl.state import Node, Route, SessionState, WeightClass

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


def _set_row(tmp_path, monkeypatch, line: str | None):
    lines = [
        entry for entry in agentctl_config.CONFIG_MD.read_text(encoding="utf-8").splitlines()
        if not entry.startswith(f"| `{ROW}` |")
    ]
    if line is not None:
        lines.append(line)
    copy = tmp_path / "config.md"
    copy.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(agentctl_config, "CONFIG_MD", copy)


@pytest.fixture(autouse=True)
def row(tmp_path, monkeypatch):
    _set_row(tmp_path, monkeypatch, f"| `{ROW}` | `{THRESHOLD}` | test |")


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


def test_altered_row_moves_the_boundary(tmp_path, monkeypatch):
    plan = _plan(tmp_path, CHAIN[:3])  # 2 edges
    assert _route(plan) is False
    _set_row(tmp_path, monkeypatch, f"| `{ROW}` | `2` | test |")
    assert _route(plan) is True


@pytest.mark.parametrize("line", [None, f"| `{ROW}` | `many` | test |"])
def test_missing_or_non_integer_row_gives_pairs(tmp_path, monkeypatch, line):
    plan = _plan(tmp_path, CHAIN[:2])
    _set_row(tmp_path, monkeypatch, line)
    assert _route(plan) is True


def test_unloadable_plan_gives_pairs(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("not [valid", encoding="utf-8")
    assert _route(bad) is True
    assert gates.pairwise_review_route(_state(tmp_path / "missing.toml")) is True


def test_one_stage_wins_over_a_missing_row(tmp_path, monkeypatch):
    _set_row(tmp_path, monkeypatch, None)
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


def test_below_threshold_plan_with_pair_records_clears_with_one_whole_plan_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    plan = _plan(tmp_path, CHAIN[:THRESHOLD])
    state = _state(plan)
    seen = {}
    monkeypatch.setattr(gates, "has_pair_records_for", lambda s, p: True)
    monkeypatch.setattr(
        gates, "_plan_review_blockers_coverage",
        lambda *a, **k: seen.setdefault("coverage", ["pairs"]),
    )
    monkeypatch.setattr(gates, "_plan_review_blockers_whole", lambda *a, **k: [])
    assert gates.plan_review_blockers(state, str(plan)) == []
    assert "coverage" not in seen


def test_mutation_route_only_is_caught(tmp_path, monkeypatch):
    spawn = Route.SPAWN.value
    monkeypatch.setattr(
        gates, "pairwise_review_route",
        lambda state, doc=None, **kw: state is not None and state.route == spawn,
    )
    with pytest.raises(AssertionError):
        test_below_threshold_gives_no_pairs(tmp_path)
