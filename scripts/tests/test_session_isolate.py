"""session-isolate.sh: git-backend isolation router (Component C, Stage 5).

Driven end-to-end via subprocess, mirroring test_hook_scope_conflict.py's style:
HOME points at a tmp tree (so session_scope's DEFAULT_SCOPES_DIR resolves under
it) and GIT_BIN is stubbed exactly like project_entry/tests/test-enter-task.sh
stubs it — logging every call, answering `rev-parse --show-toplevel` /
`worktree list --porcelain`, and never touching a real repo.

CLAUDE_DRY_RUN is the seam backends/git.sh already honors: under dry-run,
backend_ensure_workspace never calls `worktree add` (the only mutating git
call), which is the "zero mutation" half of Stage 5's done criterion. The
scope re-registration is NOT gated on dry-run (see session-isolate.sh's
comment) — it is a bookkeeping write under ~/.claude/agentctl/scopes, not a
mutation of the task's git tree, and asserting it is what proves the isolated
root is immediately visible to the conflict detector.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_scope import registry  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "session-isolate.sh"


def _stub_git(tmp_path: Path, fake_toplevel: Path, calls_log: Path, wt_list: Path) -> Path:
    """A GIT_BIN stub matching project_entry/tests/test-enter-task.sh's shape:
    logs every invocation, always answers rev-parse with fake_toplevel (ignoring
    cwd/-C so the stub works regardless of where session-isolate.sh invokes it
    from), answers `worktree list` from wt_list, and never really runs `worktree
    add` (only records that it was asked to)."""
    stub = tmp_path / "git-stub"
    stub.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>{calls_log}
shift_n=0
[[ "${{1:-}}" == "-C" ]] && shift_n=2
shift $shift_n 2>/dev/null || true
case "$1 $2" in
  "rev-parse --show-toplevel") printf '%s\\n' "{fake_toplevel}" ;;
  "worktree list")             cat {wt_list} ;;
  "worktree add")              : ;;
  *) : ;;
esac
""")
    stub.chmod(0o755)
    return stub


def _run(
    tmp_path: Path,
    task_name: str,
    home: Path,
    fake_toplevel: Path,
    git_calls: Path,
    wt_list: Path,
    session_id: "str | None" = "s-me",
    dry_run: bool = True,
    detector: "Path | None" = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    # config_root resolves CLAUDE_CONFIG_DIR/CLAUDE_AGENT_HOME before HOME —
    # strip them so the child derives its root from the tmp HOME, not the
    # developer machine's real isolated root.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_AGENT_HOME", None)
    env["GIT_BIN"] = str(_stub_git(tmp_path, fake_toplevel, git_calls, wt_list))
    env["PATH"] = "/usr/bin:/bin"
    if session_id is not None:
        env["CLAUDE_SESSION_ID"] = session_id
    else:
        env.pop("CLAUDE_SESSION_ID", None)
    if dry_run:
        env["CLAUDE_DRY_RUN"] = "1"
    else:
        env.pop("CLAUDE_DRY_RUN", None)
    if detector is not None:
        env["CLAUDE_BACKEND_DETECTOR"] = str(detector)
    else:
        # Force the org-neutral git default regardless of what's on this
        # machine's PATH (a dev box may have ya/arc installed).
        det = tmp_path / "det-git.py"
        det.write_text("print('git none')\n")
        env["CLAUDE_BACKEND_DETECTOR"] = str(det)
    return subprocess.run(
        ["bash", str(SCRIPT), task_name],
        cwd=str(fake_toplevel),
        capture_output=True,
        text=True,
        env=env,
    )


def _scopes_dir(home: Path) -> Path:
    return home / ".claude" / "agentctl" / "scopes"


def test_dry_run_reports_worktree_reregisters_scope_no_git_mutation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_toplevel = tmp_path / "myrepo"
    fake_toplevel.mkdir()
    git_calls = tmp_path / "git-calls.log"
    wt_list = tmp_path / "wt-list.txt"
    git_calls.write_text("")
    wt_list.write_text("")

    proc = _run(tmp_path, "task-name", home, fake_toplevel, git_calls, wt_list, dry_run=True)

    assert proc.returncode == 0, proc.stderr
    expected_dir = str(tmp_path / "myrepo-task-name")
    assert proc.stdout.strip().splitlines()[-1] == expected_dir

    # Zero mutation: the only mutating git call (`worktree add`) never fired.
    assert "worktree add" not in git_calls.read_text()

    # Continuation + land-back instructions surfaced to the user.
    assert "land-on-main.sh" in proc.stderr
    assert expected_dir in proc.stderr

    # Scope re-registration DID happen — the detector would see the new root.
    rec = registry.load(_scopes_dir(home), "s-me")
    assert rec is not None
    assert rec.repo_root == expected_dir
    assert rec.cwd == expected_dir
    assert rec.vcs == "git"


def test_non_dry_run_creates_worktree_via_backend(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_toplevel = tmp_path / "myrepo"
    fake_toplevel.mkdir()
    git_calls = tmp_path / "git-calls.log"
    wt_list = tmp_path / "wt-list.txt"
    git_calls.write_text("")
    wt_list.write_text("")

    proc = _run(tmp_path, "task-name", home, fake_toplevel, git_calls, wt_list, dry_run=False)

    assert proc.returncode == 0, proc.stderr
    expected_dir = str(tmp_path / "myrepo-task-name")
    assert proc.stdout.strip().splitlines()[-1] == expected_dir
    assert f"worktree add {expected_dir} -b task-name" in git_calls.read_text()

    rec = registry.load(_scopes_dir(home), "s-me")
    assert rec is not None and rec.repo_root == expected_dir


def test_existing_worktree_is_reused_not_recreated(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_toplevel = tmp_path / "myrepo"
    fake_toplevel.mkdir()
    git_calls = tmp_path / "git-calls.log"
    wt_list = tmp_path / "wt-list.txt"
    git_calls.write_text("")
    expected_dir = tmp_path / "myrepo-task-name"
    wt_list.write_text(f"worktree {expected_dir}\n")

    proc = _run(tmp_path, "task-name", home, fake_toplevel, git_calls, wt_list, dry_run=False)

    assert proc.returncode == 0, proc.stderr
    assert "worktree add" not in git_calls.read_text()
    assert proc.stdout.strip().splitlines()[-1] == str(expected_dir)


def test_missing_session_id_skips_reregistration_but_still_reports_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_toplevel = tmp_path / "myrepo"
    fake_toplevel.mkdir()
    git_calls = tmp_path / "git-calls.log"
    wt_list = tmp_path / "wt-list.txt"
    git_calls.write_text("")
    wt_list.write_text("")

    proc = _run(
        tmp_path, "task-name", home, fake_toplevel, git_calls, wt_list,
        session_id=None, dry_run=True,
    )

    assert proc.returncode == 0, proc.stderr
    expected_dir = str(tmp_path / "myrepo-task-name")
    assert proc.stdout.strip().splitlines()[-1] == expected_dir
    assert not _scopes_dir(home).exists() or not list(_scopes_dir(home).glob("*.json"))


def test_missing_task_name_argument_errors():
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode != 0
    assert "usage" in proc.stderr
