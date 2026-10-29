#!/usr/bin/env python3
"""Specialization spawns and skill / subagent invocations; entry point for `agent-report.py tool-usage`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_report import run


def main(argv=None):
    return run("tool-usage", argv)


if __name__ == "__main__":
    sys.exit(main())
