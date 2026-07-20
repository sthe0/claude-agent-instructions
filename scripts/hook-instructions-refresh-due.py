#!/usr/bin/env python3
"""UserPromptSubmit hook: per-session proactive OFFER to refresh instructions.

Replaces the silent 10-min background auto-pull (install-sync-cron.sh /
install-sync-systemd-timer.sh) with an explicit, opt-in nudge: once per
session, on the session's first prompt, check whether the Core instruction
repo and/or the current project's own git-tracked .claude/ layer are behind
their upstream, and if so print a line instructing the agent to OFFER a pull
via AskUserQuestion — never pull automatically. So every new session starts
on fresh instructions, not only the day's first session.

Throttled via a stamp file keyed on the session_id (mirrors
hook-policy-scorecard-due.py's throttle skeleton), falling back to the
calendar-day key when the payload carries no session_id. Fail-open
throughout: any git failure (offline, timeout, non-git cwd, no upstream
configured) is treated as "not behind" and stays silent, so a flaky network
can never wedge or spam a prompt. The stamp is written once the session's
due-check fires (before the fetch), so the check runs at most once per
session regardless of outcome — an offline first prompt does not trigger a
fetch on every later prompt in that session.

Output goes to stdout (UserPromptSubmit convention — becomes turn context,
mirrors hook-engine-start.py). Exit 0 always.

Also rides this same per-session throttle to check the ORTHOGONAL "deployed" axis:
whether the serving Core checkout (the one settings.json hook commands point
at) is actually the branch/tree that gets run, not just whether it is behind.
A merge to origin/main does not "deploy" anything by itself — the checkout on
disk still runs whatever it has checked out. Two sub-checks, printed on a
separate "[instructions-deploy]" line so the existing "[instructions-refresh]"
nudge assertions are unaffected: (1) the Core checkout's HEAD is not on the
default branch; (2) settings.json hook commands resolve to more than one
distinct checkout root. Both fail-open like everything else in this hook.

Two more nudges ride the same throttle, companions to hook-guard-canon-
readonly.py's hard PreToolUse deny (never auto, only OFFERs — mirrors this
file's existing pull-nudge philosophy):

- "[worktree-fresh]": when cwd is a LINKED git worktree, OFFER a rebase onto
  origin/main if it is behind (D3). The behind-count is read only AFTER a
  fresh `git fetch origin main`, never against a possibly-stale local ref —
  otherwise the count can sit at a stale 0 and the OFFER silently never
  fires. Silent for the primary checkout (out of scope here — see check_branch
  above) and for an up-to-date worktree.
- "[relocate]": when cwd is itself inside a canon (the primary Core checkout
  on any branch, or a path under a registered scripts/lib/config_root.
  canon_roots_file() entry), OFFER relocation via `scripts/session-isolate.sh`
  so a running session picks this up on its very next move (D2/R4), not only
  when hook-guard-canon-readonly.py blocks an actual edit. The canon-detection
  helpers below are deliberately duplicated from hook-guard-canon-readonly.py
  rather than imported — each hook stays a standalone script so a bug in one
  can never wedge the other.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import config_root  # noqa: E402

# Kept well under the hook's own registered timeout (10s in install-reminder-hooks.sh):
# up to two network fetches (core + project) can run sequentially in the worst case.
FETCH_TIMEOUT_S = 4
GIT_TIMEOUT_S = 3


def _stamp_path() -> Path:
    return Path.home() / ".local" / "state" / "claude-instructions-refresh.stamp"


def _due(session_key: str) -> bool:
    """Fire once per session_key. A stamp holding any other key — a prior
    session, a stale calendar-day key, or a legacy ISO datetime from before
    the per-session rekey — reads as due (fail-open)."""
    try:
        raw = _stamp_path().read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return raw != session_key


def _record(session_key: str) -> None:
    try:
        stamp = _stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(session_key, encoding="utf-8")
    except OSError:
        pass


def _run(args: list[str], cwd: str, timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None


def _count_commits(root: Path, range_expr: str) -> int | None:
    proc = _run(["git", "-C", str(root), "rev-list", "--count", range_expr], str(root), GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _core_root() -> Path:
    return Path(os.environ.get("CLAUDE_INSTRUCTIONS_REPO", str(Path.home() / "claude-agent-instructions")))


def _core_behind(core_root: Path) -> int | None:
    """Fail-open: None on any git failure (offline, missing repo, no origin/main)."""
    fetch = _run(["git", "-C", str(core_root), "fetch", "origin", "main", "-q"], str(core_root), FETCH_TIMEOUT_S)
    if fetch is None or fetch.returncode != 0:
        return None
    return _count_commits(core_root, "HEAD..origin/main")


def _project_root(cwd: str) -> Path | None:
    proc = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], cwd, GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out) if out else None


def _has_tracked_claude_dir(root: Path) -> bool:
    proc = _run(["git", "-C", str(root), "ls-files", ".claude"], str(root), GIT_TIMEOUT_S)
    return bool(proc) and proc.returncode == 0 and bool(proc.stdout.strip())


def _project_behind(root: Path) -> int | None:
    """Fail-open: None on any git failure (offline, no upstream configured)."""
    fetch = _run(["git", "-C", str(root), "fetch", "-q"], str(root), FETCH_TIMEOUT_S)
    if fetch is None or fetch.returncode != 0:
        return None
    return _count_commits(root, "HEAD..@{upstream}")


def check_layers(cwd: str) -> list[tuple[str, Path, int, str]]:
    """Return (label, root, behind_count, pull_cmd) for every layer behind its upstream."""
    behind_layers: list[tuple[str, Path, int, str]] = []

    core_root = _core_root()
    core_behind = _core_behind(core_root)
    if core_behind:
        behind_layers.append((
            "Core", core_root, core_behind,
            f"cd {core_root} && scripts/sync-instructions-repo.sh pull",
        ))

    project_root = _project_root(cwd)
    if project_root is not None and project_root.resolve() != core_root.resolve():
        if _has_tracked_claude_dir(project_root):
            project_behind = _project_behind(project_root)
            if project_behind:
                behind_layers.append((
                    "Project", project_root, project_behind,
                    f"git -C {project_root} pull --ff-only",
                ))

    return behind_layers


def _settings_path() -> Path:
    override = os.environ.get("CLAUDE_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    return config_root.agent_home() / "settings.json"


def check_branch(core_root: Path, default: str = "main") -> str | None:
    """Warn when the Core checkout's HEAD is not on `default`. Fail-open: None
    on any git failure (missing repo, detached HEAD, timeout) or when already
    on `default` — no network involved, a single local `rev-parse`."""
    proc = _run(["git", "-C", str(core_root), "rev-parse", "--abbrev-ref", "HEAD"], str(core_root), GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch or branch == default:
        return None
    return (
        f"Core checkout ({core_root}) is on {branch}, not {default} — live hooks run "
        f"stale code; switch with `git -C {core_root} switch {default}`"
    )


def distinct_roots(settings_path: Path) -> list[str]:
    """Every distinct checkout root referenced by a settings.json hook command
    (the substring before '/scripts/'). Fail-open: [] on any error (missing
    file, bad JSON, unexpected shape)."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for groups in data.get("hooks", {}).values():
            for group in groups:
                for entry in group.get("hooks", []):
                    command = entry.get("command", "")
                    marker = "/scripts/"
                    idx = command.find(marker)
                    if idx == -1:
                        continue
                    roots.add(command[:idx].strip("'\""))
        return sorted(roots)
    except Exception:
        return []


