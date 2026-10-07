"""A stage that declares both `supplies` and `depends_on` keeps both kinds of edge:
the typed supplies as written, plus an element-less edge for each depends_on index
no supply already names. Plans declaring only one of the two are unchanged."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentctl.plan import PlanError, load_plan

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"


def _stage_block(index: int, *, depends_on=None, supplies=()) -> str:
    lines = [
        "[[stage]]", f"index = {index}", f'title = "Stage {index}"',
        'executor = "spawn:developer"',
        'expected_result_image = "the stage artifact is on disk"',
        'criterion_type = "measurable"', 'done_criterion = "the artifact exists"',
    ]
    if depends_on is not None:
        lines.append(f"depends_on = {list(depends_on)}")
    for on in supplies:
        lines += ["[[stage.supplies]]", f"on = {on}"]
    return "\n".join(lines) + "\n"


def _plan(tmp_path: Path, *blocks: str):
    head = FIXTURE.read_text(encoding="utf-8").partition("\n[[stage]]")[0]
    path = tmp_path / "plan.toml"
    path.write_text(head + "\n" + "\n".join(blocks), encoding="utf-8")
    return load_plan(str(path), strict=False)


def _edges(doc, index: int) -> set[int]:
    return {s.on for s in next(st for st in doc.stages if st.index == index).supplies}


def test_depends_on_survives_alongside_supplies(tmp_path):
    doc = _plan(tmp_path, _stage_block(1, depends_on=[]), _stage_block(2, depends_on=[]),
                _stage_block(3, depends_on=[1], supplies=[2]))
    assert _edges(doc, 3) == {1, 2}
    assert set(next(st for st in doc.stages if st.index == 3).depends_on) == {1, 2}


def test_supply_naming_a_depends_on_index_is_not_duplicated(tmp_path):
    doc = _plan(tmp_path, _stage_block(1, depends_on=[]), _stage_block(2, depends_on=[1], supplies=[1]))
    stage = next(st for st in doc.stages if st.index == 2)
    assert [s.on for s in stage.supplies] == [1]


def test_supplies_only_and_depends_on_only_plans_are_unchanged(tmp_path):
    doc = _plan(tmp_path, _stage_block(1, depends_on=[]), _stage_block(2, supplies=[1]),
                _stage_block(3, depends_on=[2]))
    assert _edges(doc, 2) == {1}
    assert _edges(doc, 3) == {2}


def test_cycle_formed_only_through_the_union_is_rejected(tmp_path):
    with pytest.raises(PlanError):
        _plan(tmp_path, _stage_block(1, depends_on=[3], supplies=[4]), _stage_block(2, supplies=[1]),
              _stage_block(3, supplies=[2]), _stage_block(4, depends_on=[]))
