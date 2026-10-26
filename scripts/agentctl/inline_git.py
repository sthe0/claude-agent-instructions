"""The git side of the inline size measure: runs the argv `inline_class` builds and feeds its parsers.

Kept apart from `inline_class` so that module stays free of process I/O; the scripts that measure a
range (inline-measure.py, inline-size-calibrate.py) share this one copy.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import inline_class


def run(argv: "list[str]", cwd: Path, stdin: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True)


def git(repo: Path, argv: "list[str]", stdin: str = "") -> str:
    done = run(argv, repo, stdin)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)}: {done.stderr.strip()}")
    return done.stdout


def measure_range(repo: Path, base: str, tip: str) -> inline_class.Measure:
    changes = inline_class.parse_numstat_z(git(repo, inline_class.numstat_argv(base, tip)))
    attr_out = git(repo, inline_class.check_attr_argv(), inline_class.check_attr_stdin(changes)) if changes else ""
    return inline_class.measure(changes, inline_class.generated_from_check_attr(attr_out))
