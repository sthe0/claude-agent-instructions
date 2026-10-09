#!/usr/bin/env python3
"""Verify shared hook registry Cursor/Claude parity obligations.

Checks every row has exactly one Claude side (event or skip reason) and exactly
one Cursor side, gate rows declare cursor_fail_closed, required hooks are
Cursor-mapped, and the native auto-memory metadata gap row is present.

With --check-install PATH, also verifies a live ~/.cursor/hooks.json (or other
path) contains every managed entry derived from the registry.

Accepts (and ignores) --staged for verify-all uniformity. Exit 1 on any problem.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import hook_registry  # noqa: E402

DEFAULT_CURSOR_HOOKS = Path.home() / ".cursor" / "hooks.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="ignored; accepted for verify-all uniformity")
    parser.add_argument(
        "--check-install",
        type=Path,
        metavar="HOOKS_JSON",
        help="also verify live Cursor hooks.json matches registry-derived expectations",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="override registry path (default: scripts/hooks/desired.json)",
    )
    args = parser.parse_args(argv)

    problems: list[str] = []
    problems.extend(hook_registry.validate_registry(path=args.registry))
    problems.extend(hook_registry.check_cursor_guardians(path=args.registry))

    install_path = args.check_install
    if install_path is not None:
        problems.extend(
            hook_registry.check_cursor_installation(install_path, path=args.registry)
        )

    if problems:
        print("verify-cursor-hook-registry: FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    detail = "registry structure and Cursor guardian mappings"
    if install_path is not None:
        detail += f"; live install {install_path} matches expectations"
    print(f"verify-cursor-hook-registry: OK — {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
