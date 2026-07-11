#!/usr/bin/env python3
"""SessionStart hook: reap detached, unowned, stale git worktrees under the temp root.

Difficulty removed: `git worktree remove`'s own trap-cleanup (e.g. land-branch.py's
EXIT trap) only fires on a graceful exit. A worktree created for a benchmark or
scratch run that gets SIGKILLed (OOM, a hard timeout, a crashed harness) leaves its
directory and its `.git/worktrees/<name>` registration behind forever — `git worktree
prune` cannot reap it because the directory still exists. This script is the reactive
periodic sweep that a creation-time-only trap cannot provide (see the stage principle
this implements: "a cleanup mechanism triggered only by graceful exit cannot reap
state left by a SIGKILL").

A worktree is a removal candidate only if ALL hold:
  (a) its path is under a temp root (/tmp/cc-scratch or $TMPDIR) — never touches
      cai-main, the instructions checkout, or any other named working tree;
  (b) it is DETACHED — a worktree on a branch is presumed intentional WIP;
  (c) it is older than MIN_AGE_HOURS (default 24h, the same staleness floor the
      session-scope registry already uses for its own pruning);
  (d) it is UNOWNED — no session-scope record (~/.claude-agent/agentctl/scopes/*.json,
      see session_scope.registry) has a cwd/repo_root at-or-inside the worktree with
      either a live pid or a heartbeat within the staleness floor. Absence of a live
      process foothold is not proof of no owner: a paused/resumable session can still
      own a worktree across an idle gap (see the sibling stage's leaf,
      verify-ownership-before-shared-state-delete.md) — the registry, not a bare
      /proc scan, is the durable ownership oracle;
  (e) it is CLEAN — `git status --porcelain` is empty. A dirty worktree is never
      force-removed; it is skipped and a diff snapshot is written to
      DEADLETTER_DIR so the uncommitted work is not silently lost.

Three modes:
  (no flags)   — the SessionStart mode. Self-throttled via STAMP: at most once per
                 THROTTLE_HOURS, a run within the window prints the
                 "throttled: within window" sentinel and exits immediately without
                 touching git. A run past the window performs the real sweep and
                 rewrites STAMP.
  --dry-run    — never checks or writes STAMP; lists every candidate worktree
                 (temp-root + detached) with its verdict (REMOVE/KEEP) and reason,
                 without removing anything. Always safe, always fully evaluates
                 the current tree.
  --force-run  — never checks or writes STAMP; performs the real sweep regardless
                 of throttle state. For tests/manual verification — running it does
                 not consume or perturb the SessionStart cadence.

Strictly fail-safe: any per-worktree error (git status fails, os.stat fails) is
treated as "cannot prove removable" and the worktree is kept, never removed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_scope import registry  # noqa: E402

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
STAMP = Path.home() / ".local" / "state" / "claude-orphan-worktree-sweep.stamp"
THROTTLE_HOURS = 24.0
MIN_AGE_HOURS = 24.0
OWNER_HEARTBEAT_TTL_HOURS = 24.0
DEADLETTER_DIR = Path.home() / ".claude-agent" / "orphan-worktree-deadletter"
GIT_TIMEOUT_S = 10


@dataclass
class WorktreeInfo:
    path: str
    head: str = ""
    branch: "str | None" = None
    detached: bool = False
    bare: bool = False


def parse_worktree_porcelain(text: str) -> "list[WorktreeInfo]":
    """Parse `git worktree list --porcelain` output into WorktreeInfo records.

    Pure and hermetic — no subprocess call, so it is directly unit-testable
    against captured porcelain text for both the branch and detached shapes.
    """
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
    if current is not None:
        worktrees.append(current)
    return worktrees


def temp_roots() -> "list[str]":
    roots = ["/tmp/cc-scratch"]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        norm = os.path.normpath(tmpdir)
        if norm not in roots:
            roots.append(norm)
    return roots


def is_temp_root(path: str, roots: "list[str]") -> bool:
    path = os.path.normpath(path)
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def _at_or_inside(container: str, candidate: str) -> bool:
    """True if candidate == container or candidate is a descendant of container."""
    return candidate == container or candidate.startswith(container + os.sep)


def is_owned(
    wt_path: str,
    records: "list[registry.ScopeRecord]",
    now_ts: float,
    heartbeat_ttl_hours: float = OWNER_HEARTBEAT_TTL_HOURS,
) -> bool:
    """A worktree is owned if a scope record's cwd or repo_root sits at/inside it
    AND that session is still live — either a confirmed-alive pid, or (when no pid
    was ever recorded, or liveness can't be probed) a heartbeat within the floor."""
    wt_path = os.path.normpath(wt_path)
    ttl_s = heartbeat_ttl_hours * 3600.0
    for rec in records:
        candidates = [d for d in (rec.cwd, rec.repo_root) if d]
        if not any(_at_or_inside(wt_path, os.path.normpath(d)) for d in candidates):
            continue
        alive = rec.pid is not None and registry.pid_alive(rec.pid)
        fresh = (now_ts - rec.heartbeat_ts) <= ttl_s
        if alive or fresh:
            return True
    return False


def age_hours(path: str, now_ts: float) -> float:
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return 0.0  # unknown age -> treat as fresh -> spared
    return max(0.0, (now_ts - mtime) / 3600.0)


def is_dirty(path: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except Exception:
        return True  # can't prove clean -> treat as dirty -> never removed
    if out.returncode != 0:
        return True
    return bool(out.stdout.strip())


def classify(
    wt: WorktreeInfo,
    age_h: float,
    dirty: bool,
    owned: bool,
    roots: "list[str]",
    min_age_hours: float = MIN_AGE_HOURS,
) -> "tuple[str, str]":
    """Pure verdict function: (verdict, reason), verdict in {"remove", "keep"}.

    Unit-testable with synthetic WorktreeInfo + precomputed age/dirty/owned —
    no filesystem or git access needed to exercise the decision logic itself.
    """
    if wt.bare:
        return "keep", "bare (main-repo) worktree, never touched"
    if not is_temp_root(wt.path, roots):
        return "keep", "outside the temp root"
    if not wt.detached:
        return "keep", "on a branch, not detached"
    if age_h < min_age_hours:
        return "keep", f"fresh ({age_h:.1f}h < {min_age_hours:.0f}h floor)"
    if owned:
        return "keep", "owned by a live/heartbeated session"
    if dirty:
        return "keep", "dirty working tree — never force-removed"
    return "remove", "detached, temp-root, stale, unowned, clean"


def list_worktrees(repo_root: str) -> "list[WorktreeInfo]":
    out = subprocess.run(
        ["git", "-C", repo_root, "worktree", "list", "--porcelain"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=True,
    )
    return parse_worktree_porcelain(out.stdout)


def write_deadletter(wt: WorktreeInfo, now_ts: float) -> None:
    try:
        DEADLETTER_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{os.path.basename(wt.path.rstrip('/'))}-{int(now_ts)}.diff"
        status = subprocess.run(
            ["git", "-C", wt.path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
        diff = subprocess.run(
            ["git", "-C", wt.path, "diff", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
        (DEADLETTER_DIR / name).write_text(
            f"# worktree: {wt.path}\n# status --porcelain:\n{status.stdout}\n"
            f"# diff HEAD:\n{diff.stdout}",
            encoding="utf-8",
        )
    except Exception:
        pass  # dead-lettering is best-effort; never blocks the keep decision


def remove_worktree(repo_root: str, path: str) -> bool:
    try:
        subprocess.run(
            ["git", "-C", repo_root, "worktree", "remove", "--force", path],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=True,
        )
        return True
    except Exception:
        return False


def sweep(repo_root: str, now_ts: float, dry_run: bool) -> "list[str]":
    """Evaluate every worktree and act (unless dry_run). Returns report lines."""
    roots = temp_roots()
    worktrees = list_worktrees(repo_root)
    records = registry.load_all(registry.DEFAULT_SCOPES_DIR)
    lines: "list[str]" = []

    for wt in worktrees:
        if wt.bare or not is_temp_root(wt.path, roots) or not wt.detached:
            continue  # not even a candidate shape — no noise for the common case
        dirty = is_dirty(wt.path)
        owned = is_owned(wt.path, records, now_ts)
        age_h = age_hours(wt.path, now_ts)
        verdict, reason = classify(wt, age_h, dirty, owned, roots)
        if verdict == "remove":
            if dry_run:
                lines.append(f"REMOVE {wt.path} ({reason})")
            else:
                ok = remove_worktree(repo_root, wt.path)
                lines.append(
                    f"removed: {wt.path} ({reason})" if ok
                    else f"FAILED to remove: {wt.path} ({reason})"
                )
        else:
            if dry_run:
                lines.append(f"KEEP {wt.path} ({reason})")
            elif dirty:
                write_deadletter(wt, now_ts)
                lines.append(f"kept (dead-lettered): {wt.path} ({reason})")
    return lines


def last_sweep() -> "float | None":
    try:
        return float(STAMP.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def record_sweep(now_ts: float) -> None:
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(str(now_ts), encoding="utf-8")
    except OSError:
        pass


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, never act, never touches the throttle")
    parser.add_argument("--force-run", action="store_true", help="sweep now regardless of throttle, without consuming it")
    args = parser.parse_args(argv)

    now_ts = time.time()

    if args.dry_run:
        for line in sweep(REPO_ROOT, now_ts, dry_run=True):
            print(line)
        return 0

    if not args.force_run:
        prev = last_sweep()
        if prev is not None and (now_ts - prev) < THROTTLE_HOURS * 3600.0:
            print("throttled: within window", file=sys.stderr)
            return 0

    for line in sweep(REPO_ROOT, now_ts, dry_run=False):
        print(line, file=sys.stderr)

    if not args.force_run:
        record_sweep(now_ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
