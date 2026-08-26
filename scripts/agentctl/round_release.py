"""One reusable round-release ("Rule of Three") primitive, shared by every axis
that bounds a review/re-run loop against config.md's `effort-replan-absolute`.

Before this module, plan-review and plan-enumerate each carried their own
independent `... >= thr.effort_replan_absolute()` check inline in gates.py, and
code-review (GitHub issue #96) had none at all. Independent per-axis budgets let a
session pay 2 rounds on one axis plus 2-3 on another before either individually
fires — real session baa1daea reached 5+ combined rounds with neither valve
firing. `RoundReleaseCounter` gives every axis the same comparison over its own
count; `compute_cross_axis_ceiling` gives the three axes together ONE combined
ceiling over their SUM, so the friction is bounded as a whole, not axis-by-axis.

PURE: dataclass reads and arithmetic only — no subprocess/socket/file reach, so a
gates.py caller stays inside the AST-purity contract `ast_purity.py` enforces."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .config import Thresholds


@dataclass(frozen=True)
class RoundReleaseCounter:
    """A single axis's round-release valve.

    `name` labels the axis for messages/telemetry; `getter` extracts this axis's
    current round count from whatever subject the axis counts against — a
    SessionState for plan-review/code-review (an attribute read), a premise
    plugin bag for plan-enumerate (a dict read). The axes disagree on WHERE their
    count lives, not on the threshold or the comparison, so the getter is the one
    thing each axis supplies; `value`/`release_active` are shared.
    """

    name: str
    getter: Callable[[object], int]

    def value(self, subject: object) -> int:
        """This axis's current round count, or 0 for a missing subject (no
        session yet / no premise bag yet) — never an error, since "no rounds
        spent" is exactly what an absent subject means."""
        if subject is None:
            return 0
        return int(self.getter(subject) or 0)

    def release_active(self, subject: object, thr: Thresholds | None = None) -> bool:
        """True once this axis alone has reached the shared Rule-of-Three
        threshold (config.md's `effort-replan-absolute`)."""
        if subject is None:
            return False
        thr = thr if thr is not None else Thresholds()
        return self.value(subject) >= thr.effort_replan_absolute()


def compute_cross_axis_ceiling(values: Iterable[int], thr: Thresholds | None = None) -> bool:
    """True once the SUM of the given per-axis round counts reaches the shared
    Rule-of-Three threshold — even when every individual value is still under it.

    Deliberately a plain SUM, not a weighted combination: the diagnosis this
    exists for is that friction accumulated on ANY mix of axes is the same
    difficulty, so a round spent on plan-review counts exactly as much toward the
    combined ceiling as one spent on code-review. Reuses the SAME threshold
    constant every per-axis `RoundReleaseCounter` reads (`effort-replan-absolute`)
    rather than a separate aggregate constant, per the stage's invariant that all
    three axes share one budget."""
    thr = thr if thr is not None else Thresholds()
    return sum(int(v or 0) for v in values) >= thr.effort_replan_absolute()
