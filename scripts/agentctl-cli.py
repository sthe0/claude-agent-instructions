#!/usr/bin/env python3
"""Standalone entry point for the agentctl engine: `python3 <abs path to this
file> <verb> ...` works from any cwd, without `cd scripts && python3 -m
agentctl <verb>` first — the form a review-directive or spawn brief can name
by absolute path regardless of the delivery venue's own cwd.

Thin shim only: resolves this file's own directory onto sys.path (the same
pattern edit-ledger.py/budget-calibration.py/check-org-neutral.py already
use to import the agentctl package without a `cd`) and delegates to
agentctl.cli.main() unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main())
