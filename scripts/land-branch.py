#!/usr/bin/env python3
"""Land a branch onto trunk with ref-only, fast-forward-only git operations.

Mechanizes the dangerous, deterministic part of "land my working branch into
main": advance trunk to the branch tip and drop the now-landed branch, using
only ref pushes and a local `git branch -f` — never checkout, reset, or
clean, so the caller's working tree and index are never touched. Refuses
(exit 2) rather than force whenever the fast-forward precondition fails.

Branch deletion is PART of landing, not a separate ask: once the branch tip
is pushed onto trunk, the branch (remote + local) and its linked worktree are
leftovers, so landing finishes by removing them by default. Pass --keep-branch
to opt out (e.g. a release branch cut from the landed tip must outlive it).

  land-branch.py --check [--branch B] [--trunk main] [--remote origin] [-C DIR]
      Report landability only. Zero side effects. Prints "LANDABLE: ..."
      followed by the exact commands that would run (exit 0), or
      "NOT-LANDABLE: <reason>" (exit 2). The command list reflects the chosen
      mode (--remote-only, --keep-branch). A final "SMOKE-STATUS: ..." line
      reports the instruction smoke record (not required / admitted / pending)
      from local state only — no fetch, no smoke run; a pending record does not
      change the exit code.

  land-branch.py [--branch B] [--trunk main] [--remote origin] [-C DIR]
                 [--remote-only] [--keep-branch]
                 [--smoke-waiver REASON] [--smoke-timeout SECONDS]
      Re-checks landability, then applies the instruction smoke gate (below),
      then runs, in order:
        git push <remote> <branch>:<trunk>
        git push <remote> --delete <branch>          (unless --keep-branch)
        git branch -f <trunk> <branch-tip-sha>       (unless --remote-only)
        git worktree remove <path>                   (the branch's worktree,
                                                      unless --keep-branch)
        git branch -D <branch>                        (unless --keep-branch)
      --remote-only skips ONLY the local `git branch -f <trunk>` step, for a
      trunk that is checked out or pinned under foreign WIP (the caller's later
      `git pull --ff-only` advances local trunk instead); landability is still
      assessed against the LOCAL trunk ref.
      --keep-branch skips ALL deletion steps (remote branch, worktree, local
      branch).
      Exit 0 on full success, 2 if refused (not landable), 3 if a
      landing-critical git command failed partway (state is reported so it can
      be finished manually).

      Instruction smoke gate (scripts/instruction-smoke-gate.py, library
      scripts/lib/instruction_smoke_gate.py): before any push it fetches
      <remote> <trunk>, refuses (NOT-LANDABLE, exit 2, nothing pushed) unless
      that tip is an ancestor of the branch tip, and decides whether the gate
      applies — scripts/instruction-sandbox.sh in the base or the candidate
      tree — and whether the diff touches the instruction surface. A diff that
      does is landed only on an admitted smoke record bound to the candidate
      and that remote tip: a stored record is reused, otherwise the smoke runs
      here (one live `claude -p` launch, minutes). Exactly one "SMOKE: ..."
      line is printed before the push. --smoke-waiver REASON admits an
      UNAVAILABLE live launch with the reason recorded; --smoke-timeout bounds
      that launch. The pre-push hook (githooks/pre-push) re-checks the same
      record at push time; its "pre-push:" lines are relayed on success.

      Exit-code asymmetry (deliberate, not an accident): a remote-branch delete
      failure keeps the established exit 3 (backward-compat). Worktree / local-
      branch cleanup refusals stay exit 0 with a WARNING — the LANDING itself
      already succeeded, and the leftover branch is visible to the resolution-
      reminder probe (hook-resolution-reminder.py), which will nudge its
      deletion at the next gate.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import instruction_smoke_gate as gate  # noqa: E402


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
    remote_only: bool = False
    keep_branch: bool = False

    def commands(self) -> list[str]:
        if not self.ok:
            return []
        cmds = [f"git push {self.remote} {self.branch}:{self.trunk}"]
        if not self.keep_branch:
            cmds.append(f"git push {self.remote} --delete {self.branch}")
        if not self.remote_only:
            cmds.append(f"git branch -f {self.trunk} {self.branch_sha}")
        if not self.keep_branch:
            cmds.append(f"git worktree remove <worktree-on-{self.branch}>")
            cmds.append(f"git branch -D {self.branch}")
        return cmds


def assess(repo_root, branch_arg, trunk, remote, remote_only=False, keep_branch=False) -> Assessment:
    def _mk(ok, reason, branch, branch_sha=None):
        return Assessment(
            ok, reason, branch, trunk, remote, branch_sha,
            remote_only=remote_only, keep_branch=keep_branch,
        )

    branch, err = _resolve_branch(repo_root, branch_arg)
    if err:
        return _mk(False, err, branch)

    if branch == trunk:
        return _mk(False, f"branch equals trunk ({branch!r})", branch)

    branch_sha = _rev_parse(repo_root, branch)
    if branch_sha is None:
        return _mk(False, f"branch ref not found: {branch!r}", branch)

    trunk_sha = _rev_parse(repo_root, trunk)
    if trunk_sha is None:
        return _mk(False, f"trunk ref not found: {trunk!r}", branch, branch_sha)

    if trunk_sha == branch_sha:
        return _mk(
            False,
            f"nothing to land: {trunk!r} is already at {branch!r}'s tip",
            branch, branch_sha,
        )

    is_ancestor = _git(["merge-base", "--is-ancestor", trunk_sha, branch_sha], repo_root)
    if is_ancestor.returncode != 0:
        return _mk(
            False,
            f"non-fast-forward: {trunk!r} is not an ancestor of {branch!r} "
            "(diverged or unrelated histories)",
            branch, branch_sha,
        )

    return _mk(True, "clean fast-forward", branch, branch_sha)


def _stored_record(repo_root, sha):
    try:
        return gate.load_record(gate.git_common_dir(repo_root), sha)
    except gate.RecordError:
        return None


def _smoke_status(repo_root, assessment: Assessment) -> str:
    """One line on the stored smoke record against the LOCAL remote-tracking ref.

    Reads local state only (no fetch, no sandbox, no write), so `--check` stays the cheap,
    side-effect-free probe hook-resolution-reminder.py relies on."""
    try:
        tip = gate.resolve_commit(repo_root, f"refs/remotes/{assessment.remote}/{assessment.trunk}")
        sha = assessment.branch_sha
        if not gate.gate_applies(repo_root, tip, sha):
            return "SMOKE-STATUS: not required (repository has no instruction sandbox)"
        paths = gate.changed_paths(repo_root, tip, sha)
        decision = gate.decide(paths, True, _stored_record(repo_root, sha), sha, tip)
    except gate.GateError as exc:
        return gate.defuse(f"SMOKE-STATUS: unknown ({gate.one_line(exc, 200)})")
    if decision.kind == gate.NOT_REQUIRED:
        return f"SMOKE-STATUS: not required ({decision.reason})"
    if decision.allowed:
        return f"SMOKE-STATUS: admitted ({gate.short(sha)}: {decision.reason})"
    return gate.defuse(
        f"SMOKE-STATUS: pending ({decision.reason}); landing runs the smoke, "
        f"or run: {gate.RUN_COMMAND} --ref {assessment.branch}"
    )


def _report_check(repo_root, assessment: Assessment) -> int:
    if not assessment.ok:
        print(f"NOT-LANDABLE: {assessment.reason}")
        return 2
    print(
        f"LANDABLE: {assessment.branch} -> {assessment.trunk} via "
        f"{assessment.remote} ({assessment.reason})"
    )
    for cmd in assessment.commands():
        print(f"  {cmd}")
    print(_smoke_status(repo_root, assessment))
    return 0


def _smoke_gate(repo_root, assessment: Assessment, timeout_s, waiver, runner):
    """The pre-push smoke gate. Returns (None, line) when landing may proceed, else (reason, None).

    Fetches the remote tip, requires it to be an ancestor of the candidate, then decides through
    the library: not required, a stored admitted record, or a fresh smoke run."""
    remote, branch, trunk, sha = (
        assessment.remote, assessment.branch, assessment.trunk, assessment.branch_sha
    )
    try:
        tip = gate.fetch_remote_tip(repo_root, remote, trunk)
        if not gate.is_ancestor(repo_root, tip, sha):
            return (
                f"{remote}/{trunk} {gate.short(tip)} is not an ancestor of {branch!r}; "
                f"merge {remote}/{trunk} into {branch!r} and rerun",
                None,
            )
        if not gate.gate_applies(repo_root, tip, sha):
            return None, "SMOKE: not required (repository has no instruction sandbox)"
        paths = gate.changed_paths(repo_root, tip, sha)
        if not gate.touches_surface(paths):
            return None, "SMOKE: not required (diff touches only exempt paths)"
        stored = _stored_record(repo_root, sha)
        decision = gate.decide(paths, True, stored, sha, tip)
        if decision.allowed:
            return None, (
                f"SMOKE: admitted {gate.short(sha)} by stored record "
                f"({gate.admission_summary(stored)})"
            )
        outcome = gate.run_smoke(
            repo_root, ref=sha, remote=remote, trunk=trunk,
            timeout_s=timeout_s, waiver=waiver, runner=runner,
        )
    except gate.GateError as exc:
        return f"instruction smoke gate: {gate.one_line(exc, 400)}", None
    if not outcome.admitted:
        kept = f"; sandbox kept at {outcome.sandbox_root}" if outcome.sandbox_root else ""
        return (
            f"instruction smoke gate: {outcome.record['result']} for {gate.short(sha)}: "
            f"{outcome.reason} (record {outcome.record_path}{kept})",
            None,
        )
    return None, (
        f"SMOKE: ran and admitted {gate.short(sha)} "
        f"({gate.admission_summary(outcome.record)}; record {outcome.record_path})"
    )


def _worktree_for_branch(repo_root, branch):
    """Path of the linked worktree that has `branch` checked out, or None.

    Parses `git worktree list --porcelain` into per-worktree records and
    returns the first whose branch matches. Any failure -> None (cleanup then
    falls back to a direct `git branch -D`)."""
    proc = _git(["worktree", "list", "--porcelain"], repo_root)
    if proc.returncode != 0:
        return None
    path = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            if ref == f"refs/heads/{branch}" and path is not None:
                return path
    return None


def _cleanup_local_branch(repo_root, branch):
    """Remove the landed branch's linked worktree (if any, clean-only) and its
    local ref. Best-effort: any refusal is a WARNING and never changes the exit
    code — the landing itself already succeeded and the leftover is visible to
    the resolution-reminder probe."""
    wt = _worktree_for_branch(repo_root, branch)
    if wt is not None and os.path.realpath(wt) == os.path.realpath(repo_root):
        # The branch is checked out in the invocation repo itself: git would
        # refuse to delete it, and we must not remove the caller's own worktree.
        print(
            f"[land-branch] WARNING: {branch!r} is checked out in this repo; "
            f"leaving the local branch (delete it after switching away).",
            file=sys.stderr,
        )
        return
    if wt is not None:
        remove = _git(["worktree", "remove", wt], repo_root)
        if remove.returncode != 0:
            print(
                f"[land-branch] WARNING: could not remove worktree {wt} "
                f"(dirty?; manually: git worktree remove {wt}); leaving the "
                f"local branch {branch!r}:\n{remove.stderr.strip()}",
                file=sys.stderr,
            )
            return
        print(f"[land-branch] removed worktree {wt}")

    delete_local = _git(["branch", "-D", branch], repo_root)
    if delete_local.returncode == 0:
        print(f"[land-branch] deleted local branch {branch}")
    else:
        print(
            f"[land-branch] WARNING: could not delete local branch {branch} "
            f"(manually: git branch -D {branch}):\n{delete_local.stderr.strip()}",
            file=sys.stderr,
        )


def _do_land(repo_root, assessment: Assessment, smoke_timeout=gate.DEFAULT_TIMEOUT_S,
             smoke_waiver=None, runner=None) -> int:
    if not assessment.ok:
        print(f"NOT-LANDABLE: {assessment.reason}", file=sys.stderr)
        return 2

    remote, branch, trunk, sha = (
        assessment.remote, assessment.branch, assessment.trunk, assessment.branch_sha
    )

    refusal, smoke_line = _smoke_gate(repo_root, assessment, smoke_timeout, smoke_waiver, runner)
    if refusal is not None:
        print(gate.defuse(f"NOT-LANDABLE: {refusal}"), file=sys.stderr)
        return 2
    print(smoke_line)

    push_landing = _git(["push", remote, f"{branch}:{trunk}"], repo_root)
    if push_landing.returncode != 0:
        print(
            f"[land-branch] push {branch}:{trunk} to {remote} FAILED (nothing else "
            f"attempted):\n{push_landing.stderr.strip()}",
            file=sys.stderr,
        )
        return 3
    for line in push_landing.stderr.splitlines():
        if line.startswith("pre-push:"):
            print(line)
    print(f"[land-branch] pushed {branch} -> {remote}/{trunk} ({sha})")

    # delete_ok / branch_f_ok are the two landing-critical steps that gate the
    # exit code (3 on failure). Worktree + local-branch cleanup below never
    # affects the exit code — see the module docstring's exit-code asymmetry.
    delete_ok = True
    if not assessment.keep_branch:
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

    branch_f_ok = True
    if not assessment.remote_only:
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

    if not assessment.keep_branch:
        _cleanup_local_branch(repo_root, branch)

    if delete_ok and branch_f_ok:
        print(f"[land-branch] landed {branch} onto {trunk} ({sha})")
        return 0
    return 3


def main(argv=None, *, runner=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("-C", dest="repo_dir", default=".", help="run as if started in DIR")
    parser.add_argument("--branch", default=None, help="branch to land (default: current branch)")
    parser.add_argument("--trunk", default="main", help="trunk branch to land onto (default: main)")
    parser.add_argument("--remote", default="origin", help="remote name (default: origin)")
    parser.add_argument(
        "--check", action="store_true", help="report landability only; make no changes"
    )
    parser.add_argument(
        "--remote-only", action="store_true",
        help="skip the local `git branch -f <trunk>` step (for a trunk that is "
             "checked out or pinned under other WIP; the caller advances local "
             "trunk with a later `git pull --ff-only`)",
    )
    parser.add_argument(
        "--keep-branch", action="store_true",
        help="skip all branch deletion (remote branch, worktree, local branch)",
    )
    parser.add_argument(
        "--smoke-waiver", metavar="REASON", default=None,
        help="admit an UNAVAILABLE live launch of the instruction smoke, recording REASON",
    )
    parser.add_argument(
        "--smoke-timeout", type=int, default=gate.DEFAULT_TIMEOUT_S, metavar="SECONDS",
        help="seconds allowed for the live launch of the instruction smoke "
             f"(default: {gate.DEFAULT_TIMEOUT_S})",
    )
    args = parser.parse_args(argv)

    root_proc = _git(["rev-parse", "--show-toplevel"], args.repo_dir)
    if root_proc.returncode != 0:
        print(f"[land-branch] not a git repository: {args.repo_dir}", file=sys.stderr)
        return 2
    repo_root = root_proc.stdout.strip()

    assessment = assess(
        repo_root, args.branch, args.trunk, args.remote,
        remote_only=args.remote_only, keep_branch=args.keep_branch,
    )

    if args.check:
        return _report_check(repo_root, assessment)
    return _do_land(
        repo_root, assessment,
        smoke_timeout=args.smoke_timeout, smoke_waiver=args.smoke_waiver, runner=runner,
    )


if __name__ == "__main__":
    sys.exit(main())
