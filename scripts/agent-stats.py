#!/usr/bin/env python3
"""Usage report over the local ledgers; entry point for `agent-report.py stats`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_report import run
from lib.agent_report.stats import (  # noqa: F401 -- loaded by path by other scripts
    DEFAULT_DAYS,
    POLICY_LEDGER,
    SPAWN_COST_LOG,
    TASK_QUALITY_LOG,
    aggregate,
    cost_report,
    filter_by_time,
    project_sessions,
    read_rows,
    render_md,
    resolve_since,
    sanitize_cwd,
    scope_rows,
)


def main(argv=None):
    return run("stats", argv)


if __name__ == "__main__":
    sys.exit(main())
