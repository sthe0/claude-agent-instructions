#!/usr/bin/env python3
"""Compatibility entry point: the worktree sweep is now the `git-worktrees` reaper.

Difficulty removed: the sweep was a stand-alone SessionStart hook wired by name into
machine-local settings, so folding it into the reaper framework must not break those
registrations. This shim runs the whole reaper runner with the same flags (`--dry-run`,
`--force-run`, none); the worktree rules now live in scripts/reaper/builtin/git_worktrees.py
and the shared ones in docs/operations/reapers.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
