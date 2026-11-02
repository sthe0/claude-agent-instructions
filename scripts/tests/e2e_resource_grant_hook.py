"""End-to-end check of the resource-grant hook's registration in a spawned child.

    python3 scripts/tests/e2e_resource_grant_hook.py --dry-run   # any session; starts no `claude`
    python3 scripts/tests/e2e_resource_grant_hook.py --live      # root only; starts a real child

`--dry-run` drives a real engine session to EXECUTING (the pytest fixtures isolate every ledger) and
asserts that `spawn-specialist.py --dry-run` puts the hook into the child's `--settings`
(`hooks.PreToolUse`, matcher, timeout from `resource-grant-hook-timeout-s`), keeps the declared
grant in `permissions.allow` and lists the `--add-dir` paths; a switched-off kill switch registers
nothing. It prints the child payload it asserted on.

`--live` is for the root coordinator: a spawned developer cannot start `claude`. It spawns a real
linked child for a stage whose branch it may commit to, asks it to run a covered
`echo probe > <venue>/probe.txt`, an uncovered `touch <outside>` and a commit, and checks the decision
log it left: one `covered`, one `uncovered-write` deny, the commit logged `undeterminable` (the hook
never allows a line that runs git) and a file that was not created.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
HOOK_TESTS = HERE / "test_hook_resource_grant_check.py"


def dry_run() -> int:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(HOOK_TESTS), "-k", "dry_run", "-q", "-rP", "-p", "no:cacheprovider"],
        capture_output=True, text=True,
    )
    for line in done.stdout.splitlines():
        if line.startswith("child payload:") or " passed" in line or " failed" in line or " error" in line:
            print(line)
    if done.returncode != 0:
        print(done.stdout[-2000:])
        print(done.stderr[-1000:], file=sys.stderr)
    return done.returncode


def live() -> int:
    if int(os.environ.get("AGENT_RECURSION_DEPTH", "0") or 0) != 0:
        print("error: --live is root-only (AGENT_RECURSION_DEPTH must be 0)", file=sys.stderr)
        return 2
    if shutil.which("claude") is None:
        print("error: `claude` is not on PATH", file=sys.stderr)
        return 2
    sys.path[:0] = [str(HERE), str(SCRIPTS)]
    import resource_grant_hook_support as support
    import test_tool_contracts_segments as seg_tests

    work = Path(tempfile.mkdtemp(prefix="resource-grant-hook-live-"))
    venue = seg_tests._init_repo(work / "venue", "feat")
    outside = work / "outside" / "must-not-exist"
    spawn = support.load_spawn()
    child = support.link_child(spawn, work / "state", venue, branch="feat", allow=[])
    brief = (
        f"In {venue} run these three commands one at a time and report each outcome verbatim: "
        f"`echo probe > {venue}/probe.txt`, `touch {outside}`, `git commit --allow-empty -m probe`."
    )
    done = subprocess.run(
        ["claude", "-p", "--settings", json.dumps({"hooks": {"PreToolUse": [{
            "matcher": spawn.RESOURCE_GRANT_HOOK_MATCHER,
            "hooks": [{"type": "command", "command": spawn.resource_grant_hook_command(), "timeout": 20}]}]}}),
         "--permission-mode", child.mode, "--add-dir", str(venue)],
        input=brief, env={**os.environ, **child.env}, cwd=venue, capture_output=True, text=True, timeout=600,
    )
    rows = child.log_rows()
    classes = [row["reason_class"] for row in rows]
    print(json.dumps({"rc": done.returncode, "reason_classes": classes, "outside_created": outside.exists()}))
    ok = {"covered", "uncovered-write", "undeterminable"} <= set(classes) and not outside.exists()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    return dry_run() if args.dry_run else live()


if __name__ == "__main__":
    sys.exit(main())
