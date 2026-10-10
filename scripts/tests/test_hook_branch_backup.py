"""Unit tests for hook-branch-backup.py: it only starts the upkeep detached, and stays
silent and exit-0 on every path (malformed stdin, a failing spawn, any cwd).
"""
from __future__ import annotations

import importlib.util
import io
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("hook_branch_backup", SCRIPTS / "hook-branch-backup.py")
hook = importlib.util.module_from_spec(_spec)
sys.modules["hook_branch_backup"] = hook
_spec.loader.exec_module(hook)


@pytest.fixture
def popen(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(hook.subprocess, "Popen", FakePopen)
    return calls


def test_it_starts_the_upkeep_detached_without_waiting(popen, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    [(argv, kwargs)] = popen
    assert argv == ["python3", str(hook.REPO_ROOT / "scripts" / "hook-reaper.py"), "--upkeep-only", "--no-wait"]
    assert kwargs["start_new_session"] is True


def test_it_acts_on_its_own_repo_whatever_the_cwd(popen, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert hook.main() == 0
    [(argv, kwargs)] = popen
    assert Path(argv[1]).is_file()
    assert kwargs["cwd"] == str(hook.REPO_ROOT)


def test_it_prints_nothing(popen, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_malformed_stdin_does_not_matter(popen, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert hook.main() == 0
    assert len(popen) == 1


def test_an_unreadable_stdin_does_not_matter(popen, monkeypatch):
    class Broken:
        def read(self):
            raise OSError("closed")

    monkeypatch.setattr(sys, "stdin", Broken())
    assert hook.main() == 0
    assert len(popen) == 1


def test_a_failing_spawn_is_swallowed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def refuse(*args, **kwargs):
        raise FileNotFoundError("python3")

    monkeypatch.setattr(hook.subprocess, "Popen", refuse)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_it_is_registered_once_for_stop_and_once_for_session_start():
    text = (SCRIPTS / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    rows = re.findall(r'\(\s*"(\w+)",\s*\w+,\s*"hook-branch-backup\.py",\s*(\d+)\s*\)', text)
    assert sorted(rows) == [("SessionStart", "5"), ("Stop", "5")]
