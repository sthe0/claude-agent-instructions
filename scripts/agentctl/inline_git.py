"""The git side of the inline size measure: runs the argv `inline_class` builds and feeds its parsers.

Kept apart from `inline_class` so that module stays free of process I/O; the scripts that measure a
range (inline-measure.py, inline-size-calibrate.py) and the mandate driver share this one copy.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from . import inline_class

Runner = Callable[[Path, "list[str]", str], str]
"""Runs `argv` in a directory with a stdin text and returns stdout; raises on a failed command."""


def run(argv: "list[str]", cwd: Path, stdin: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True)


def git(repo: Path, argv: "list[str]", stdin: str = "") -> str:
    done = run(argv, repo, stdin)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)}: {done.stderr.strip()}")
    return done.stdout


def measure_range(
    repo: Path, base: str, tip: str, *, attrs_repo: "Path | None" = None, attrs_source: "str | None" = None,
    runner: Runner = git,
) -> inline_class.Measure:
    """Hand-written production lines of `base..tip`. Which paths are generated is read in `attrs_repo`
    (default `repo`), at `attrs_source` when given, so a caller can take it from a tree other than the
    one holding the change."""
    changes = inline_class.parse_numstat_z(runner(repo, inline_class.numstat_argv(base, tip), ""))
    attr_out = ""
    if changes:
        attr_out = runner(
            attrs_repo or repo, inline_class.check_attr_argv(attrs_source), inline_class.check_attr_stdin(changes),
        )
    return inline_class.measure(changes, inline_class.generated_from_check_attr(attr_out))
