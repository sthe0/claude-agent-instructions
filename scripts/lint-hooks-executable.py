#!/usr/bin/env python3
"""Verify every shebang-bearing script in scripts/ carries the executable bit.

Difficulty removed: the harness (and skills like overcome-difficulty /
resolution flows) exec scripts directly. A script committed with a `#!` shebang
but without the executable bit (100644 instead of 100755) fails silently with
"Permission denied" when invoked as `./script.py` — the work it did is lost
without any error surfaced to the workflow. This originally guarded only the
hook-*.py family, but the same trap bit non-hook scripts (record-experience.py,
verify-*.py) that are sometimes invoked directly: the executable bit is a
property of "has a shebang and may be run directly", not of the filename
prefix. So the check now covers every shebang-bearing script under scripts/.

Checks both the on-disk mode and git's recorded mode (the bit git actually
tracks), so a +x that was never `git add`-ed is still flagged.

Exit code 1 if any such script lacks +x. --root for project-repo reuse;
--staged is accepted but ignored (whole-tree check).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_REL = "scripts"
SCRIPT_GLOBS = ("*.py", "*.sh")


def _has_shebang(path: Path) -> bool:
    """True if the file's first line starts with '#!'."""
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _git_mode(root: Path, path: Path) -> str | None:
    """Return git's recorded mode for path (e.g. '100755'), or None if untracked."""
    try:
        rel = path.relative_to(root)
        out = subprocess.run(
            ["git", "ls-files", "--stage", "--", str(rel)],
            cwd=str(root), capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except Exception:
        return None
    # Format: "<mode> <sha> <stage>\t<path>"
    return out.split(" ", 1)[0] if out else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--staged", action="store_true", help="Accepted but ignored")
    args = parser.parse_args(argv)

    root = args.root if args.root is not None else Path(__file__).resolve().parent.parent
    scripts = root / SCRIPTS_REL
    candidates: list[Path] = []
    if scripts.is_dir():
        seen: set[Path] = set()
        for glob in SCRIPT_GLOBS:
            for path in scripts.glob(glob):
                if path in seen or not _has_shebang(path):
                    continue
                seen.add(path)
                candidates.append(path)
        candidates.sort()
    if not candidates:
        print("lint-hooks-executable: OK — no shebang scripts")
        return 0

    failures: list[str] = []
    for script in candidates:
        rel = script.relative_to(root)
        if not os.access(script, os.X_OK):
            failures.append(f"  {rel}: not executable on disk — `chmod +x {rel}`")
            continue
        gm = _git_mode(root, script)
        if gm is not None and not gm.endswith("755"):
            failures.append(f"  {rel}: git mode {gm} (not executable) — `git add --chmod=+x {rel}`")

    if failures:
        print("lint-hooks-executable: FAIL")
        for f in failures:
            print(f)
        return 1
    print(f"lint-hooks-executable: OK — {len(candidates)} shebang script(s) executable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
