#!/usr/bin/env python3
"""Malformed-spawn defect rates; entry point for `agent-report.py spawn-outcomes`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_report import run


def main(argv=None):
    return run("spawn-outcomes", argv)


if __name__ == "__main__":
    sys.exit(main())
