"""Shared whole-invocation deadline for hooks that call one or more slow judge
subprocesses (``agentctl.advisor.*``).

Difficulty removed: hook-deferring-disposition-gate.py opened a
``time.monotonic()`` deadline itself, computed the per-call timeout as
``min(remaining, per_call_cap)`` and compared the remainder against a
"can a call still plausibly finish" floor — logic that has nothing to do with
deferring-disposition specifically. The driver for extracting it is
hook-turn-end-gate.py, which will make three judge calls in one invocation and
today bounds none of them; hook-escalation-diagnosis-gate.py will make a single
call, where a whole-invocation deadline degenerates into a per-call ceiling but
still answers the same three questions (how much is left, is it enough for a
meaningful call, what timeout does the next call get). Each hook picks its own
whole-budget and per-call floor, so those stay constructor inputs here, never
constants baked into this module.
"""
from __future__ import annotations

import time
from typing import Callable


class JudgeBudget:
    """A single deadline covering every judge call made during one hook
    invocation — distinct from the per-call timeout passed to any individual
    judge. The deadline opens the moment the object is constructed.

    ``min_call_s`` is the floor below which a judge call cannot plausibly
    finish (set from the judge's own measured latency) — once the remaining
    budget drops under it, the caller must stop issuing calls and fail open,
    the same posture as every other unreachable-judge path.
    """

    def __init__(
        self,
        total_s: float,
        min_call_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_call_s = min_call_s
        self._clock = clock
        self._deadline = clock() + total_s

    def remaining(self) -> float:
        """Seconds left until the deadline (one clock read); goes negative
        once exhausted. Public for the execution ledger, which records the
        remainder at entry without issuing a call."""
        return self._deadline - self._clock()

    def next_call_timeout(self, cap_s: float) -> float | None:
        """The timeout to pass to the next judge call, capped at ``cap_s``
        (that judge's own per-call ceiling), or ``None`` when too little
        budget remains for a call to plausibly finish — the caller must then
        stop and fail open rather than attempt a doomed call.

        Reads the clock exactly once, so the go/no-go decision and the
        returned timeout are computed from the same reading instead of two
        readings that could straddle real elapsed time."""
        remaining = self.remaining()
        if remaining < self._min_call_s:
            return None
        return min(remaining, cap_s)
