"""Tests for lint-hooks-executable.py.

Builds a scripts/ dir with hook-*.py files under a throwaway git repo and checks
that a non-executable hook (on disk or in git's index) fails the lint.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "lint-hooks-executable.py"
    spec = importlib.util.spec_from_file_location("lint_hooks_executable", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_mod()
main = _mod.main


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)


def _make_repo(tmp: Path) -> Path:
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "t@t")
    _git(tmp, "config", "user.name", "t")
    (tmp / "scripts").mkdir()
    return tmp / "scripts"


def _add_hook(scripts: Path, name: str, *, executable: bool) -> Path:
    p = scripts / name
    p.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    if executable:
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_all_executable_returns_0(tmp_path):
    scripts = _make_repo(tmp_path)
    _add_hook(scripts, "hook-a.py", executable=True)
    _add_hook(scripts, "hook-b.py", executable=True)
    _git(tmp_path, "add", "-A")
    assert main(["--root", str(tmp_path)]) == 0


def test_non_executable_on_disk_returns_1(tmp_path):
    scripts = _make_repo(tmp_path)
    _add_hook(scripts, "hook-a.py", executable=True)
    _add_hook(scripts, "hook-bad.py", executable=False)
    _git(tmp_path, "add", "-A")
    assert main(["--root", str(tmp_path)]) == 1


def test_git_mode_non_executable_returns_1(tmp_path):
    scripts = _make_repo(tmp_path)
    bad = _add_hook(scripts, "hook-bad.py", executable=False)
    _git(tmp_path, "add", "-A")  # recorded as 100644
    # Make it executable on disk only; git still has 100644 until re-added.
    bad.chmod(bad.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert os.access(bad, os.X_OK)
    assert main(["--root", str(tmp_path)]) == 1


def test_no_hooks_returns_0(tmp_path):
    _make_repo(tmp_path)
    assert main(["--root", str(tmp_path)]) == 0
