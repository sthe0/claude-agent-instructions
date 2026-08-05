"""Tests for lib/judge_budget.py — the shared whole-invocation deadline used
by hooks that call one or more slow judge subprocesses.

Isolated from every hook that consumes it: these tests fake the clock
directly and never touch a payload, a prefilter, or advisor.* — they pin the
primitive's own contract independently of any one caller.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "lib_judge_budget", SCRIPTS_DIR / "lib" / "judge_budget.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()


class _FakeClock:
    """Returns values from `sequence` in call order, holding the last value
    once exhausted — same contract as the hook tests' _FakeTime."""

    def __init__(self, sequence):
        self._values = list(sequence)
        self._i = 0

    def __call__(self):
        value = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return value


def test_remaining_reflects_elapsed_time():
    clock = _FakeClock([0.0, 5.0])
    budget = _mod.JudgeBudget(20, 12, clock=clock)
    assert budget.remaining() == 15.0


def test_default_clock_is_real_time_monotonic():
    budget = _mod.JudgeBudget(5, 1)
    assert 4.5 <= budget.remaining() <= 5.0


def test_next_call_timeout_is_capped_remaining_when_above_the_floor():
    clock = _FakeClock([0.0, 5.0])
    budget = _mod.JudgeBudget(20, 12, clock=clock)
    assert budget.next_call_timeout(30) == 15.0


def test_next_call_timeout_caps_at_the_call_ceiling_not_the_remaining_budget():
    clock = _FakeClock([0.0, 2.0])
    budget = _mod.JudgeBudget(20, 12, clock=clock)
    # remaining = 18.0, well above the floor, but the per-call ceiling (10)
    # is tighter -- the cap must win.
    assert budget.next_call_timeout(10) == 10


def test_next_call_timeout_is_none_when_remaining_is_below_the_floor():
    clock = _FakeClock([0.0, 18.0])
    budget = _mod.JudgeBudget(20, 12, clock=clock)
    assert budget.next_call_timeout(30) is None


def test_exactly_at_the_floor_is_still_enough_budget():
    # remaining == min_call_s exactly: `<` (not `<=`) treats this as
    # sufficient -- pin the boundary the original hook relied on.
    clock = _FakeClock([0.0, 8.0])
    budget = _mod.JudgeBudget(20, 12, clock=clock)
    assert budget.next_call_timeout(30) == 12.0


def test_one_deadline_spans_successive_calls_rather_than_resetting():
    # The property the class is named for: successive calls draw down ONE
    # deadline. A per-call budget would answer 30/30/30 here; a whole-
    # invocation budget runs out on the third.
    clock = _FakeClock([0.0, 0.0, 10.0, 30.0])
    budget = _mod.JudgeBudget(40, 12, clock=clock)
    assert budget.next_call_timeout(30) == 30
    assert budget.next_call_timeout(30) == 30
    assert budget.next_call_timeout(30) is None


def test_next_call_timeout_reads_the_clock_exactly_once():
    # A caller decides go/no-go AND the timeout from a single reading -- two
    # reads per call would let the two decisions see different remaining
    # values on a real (non-fake) clock. Counting starts AFTER construction
    # (which itself takes one reading to open the deadline).
    calls = []
    clock = _FakeClock([0.0, 5.0])

    def counting_clock():
        value = clock()
        calls.append(value)
        return value

    budget = _mod.JudgeBudget(20, 12, clock=counting_clock)
    calls.clear()
    budget.next_call_timeout(30)
    assert len(calls) == 1
