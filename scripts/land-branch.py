#!/usr/bin/env python3
"""Land a branch onto trunk with ref-only, fast-forward-only git operations.

Mechanizes the dangerous, deterministic part of "land my working branch into
main": advance trunk to the branch tip and drop the now-landed branch, using
only ref pushes and a local `git branch -f` — never checkout, reset, or
clean, so the caller's working tree and index are never touched. Refuses
(exit 2) rather than force whenever the fast-forward precondition fails.

  land-branch.py --check [--branch B] [--trunk main] [--remote origin] [-C DIR]
      Report landability only. Zero side effects. Prints "LANDABLE: ..."
      followed by the exact commands that would run (exit 0), or
      "NOT-LANDABLE: <reason>" (exit 2).

  land-branch.py [--branch B] [--trunk main] [--remote origin] [-C DIR]
      Re-checks landability, then runs, in order:
        git push <remote> <branch>:<trunk>
        git push <remote> --delete <branch>
        git branch -f <trunk> <branch-tip-sha>
      Exit 0 on full success, 2 if refused (not landable), 3 if a git
      command failed partway (state is reported so it can be finished
      manually).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )


def _rev_parse(repo_root, ref):
    proc = _git(["rev-parse", "--verify", "--quiet", ref], repo_root)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _resolve_branch(repo_root, explicit_branch):
    """Returns (branch, error). error is set iff branch could not be inferred."""
    if explicit_branch:
        return explicit_branch, None
    proc = _git(["symbolic-ref", "--short", "-q", "HEAD"], repo_root)
    if proc.returncode != 0:
        return None, "detached HEAD: cannot infer branch (pass --branch explicitly)"
    return proc.stdout.strip(), None


@dataclass
class Assessment:
    ok: bool
    reason: str
    branch: str | None
    trunk: str
    remote: str
    branch_sha: str | None = None

    def commands(self) -> list[str]:
        if not self.ok:
            return []
        return [
            f"git push {self.remote} {self.branch}:{self.trunk}",
            f"git push {self.remote} --delete {self.branch}",
            f"git branch -f {self.trunk} {self.branch_sha}",
        ]


def assess(repo_root, branch_arg, trunk, remote) -> Assessment:
    branch, err = _resolve_branch(repo_root, branch_arg)
    if err:
        return Assessment(False, err, branch, trunk, remote)

    if branch == trunk:
        return Assessment(False, f"branch equals trunk ({branch!r})", branch, trunk, remote)

    branch_sha = _rev_parse(repo_root, branch)
    if branch_sha is None:
        return Assessment(False, f"branch ref not found: {branch!r}", branch, trunk, remote)

    trunk_sha = _rev_parse(repo_root, trunk)
    if trunk_sha is None:
        return Assessment(
            False, f"trunk ref not found: {trunk!r}", branch, trunk, remote, branch_sha
        )

    if trunk_sha == branch_sha:
        return Assessment(
            False,
            f"nothing to land: {trunk!r} is already at {branch!r}'s tip",
            branch, trunk, remote, branch_sha,
        )

    is_ancestor = _git(["merge-base", "--is-ancestor", trunk_sha, branch_sha], repo_root)
    if is_ancestor.returncode != 0:
        return Assessment(
            False,
            f"non-fast-forward: {trunk!r} is not an ancestor of {branch!r} "
            "(diverged or unrelated histories)",
            branch, trunk, remote, branch_sha,
        )

    return Assessment(True, "clean fast-forward", branch, trunk, remote, branch_sha)


def _report_check(assessment: Assessment) -> int:
    if not assessment.ok:
        print(f"NOT-LANDABLE: {assessment.reason}")
        return 2
    print(
        f"LANDABLE: {assessment.branch} -> {assessment.trunk} via "
        f"{assessment.remote} ({assessment.reason})"
    )
    for cmd in assessment.commands():
        print(f"  {cmd}")
    return 0


def _do_land(repo_root, assessment: Assessment) -> int:
    if not assessment.ok:
        print(f"NOT-LANDABLE: {assessment.reason}", file=sys.stderr)
        return 2

    remote, branch, trunk, sha = (
        assessment.remote, assessment.branch, assessment.trunk, assessment.branch_sha
    )

    push_landing = _git(["push", remote, f"{branch}:{trunk}"], repo_root)
    if push_landing.returncode != 0:
        print(
            f"[land-branch] push {branch}:{trunk} to {remote} FAILED (nothing else "
            f"attempted):\n{push_landing.stderr.strip()}",
            file=sys.stderr,
        )
        return 3
    print(f"[land-branch] pushed {branch} -> {remote}/{trunk} ({sha})")

    delete_remote = _git(["push", remote, "--delete", branch], repo_root)
    delete_ok = delete_remote.returncode == 0
    if delete_ok:
        print(f"[land-branch] deleted {remote}/{branch}")
    else:
        print(
            f"[land-branch] WARNING: could not delete {remote}/{branch} "
            f"(manually: git push {remote} --delete {branch}):\n"
            f"{delete_remote.stderr.strip()}",
            file=sys.stderr,
        )

    branch_f = _git(["branch", "-f", trunk, sha], repo_root)
    branch_f_ok = branch_f.returncode == 0
    if branch_f_ok:
        print(f"[land-branch] local {trunk} fast-forwarded to {sha}")
    else:
        print(
            f"[land-branch] WARNING: could not fast-forward local {trunk} "
            f"(manually: git branch -f {trunk} {sha}):\n{branch_f.stderr.strip()}",
            file=sys.stderr,
        )

    if delete_ok and branch_f_ok:
        print(f"[land-branch] landed {branch} onto {trunk} ({sha})")
        return 0
    return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("-C", dest="repo_dir", default=".", help="run as if started in DIR")
    parser.add_argument("--branch", default=None, help="branch to land (default: current branch)")
    parser.add_argument("--trunk", default="main", help="trunk branch to land onto (default: main)")
    parser.add_argument("--remote", default="origin", help="remote name (default: origin)")
    parser.add_argument(
        "--check", action="store_true", help="report landability only; make no changes"
    )
    args = parser.parse_args(argv)

    root_proc = _git(["rev-parse", "--show-toplevel"], args.repo_dir)
    if root_proc.returncode != 0:
        print(f"[land-branch] not a git repository: {args.repo_dir}", file=sys.stderr)
        return 2
    repo_root = root_proc.stdout.strip()

    assessment = assess(repo_root, args.branch, args.trunk, args.remote)

    if args.check:
        return _report_check(assessment)
    return _do_land(repo_root, assessment)


if __name__ == "__main__":
    sys.exit(main())
