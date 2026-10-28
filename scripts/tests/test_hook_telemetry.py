"""hook_telemetry.run: one decision row per evaluation, hook behaviour unchanged.

The helper redirects file descriptor 1, so each case runs a throwaway hook script in a
subprocess and compares its stdout and exit code with what the same script does bare."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lib import hook_telemetry

SCRIPTS = Path(__file__).resolve().parents[1]


def _script(tmp_path: Path, body: str, *, wrapped: bool) -> Path:
    call = ('sys.exit(hook_telemetry.run(main, mechanism="hook-x"))' if wrapped
            else "sys.exit(main())")
    path = tmp_path / ("wrapped.py" if wrapped else "bare.py")
    path.write_text(
        f"import json, os, sys\nsys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from lib import hook_telemetry\n"
        + textwrap.dedent(body) + f"\nif __name__ == '__main__':\n    {call}\n",
        encoding="utf-8")
    return path


def _run(script: Path, log: Path, stdin: str = ""):
    env = {**os.environ, "AGENTCTL_DECISION_LOG": str(log)}
    return subprocess.run([sys.executable, str(script)], input=stdin, env=env,
                          capture_output=True, text=True, timeout=60)


def _rows(log: Path) -> list:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


DENY_JSON = '{"hookSpecificOutput": {"permissionDecision": "deny"}}'
HOOKS = {
    "silent": ("def main():\n    return 0\n", "silent"),
    "nudge": ("def main():\n    print('{\"systemMessage\": \"hi\"}')\n    return 0\n", "nudge"),
    "ask": ("def main():\n    print('{\"hookSpecificOutput\": {\"permissionDecision\": \"ask\"}}')\n"
            "    return 0\n", "nudge"),
    "deny_json": (f"def main():\n    print({DENY_JSON!r})\n    return 0\n", "deny"),
    "block": ("def main():\n    print('{\"decision\": \"block\"}')\n    return 0\n", "deny"),
    "exit_two": ("def main():\n    print('x', file=sys.stderr)\n    return 2\n", "deny"),
    "plain_text": ("def main():\n    print('remember this')\n    return 0\n", "nudge"),
}


@pytest.mark.parametrize("name", sorted(HOOKS))
def test_one_row_with_the_right_decision_and_behaviour_unchanged(tmp_path, name):
    body, decision = HOOKS[name]
    body = body.replace("def main():\n", "def main():\n    sys.stdin.read()\n")
    payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s-1"})
    log = tmp_path / "log.jsonl"
    bare = _run(_script(tmp_path, body, wrapped=False), tmp_path / "unused.jsonl", payload)
    wrapped = _run(_script(tmp_path, body, wrapped=True), log, payload)
    assert (wrapped.stdout, wrapped.returncode) == (bare.stdout, bare.returncode)
    (row,) = _rows(log)
    assert (row["decision"], row["mechanism"], row["event"], row["session"], row["layer"]) == (
        decision, "hook-x", "PreToolUse", "s-1", "core")
    assert row["test"] is True and row["ts"].endswith("Z")


def test_a_crash_reraises_with_the_same_exit_code_and_stderr_trace(tmp_path):
    body = "def main():\n    print('partial')\n    raise RuntimeError('boom')\n"
    bare = _run(_script(tmp_path, body, wrapped=False), tmp_path / "unused.jsonl")
    log = tmp_path / "log.jsonl"
    wrapped = _run(_script(tmp_path, body, wrapped=True), log)
    assert (wrapped.stdout, wrapped.returncode) == (bare.stdout, bare.returncode) == ("partial\n", 1)
    assert "RuntimeError: boom" in wrapped.stderr
    assert [r["decision"] for r in _rows(log)] == ["nudge"]


def test_system_exit_status_passes_through(tmp_path):
    body = "def main():\n    sys.exit(3)\n"
    log = tmp_path / "log.jsonl"
    assert _run(_script(tmp_path, body, wrapped=True), log).returncode == 3
    assert len(_rows(log)) == 1


def test_output_of_a_child_process_is_reemitted(tmp_path):
    body = ("import subprocess\n"
            "def main():\n    subprocess.run(['echo', 'from child'])\n    return 0\n")
    wrapped = _run(_script(tmp_path, body, wrapped=True), tmp_path / "log.jsonl")
    assert wrapped.stdout == "from child\n"


def test_an_uncreatable_log_switches_telemetry_off_and_changes_nothing(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    body, _ = HOOKS["deny_json"]
    bare = _run(_script(tmp_path, body, wrapped=False), tmp_path / "unused.jsonl")
    wrapped = _run(_script(tmp_path, body, wrapped=True), blocker / "log.jsonl")
    assert (wrapped.stdout, wrapped.returncode) == (bare.stdout, bare.returncode)
    assert not (blocker / "log.jsonl").exists()


def test_record_itself_swallows_an_unwritable_log(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("AGENTCTL_DECISION_LOG", str(blocker / "log.jsonl"))
    hook_telemetry.record("hook-x", "PreToolUse", "deny", "s1")
    assert not (blocker / "log.jsonl").exists()


def test_an_uncreatable_capture_file_runs_the_hook_untouched(monkeypatch, capsys):
    def no_capture():
        raise OSError("no space left")

    monkeypatch.setattr(hook_telemetry, "_open_capture", no_capture)
    assert hook_telemetry.run(lambda: 7, mechanism="hook-x") == 7


FULL_DISK_CAPTURE = textwrap.dedent("""
    class Full:
        def __init__(self):
            self._f = open('/dev/full', 'wb')
        def fileno(self):
            return self._f.fileno()
        def seek(self, *args):
            pass
        def read(self):
            return b''
        def close(self):
            self._f.close()
    hook_telemetry._open_capture = Full