def check_homogeneity(settings_path: Path) -> str | None:
    """Warn when settings.json hook commands span more than one checkout root."""
    roots = distinct_roots(settings_path)
    if len(roots) > 1:
        return (
            f"settings.json hooks span {len(roots)} distinct checkout roots "
            f"({', '.join(roots)}) — deployed hook behavior is inconsistent; "
            "point every hook command at one checkout"
        )
    return None


def _git_info(cwd: str):
    """(toplevel, git_dir_abs, git_common_dir_abs, branch) for `cwd`, or None on
    any failure. Duplicated from hook-guard-canon-readonly.py's _git_info (not
    imported — see module docstring) rather than shared, so a bug in one hook
    can never wedge the other."""
    proc = _run(
        ["git", "-C", cwd, "rev-parse",
         "--show-toplevel", "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD"],
        cwd, GIT_TIMEOUT_S,
    )
    if proc is None or proc.returncode != 0:
        return None
    lines = proc.stdout.splitlines()
    if len(lines) < 4:
        return None
    toplevel, git_dir, git_common_dir, branch = lines[0], lines[1], lines[2], lines[3]
    git_dir_abs = os.path.realpath(os.path.join(cwd, git_dir))
    git_common_abs = os.path.realpath(os.path.join(cwd, git_common_dir))
    return os.path.realpath(toplevel), git_dir_abs, git_common_abs, branch


