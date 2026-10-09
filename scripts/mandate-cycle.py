#!/usr/bin/env python3
"""Run one background debt cycle under the standing mandate.

    mandate-cycle.py run                  one cycle: fix eligible issues, triage the rest
    mandate-cycle.py run --dry-run --json what a cycle would take and triage; writes nothing
    mandate-cycle.py notify-test --json   which notifier would carry a digest, and does it work

Thin shim over `mandate_cycle.driver`; the timer installed by `install-mandate-timer.sh`
calls `run`. The mandate itself is granted and stopped with the `agentctl mandate-*` verbs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mandate_cycle import driver  # noqa: E402

if __name__ == "__main__":
    sys.exit(driver.main())
