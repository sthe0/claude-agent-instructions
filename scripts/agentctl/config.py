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
