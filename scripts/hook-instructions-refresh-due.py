#!/usr/bin/env python3
"""UserPromptSubmit hook: once-daily proactive OFFER to refresh instructions.

Replaces the silent 10-min background auto-pull (install-sync-cron.sh /
install-sync-systemd-timer.sh) with an explicit, opt-in nudge: once per
calendar day, on the day's first prompt, check whether the Core instruction
repo and/or the current project's own git-tracked .claude/ layer are behind
their upstream, and if so print a line instructing the agent to OFFER a pull
via AskUserQuestion — never pull automatically.

Throttled via a stamp file (mirrors hook-policy-scorecard-due.py's throttle
skeleton) and fail-open throughout: any git failure (offline, timeout,
non-git cwd, no upstream configured) is treated as "not behind" and stays
silent, so a flaky network can never wedge or spam a prompt. The stamp is
written once the day's due-check fires (before the fetch), so the check runs
at most once per calendar day regardless of outcome — an offline morning does
not trigger a fetch on every later prompt.

Output goes to stdout (UserPromptSubmit convention — becomes turn context,
mirrors hook-engine-start.py). Exit 0 always.

Also rides this same daily throttle to check the ORTHOGONAL "deployed" axis:
whether the serving Core checkout (the one settings.json hook commands point
at) is actually the branch/tree that gets run, not just whether it is behind.
A merge to origin/main does not "deploy" anything by itself — the checkout on
disk still runs whatever it has checked out. Two sub-checks, printed on a
separate "[instructions-deploy]" line so the existing "[instructions-refresh]"
nudge assertions are unaffected: (1) the Core checkout's HEAD is not on the
default branch; (2) settings.json hook commands resolve to more than one
distinct checkout root. Both fail-open like everything else in this hook.
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


def _due(now: dt.datetime) -> bool:
    try:
        raw = _stamp_path().read_text(encoding="utf-8").strip()
        prev = dt.datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return True
    return prev.date() != now.date()


def _record(now: dt.datetime) -> None:
    try:
        stamp = _stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(now.isoformat(), encoding="utf-8")
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
    if not _due(now):
        return 0

    _record(now)

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

    return 0


if __name__ == "__main__":
    sys.exit(main())
