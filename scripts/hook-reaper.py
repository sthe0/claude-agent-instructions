#!/usr/bin/env python3
"""SessionStart hook: run every registered reaper (stale-residue cleanup) under shared rules.

Difficulty removed: each cleanup script hand-rolled its own throttle, dry-run, ownership
check and log, so a new kind of residue meant another one-off hook that decided alone.
The runner owns those rules; a reaper only proves what it can about its own residue.
Reapers are discovered from Core (scripts/reaper/builtin), a machine-local plugin dir
and the project's .claude/reapers. Contract, flags and layers: docs/operations/reapers.md.

  (no flags)   SessionStart mode: throttled per reaper, always exits 0.
  --dry-run    print one `<NAME> REMOVE|KEEP <path> (<reason>)` line per verdict; change nothing.
  --force-run  run now, ignoring throttle stamps (and not writing them).
  --only NAME  restrict the run to one reaper; the others still veto.
  --list       print `<NAME> <layer> <file>` per discovered reaper.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
