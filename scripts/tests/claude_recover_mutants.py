#!/usr/bin/env python3
"""Mutation catalogue for scripts/claude-recover.py.

Why: a green test suite does not show that the tests can tell a broken script from a correct
one. Each mutant here is a named textual edit of a copy of the module; the suite is run
against the copy through $CLAUDE_RECOVER_PY, and a mutant is *killed* when the suite fails.

    claude_recover_mutants.py list
    claude_recover_mutants.py run <name>                  1 = killed (the suite failed on the
                                                          mutant), 0 = survived, 3 = the
                                                          catalogue itself is broken (anchor not
                                                          found, mutated copy does not compile,
                                                          pytest ended with neither pass nor fail)
    claude_recover_mutants.py --expect-killed <name>...   exit 0 only if every named mutant exits 1;
                                                          a survivor or an error is exit 1

An error is not a kill: a suite that cannot even be collected "fails" for the wrong reason.

scripts/tests/test_claude_recover_mutants.py runs this catalogue from pytest.
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
    "snapshot-during-restore": (
        "a snapshot of the new boot is written while this boot's restore is pending or running",
        "        reason = restore_pending(sd, boot_id)\n",
        "        reason = None\n",
    ),
    "stale-tmp-kills-live": (
        "a temp file of a live writer is deleted as stale",
        "        if _tmp_in_use(stale):\n",
        "        if False:\n",
    ),
    "phase-name-drift": (
        "the first phase is named disk instead of disk-pre",
        'PHASES = ("disk-pre", "mounts"',
        'PHASES = ("disk", "mounts"',
    ),
    "deadline-override-unclamped": (
        "CLAUDE_RECOVER_DEADLINE_S may exceed the DEADLINE_S constant",
        'Deadline(min(env_float("CLAUDE_RECOVER_DEADLINE_S", DEADLINE_S), DEADLINE_S))',
        'Deadline(env_float("CLAUDE_RECOVER_DEADLINE_S", DEADLINE_S))',
    ),
    "hook-log-is-recover-log": (
        "hooks append to recover.log, so their output is logged twice",
        'sd / "restore-plan.json", sd / "hooks.log", hook_files)',
        'sd / "restore-plan.json", log_path, hook_files)',
    ),
    "hook-output-untruncated-in-plan": (
        "the plan keeps 2000 chars of hook output instead of 500",
        "tail_text(out, HOOK_PLAN_OUTPUT_CHARS)",
        "tail_text(out)",
    ),
    "session-command-new-session": (
        "a session is reopened with new-session, which would create a tmux session instead of a window",
        '"new-window", "-d", "-t"',
        '"new-session", "-d", "-t"',
    ),
    "restore-calls-systemctl": (
        "a restore run calls systemctl",
        "    def run(self) -> None:\n        for phase in PHASES:\n",
        "    def run(self) -> None:\n"
        '        subprocess.run(["systemctl", "--user", "show", "x"], check=False)\n'
        "        for phase in PHASES:\n",
    ),
    "restore-kill-server": (
        "a restore run kills the tmux server",
        "    def run(self) -> None:\n        for phase in PHASES:\n",
        '    def run(self) -> None:\n        tmux_run(["kill-server"])\n        for phase in PHASES:\n',
    ),
    "restore-kill-window": (
        "a window is killed before the session's new window is opened",
        '                rc, msg = tmux_run(sess["command"][1:])\n',
        '                tmux_run(["kill-window", "-t", "x"])\n'
        '                rc, msg = tmux_run(sess["command"][1:])\n',
    ),
    "restore-kill-session": (
        "a tmux session is killed before the session's new window is opened",
        '                rc, msg = tmux_run(sess["command"][1:])\n',
        '                tmux_run(["kill-session", "-t", "x"])\n'
        '                rc, msg = tmux_run(sess["command"][1:])\n',
    ),
    "hook-exit-10-is-failure": (
        "a hook's 'nothing to do' exit code counts as a failure",
        "return all(rc in (0, HOOK_EXIT_NOTHING) for rc in exits)",
        "return all(rc == 0 for rc in exits)",
    ),
    "disk-level-env-constant": (
        "hooks are told the disk is ok whatever the measured level",
        'RECOVER_DISK_LEVEL=self.plan["disk"]["level"]',
        'RECOVER_DISK_LEVEL="ok"',
    ),
    "disk-warn-never": (
        "a disk that is low but not critical is classed ok",
        '    if free_gib < DISK_WARN_GIB or free_pct < DISK_WARN_PCT:\n        return "warn"',
        '    if False:\n        return "warn"',
    ),
    "disk-hard-never": (
        "a nearly full disk is never classed hard",
        '    if free_gib < DISK_HARD_GIB:\n        return "hard"',
        '    if False:\n        return "hard"',
    ),
    "disk-warn-needs-both": (
        "the warn level needs both the GiB and the percentage threshold to be crossed",
        "if free_gib < DISK_WARN_GIB or free_pct < DISK_WARN_PCT:",
        "if free_gib < DISK_WARN_GIB and free_pct < DISK_WARN_PCT:",
    ),
    "dry-run-env-zero": (
        "hooks of a dry run are told RECOVER_DRY_RUN=0",
        'RECOVER_DRY_RUN="1" if self.dry_run else "0"',
        'RECOVER_DRY_RUN="0"',
    ),
    "dry-run-opens-sessions": (
        "a dry run opens tmux windows",
        "if self.dry_run or not pending:",
        "if not pending:",
    ),
    "dry-run-mounts-executed": (
        "a dry run goes through the mounting loop, which redecides and rewrites the plan",
        '        if self.dry_run:\n            self.run_hooks("mounts", cap)\n            return\n',
        "",
    ),
    "dry-run-expires-remaining": (
        "a dry run turns every planned entry into 'skipped: deadline reached'",
        "        if not self.dry_run:\n            self.expire_remaining()",
        "        self.expire_remaining()",
    ),
    "dry-run-confirm-marked-skipped": (
        "a dry run marks the confirm-class actions skipped instead of leaving them planned",
        '                if not self.dry_run:\n                    act["status"], act["detail"] = "skipped", "needs your decision',
        '                if True:\n                    act["status"], act["detail"] = "skipped", "needs your decision',
    ),
    "dry-run-core-action-runs": (
        "core cleanup actions run only in a dry run (the guard is inverted)",
        'elif act["owner"] == "core" and not self.dry_run:',
        'elif act["owner"] == "core" and self.dry_run:',
    ),
    "dry-run-hook-status-set": (
        "a dry run records a status on the hook-owned cleanup actions",
        'if act["phase"] == phase and act["owner"] == "hook" and not self.dry_run:',
        'if act["phase"] == phase and act["owner"] == "hook":',
    ),
    "dry-run-disk-measured": (
        "a dry run re-measures the disk after a phase and re-decides mounts and sessions",
        '        if not self.dry_run:\n            self.plan["disk"].update(measure_disk())',
        '        if True:\n            self.plan["disk"].update(measure_disk())',
    ),
    "dry-run-real-state-dir": (
        "a dry run takes the real-run branch and writes into the real state directory",
        '    if args.dry_run:\n        with tempfile.TemporaryDirectory(prefix="claude-recover-dry-")',
        '    if False:\n        with tempfile.TemporaryDirectory(prefix="claude-recover-dry-")',
    ),
    "restore-sha256-unchecked": (
        "a snapshot whose content does not match its sha256 is restored",
        'if "sha256" in snapshot and snapshot["sha256"] != content_hash(snapshot):',
        "if False:",
    ),
    "snapshot-always-rewrites": (
        "every snapshot run writes a new file, even when nothing changed",
        'if args.force or snap["sha256"] != previous_sha:',
        "if True:",
    ),
    "boot-id-regex-permissive": (
        "any string is accepted as a boot id, so it can name a path outside the state directory",
        're.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")',
        're.compile(r".*")',
    ),
    "simulate-reboot-unguarded": (
        "--simulate-reboot is accepted without --dry-run",
        "    if args.simulate_reboot and not args.dry_run:\n",
        "    if False:\n",
    ),
    "simulate-reboot-reads-live": (
        "a simulated reboot still reads what is really mounted and running",
        '    if not args.simulate_reboot:\n        live["mounted"]',
        '    if True:\n        live["mounted"]',
    ),
    "snapshot-tmux-error-exit-0": (
        "snapshot reports success when tmux could not be read",
        'snapshot not taken: {exc}", file=sys.stderr)\n            return 1',
        'snapshot not taken: {exc}", file=sys.stderr)\n            return 0',
    ),
}

# Occurrences of `dry_run` in claude-recover.py that are not a guard of a side effect: a mutant
# anchor covers every guard, this table covers the rest. fragment -> why it needs no mutant.
# test_claude_recover_mutants.py fails when a `dry_run` occurrence is in neither.
DRY_RUN_GUARD_EXEMPT: dict[str, str] = {
    'self.dry_run = args.dry_run':
        "stores the flag; the behaviours it controls are the guard sites",
    '"dry_run": config["dry_run"],':
        "copies the flag into the plan; the plan_json_contract test asserts the key",
    '"dry_run": args.dry_run,':
        "builds the restore config from the flag; test_restore_is_the_default_command asserts plan['dry_run']",
    "'dry-run' if plan['dry_run'] else 'restore'":
        "the title line of the text report",
}


def mutate(name: str, workdir: Path) -> Path | None:
    _why, anchor, replacement = MUTANTS[name]
    source = TARGET.read_text()
    if anchor not in source:
        return None
    path = workdir / "claude-recover.py"
    path.write_text(source.replace(anchor, replacement, 1))
    return path


KILLED, SURVIVED, CATALOGUE_ERROR = 1, 0, 3


def run_mutant(name: str) -> int:
    """KILLED when the suite ran and failed, SURVIVED when it passed, CATALOGUE_ERROR otherwise.

    Only pytest's exit code 1 is a kill; a collection error or an interrupt (codes 2-5) says
    nothing about the tests. `-p no:xdist` keeps the inner run to one process even when the outer
    pytest was started with -n.
    """
    with tempfile.TemporaryDirectory(prefix="claude-recover-mutant-") as tmp:
        mutant = mutate(name, Path(tmp))
        if mutant is None:
            print(f"{name}: ERROR mutation anchor not found; the control does not discriminate",
                  file=sys.stderr)
            return CATALOGUE_ERROR
        compiled = subprocess.run([sys.executable, "-m", "py_compile", str(mutant)],
                                  capture_output=True, text=True)
        if compiled.returncode:
            print(f"{name}: ERROR the mutated copy does not compile: "
                  f"{compiled.stderr.strip().splitlines()[-1:]}", file=sys.stderr)
            return CATALOGUE_ERROR
        env = dict(os.environ, CLAUDE_RECOVER_PY=str(mutant))
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:xdist",
             f"--rootdir={HERE}", str(TEST_FILE)],
            env=env, capture_output=True, text=True, cwd=tmp)
        if res.returncode == 1:
            failed = next((ln for ln in res.stdout.splitlines() if ln.startswith("FAILED ")), "FAILED ?")
            print(f"{name}: killed by {failed.split(' - ')[0][len('FAILED '):]}")
            return KILLED
        if res.returncode == 0:
            print(f"{name}: SURVIVED")
            return SURVIVED
        tail = (res.stdout.strip().splitlines() or [""])[-1]
        print(f"{name}: ERROR pytest exited {res.returncode} ({tail})", file=sys.stderr)
        return CATALOGUE_ERROR


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
        not_killed = [name for name in args.expect_killed if run_mutant(name) != KILLED]
        if not_killed:
            print(f"NOT KILLED: {', '.join(not_killed)}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "run":
        return run_mutant(args.name)
    for name, (why, _a, _r) in MUTANTS.items():
        print(f"{name}\t{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
