#!/usr/bin/env python3
"""Enforce every discovered term ruleset against the tracked tree (C1 mechanism).

Core carries no org-internal denylist itself — see ``lib/term_ruleset.py`` for
the schema/discovery/compose semantics. With zero rulesets discovered (the
default on a foreign clone with no machine-local ruleset installed), this is
a reported no-op: it prints "rulesets discovered: 0" and exits 0, never
silently. Registered in ``verify-all.py``.

Usage:
    verify-terms.py [--staged] [--repo-root PATH]
                     [--require-clean-path PREFIX ...]
                     [--assert-grandfather-empty] [--ignore-grandfather]
                     [--assert-rulesets-min N] [--expect-rulesets N]
                     [--list-rulesets]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import config_root  # noqa: E402
from lib import term_ruleset as tr  # noqa: E402


def list_paths(mode: str, repo_root: Path) -> list[str]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=True)
    return [line for line in completed.stdout.splitlines() if line]


def _read_blob(repo_root: Path, relpath: str, mode: str) -> str | None:
    if mode == "staged":
        blob = subprocess.run(
            ["git", "show", f":{relpath}"], cwd=repo_root, capture_output=True, check=False,
        )
        if blob.returncode != 0:
            return None
        try:
            return blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None
    p = repo_root / relpath
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--repo-root", default=None, help="defaults to this script's own repo")
    parser.add_argument("--require-clean-path", action="append", default=[], metavar="PREFIX")
    parser.add_argument("--assert-grandfather-empty", action="store_true")
    parser.add_argument("--ignore-grandfather", action="store_true")
    parser.add_argument("--assert-rulesets-min", type=int, default=None, metavar="N")
    parser.add_argument("--expect-rulesets", type=int, default=None, metavar="N")
    parser.add_argument("--list-rulesets", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else SCRIPTS_DIR.parent

    try:
        rulesets = tr.discover_rulesets(
            agent_home=config_root.agent_home(),
            project_dir=repo_root,
            guarded_repo_root=repo_root,
        )
    except tr.RulesetError as exc:
        print(f"verify-terms: FAIL loading ruleset — {exc}")
        return 1

    print(f"rulesets discovered: {len(rulesets)}")

    if args.list_rulesets:
        for rs in rulesets:
            print(f"  - {rs.name}  ({rs.source_path})  "
                  f"deny={len(rs.deny)} exempt={len(rs.exempt)} grandfather={len(rs.grandfather)}")
        return 0

    if args.expect_rulesets is not None and len(rulesets) != args.expect_rulesets:
        print(f"verify-terms: FAIL — expected {args.expect_rulesets} ruleset(s), "
              f"discovered {len(rulesets)}")
        return 1
    if args.assert_rulesets_min is not None and len(rulesets) < args.assert_rulesets_min:
        print(f"verify-terms: FAIL — expected at least {args.assert_rulesets_min} ruleset(s), "
              f"discovered {len(rulesets)}")
        return 1
    if args.assert_grandfather_empty:
        remaining = [(rs.name, g.path_glob) for rs in rulesets for g in rs.grandfather]
        if remaining:
            print(f"verify-terms: FAIL — {len(remaining)} grandfather entr{'y' if len(remaining) == 1 else 'ies'} remain:")
            for name, glob in remaining:
                print(f"  {name}: {glob}")
            return 1

    if not rulesets:
        print("verify-terms: OK — no-op (no rulesets to enforce)")
        return 0

    mode = "staged" if args.staged else "all"
    relpaths = list_paths(mode, repo_root)
    if args.require_clean_path:
        relpaths = [p for p in relpaths if any(p.startswith(prefix) for prefix in args.require_clean_path)]

    all_hits: list[tr.Hit] = []
    for relpath in relpaths:
        content = _read_blob(repo_root, relpath, mode)
        if content is None:
            continue
        all_hits.extend(tr.scan(content, rulesets, path=relpath, ignore_grandfather=args.ignore_grandfather))

    if all_hits:
        files_hit = {h.path for h in all_hits}
        print(f"verify-terms: FAIL — {len(all_hits)} hit(s) in {len(files_hit)} file(s)")
        for h in all_hits:
            print(f"  {h.format()}")
        return 1

    print(f"verify-terms: OK — 0 hits across {len(relpaths)} file(s) scanned ({mode} mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
