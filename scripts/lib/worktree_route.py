"""Route a write into the canonical instructions checkout through a worktree.

Difficulty removed: a writer that edits files in the canonical (main) checkout
leaves an uncommitted change there; the canon read-only guard denies hand
edits, so the change sits dirty until someone copies it into a worktree by
hand. `run_routed` performs that route as one function that fails safe:

    preflight -> worktree cut from origin/main -> child -> commit -> land
    (land-branch.py) -> `git pull --ff-only` in the canonical checkout ->
    remove worktree and branch

From a linked worktree it only calls the child in place. On any failure after
the worktree exists the worktree is kept (unless the child wrote nothing), the
step and its path are printed, and the canonical files are left as they were
before the step that failed.

`WORKTREE_ROUTE_DISABLE` is a test seam (the negative control for the routing
tests); it must not be set in normal use.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

DISABLE_ENV = "WORKTREE_ROUTE_DISABLE"
CO_AUTHOR = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"

ChildResult = tuple[int, str]


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def routing_disabled() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


def is_main_checkout(repo) -> bool:
    """True when `repo` is the primary checkout (its git dir is the common dir)."""
    git_dir = _git(["rev-parse", "--absolute-git-dir"], repo)
    common = _git(["rev-parse", "--git-common-dir"], repo)
    if git_dir.returncode != 0 or common.returncode != 0:
        return False
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = Path(repo) / common_path
    return os.path.realpath(git_dir.stdout.strip()) == os.path.realpath(common_path)


def _preflight(repo, trunk: str, remote: str) -> str | None:
    """Reason to refuse, or None when the route may start."""
    fetch = _git(["fetch", remote, trunk], repo)
    if fetch.returncode != 0:
        return f"git fetch {remote} {trunk} failed: {fetch.stderr.strip()}"
    branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], repo)
    if branch.returncode != 0 or branch.stdout.strip() != trunk:
        return f"the main checkout is not on {trunk!r}"
    ahead = _git(["rev-list", "--count", f"{remote}/{trunk}..{trunk}"], repo)
    if ahead.returncode != 0:
        return f"cannot compare {trunk} with {remote}/{trunk}: {ahead.stderr.strip()}"
    if int(ahead.stdout.strip() or "0") > 0:
        return f"{trunk} has {ahead.stdout.strip()} commit(s) not on {remote}/{trunk}"
    status = _git(["status", "--porcelain"], repo)
    if status.returncode != 0 or status.stdout.strip():
        return "the main checkout has uncommitted changes"
    return None


def _fail(step: str, worktree: Path, detail: str) -> int:
    print(
        f"[worktree-route] step '{step}' failed: {detail}\n"
        f"[worktree-route] worktree kept at {worktree} (canonical checkout "
        f"unchanged by this step); finish by hand there, or remove it with "
        f"`git worktree remove --force {worktree}`.",
        file=sys.stderr,
    )
    return 1


def run_routed(
    repo,
    run_child: Callable[[Path], ChildResult],
    message: str,
    *,
    land_script: str | os.PathLike | None = None,
    trunk: str = "main",
    remote: str = "origin",
) -> int:
    """Run `run_child(worktree)` so its writes land on `remote/trunk`.

    `run_child` returns (exit_code, stdout); the stdout is printed here.
    Returns the process exit code.
    """
    repo = Path(repo)
    if routing_disabled() or not is_main_checkout(repo):
        code, out = run_child(repo)
        sys.stdout.write(out)
        return code

    reason = _preflight(repo, trunk, remote)
    if reason:
        print(f"[worktree-route] refused: {reason}", file=sys.stderr)
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch = f"memory-{stamp}-{os.getpid()}"
    worktree = Path(f"{repo}-mem-{stamp}-{os.getpid()}")
    add = _git(["worktree", "add", "-b", branch, str(worktree), f"{remote}/{trunk}"], repo)
    if add.returncode != 0:
        print(f"[worktree-route] refused: git worktree add failed: {add.stderr.strip()}",
              file=sys.stderr)
        return 1

    code, out = run_child(worktree)
    sys.stdout.write(out)
    if code != 0:
        if not _git(["status", "--porcelain"], worktree).stdout.strip():
            _git(["worktree", "remove", "--force", str(worktree)], repo)
            _git(["branch", "-D", branch], repo)
            print(f"[worktree-route] step 'child' failed with exit {code}; it wrote "
                  f"nothing, worktree {worktree} removed", file=sys.stderr)
            return code
        _fail("child", worktree, f"exit {code}")
        return code

    _git(["add", "-A"], worktree)
    if _git(["diff", "--cached", "--quiet"], worktree).returncode == 0:
        _git(["worktree", "remove", "--force", str(worktree)], repo)
        _git(["branch", "-D", branch], repo)
        return 0
    commit = _git(["commit", "-m", message, "-m", CO_AUTHOR], worktree)
    if commit.returncode != 0:
        return _fail("commit", worktree, (commit.stderr or commit.stdout).strip())

    land = land_script or Path(repo) / "scripts" / "land-branch.py"
    landed = subprocess.run(
        [sys.executable, str(land), "-C", str(worktree), "--branch", branch,
         "--trunk", trunk, "--remote", remote, "--remote-only", "--keep-branch"],
        capture_output=True, text=True,
    )
    if landed.returncode != 0:
        return _fail("land", worktree, (landed.stderr or landed.stdout).strip())

    pull = _git(["pull", "--ff-only", remote, trunk], repo)
    if pull.returncode != 0:
        return _fail("pull", worktree,
                     f"{pull.stderr.strip()} (the change is already on {remote}/{trunk})")

    removed = _git(["worktree", "remove", str(worktree)], repo)
    deleted = _git(["branch", "-d", branch], repo)
    for proc, what in ((removed, f"worktree {worktree}"), (deleted, f"branch {branch}")):
        if proc.returncode != 0:
            print(f"[worktree-route] WARNING: could not remove {what}: "
                  f"{proc.stderr.strip()}", file=sys.stderr)
    return 0
