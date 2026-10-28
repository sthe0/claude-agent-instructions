"""The agent-report views: one module per report, dispatched by `run`."""
from __future__ import annotations

import importlib

VIEWS = {
    "stats": "stats",
    "cost": "cost",
    "spawn-outcomes": "spawn_outcomes",
    "tool-usage": "tool_usage",
    "skill-usage": "skill_usage",
    "hooks": "hooks",
}


def run(view: str, argv: list[str] | None = None) -> int:
    """Run one view's `main` with its own arguments (None = the process's)."""
    module = importlib.import_module(f"lib.agent_report.{VIEWS[view]}")
    return module.main(argv)
