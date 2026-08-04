"""Parse coordination thresholds from config.md.

config.md is the single source of truth for the numeric constants the
coordination machinery uses (CLAUDE.md references them by key, never by value).
The same markdown-table format is parsed by spawn-specialist.py; this module is
the shared, typed accessor for the agentctl engine.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_MD = REPO_ROOT / "config.md"

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")


def parse_config_md(path: Path | None = None) -> dict[str, str]:
    """Extract `key` -> `value` from the markdown table in config.md."""
    cfg_path = path or CONFIG_MD
    constants: dict[str, str] = {}
    for line in cfg_path.read_text(encoding="utf-8").splitlines():
        m = CONFIG_KEY_RE.match(line)
        if m:
            constants[m.group(1)] = m.group(2)
    return constants


class Thresholds:
    """Typed view over the parsed config.md constants used by the engine."""

    def __init__(self, constants: dict[str, str] | None = None):
        self._c = constants if constants is not None else parse_config_md()

    def _int(self, key: str) -> int:
        if key not in self._c:
            raise KeyError(f"{key} not defined in config.md")
        return int(self._c[key])

    def _str(self, key: str) -> str:
        if key not in self._c:
            raise KeyError(f"{key} not defined in config.md")
        return self._c[key]

    def _float(self, key: str) -> float:
        if key not in self._c:
            raise KeyError(f"{key} not defined in config.md")
        return float(self._c[key])

    @property
    def small_change_max_lines(self) -> int:
        return self._int("small-change-max-lines")

    @property
    def substantive_wall_clock_min(self) -> int:
        return self._int("substantive-wall-clock-min")

    @property
    def max_recursion_depth(self) -> int:
        return self._int("max-recursion-depth")

    @property
    def loop_sensitivity_depth(self) -> int:
        return self._int("loop-sensitivity-depth")

    def budget_usd(self, tier: str) -> str:
        """Expected-size telemetry LABEL for a tier — NOT the applied kill-cap.
        The cap passed to `claude -p --max-budget-usd` is runaway_ceiling_usd()."""
        return self._str(f"budget-{tier}-usd")

    def budget_usd_float(self, tier: str) -> float:
        """Same value as budget_usd, as a float — for arithmetic (e.g. summing an
        estimate across tiers), where budget_usd's str would TypeError."""
        return self._float(f"budget-{tier}-usd")

    def effort_stage_minutes(self, tier: str) -> int:
        """Expected active-wall-clock minutes for a cost tier — the wall-clock
        companion to budget_usd's dollar label, used by the effort-divergence
        trigger's wall-clock scale."""
        return self._int(f"effort-stage-minutes-{tier}")

    def effort_divergence_multiple(self) -> float:
        """Ratio of accumulated actual effort to the re-derived estimate at/above
        which the effort-divergence trigger fires."""
        return self._float("effort-divergence-multiple")

    def effort_replan_absolute(self) -> int:
        """Replan count on the current plan at/above which the effort-divergence
        trigger fires regardless of the multiple."""
        return self._int("effort-replan-absolute")

    def effort_absolute_interactions(self) -> int:
        """Absolute threshold on user-interaction count for the interactions scale;
        `0` means the scale is accounting-only / disabled (see config.md row for
        the re-enabling contract)."""
        return self._int("effort-absolute-interactions")

    def runaway_ceiling_usd(self) -> str:
        """The single global runaway backstop actually passed as --max-budget-usd
        to every spawn (spawn-runaway-ceiling-usd). Fail-safe to the large tier if
        the key is absent, mirroring spawn-specialist.runaway_ceiling — never
        unbounded."""
        key = "spawn-runaway-ceiling-usd"
        if key in self._c:
            return self._c[key]
        return self.budget_usd("large")

    @property
    def advisor_mode(self) -> str:
        """'off' or 'substantive' — gates advisor.resolve_enabled's config layer."""
        return self._str("advisor-mode")
