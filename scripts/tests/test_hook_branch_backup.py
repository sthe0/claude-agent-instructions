"""Unit tests for hook-branch-backup.py: it only starts the upkeep and the disk-pressure
check detached, and stays silent and exit-0 on every path (malformed stdin, a failing
spawn, any cwd).
"""
from __future__ import annotations

import importlib.util
import io
import json
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


REAPER = str(hook.REPO_ROOT / "scripts" / "hook-reaper.py")


def flags_of(calls):
    return [argv[2:] for argv, _ in calls]


def test_it_starts_the_upkeep_detached_without_waiting(popen, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    upkeep = [(argv, kwargs) for argv, kwargs in popen if "--upkeep-only" in argv]
    [(argv, kwargs)] = upkeep
    assert argv == ["python3", REAPER, "--upkeep-only", "--no-wait"]
    assert kwargs["start_new_session"] is True


def test_it_starts_the_pressure_check_detached_beside_the_upkeep(popen, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    assert flags_of(popen) == [["--upkeep-only", "--no-wait"], ["--on-pressure"]]
    pressure = [(argv, kwargs) for argv, kwargs in popen if "--on-pressure" in argv]
    [(argv, kwargs)] = pressure
    assert argv == ["python3", REAPER, "--on-pressure"]
    assert kwargs["start_new_session"] is True


def test_the_pressure_check_logs_to_its_own_file(popen, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    state = tmp_path / "home" / ".local" / "state" / "claude-reaper"
    assert sorted(p.name for p in state.iterdir()) == ["pressure.log", "upkeep.log"]


def test_the_pressure_check_starts_for_a_stop_payload(popen, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    assert hook.main() == 0
    assert ["--on-pressure"] in flags_of(popen)


def test_the_pressure_check_starts_for_a_session_start_payload(popen, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart"})))
    assert hook.main() == 0
    assert ["--on-pressure"] in flags_of(popen)


def test_a_failing_first_spawn_does_not_stop_the_second(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls = []

    class FirstFails:
        def __init__(self, argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise FileNotFoundError("python3")

    monkeypatch.setattr(hook.subprocess, "Popen", FirstFails)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook.main() == 0
    assert [argv[2] for argv in calls] == ["--upkeep-only", "--on-pressure"]


def test_it_acts_on_its_own_repo_whatever_the_cwd(popen, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert hook.main() == 0
    assert len(popen) == 2
    for argv, kwargs in popen:
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
    assert len(popen) == 2


def test_an_unreadable_stdin_does_not_matter(popen, monkeypatch):
    class Broken:
        def read(self):
            raise OSError("closed")

    monkeypatch.setattr(sys, "stdin", Broken())
    assert hook.main() == 0
    assert len(popen) == 2


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
