"""Tests for hook-guard-canon-readonly.py — deny edits/commits anywhere in canon:
the serving/PRIMARY Core checkout (on ANY branch) plus any machine-local
canon-roots entry (scripts/lib/config_root.canon_roots_file()).

Hermetic: every repo is a local `git init` in tmp_path; the hook is invoked as a
subprocess with a JSON payload on stdin (mirrors test_hook_instructions_refresh.py).
CLAUDE_INSTRUCTIONS_REPO points the hook at the fixture "core" repo; CLAUDE_CANON_ROOTS_FILE
seeds the extra canon-roots source for the tests that exercise it.

Covers: primary-canon deny on main AND off-main, deny for git-commit, deny for
memory-global/ writes (the dropped exemption), canon-roots-file deny + sibling-
path allow, symlink realpath-normalization both directions, and the fail-open
ALLOW cases — linked worktree, path outside canon, non-git path, /tmp, personal
auto-memory, ref-only/pull git commands, a missing canon-roots file, and
malformed stdin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-canon-readonly.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_ENV},
        check=check,
        capture_output=True,
        text=True,
    )


def make_core(tmp_path: Path) -> Path:
    """A fresh git repo on `main` with a scripts/ dir and a memory-global/ leaf."""
    core = tmp_path / "core"
    core.mkdir()
    git("init", "--quiet", "-b", "main", ".", cwd=core)
    (core / "README.md").write_text("seed\n")
    (core / "scripts").mkdir()
    (core / "scripts" / "existing.py").write_text("x = 1\n")
    leaves = core / "memory-global" / "leaves"
    leaves.mkdir(parents=True)
    (leaves / "note.md").write_text("leaf\n")
    git("add", "-A", cwd=core)
    git("commit", "--quiet", "-m", "seed", cwd=core)
    return core


def off_branch(core: Path, name: str = "feat/x") -> None:
    git("switch", "--quiet", "-c", name, cwd=core)


def run_hook(core: "Path | str", payload: dict, extra_env: "dict | None" = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV, "CLAUDE_INSTRUCTIONS_REPO": str(core)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )


def _denied(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0 and '"permissionDecision": "deny"' in proc.stdout


def _allowed(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0 and proc.stdout.strip() == ""


# --- primary Core checkout: deny on ANY branch (no more off-main-only check) ---

def test_primary_offmain_edit_denies(tmp_path):
    core = make_core(tmp_path)
    off_branch(core)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(core / "scripts" / "existing.py")},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout
    assert "session-isolate.sh" in proc.stdout


def test_primary_on_main_edit_denies(tmp_path):
    core = make_core(tmp_path)  # stays on main
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(core / "scripts" / "new_file.py")},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout


def test_bash_git_commit_on_main_denies(tmp_path):
    core = make_core(tmp_path)  # stays on main
    proc = run_hook(core, {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'wip'"},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout


def test_bash_git_commit_offmain_denies(tmp_path):
    core = make_core(tmp_path)
    off_branch(core)
    proc = run_hook(core, {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'wip'"},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout


def test_detached_head_denies(tmp_path):
    core = make_core(tmp_path)
    git("checkout", "--quiet", "--detach", "HEAD", cwd=core)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(core / "scripts" / "existing.py")},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout


# --- the dropped memory-global/ exemption: now denied like everything else ---

def test_memory_global_write_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(core / "memory-global" / "leaves" / "new-leaf.md")},
        "cwd": str(core),
    })
    assert _denied(proc), proc.stdout


# --- fail-open ALLOW cases around the primary-Core check ---

def test_linked_worktree_edit_allows(tmp_path):
    core = make_core(tmp_path)
    off_branch(core)
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", "wt-branch", str(wt), "main", cwd=core)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(wt / "scripts" / "existing.py")},
        "cwd": str(wt),
    })
    assert _allowed(proc), proc.stdout


def test_file_outside_core_repo_allows(tmp_path):
    core = make_core(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    git("init", "--quiet", "-b", "main", ".", cwd=other)
    (other / "f.py").write_text("y\n")
    git("add", "-A", cwd=other)
    git("commit", "--quiet", "-m", "s", cwd=other)
    proc = run_hook(core, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(other / "f.py")},
        "cwd": str(other),
    })
    assert _allowed(proc), proc.stdout


def test_non_git_path_allows(tmp_path):
    core = make_core(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(plain / "z.txt")},
        "cwd": str(plain),
    })
    assert _allowed(proc), proc.stdout


def test_bash_unrelated_mentioning_git_commit_allows(tmp_path):
    core = make_core(tmp_path)
    for cmd in ('echo "run git commit later"', 'git log --grep="git commit"'):
        proc = run_hook(core, {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(core),
        })
        assert _allowed(proc), f"{cmd!r}: {proc.stdout}"


def test_git_pull_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, {
        "tool_name": "Bash",
        "tool_input": {"command": "git pull --ff-only"},
        "cwd": str(core),
    })
    assert _allowed(proc), proc.stdout


def test_sync_instructions_repo_pull_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, {
        "tool_name": "Bash",
        "tool_input": {"command": "scripts/sync-instructions-repo.sh pull"},
        "cwd": str(core),
    })
    assert _allowed(proc), proc.stdout


def test_ref_only_git_commands_allow(tmp_path):
    core = make_core(tmp_path)
    for cmd in ("git merge --ff-only origin/main", "git update-ref refs/heads/main HEAD"):
        proc = run_hook(core, {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(core),
        })
        assert _allowed(proc), f"{cmd!r}: {proc.stdout}"


def test_tmp_path_allows(tmp_path):
    core = make_core(tmp_path)
    target = Path("/tmp") / "claude-canon-readonly-test.txt"
    try:
        proc = run_hook(core, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "cwd": "/tmp",
        })
        assert _allowed(proc), proc.stdout
    finally:
        target.unlink(missing_ok=True)


def test_personal_automemory_path_allows(tmp_path):
    core = make_core(tmp_path)
    mem = tmp_path / "home" / ".claude-agent" / "projects" / "somehash" / "memory" / "MEMORY.md"
    mem.parent.mkdir(parents=True)
    mem.write_text("note\n")
    proc = run_hook(core, {
        "tool_name": "Write",
        "tool_input": {"file_path": str(mem)},
        "cwd": str(mem.parent),
    })
    assert _allowed(proc), proc.stdout


# --- canon-roots file: an org-neutral, machine-local extra canon source ---

def _no_primary_core(tmp_path: Path) -> Path:
    """A CLAUDE_INSTRUCTIONS_REPO value that will never match any target_dir in
    these canon-roots tests, isolating them to _under_registered_canon alone."""
    return tmp_path / "no-such-core"


def test_canon_roots_file_edit_denies(tmp_path):
    anchor = tmp_path / "canon-mirror"
    anchor.mkdir()
    (anchor / "doc.md").write_text("x\n")
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")
    proc = run_hook(_no_primary_core(tmp_path), {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(anchor / "doc.md")},
        "cwd": str(anchor),
    }, extra_env={"CLAUDE_CANON_ROOTS_FILE": str(roots_file)})
    assert _denied(proc), proc.stdout
    assert "session-isolate.sh" in proc.stdout


def test_canon_roots_sibling_path_allows(tmp_path):
    """A path that shares anchor's string prefix but is NOT a path-part
    descendant (e.g. a second mount named similarly) must not be caught by a
    naive startswith without a separator check."""
    anchor = tmp_path / "canon-mirror"
    anchor.mkdir()
    sibling = tmp_path / "canon-mirror-sibling"
    sibling.mkdir()
    (sibling / "f.py").write_text("y\n")
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")
    proc = run_hook(_no_primary_core(tmp_path), {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(sibling / "f.py")},
        "cwd": str(sibling),
    }, extra_env={"CLAUDE_CANON_ROOTS_FILE": str(roots_file)})
    assert _allowed(proc), proc.stdout


def test_missing_canon_roots_file_does_not_raise(tmp_path):
    proc = run_hook(_no_primary_core(tmp_path), {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "somewhere" / "f.py")},
        "cwd": str(tmp_path),
    }, extra_env={"CLAUDE_CANON_ROOTS_FILE": str(tmp_path / "does-not-exist.local")})
    assert _allowed(proc), proc.stdout


# --- symlinks: realpath both sides, so resolution decides, not the literal path ---

def test_symlink_into_canon_denies(tmp_path):
    anchor = tmp_path / "canon-mirror"
    anchor.mkdir()
    (anchor / "real.md").write_text("x\n")
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "link.md"
    link.symlink_to(anchor / "real.md")

    proc = run_hook(_no_primary_core(tmp_path), {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(link)},
        "cwd": str(outside),
    }, extra_env={"CLAUDE_CANON_ROOTS_FILE": str(roots_file)})
    assert _denied(proc), proc.stdout


def test_symlink_outside_canon_allows(tmp_path):
    anchor = tmp_path / "canon-mirror"
    anchor.mkdir()
    roots_file = tmp_path / "canon-roots.local"
    roots_file.write_text(f"{anchor}\n")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.md").write_text("y\n")
    link = anchor / "link.md"  # literal location is inside the anchor...
    link.symlink_to(outside / "real.md")  # ...but resolves outside it

    proc = run_hook(_no_primary_core(tmp_path), {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(link)},
        "cwd": str(anchor),
    }, extra_env={"CLAUDE_CANON_ROOTS_FILE": str(roots_file)})
    assert _allowed(proc), proc.stdout


def test_malformed_stdin_allows(tmp_path):
    core = make_core(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="not json",
        env={**os.environ, **GIT_ENV, "CLAUDE_INSTRUCTIONS_REPO": str(core)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
