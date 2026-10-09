#!/usr/bin/env python3
"""Mutation catalogue for scripts/claude-recover.py.

Why: a green test suite does not show that the tests can tell a broken script from a correct
one. Each mutant here is a named textual edit of a copy of the module; the suite is run
against the copy through $CLAUDE_RECOVER_PY, and a mutant is *killed* when the suite fails.

    claude_recover_mutants.py list
    claude_recover_mutants.py run <name>                  exit code of the suite on the mutant
                                                          (non-zero = killed); a mutation anchor
                                                          that is not found is reported and
                                                          exits 0, i.e. the control does not
                                                          discriminate
    claude_recover_mutants.py --expect-killed <name>...   exit 0 only if every named mutant is
                                                          killed
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "claude-recover.py"
TEST_FILE = HERE / "test_claude_recover.py"

# name -> (what it breaks, anchor text, replacement)
MUTANTS: dict[str, tuple[str, str, str]] = {
    "env-whitelist-off": (
        "process environment copied wholesale instead of whitelisted",
        "out = {k: environ[k] for k in ENV_WHITELIST if k in environ}",
        "out = dict(environ)",
    ),
    "nested-claude-no-dedup": (
        "every claude under a pane is recorded, not only the top one",
        "chosen = [min(items, key=_top_key)]",
        "chosen = items",
    ),
    "rotation-drops-prev-boot": (
        "rotation deletes the last snapshot of previous boots too",
        "        keep = set(files[-1:])\n",
        "        keep = set()\n",
    ),
    "check-fresh-ignores-age": (
        "an arbitrarily old last-ok stamp counts as fresh",
        "if now() - stamp > args.check_fresh:",
        "if False:",
    ),
    "select-ignores-boot-id": (
        "the current boot's snapshot can be chosen for restore",
        "if not bdir.is_dir() or (bdir.name == current_boot_id and not any_boot):",
        "if not bdir.is_dir():",
    ),
    "unchanged-skips-stamp": (
        "the last-ok stamp is refreshed only when a new snapshot file was written",
        "touch_stamp(sd, ts)\n    print(",
        "if written:\n            touch_stamp(sd, ts)\n    print(",
    ),
    "tmux-failure-writes-empty": (
        "a failing tmux is read as 'no panes', so an empty snapshot is written and the stamp refreshed",
        'raise TmuxError(f"{tmux} list-panes exited {res.returncode}: {res.stderr.strip()[:200]}")',
        "return []",
    ),
    "collision-suffix-order": (
        "same-second collision suffix sorts before the original file name",
        'COLLISION_SEP = "~"',
        'COLLISION_SEP = "-"',
    ),
    "rotation-drops-nonempty": (
        "rotation keeps only the newest snapshot of a previous boot even if it is empty",
        "        if newest_nonempty is not None:\n            keep.add(newest_nonempty)\n",
        "",
    ),
    "write-not-atomic": (
        "files are written in place instead of via a temp file and rename",
        'tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")',
        "tmp = path",
    ),
    "status-corrupt-raises": (
        "a corrupt latest snapshot makes status crash instead of failing",
        "except (OSError, ValueError) as exc:\n        return _fail(f\"latest snapshot",
        "except OSError as exc:\n        return _fail(f\"latest snapshot",
    ),
    "variadic-flag-single-value": (
        "--add-dir keeps only its first value",
        "skip_value_of = \"keep-many\"",
        "skip_value_of = \"keep\"",
    ),
    "state-dir-world-readable": (
        "state directories are created with default permissions",
        "path.mkdir(mode=0o700, parents=True, exist_ok=True)\n    path.chmod(0o700)",
        "path.mkdir(parents=True, exist_ok=True)",
    ),
    "stale-tmp-kept": (
        "leftover temp files are never cleaned",
        "        remove_stale_tmp(sd)\n",
        "",
    ),
    "mount-no-skip": (
        "an already mounted target is planned for mounting again",
        "        if target in mounted:\n",
        "        if False:\n",
    ),
    "dry-run-executes": (
        "a dry run runs the cleanup commands",
        'elif act["owner"] == "core" and not self.dry_run:',
        'elif act["owner"] == "core":',
    ),
    "confirm-executes": (
        "commands that need a human decision are run",
        '            if act["class"] == "confirm":\n',
        "            if False:\n",
    ),
    "hard-mode-mounts-all": (
        "with a nearly full disk every mount is restored, not only the essential ones",
        'elif level == "hard" and not is_essential:',
        "elif False:",
    ),
    "alive-not-skipped": (
        "a session that is already running is opened a second time",
        "        if sid in alive:\n",
        "        if False:\n",
    ),
    "starts-tmux-server": (
        "waiting for the tmux session starts a tmux server",
        "    end = time.monotonic() + wait_s\n",
        '    tmux_run(["start-server"])\n    end = time.monotonic() + wait_s\n',
    ),
    "phase-name-drift": (
        "the first phase is named disk instead of disk-pre",
        'PHASES = ("disk-pre", "mounts"',
        'PHASES = ("disk", "mounts"',
    ),
}


def mutate(name: str, workdir: Path) -> Path | None:
    _why, anchor, replacement = MUTANTS[name]
    source = TARGET.read_text()
    if anchor not in source:
        return None
    path = workdir / "claude-recover.py"
    path.write_text(source.replace(anchor, replacement, 1))
    return path


def run_mutant(name: str) -> int:
    with tempfile.TemporaryDirectory(prefix="claude-recover-mutant-") as tmp:
        mutant = mutate(name, Path(tmp))
        if mutant is None:
            print(f"{name}: mutation anchor not found; the control does not discriminate",
                  file=sys.stderr)
            return 0
        env = dict(os.environ, CLAUDE_RECOVER_PY=str(mutant))
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
             f"--rootdir={HERE}", str(TEST_FILE)],
            env=env, capture_output=True, text=True, cwd=tmp)
        tail = res.stdout.strip().splitlines()[-1:] or [""]
        print(f"{name}: {'killed' if res.returncode else 'SURVIVED'} ({tail[0]})")
        return res.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--expect-killed", nargs="+", metavar="MUTANT")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list")
    p_run = sub.add_parser("run")
    p_run.add_argument("name", choices=sorted(MUTANTS))
    args = parser.parse_args(argv)
    if args.expect_killed:
        unknown = [n for n in args.expect_killed if n not in MUTANTS]
        if unknown:
            parser.error(f"unknown mutants: {unknown}")
        survivors = []
        for name in args.expect_killed:
            if run_mutant(name) == 0:
                survivors.append(name)
        if survivors:
            print(f"NOT KILLED: {', '.join(survivors)}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "run":
        return run_mutant(args.name)
    for name, (why, _a, _r) in MUTANTS.items():
        print(f"{name}\t{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
