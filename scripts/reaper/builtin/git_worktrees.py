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
            unowned, clean, and its HEAD an ancestor of origin/main (so no commit is
            lost). A dirty one is dead-lettered (a diff snapshot is written to
            ctx.deadletter_dir), never removed.
  branch    anywhere, except branch main: older than MIN_AGE_HOURS, unowned, clean
            (`git status --porcelain` empty, untracked files count) and landed
            (`git cherry origin/main <branch>` prints no '+' line). First
            `<iso-time> <branch> <sha> <path>` is appended to
            <dead-letter dir>/reaped-branches.log, then the worktree goes, and only
            then `git branch -D` runs.

Removal is never forced: git itself refuses a worktree that turned dirty since the
scan, and the runner keeps it.

Stale unowned branch worktrees with unlanded commits or local changes are kept; once one
is older than REPORT_AGE_HOURS it is flagged `report` and summary() names the count.
Every per-item error resolves to keep. Paths are compared by realpath, because git prints
resolved paths while a temp root or a session's cwd may be spelled through a symlink.

Backup duty (``upkeep``, run only by ``runner --upkeep-only``): a worktree branch is the
only copy of its commits, and a session that dies before landing takes it with the
machine. Every branch of a worktree (main, master and release-* excepted) that holds
commits origin lacks (`git cherry origin/main` prints a '+', and `refs/remotes/origin/<b>`
is missing or not containing the tip) is pushed with a plain `git push origin
refs/heads/<b>:refs/heads/<b>`: never forced, never a working-tree operation, so only
committed work travels. A branch whose origin counterpart is not an ancestor of it
(diverged) is reported and left alone. The outgoing patch is checked by
check-org-neutral.py first. Every outcome is one JSON line in
<agent home>/reaper/branch-backup.jsonl. Candidates come from local refs only; a
successful push updates `refs/remotes/origin/<b>`, which is what makes a rerun a no-op.
"""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lib import config_root
from reaper.contract import KEEP, REMOVE, ReapContext, Verdict

NAME = "git-worktrees"
THROTTLE_HOURS = 24.0
MIN_AGE_HOURS = 24.0
REPORT_AGE_HOURS = 7 * 24.0
GIT_TIMEOUT_S = 10
PUSH_TIMEOUT_S = 60
CHECK_TIMEOUT_S = 30
SSH_CONNECT_TIMEOUT_S = 10
TRUNK_REF = "origin/main"
MAIN_BRANCH_REF = "refs/heads/main"
REAPED_BRANCHES_LOG = "reaped-branches.log"
BACKUP_LOG = "branch-backup.jsonl"
CHECKER_RELPATH = "scripts/check-org-neutral.py"
UNBACKED_TAG = "unbacked"

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


def _git(repo: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout,
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


def head_in_trunk(path: str) -> bool:
    """True if the worktree's HEAD is an ancestor of origin/main; raises if git cannot tell."""
    out = _git(path, "merge-base", "--is-ancestor", "HEAD", TRUNK_REF)
    if out.returncode not in (0, 1):
        raise RuntimeError(f"git merge-base --is-ancestor failed: {out.stderr.strip()}")
    return out.returncode == 0


def _evaluate_detached(wt: WorktreeInfo, ctx: ReapContext, roots: "list[str]", age_h: float) -> Verdict:
    if not is_temp_root(wt.path, roots):
        return Verdict(wt.path, KEEP, "not under temp root")
    dirty = is_dirty(wt.path)
    verdict, reason = classify_detached(age_h, dirty, ctx.owned_path(wt.path))
    if verdict == KEEP and dirty and ctx.due and not ctx.dry_run:
        write_deadletter(wt, ctx)
    if verdict == REMOVE and not head_in_trunk(wt.path):
        return Verdict(wt.path, KEEP, f"detached HEAD not in {TRUNK_REF}")
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
            verdicts.append(_tag_unbacked(repo, wt, _evaluate(wt, ctx, roots, repo)))
        except Exception as exc:
            verdicts.append(Verdict(wt.path, KEEP, f"error: {type(exc).__name__}: {exc}"))
    return verdicts


