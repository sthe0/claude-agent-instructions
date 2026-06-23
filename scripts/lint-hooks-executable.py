#!/usr/bin/env python3
"""Verify every hook script carries the executable bit.

Difficulty removed: the harness execs each hook directly via /bin/sh. A hook
committed without the executable bit (100644 instead of 100755) fails silently
every turn with "Permission denied" — the hook never runs, and whatever it
enforced (e.g. hook-engine-start.py auto-starting the agentctl engine) is lost
without any error surfaced to the workflow. setup-symlinks.sh chmods the whole
hook-*.py family, but a fresh checkout or an editor-created file can still land
non-executable; this check fails the pre-commit gate before that ships.

Checks both the on-disk mode and git's recorded mode (the bit git actually
tracks), so a +x that was never `git add`-ed is still flagged.

Exit code 1 if any hook lacks +x. --root for project-repo reuse;
--staged is accepted but ignored (whole-tree check).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_REL = "scripts"
HOOK_GLOB = "hook-*.py"


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
    hooks = sorted(scripts.glob(HOOK_GLOB)) if scripts.is_dir() else []
    if not hooks:
        print("lint-hooks-executable: OK — no hook scripts")
        return 0

    failures: list[str] = []
    for hook in hooks:
        rel = hook.relative_to(root)
        if not os.access(hook, os.X_OK):
            failures.append(f"  {rel}: not executable on disk — `chmod +x {rel}`")
            continue
        gm = _git_mode(root, hook)
        if gm is not None and not gm.endswith("755"):
            failures.append(f"  {rel}: git mode {gm} (not executable) — `git add --chmod=+x {rel}`")

    if failures:
        print("lint-hooks-executable: FAIL")
        for f in failures:
            print(f)
        return 1
    print(f"lint-hooks-executable: OK — {len(hooks)} hook(s) executable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
