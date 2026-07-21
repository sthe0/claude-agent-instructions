"""Tests for the post-pull reminder-hook rewire in sync-instructions-repo.sh.

After a pull that APPLIED commits (the behind>0 reconcile path), the pull re-runs
the reminder-hook installer so hooks added to the repo after onboarding reach live
settings.json. The installer entrypoint is an env seam (CLAUDE_INSTALL_HOOKS_BIN)
so tests can stub it. Fail-open: an installer failure emits a WARN but the pull
still returns 0; and the rewire must NOT run on an up-to-date pull (behind==0).

Each test builds a throwaway bare "remote" + a clone one commit behind, then runs
the real script with the installer stubbed via the env seam. Mirrors
test_sync_pull_integrity_gate.py. A no-op CLAUDE_VERIFY_ALL_BIN stub keeps the
integrity gate quiet so assertions isolate the rewire behavior.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "sync-instructions-repo.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_repos(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir()
    seed.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "c1")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)],
                   check=True, capture_output=True, text=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")

    (seed / "b.txt").write_text("2\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "c2")
    _git(seed, "push", "origin", "main")
    return clone


def _installer_stub(path: Path, marker: Path, rc: int) -> Path:
    """A shell installer stub that touches `marker` when invoked, then exits rc."""
    path.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit {rc}\n")
    path.chmod(0o755)
    return path


def _ok_stub(path: Path) -> Path:
    path.write_text("import sys\nsys.exit(0)\n")
    return path


def _run_pull(clone: Path, cwd: Path, tmp_path: Path, **env_extra: str):
    env = dict(os.environ)
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(clone)
    env["CLAUDE_SYNC_NONINTERACTIVE"] = "1"
    env["HOME"] = str(tmp_path)
    env["CLAUDE_VERIFY_ALL_BIN"] = str(_ok_stub(tmp_path / "va.py"))
    env.update(env_extra)
    return subprocess.run(["bash", str(SCRIPT), "pull"], cwd=str(cwd),
                          env=env, capture_output=True, text=True)


class TestPullRewiresHooks:
    def test_applied_pull_runs_the_installer(self, tmp_path):
        clone = _make_repos(tmp_path)
        marker = tmp_path / "installer-ran"
        stub = _installer_stub(tmp_path / "install.sh", marker, 0)
        r = _run_pull(clone, tmp_path, tmp_path, CLAUDE_INSTALL_HOOKS_BIN=str(stub))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert marker.exists(), "installer stub was not invoked on an applied pull"
        assert "reminder hooks rewired" in out

    def test_up_to_date_pull_skips_the_installer(self, tmp_path):
        clone = _make_repos(tmp_path)
        marker = tmp_path / "installer-ran"
        stub = _installer_stub(tmp_path / "install.sh", marker, 0)
        # first pull applies the commit and runs the rewire
        _run_pull(clone, tmp_path, tmp_path, CLAUDE_INSTALL_HOOKS_BIN=str(stub))
        marker.unlink(missing_ok=True)
        # second pull: behind==0 -> rewire must NOT run
        r = _run_pull(clone, tmp_path, tmp_path, CLAUDE_INSTALL_HOOKS_BIN=str(stub))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "already up to date" in out
        assert not marker.exists(), "installer ran on an up-to-date pull"

    def test_installer_failure_is_fail_open(self, tmp_path):
        clone = _make_repos(tmp_path)
        marker = tmp_path / "installer-ran"
        stub = _installer_stub(tmp_path / "install.sh", marker, 3)  # installer FAILS
        r = _run_pull(clone, tmp_path, tmp_path, CLAUDE_INSTALL_HOOKS_BIN=str(stub))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out            # fail-open: pull still succeeds
        assert "pull: done" in out
        assert marker.exists()
        assert "WARN reminder-hook rewire" in out

    def test_missing_installer_is_a_noop(self, tmp_path):
        clone = _make_repos(tmp_path)
        r = _run_pull(clone, tmp_path, tmp_path,
                      CLAUDE_INSTALL_HOOKS_BIN=str(tmp_path / "does-not-exist.sh"))
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "pull: done" in out
