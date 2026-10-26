#!/usr/bin/env python3
"""Measure a change in hand-written production lines, the size the inline class is bounded by.

Difficulty removed: a raw changed-line count is dominated by generated files and tests, so it
neither says whether a change is small enough to need no plan nor can be compared with the
`small-change-max-lines` bound. This counts exactly the lines scripts/agentctl/inline_class.py
counts (tests, generated files and memory leaves excluded) over `git diff BASE TIP`.

  inline-measure.py [--repo DIR] [--lines] BASE TIP

Default output is one JSON object (production_lines, bound, within_bound, counted_paths,
excluded by reason, binary_paths). Exit 0 within the bound, 1 over it, 2 on error. With --lines it
prints only the integer and exits 0, or 2 on error. The bound is read from the config.md of the
repo this script lives in; the diff and `.gitattributes` are read from --repo (default: the same).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl import inline_class  # noqa: E402
from agentctl.config import Thresholds  # noqa: E402


def _git(repo: Path, argv: "list[str]", stdin: str = "") -> str:
    done = subprocess.run(argv, cwd=repo, input=stdin, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)}: {done.stderr.strip()}")
    return done.stdout


def measure_range(repo: Path, base: str, tip: str) -> inline_class.Measure:
    changes = inline_class.parse_numstat_z(_git(repo, inline_class.numstat_argv(base, tip)))
    attr_out = _git(repo, inline_class.check_attr_argv(), inline_class.check_attr_stdin(changes)) if changes else ""
    return inline_class.measure(changes, inline_class.generated_from_check_attr(attr_out))


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=SCRIPTS_DIR.parent)
    parser.add_argument("--lines", action="store_true", help="print only the integer line count")
    parser.add_argument("base")
    parser.add_argument("tip")
    args = parser.parse_args(argv)
    try:
        measured = measure_range(args.repo, args.base, args.tip)
        bound = Thresholds().small_change_max_lines
    except (RuntimeError, OSError, KeyError, ValueError) as exc:
        print(f"inline-measure: {exc}", file=sys.stderr)
        return 2
    if args.lines:
        print(measured.production_lines)
        return 0
    within = inline_class.within_bound(measured, bound)
    print(json.dumps({
        "production_lines": measured.production_lines,
        "bound": bound,
        "within_bound": within,
        "counted_paths": list(measured.counted_paths),
        "excluded": {reason: list(paths) for reason, paths in measured.excluded.items()},
        "binary_paths": list(measured.binary_paths),
    }, indent=2))
    return 0 if within else 1


if __name__ == "__main__":
    sys.exit(main())
