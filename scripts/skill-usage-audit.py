#!/usr/bin/env python3
"""Which user-invocable skills are used; entry point for `agent-report.py skill-usage`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_report import run


def main(argv=None):
    return run("skill-usage", argv)


if __name__ == "__main__":
    sys.exit(main())
