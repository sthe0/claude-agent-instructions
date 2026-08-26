"""Stage 5 / item A: the standalone `RoundReleaseCounter` / `compute_cross_axis_ceiling`
primitive (agentctl.round_release), tested in isolation from any axis that wires it up.
Per-axis behavior via `gates.py`'s three counters is covered by
test_gates_round_release.py; this module locks the primitive's own contract so a future
axis can reuse it with confidence."""
from __future__ import annotations

from agentctl.config import Thresholds
from agentctl.round_release import RoundReleaseCounter, compute_cross_axis_ceiling


class _Subject:
    def __init__(self, rounds):
        self.rounds = rounds


_COUNTER = RoundReleaseCounter(name="demo", getter=lambda s: s.rounds)


# --- RoundReleaseCounter.value ------------------------------------------------

def test_value_reads_via_the_getter():
    assert _COUNTER.value(_Subject(rounds=4)) == 4


def test_value_is_zero_for_none_subject():
    assert _COUNTER.value(None) == 0


def test_value_coerces_a_none_count_to_zero():
    """A getter that reads an unset/legacy field can itself return None (e.g. a dict
    `.get()` on a missing key) — value() must degrade that to 0, not raise or propagate
    None to a numeric comparison downstream."""
    assert _COUNTER.value(_Subject(rounds=None)) == 0


# --- RoundReleaseCounter.release_active ---------------------------------------

def test_release_inactive_below_threshold():
    assert _COUNTER.release_active(_Subject(rounds=0)) is False
    assert _COUNTER.release_active(_Subject(rounds=2)) is False


def test_release_active_at_and_past_threshold():
    assert _COUNTER.release_active(_Subject(rounds=3)) is True
    assert _COUNTER.release_active(_Subject(rounds=4)) is True


def test_release_inactive_for_none_subject():
    assert _COUNTER.release_active(None) is False


def test_release_threshold_comes_from_config_not_a_literal():
    """The whole point of reuse is that no axis mints its own threshold — a hardcoded
    3 in the implementation would satisfy every other test in this file just as well."""
    retuned = Thresholds({"effort-replan-absolute": "2"})
    assert _COUNTER.release_active(_Subject(rounds=2), retuned) is True
    assert _COUNTER.release_active(_Subject(rounds=1), retuned) is False


# --- compute_cross_axis_ceiling ------------------------------------------------

def test_cross_axis_ceiling_sums_rather_than_compares_individually():
    """The done-criterion scenario: two axes each individually BELOW the threshold
    (2 and 2) still trip the combined ceiling once their SUM (4) reaches it."""
    assert compute_cross_axis_ceiling([2, 2]) is True


def test_cross_axis_ceiling_inactive_below_the_summed_threshold():
    assert compute_cross_axis_ceiling([1, 1]) is False


def test_cross_axis_ceiling_active_at_exactly_the_threshold():
    assert compute_cross_axis_ceiling([1, 1, 1]) is True


def test_cross_axis_ceiling_treats_none_values_as_zero():
    assert compute_cross_axis_ceiling([3, None]) is True
    assert compute_cross_axis_ceiling([None, None]) is False


def test_cross_axis_ceiling_empty_iterable_is_inactive():
    assert compute_cross_axis_ceiling([]) is False


def test_cross_axis_ceiling_threshold_comes_from_config_not_a_literal():
    retuned = Thresholds({"effort-replan-absolute": "5"})
    assert compute_cross_axis_ceiling([2, 2], retuned) is False
    assert compute_cross_axis_ceiling([2, 3], retuned) is True


def test_cross_axis_ceiling_is_a_plain_sum_not_weighted():
    """A round on any one axis must count exactly as much as a round on any other —
    otherwise the combined ceiling would quietly favor whichever axis happens to sort
    first, which is not what the stage's invariant (ONE shared budget) asks for."""
    assert compute_cross_axis_ceiling([3, 0, 0]) == compute_cross_axis_ceiling([1, 1, 1])
    assert compute_cross_axis_ceiling([0, 3, 0]) == compute_cross_axis_ceiling([1, 1, 1])
