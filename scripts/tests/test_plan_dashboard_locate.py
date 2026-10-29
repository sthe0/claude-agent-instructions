"""Tests for the locator seam's ``inputs_stamp`` hook: a raising or malformed stamp is absent."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from plan_dashboard import locate


def locator_returning(value):
    return SimpleNamespace(NAME="fake", inputs_stamp=lambda: value)


def raising():
    raise RuntimeError("event log unreadable")


@pytest.mark.parametrize("value, stamp", [
    ([12, 345], (12, 345)), ((12, 345), (12, 345)), (7, (7,)), ("v3", ("v3",)), (["a", 1], ("a", 1)),
])
def test_a_well_formed_stamp_is_returned_hashable(value, stamp):
    result = locate.inputs_stamp(locator_returning(value))
    assert result == stamp
    assert hash(result) == hash(stamp)


@pytest.mark.parametrize("value", [None, True, 1.5, [], [1, None], [[1]], {"mtime": 1}, [True, 2], b"x"])
def test_a_malformed_stamp_is_absent(value):
    assert locate.inputs_stamp(locator_returning(value)) is None


def test_a_raising_or_missing_stamp_is_absent():
    assert locate.inputs_stamp(SimpleNamespace(NAME="fake", inputs_stamp=raising)) is None
    assert locate.inputs_stamp(SimpleNamespace(NAME="fake")) is None
    assert locate.inputs_stamp(None) is None


def test_inputs_stamp_is_a_named_hook():
    assert "inputs_stamp" in locate.defined_hooks(locator_returning(1))
