#!/usr/bin/env python3
"""OpenCode probe suite CLI; the logic lives in the ``opencode_probe`` package so the
suite hash covers it. See scripts/README.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opencode_probe.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
