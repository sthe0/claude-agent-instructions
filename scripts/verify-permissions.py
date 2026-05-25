#!/usr/bin/env python3
"""Verify the structure of permissions JSON files.

Checks:
- File is valid JSON with a top-level `permissions` array.
- Each entry has `pattern` (non-empty string), `granted_at` (ISO date),
  `context` (non-empty string).
- No duplicate patterns within a file.

Files checked: every `permissions/*.json` tracked by git.
Exit code 1 if any violation is found.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FIELDS = ("pattern", "granted_at", "context")


def list_paths(mode: str) -> list[Path]:
    if mode == "staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [
        REPO_ROOT / line
        for line in out.stdout.splitlines()
        if line.startswith("permissions/") and line.endswith(".json")
    ]


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return [f"{path.name}: invalid JSON ({exc})"]
    except OSError as exc:
        return [f"{path.name}: cannot read ({exc})"]

    if not isinstance(data, dict) or "permissions" not in data:
        return [f"{path.name}: top-level object must have a 'permissions' field"]
    perms = data["permissions"]
    if not isinstance(perms, list):
        return [f"{path.name}: 'permissions' must be a JSON array"]

    seen: set[str] = set()
    for i, entry in enumerate(perms):
        loc = f"{path.name}[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{loc}: entry must be an object")
            continue
        for field in REQUIRED_FIELDS:
            if field not in entry:
                errors.append(f"{loc}: missing field '{field}'")
        for field in ("pattern", "context"):
            v = entry.get(field)
            if not (isinstance(v, str) and v.strip()):
                errors.append(f"{loc}: '{field}' must be a non-empty string")
        date_str = entry.get("granted_at")
        if isinstance(date_str, str):
            try:
                dt.date.fromisoformat(date_str)
            except ValueError:
                errors.append(f"{loc}: 'granted_at' must be ISO date YYYY-MM-DD, got {date_str!r}")
        pat = entry.get("pattern")
        if isinstance(pat, str):
            if pat in seen:
                errors.append(f"{loc}: duplicate pattern {pat!r}")
            seen.add(pat)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)
    mode = "staged" if args.staged else "all"

    paths = list_paths(mode)
    all_errors: list[str] = []
    for p in paths:
        if not p.exists():
            continue
        all_errors.extend(check_file(p))

    if all_errors:
        print(f"verify-permissions: FAIL — {len(all_errors)} issue(s) in {len(paths)} file(s) ({mode} mode)")
        for e in all_errors:
            print(f"  {e}")
        return 1
    print(f"verify-permissions: OK — {len(paths)} file(s) scanned ({mode} mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
