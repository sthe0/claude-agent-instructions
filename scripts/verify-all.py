#!/usr/bin/env python3
"""Run all instruction-repo verifiers.

Each verifier is a sibling script `verify-<name>.py` exposing a `main(argv)`
that returns an exit code. Add new checks to CHECKS.

Modes:
  --staged   only files staged for commit (pre-commit hook uses this)
  default    all tracked files
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

CHECKS: list[str] = [
    "verify-language",
    "lint-permissions",
    "verify-cross-refs",
    "lint-cursor-mirror",
]


def load_check(name: str, scripts_dir: Path):
    path = scripts_dir / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)

    scripts_dir = Path(__file__).resolve().parent
    sub_argv = ["--staged"] if args.staged else []

    failed: list[str] = []
    for name in CHECKS:
        mod = load_check(name, scripts_dir)
        if mod is None:
            print(f"verify-all: skip {name} (missing)")
            continue
        print(f"=== {name} ===")
        rc = mod.main(sub_argv)
        if rc != 0:
            failed.append(name)
        print()

    if failed:
        print(f"verify-all: FAIL — {len(failed)}/{len(CHECKS)} check(s) failed: {', '.join(failed)}")
        return 1
    print(f"verify-all: OK — all {len(CHECKS)} check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
