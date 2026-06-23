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

# ---------------------------------------------------------------------------
# Action-level side-effect classification
# ---------------------------------------------------------------------------

READONLY_BASH: set[str] = {
    "ls", "head", "tail", "cat", "less", "more", "find", "wc", "stat", "file",
    "tree", "du", "df", "grep", "rg", "awk", "sed", "echo", "printf", "jq",
    "realpath", "readlink", "which", "whoami", "date", "pwd", "env", "printenv",
    "python3",  # classify_action returns "unknown" for python3; caller applies READONLY_PYTHON3
}
READONLY_GIT: set[str] = {
    "status", "log", "diff", "show", "branch", "remote", "config", "rev-parse",
    "ls-files", "blame",
}
READONLY_ARC: set[str] = {"info", "status", "log", "diff", "show", "branch", "grep"}

MCP_READONLY_PREFIXES = ("get", "list", "search", "describe")

_MUTATING_BASH: set[str] = {
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "chown",
    "dd", "tee", "curl", "wget", "push", "kill",
}


def classify_action(
    tool: str, verb: str | None = None, subverb: str | None = None
) -> str:
    """Return 'side-effect-free' / 'needs-approval' / 'unknown' for a tool call.

    Verb semantics live here; settings.json string syntax (`:*` stripping,
    python3 arg patterns) is the caller's responsibility.
    """
    if tool in {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}:
        return "side-effect-free"
    if tool in {"Edit", "Write", "NotebookEdit"}:
        return "needs-approval"
    if tool.startswith("mcp__"):
        method = tool.rsplit("__", 1)[-1].lower()
        if method.startswith(MCP_READONLY_PREFIXES) or "search" in method:
            return "side-effect-free"
        return "unknown"
    if tool == "Bash":
        if verb == "git":
            return "side-effect-free" if subverb in READONLY_GIT else "needs-approval"
        if verb == "arc":
            return "side-effect-free" if subverb in READONLY_ARC else "needs-approval"
        if verb == "python3":
            return "unknown"  # arg-level refinement (READONLY_PYTHON3) stays with the caller
        if verb in READONLY_BASH:
            return "side-effect-free"
        if verb in _MUTATING_BASH:
            return "needs-approval"
        return "unknown"
    return "unknown"


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