""")


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
def test_a_capture_that_runs_out_of_space_cannot_turn_a_deny_into_an_allow(tmp_path):
    body, _ = HOOKS["deny_json"]
    bare = _run(_script(tmp_path, body, wrapped=False), tmp_path / "unused.jsonl")
    wrapped = _run(_script(tmp_path, FULL_DISK_CAPTURE + body, wrapped=True), tmp_path / "log.jsonl")
    assert wrapped.returncode == bare.returncode == 0
    assert wrapped.stdout == bare.stdout == DENY_JSON + "\n"
    assert wrapped.stderr == ""


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
def test_a_capture_that_runs_out_of_space_keeps_the_exit_code_and_stderr(tmp_path):
    body, _ = HOOKS["exit_two"]
    bare = _run(_script(tmp_path, body, wrapped=False), tmp_path / "unused.jsonl")
    wrapped = _run(_script(tmp_path, FULL_DISK_CAPTURE + body, wrapped=True), tmp_path / "log.jsonl")
    assert (wrapped.returncode, wrapped.stderr) == (bare.returncode, bare.stderr) == (2, "x\n")


def test_any_failure_after_the_hook_returns_changes_nothing(tmp_path):
    body = ("from lib import config_root\n"
            "def broken():\n    raise RuntimeError('no log')\n"
            "config_root.decision_log = broken\n" + HOOKS["deny_json"][0])
    bare = _run(_script(tmp_path, HOOKS["deny_json"][0], wrapped=False), tmp_path / "unused.jsonl")
    wrapped = _run(_script(tmp_path, body, wrapped=True), tmp_path / "log.jsonl")
    assert (wrapped.stdout, wrapped.returncode, wrapped.stderr) == (bare.stdout, bare.returncode, "")
    assert not (tmp_path / "log.jsonl").exists()


def test_a_row_stays_under_the_byte_cap_whatever_the_fields_hold():
    row = hook_telemetry.decision_row('"\\' * 300, "é" * 300, "deny", '𝄞"' * 300)
    assert len(json.dumps(row).encode()) < 2048


def test_stdin_is_not_consumed_for_a_hook_that_never_reads_it(tmp_path):
    wrapped = _run(_script(tmp_path, "def main():\n    return 0\n", wrapped=True),
                   tmp_path / "log.jsonl", stdin="not json")
    assert wrapped.returncode == 0
    assert _rows(tmp_path / "log.jsonl")[0]["event"] == "unknown"


def test_stdin_is_handed_to_the_hook_unchanged(tmp_path):
    body = "def main():\n    print(json.load(sys.stdin)['k'])\n    return 0\n"
    wrapped = _run(_script(tmp_path, body, wrapped=True), tmp_path / "log.jsonl", '{"k": "v"}')
    assert wrapped.stdout == "v\n"


@pytest.mark.parametrize("marker,session,expected", [
    ({}, "s-1", False),
    ({"PYTEST_CURRENT_TEST": "t"}, "s-1", True),
    ({"AGENT_TELEMETRY_TEST": "1"}, "s-1", True),
    ({"AGENT_TELEMETRY_TEST": "0"}, "s-1", False),
    ({}, "agentctl-test-session", True),
])
def test_test_marker(monkeypatch, marker, session, expected):
    for name in ("PYTEST_CURRENT_TEST", "AGENT_TELEMETRY_TEST"):
        monkeypatch.delenv(name, raising=False)
    for name, value in marker.items():
        monkeypatch.setenv(name, value)
    assert hook_telemetry.is_test_run(session) is expected


def test_record_writes_a_bounded_single_line(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("AGENTCTL_DECISION_LOG", str(log))
    hook_telemetry.record("m" * 5000, "e", "silent", "s" * 5000, layer="arc")
    (line,) = log.read_text().splitlines()
    assert len(line.encode()) < 2048 and json.loads(line)["layer"] == "arc"