def _tag_unbacked(repo: str, wt: WorktreeInfo, verdict: Verdict) -> Verdict:
    if not wt.branch:
        return verdict
    try:
        item = backup_state(repo, _short(wt.branch), wt.head)
    except Exception:
        return verdict
    if item is None:
        return verdict
    return dataclasses.replace(verdict, tags=verdict.tags + (UNBACKED_TAG,))


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
    branch = _short(entry.branch) if entry.branch else None
    if branch:
        _record_reaped_branch(ctx, branch, entry.head, entry.path)
    # Unforced on purpose: git itself refuses a worktree that turned dirty since the scan.
    _git_checked(repo, "worktree", "remove", entry.path)
    if branch:
        _git_checked(repo, "branch", "-D", branch)


@dataclass
class BackupItem:
    branch: str
    sha: str
    diverged: bool = False
    base: str = TRUNK_REF
    error: str = ""


def is_trunk_branch(name: str) -> bool:
    return name in ("main", "master") or name.startswith(("release-", "release/"))


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    out = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if out.returncode not in (0, 1):
        raise RuntimeError(f"git merge-base --is-ancestor failed: {out.stderr.strip()}")
    return out.returncode == 0


def _ref_exists(repo: str, ref: str) -> bool:
    out = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    if out.returncode not in (0, 1):
        raise RuntimeError(f"git rev-parse {ref} failed: {out.stderr.strip()}")
    return out.returncode == 0


def backup_state(repo: str, branch: str, sha: str) -> "BackupItem | None":
    """The branch as a backup candidate, or None when it needs none (local refs only)."""
    if is_trunk_branch(branch):
        return None
    if unlanded_count(repo, f"refs/heads/{branch}") == 0:
        return None
    remote = f"refs/remotes/origin/{branch}"
    if not _ref_exists(repo, remote):
        return BackupItem(branch, sha)
    if _is_ancestor(repo, sha, remote):
        return None
    return BackupItem(branch, sha, diverged=not _is_ancestor(repo, remote, sha), base=f"origin/{branch}")


def backup_candidates(repo: str) -> "list[BackupItem]":
    entries = parse_worktree_porcelain(_git_checked(repo, "worktree", "list", "--porcelain"))
    items: "list[BackupItem]" = []
    seen: "set[str]" = set()
    for wt in entries:
        if wt.bare or wt.detached or not wt.branch or not wt.branch.startswith("refs/heads/"):
            continue
        branch = _short(wt.branch)
        if branch in seen:
            continue
        seen.add(branch)
        try:
            item = backup_state(repo, branch, wt.head)
        except Exception as exc:
            item = BackupItem(branch, wt.head, error=f"{type(exc).__name__}: {exc}")
        if item is not None:
            items.append(item)
    return items


def _org_neutral_block(repo: str, item: BackupItem, checker: str) -> "str | None":
    """None when the outgoing patch passes the org-neutral check, else the blocked detail."""
    try:
        patch = subprocess.run(
            ["git", "-C", repo, "log", "-p", "--no-color", "--no-ext-diff", f"{item.base}..refs/heads/{item.branch}"],
            capture_output=True, timeout=CHECK_TIMEOUT_S,
        )
        if patch.returncode != 0:
            return "checker error"
        verdict = subprocess.run(
            [sys.executable or "python3", checker, "-"],
            input=patch.stdout, capture_output=True, cwd=repo, timeout=CHECK_TIMEOUT_S,
        )
    except Exception:
        return "checker error"
    if verdict.returncode == 0:
        return None
    return "org-neutral" if verdict.returncode == 1 else "checker error"


