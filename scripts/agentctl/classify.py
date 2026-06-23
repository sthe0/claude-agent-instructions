"""Pure task-weight classification and routing.

No filesystem, no state mutation — given signals + thresholds, decide the weight
class and the route. The thresholds come from config.md (small-change-max-lines,
substantive-wall-clock-min); the rules mirror CLAUDE.md § Classify task weight.

Overrides that are NOT size-derived (mirroring the prose):
  - a tracker key (ABC-123) forces SUBSTANTIVE — the ticket boundary is the scope
    boundary, regardless of apparent size.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Thresholds
from .state import Route, WeightClass

TRACKER_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


@dataclass
class Signals:
    is_chat: bool = False
    changed_lines: int = 0
    files: int = 1
    wall_clock_min: int = 0
    tracker_key: str | None = None
    architectural: bool = False
    external_effect: bool = False
    new_dependency: bool = False
    public_api_change: bool = False


@dataclass
class Classification:
    weight_class: str
    route: str
    reasons: list[str] = field(default_factory=list)


ROUTE_BY_WEIGHT = {
    WeightClass.CHAT.value: Route.DIRECT.value,
    WeightClass.SMALL_CHANGE.value: Route.IN_THREAD.value,
    WeightClass.SUBSTANTIVE.value: Route.SPAWN.value,
}


def classify(sig: Signals, thr: Thresholds) -> Classification:
    reasons: list[str] = []

    if sig.tracker_key and TRACKER_KEY_RE.match(sig.tracker_key):
        reasons.append(f"tracker key {sig.tracker_key} -> substantive (ticket = scope boundary)")
        return Classification(WeightClass.SUBSTANTIVE.value, Route.SPAWN.value, reasons)

    if sig.is_chat:
        reasons.append("chat: no file changes -> direct answer, terminal at ROUTED")
        return Classification(WeightClass.CHAT.value, Route.DIRECT.value, reasons)

    substantive_signals = []
    if sig.architectural:
        substantive_signals.append("architectural decision")
    if sig.external_effect:
        substantive_signals.append("external/irreversible effect")
    if sig.new_dependency:
        substantive_signals.append("new dependency")
    if sig.public_api_change:
        substantive_signals.append("public-API change")
    if sig.files > 1:
        substantive_signals.append(f"{sig.files} files (>1)")
    if sig.changed_lines > thr.small_change_max_lines:
        substantive_signals.append(
            f"{sig.changed_lines} changed lines (> small-change-max-lines={thr.small_change_max_lines})"
        )
    if sig.wall_clock_min >= thr.substantive_wall_clock_min:
        substantive_signals.append(
            f"{sig.wall_clock_min}min (>= substantive-wall-clock-min={thr.substantive_wall_clock_min})"
        )

    if substantive_signals:
        reasons.extend(substantive_signals)
        weight = WeightClass.SUBSTANTIVE.value
    else:
        reasons.append(
            f"<= {thr.small_change_max_lines} lines, single file, no architectural/external -> small change"
        )
        weight = WeightClass.SMALL_CHANGE.value

    return Classification(weight, ROUTE_BY_WEIGHT[weight], reasons)
