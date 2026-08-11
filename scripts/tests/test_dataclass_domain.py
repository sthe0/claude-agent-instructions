"""Direct, synthetic-dataclass tests for `dataclass_domain.py`'s two helpers.

`test_contract_coverage.py` and `test_renormalization.py` both exercise `leaf_paths`
only through `agentctl.state.Stage`'s actual shape — which never puts a dataclass
behind a `dict[str, X]` or `tuple[X, ...]` field, and never reaches the same nested
type by two different paths. Those are exactly the two gaps a stage-10 review found
in `_unwrap` (dict/tuple unhandled) and `dataclasses_reached` (cycle-dedup broken by
a fresh `seen` list per recursive call) — real for the module's stated general
contract even though `Stage` today never triggers either. This file constructs the
minimal synthetic shapes that do, so both are pinned independent of whether `Stage`
ever grows a field of that shape.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from dataclass_domain import CyclicDataclassError, dataclasses_reached, leaf_paths


@dataclass
class _Inner:
    value: str


@dataclass
class _HasDict:
    by_key: dict[str, _Inner]


@dataclass
class _HasVariadicTuple:
    items: tuple[_Inner, ...]


@dataclass
class _HasFixedTuple:
    pair: tuple[int, int]


@dataclass
class _Leaf:
    value: str


@dataclass
class _Mid:
    leaf: _Leaf


@dataclass
class _Diamond:
    """Two fields of the SAME nested type, reached by two different paths.
    Dedup at the point each recursive call's result is merged back into the
    caller's `seen` already covers this shape even under the old fresh-
    `seen`-per-call code — it is not what the review's SHOULD-FIX 5 flagged.
    The genuine reproduction is `_CycleA`/`_CycleB` below."""
    left: _Mid
    right: _Mid


@dataclass
class _CycleA:
    b: "_CycleB"


@dataclass
class _CycleB:
    """A -> B -> A: a real cycle in the type graph, unlike the diamond
    above's shared-descendant DAG. The old fresh-`seen`-per-call code's inner
    call for `_CycleB` starts a `seen` list containing only `_CycleB` — it
    has no memory that `_CycleA` is an ancestor — so it recurses back into
    `dataclasses_reached(_CycleA)`, which does the same in the other
    direction, without terminating."""
    a: "_CycleA | None" = None


def test_leaf_paths_recurses_through_a_dict_value_type():
    assert leaf_paths(_HasDict) == ("by_key.value",)


def test_leaf_paths_recurses_through_a_variadic_tuple_element_type():
    assert leaf_paths(_HasVariadicTuple) == ("items.value",)


def test_leaf_paths_treats_a_fixed_heterogeneous_tuple_as_a_single_leaf():
    """`tuple[int, int]` has no single nested type to unwrap to — it is a leaf,
    the same status `Order.requirements_dropped: tuple[int, int] | None` has in
    the real domain."""
    assert leaf_paths(_HasFixedTuple) == ("pair",)


def test_dataclasses_reached_records_a_diamond_reached_type_exactly_once():
    reached = dataclasses_reached(_Diamond)
    assert reached == (_Diamond, _Mid, _Leaf)
    assert len(reached) == len(set(reached)), (
        f"a type reachable by two paths was recorded more than once: {reached}"
    )


def test_dataclasses_reached_terminates_on_a_two_hop_cycle():
    assert dataclasses_reached(_CycleA) == (_CycleA, _CycleB)


def test_leaf_paths_descends_a_diamond_on_both_branches():
    """The control on `leaf_paths`' cycle guard being PATH-local: a global `seen`
    would terminate the cycle too, and silently drop `right.leaf.value` as
    already-visited. A path yielded per branch is the property that forbids it."""
    assert leaf_paths(_Diamond) == ("left.leaf.value", "right.leaf.value")


def test_leaf_paths_refuses_a_two_hop_cycle_by_name():
    """`dataclasses_reached` terminates on `_CycleA` and returns; `leaf_paths` has no
    finite answer to return, so it refuses. Before this guard it recursed to an
    opaque `RecursionError` — an asymmetry with its sibling that the module's own
    docstring, promising one shared traversal, did not admit."""
    with pytest.raises(CyclicDataclassError) as excinfo:
        leaf_paths(_CycleA)
    assert "b.a" in str(excinfo.value)
