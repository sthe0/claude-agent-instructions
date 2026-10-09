"""Reaper `git-worktrees`: stale linked worktrees of the instructions repo.

Difficulty removed: `git worktree remove`'s own trap-cleanup (e.g. land-branch.py's EXIT
trap) only fires on a graceful exit, so a SIGKILLed benchmark or scratch run leaves its
directory and `.git/worktrees/<name>` registration behind, and `git worktree prune`
cannot reap a worktree whose directory still exists. Branch worktrees of landed work
piled up the same way, and the earlier sweep neither saw them (it skipped every worktree
on a branch) nor said what it kept (it compared temp roots without realpath, so
TMPDIR=/var/tmp, which resolves to another directory, never matched).

scan() returns exactly one verdict for every `git worktree list --porcelain` entry except
the first (the main checkout). Two kinds of removal candidate:

  detached  under a temp root (/tmp/cc-scratch, $TMPDIR), older than MIN_AGE_HOURS,
            unowned, clean. A dirty one is dead-lettered (a diff snapshot is written to
            ctx.deadletter_dir), never removed.
  branch    anywhere, except branch main: older than MIN_AGE_HOURS, unowned, clean
            (`git status --porcelain` empty, untracked files count) and landed
            (`git cherry origin/main <branch>` prints no '+' line). The worktree goes
            first, then `<iso-time> <branch> <sha> <path>` is appended to
            <dead-letter dir>/reaped-branches.log, and only then `git branch -D` runs.

Stale unowned branch worktrees with unlanded commits or local changes are kept; once one
is older than REPORT_AGE_HOURS it is flagged `report` and summary() names the count.
Every per-item error resolves to keep. Paths are compared by realpath, because git prints
resolved paths while a temp root or a session's cwd may be spelled through a symlink.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reaper.contract import KEEP, REMOVE, ReapContext, Verdict

NAME = "git-worktrees"
THROTTLE_HOURS = 24.0
MIN_AGE_HOURS = 24.0
REPORT_AGE_HOURS = 7 * 24.0
GIT_TIMEOUT_S = 10
TRUNK_REF = "origin/main"
MAIN_BRANCH_REF = "refs/heads/main"
REAPED_BRANCHES_LOG = "reaped-branches.log"

REPO_ROOT = str(Path(__file__).resolve().parents[3])
_RUNNER_CHECKOUT = REPO_ROOT


@dataclass
class WorktreeInfo:
    path: str
    head: str = ""
    branch: "str | None" = None
    detached: bool = False
    bare: bool = False
    locked: bool = False


def parse_worktree_porcelain(text: str) -> "list[WorktreeInfo]":
    """Parse `git worktree list --porcelain` output; pure, so testable on captured text."""
    worktrees: "list[WorktreeInfo]" = []
    current: "WorktreeInfo | None" = None
    for line in text.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                worktrees.append(current)
            current = WorktreeInfo(path=line[len("worktree "):])
        elif current is None:
            continue
        elif line == "bare":
            current.bare = True
        elif line.startswith("HEAD "):
            current.head = line[len("HEAD "):]
        elif line.startswith("branch "):
            current.branch = line[len("branch "):]
        elif line == "detached":
            current.detached = True
        elif line == "locked" or line.startswith("locked "):
            current.locked = True
    if current is not None:
        worktrees.append(current)
    return worktrees


def temp_roots() -> "list[str]":
    roots = ["/tmp/cc-scratch"]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and os.path.normpath(tmpdir) not in roots:
        roots.append(os.path.normpath(tmpdir))
    return roots


def is_temp_root(path: str, roots: "list[str]") -> bool:
    """Both sides are resolved: git prints real paths, while TMPDIR may name a symlink."""
    real = os.path.realpath(path)
    for root in roots:
        root = os.path.realpath(root)
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def age_hours(path: str, now_ts: float) -> float:
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return 0.0  # unknown age -> treat as fresh -> spared
    return max(0.0, (now_ts - mtime) / 3600.0)


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
    )


def _git_checked(repo: str, *args: str) -> str:
    out = _git(repo, *args)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def list_worktrees() -> "list[WorktreeInfo]":
    return parse_worktree_porcelain(_git_checked(REPO_ROOT, "worktree", "list", "--porcelain"))


def is_dirty(path: str) -> bool:
    try:
        out = _git(path, "status", "--porcelain")
    except Exception:
        return True  # can't prove clean -> treat as dirty -> never removed
    if out.returncode != 0:
        return True
    return bool(out.stdout.strip())


def unlanded_count(repo: str, branch_ref: str) -> int:
    """Commits on ``branch_ref`` with no patch-equivalent on origin/main (`git cherry` '+' lines)."""
    out = _git_checked(repo, "cherry", TRUNK_REF, branch_ref)
    return sum(1 for line in out.splitlines() if line.startswith("+"))


def classify_detached(
    age_h: float, dirty: bool, owned: bool, min_age_hours: float = MIN_AGE_HOURS,
) -> "tuple[str, str]":
    if age_h < min_age_hours:
        return KEEP, f"fresh ({age_h:.1f}h < {min_age_hours:.0f}h floor)"
    if owned:
        return KEEP, "owned by a live/heartbeated session"
    if dirty:
        return KEEP, "dirty working tree — never force-removed"
    return REMOVE, "detached, temp-root, stale, unowned, clean"


def _short(branch_ref: str) -> str:
    return branch_ref[len("refs/heads/"):] if branch_ref.startswith("refs/heads/") else branch_ref


def _holds_current_process(path: str) -> bool:
    real = os.path.realpath(path)
    held = [_RUNNER_CHECKOUT]
    try:
        held.append(os.getcwd())
    except OSError:
        pass
    return any(
        os.path.realpath(h) == real or os.path.realpath(h).startswith(real + os.sep) for h in held
    )


def _evaluate_branch(wt: WorktreeInfo, ctx: ReapContext, repo: str, age_h: float) -> Verdict:
    if age_h < MIN_AGE_HOURS:
        return Verdict(wt.path, KEEP, f"fresh ({age_h:.1f}h < {MIN_AGE_HOURS:.0f}h floor)")
    if ctx.owned_path(wt.path):
        return Verdict(wt.path, KEEP, "owned by a live/heartbeated session")
    stale_for_report = age_h >= REPORT_AGE_HOURS
    if is_dirty(wt.path):
        return Verdict(wt.path, KEEP, "dirty", report=stale_for_report)
    ahead = unlanded_count(repo, wt.branch)
    if ahead:
        return Verdict(wt.path, KEEP, f"unlanded {ahead}", report=stale_for_report)
    return Verdict(wt.path, REMOVE, "landed branch worktree, stale, unowned, clean")


def _evaluate_detached(wt: WorktreeInfo, ctx: ReapContext, roots: "list[str]", age_h: float) -> Verdict:
    if not is_temp_root(wt.path, roots):
        return Verdict(wt.path, KEEP, "not under temp root")
    dirty = is_dirty(wt.path)
    verdict, reason = classify_detached(age_h, dirty, ctx.owned_path(wt.path))
    if verdict == KEEP and dirty and ctx.due and not ctx.dry_run:
        write_deadletter(wt, ctx)
    return Verdict(wt.path, verdict, reason)


def _evaluate(wt: WorktreeInfo, ctx: ReapContext, roots: "list[str]", repo: str) -> Verdict:
    if not os.path.isdir(wt.path):
        return Verdict(wt.path, KEEP, "missing directory")
    if wt.locked:
        return Verdict(wt.path, KEEP, "locked")
    if wt.bare:
        return Verdict(wt.path, KEEP, "bare (main-repo) worktree, never touched")
    if wt.branch == MAIN_BRANCH_REF:
        return Verdict(wt.path, KEEP, "main branch")
    if _holds_current_process(wt.path):
        return Verdict(wt.path, KEEP, "current checkout")
    age_h = age_hours(wt.path, ctx.now)
    if wt.branch:
        return _evaluate_branch(wt, ctx, repo, age_h)
    if wt.detached:
        return _evaluate_detached(wt, ctx, roots, age_h)
    return Verdict(wt.path, KEEP, "unrecognised HEAD state")


def scan(ctx: ReapContext) -> "list[Verdict]":
    worktrees = list_worktrees()
    if not worktrees:
        return []
    repo = worktrees[0].path
    roots = temp_roots()
    verdicts: "list[Verdict]" = []
    for wt in worktrees[1:]:
        try:
            verdicts.append(_evaluate(wt, ctx, roots, repo))
        except Exception as exc:
            verdicts.append(Verdict(wt.path, KEEP, f"error: {type(exc).__name__}: {exc}"))
    return verdicts


def write_deadletter(wt: WorktreeInfo, ctx: ReapContext) -> None:
    try:
        ctx.deadletter_dir.mkdir(parents=True, exist_ok=True)
        name = f"{os.path.basename(wt.path.rstrip('/'))}-{int(ctx.now)}.diff"
        status = _git(wt.path, "status", "--porcelain")
        diff = _git(wt.path, "diff", "HEAD")
        (ctx.deadletter_dir / name).write_text(
            f"# worktree: {wt.path}\n# status --porcelain:\n{status.stdout}\n"
            f"# diff HEAD:\n{diff.stdout}",
            encoding="utf-8",
        )
    except Exception:
        pass  # dead-lettering is best-effort; never blocks the keep decision


def _record_reaped_branch(ctx: ReapContext, branch: str, sha: str, path: str) -> None:
    stamp = datetime.fromtimestamp(ctx.now, timezone.utc).isoformat(timespec="seconds")
    ctx.deadletter_dir.mkdir(parents=True, exist_ok=True)
    with (ctx.deadletter_dir / REAPED_BRANCHES_LOG).open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {branch} {sha} {path}\n")


def remove(path: str, ctx: ReapContext) -> None:
    entries = list_worktrees()
    real = os.path.realpath(path)
    entry = next((e for e in entries[1:] if os.path.realpath(e.path) == real), None)
    if entry is None:
        raise RuntimeError(f"{path} is not a linked worktree")
    if entry.branch == MAIN_BRANCH_REF:
        raise RuntimeError(f"{path} is on branch main")
    repo = entries[0].path
    if not entry.branch:
        _git_checked(repo, "worktree", "remove", "--force", entry.path)
        return
    # No --force: git itself refuses a worktree that turned dirty since the scan.
    _git_checked(repo, "worktree", "remove", entry.path)
    branch = _short(entry.branch)
    _record_reaped_branch(ctx, branch, entry.head, entry.path)
    _git_checked(repo, "branch", "-D", branch)


def summary(verdicts: "list[Verdict]") -> "str | None":
    stale = sum(1 for v in verdicts if v.report)
    if not stale:
        return None
    return (
        f"git-worktrees: {stale} branch worktree(s) older than {REPORT_AGE_HOURS / 24:.0f} days hold "
        "unlanded commits or local changes and are kept; list them with "
        "`python3 scripts/hook-reaper.py --dry-run --only git-worktrees`"
    )
