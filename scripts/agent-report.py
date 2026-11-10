#!/usr/bin/env python3
"""Usage and cost reports over the local ledgers and session transcripts.

  agent-report.py <view> [view arguments]      (`<view> --help` lists them)

Views:
  stats           invocations, resolved tasks, quality and spawn cost per project
  cost            spawn cost log summary, or a project's full-budget interval
  spawn-outcomes  malformed-spawn and re-spawn rates, baseline and delta check
  tool-usage      specialization spawns and skill / subagent invocations
  skill-usage     which user-invocable skills are actually used
  hooks           decisions (deny / nudge / silent) per hook and engine gate
  time-profile    where session wall-clock goes: decayed mean ± SE per mechanism,
                  [--format text|json] [--half-life D] [--by-sha] [--check]
  core-version    per Core HEAD x weight class from the telemetry record: convergence,
                  reliability, resources, causes, mechanisms, losses
                  [--format text|json] [--task T] [--check] [--check-self-cost]
                  [--include-synthetic]

The implementation of each view is scripts/lib/agent_report/<view>.py (core-version:
scripts/lib/telemetry/report.py); the old
per-report script names (agent-stats.py, cost-report.py, spawn-outcome-report.py,
tool-usage-report.py, skill-usage-audit.py) remain as thin entry points.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_report import VIEWS, run

CORE_VERSION = "core-version"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    if args[0] == CORE_VERSION:
        from lib.telemetry import report
        return report.main(args[1:])
    if args[0] not in VIEWS:
        print(f"agent-report.py: unknown view {args[0]!r}; choose from {', '.join(VIEWS)}", file=sys.stderr)
        return 2
    return run(args[0], args[1:])


if __name__ == "__main__":
    sys.exit(main())