def check_worktree_fresh(cwd: str) -> str | None:
    """D3: OFFER a rebase onto origin/main when cwd is a LINKED git worktree
    that is behind. Fetches origin/main first and only THEN counts commits —
    counting against a stale local ref could read 0-behind forever and the
    OFFER would never fire. Fail-open: None for a non-git cwd, the PRIMARY
    checkout (git_dir == git_common_dir; out of scope here), any git failure,
    or an up-to-date worktree."""
    info = _git_info(cwd)
    if info is None:
        return None
    toplevel, git_dir_abs, git_common_abs, _branch = info
    if git_dir_abs == git_common_abs:
        return None  # primary checkout, not a linked worktree

    fetch = _run(["git", "-C", toplevel, "fetch", "origin", "main", "-q"], toplevel, FETCH_TIMEOUT_S)
    if fetch is None or fetch.returncode != 0:
        return None
    behind = _count_commits(Path(toplevel), "HEAD..origin/main")
    if not behind:
        return None
    return (
        f"[worktree-fresh] this linked worktree ({toplevel}) is {behind} commit(s) behind "
        f"origin/main — OFFER the user a rebase via AskUserQuestion: "
        f"`git -C {toplevel} rebase origin/main` (never automatic)."
    )


def _read_canon_roots() -> list[str]:
    """Non-empty, non-comment lines of the canon-roots file, or [] on any error
    — fail-open. Duplicated from hook-guard-canon-readonly.py (see module
    docstring)."""
    try:
        path = config_root.canon_roots_file()
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _under_canon_root(target: str) -> bool:
    """True iff realpath(target) is, or is a descendant of, the realpath of any
    registered canon-roots entry."""
    target_real = os.path.realpath(target)
    for root in _read_canon_roots():
        try:
            root_real = os.path.realpath(root)
        except Exception:
            continue
        if target_real == root_real or target_real.startswith(root_real + os.sep):
            return True
    return False


def check_in_canon(cwd: str) -> str | None:
    """D2/R4: OFFER relocation when cwd is itself inside a canon — the PRIMARY
    Core checkout (any branch) or a path under a registered canon-roots entry
    — so a running session picks this up on its very next move, not only when
    hook-guard-canon-readonly.py blocks an actual edit. Fail-open: None on any
    git failure, a linked worktree, a second mount, or a plain non-canon dir."""
    in_primary = False
    info = _git_info(cwd)
    if info is not None:
        toplevel, git_dir_abs, git_common_abs, _branch = info
        if toplevel == os.path.realpath(str(_core_root())) and git_dir_abs == git_common_abs:
            in_primary = True

    if not in_primary and not _under_canon_root(cwd):
        return None
    return (
        f"[relocate] {cwd} is inside a canon (read-only from a live session) — do feature "
        f"work in an isolated copy instead: `scripts/session-isolate.sh <task-name>` (a linked "
        f"git worktree, or a second mount for other VCS backends)."
    )


def build_nudge(layers: list[tuple[str, Path, int, str]]) -> str:
    parts = [
        f"{label} ({root}) is {count} commit(s) behind — pull with `{cmd}`"
        for label, root, count, cmd in layers
    ]
    return (
        "[instructions-refresh] " + "; ".join(parts) + ". "
        "OFFER this refresh to the user via AskUserQuestion before running it — "
        "an explicit opt-in pull, never automatic."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()

    now = dt.datetime.now()
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    session_key = session_id if isinstance(session_id, str) and session_id else now.date().isoformat()
    if not _due(session_key):
        return 0

    _record(session_key)

    layers = check_layers(cwd)
    if layers:
        print(build_nudge(layers))

    deploy_warnings: list[str] = []
    branch_warning = check_branch(_core_root())
    if branch_warning:
        deploy_warnings.append(branch_warning)
    homogeneity_warning = check_homogeneity(_settings_path())
    if homogeneity_warning:
        deploy_warnings.append(homogeneity_warning)
    if deploy_warnings:
        print("[instructions-deploy] " + " ".join(deploy_warnings))

    worktree_offer = check_worktree_fresh(cwd)
    if worktree_offer:
        print(worktree_offer)

    relocate_offer = check_in_canon(cwd)
    if relocate_offer:
        print(relocate_offer)

    return 0


if __name__ == "__main__":
    sys.exit(main())