def push_env(repo: str) -> "dict[str, str]":
    """Non-interactive push environment; a configured ssh command is extended, not replaced."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    base = env.get("GIT_SSH_COMMAND", "").strip()
    if not base:
        try:
            base = _git(repo, "config", "--get", "core.sshCommand").stdout.strip()
        except Exception:
            base = ""
    if not base and env.get("GIT_SSH"):
        return env
    env["GIT_SSH_COMMAND"] = f"{base or 'ssh'} -o BatchMode=yes -o ConnectTimeout={SSH_CONNECT_TIMEOUT_S}"
    return env


def _run_bounded(argv: "list[str]", env: "dict[str, str]", timeout: float) -> subprocess.CompletedProcess:
    """Run ``argv`` in its own process group and kill the whole group on timeout, so a
    wedged ssh child cannot outlive the push."""
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=GIT_TIMEOUT_S)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _push(repo: str, branch: str) -> "tuple[str, str]":
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    try:
        out = _run_bounded(["git", "-C", repo, "push", "origin", refspec], push_env(repo), PUSH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "timeout", f"push exceeded {PUSH_TIMEOUT_S}s"
    if out.returncode == 0:
        return "pushed", ""
    lines = [line.strip() for line in (out.stderr or "").splitlines() if line.strip()]
    refusal = next((line for line in lines if "rejected" in line), None)
    detail = (refusal or (lines[-1] if lines else f"git push exited {out.returncode}"))[:300]
    return ("rejected" if refusal else "error"), detail


def _back_up(repo: str, ctx: ReapContext, item: BackupItem, checker: str) -> "tuple[str, str]":
    if item.error:
        return "error", item.error
    if item.diverged:
        return "diverged", "diverged, not force-pushed"
    blocked = _org_neutral_block(repo, item, checker)
    if blocked:
        return "blocked", blocked
    if ctx.dry_run:
        return "would-push", ""
    return _push(repo, item.branch)


def backup_log_path() -> Path:
    return config_root.agent_home() / "reaper" / BACKUP_LOG


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _append_backup_log(log_path: Path, ctx: ReapContext, item: BackupItem, outcome: str, detail: str) -> None:
    record = {"ts": _iso(ctx.now), "branch": item.branch, "sha": item.sha, "outcome": outcome, "detail": detail}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _report_line(outcome: str, item: BackupItem, detail: str) -> str:
    word = outcome.upper()
    if outcome == "diverged":
        return f"{NAME} {word} {item.branch}"
    return f"{NAME} {word} {item.branch} {item.sha}" + (f" {detail}" if detail else "")


def backup_push(
    repo: str, ctx: ReapContext, *, checker: "str | None" = None, log_path: "Path | None" = None,
) -> "list[str]":
    """Back up every candidate of ``repo``; one report line per branch, never raises per branch."""
    checker = checker or os.path.join(REPO_ROOT, CHECKER_RELPATH)
    log_path = log_path or backup_log_path()
    lines: "list[str]" = []
    for item in backup_candidates(repo):
        try:
            outcome, detail = _back_up(repo, ctx, item, checker)
        except Exception as exc:
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
        if not ctx.dry_run:
            _append_backup_log(log_path, ctx, item, outcome, detail)
        lines.append(_report_line(outcome, item, detail))
    return lines


def upkeep(ctx: ReapContext) -> "list[str]":
    worktrees = list_worktrees()
    if not worktrees:
        return []
    return backup_push(worktrees[0].path, ctx)


def summary(verdicts: "list[Verdict]") -> "str | None":
    notices = []
    stale = sum(1 for v in verdicts if v.report)
    if stale:
        notices.append(
            f"git-worktrees: {stale} branch worktree(s) older than {REPORT_AGE_HOURS / 24:.0f} days hold "
            "unlanded commits or local changes and are kept; list them with "
            "`python3 scripts/hook-reaper.py --dry-run --only git-worktrees`"
        )
    unbacked = sum(1 for v in verdicts if UNBACKED_TAG in v.tags)
    if unbacked:
        notices.append(
            f"git-worktrees: {unbacked} worktree branch(es) hold commits origin lacks; the turn-end backup "
            "pushes them, see reaper/branch-backup.jsonl for what it could not, or list with "
            "`python3 scripts/hook-reaper.py --upkeep-only --dry-run --only git-worktrees`"
        )
    return "\n".join(notices) or None
