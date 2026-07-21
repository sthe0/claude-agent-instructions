"""Tests for the extended Bash branch of hook-guard-canon-readonly.py — deny
common in-place write verbs (`sed -i`, `>>`/`>`, `tee`, `cp`/`mv` dest, `patch`,
`git apply`) whose target resolves under canon, while ALLOWING the same verbs
into a linked worktree and staying fail-open on garbage / plain reads.

Hermetic: every repo is a local `git init` in tmp_path; the hook is invoked as a
subprocess with a JSON payload on stdin. Mirrors test_hook_guard_canon_readonly.py
(the git-commit deny tests live there; this file covers only the new write verbs).
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
    core = tmp_path / "core"
    core.mkdir()
    git("init", "--quiet", "-b", "main", ".", cwd=core)
    (core / "README.md").write_text("seed\n")
    (core / "scripts").mkdir()
    (core / "scripts" / "existing.py").write_text("x = 1\n")
    git("add", "-A", cwd=core)
    git("commit", "--quiet", "-m", "seed", cwd=core)
    return core


def make_worktree(core: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", "wt-branch", str(wt), "main", cwd=core)
    return wt


def run_hook(core, command: str, cwd) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV, "CLAUDE_INSTRUCTIONS_REPO": str(core)}
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )


def _denied(proc) -> bool:
    return proc.returncode == 0 and '"permissionDecision": "deny"' in proc.stdout


def _allowed(proc) -> bool:
    return proc.returncode == 0 and proc.stdout.strip() == ""


# --- canon writes: DENY ---

def test_sed_in_place_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed -i 's/x/y/' scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_sed_in_place_with_suffix_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed -i.bak 's/x/y/' scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_append_redirect_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi >> scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_glued_redirect_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi >scripts/new.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_tee_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "echo hi | tee scripts/existing.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_cp_dest_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "cp /tmp/whatever scripts/copied.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_mv_dest_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "mv /tmp/whatever scripts/moved.py", cwd=core)
    assert _denied(proc), proc.stdout


def test_cp_target_directory_flag_into_canon_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "cp /tmp/a /tmp/b -t scripts", cwd=core)
    assert _denied(proc), proc.stdout


def test_patch_in_canon_cwd_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "patch -p1 < /tmp/some.diff", cwd=core)
    assert _denied(proc), proc.stdout


def test_git_apply_in_canon_cwd_denies(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "git apply /tmp/some.diff", cwd=core)
    assert _denied(proc), proc.stdout


def test_cd_into_canon_then_sed_denies(tmp_path):
    """A leading `cd <canon>` must make the write resolve against canon even when
    the session payload cwd is elsewhere."""
    core = make_core(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = run_hook(core, f"cd {core} && sed -i 's/x/y/' scripts/existing.py", cwd=outside)
    assert _denied(proc), proc.stdout


# --- same verbs into a linked worktree: ALLOW ---

def test_sed_in_place_into_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "sed -i 's/x/y/' scripts/existing.py", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_append_redirect_into_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "echo hi >> scripts/existing.py", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_tee_into_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "echo hi | tee scripts/existing.py", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_git_apply_in_worktree_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, "git apply /tmp/some.diff", cwd=wt)
    assert _allowed(proc), proc.stdout


def test_cd_into_worktree_then_sed_allows(tmp_path):
    core = make_core(tmp_path)
    wt = make_worktree(core, tmp_path)
    proc = run_hook(core, f"cd {wt} && sed -i 's/x/y/' scripts/existing.py", cwd=core)
    assert _allowed(proc), proc.stdout


# --- copying OUT of canon is a read of the source: ALLOW ---

def test_cp_canon_source_out_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "cp scripts/existing.py /tmp/exfil.py", cwd=core)
    assert _allowed(proc), proc.stdout


# --- non-write / read-only Bash in canon: ALLOW ---

def test_sed_without_in_place_allows(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed 's/x/y/' scripts/existing.py", cwd=core)
    assert _allowed(proc), proc.stdout


def test_plain_read_allows(tmp_path):
    core = make_core(tmp_path)
    for cmd in ("cat scripts/existing.py", "grep x scripts/existing.py", "ls scripts"):
        proc = run_hook(core, cmd, cwd=core)
        assert _allowed(proc), f"{cmd!r}: {proc.stdout}"


def test_input_redirect_only_allows(tmp_path):
    """`<` is an input read, not a write — a bare read redirect must not deny."""
    core = make_core(tmp_path)
    proc = run_hook(core, "cat < scripts/existing.py", cwd=core)
    assert _allowed(proc), proc.stdout


# --- fail-open on unparseable input ---

def test_unbalanced_quote_fails_open(tmp_path):
    core = make_core(tmp_path)
    proc = run_hook(core, "sed -i 's/x/y/ scripts/existing.py", cwd=core)  # unterminated quote
    assert _allowed(proc), proc.stdout
