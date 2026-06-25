"""Stage 5: long-job-arm advisory hook — fires on a long-job launch, silent
otherwise, never blocks, once per session."""
import importlib.util
import io
import json
import uuid
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "hook_long_job_arm",
    Path(__file__).resolve().parents[1] / "hook-long-job-arm.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _run(monkeypatch, capsys, command, session=None, tool="Bash"):
    sid = session or f"s-{uuid.uuid4().hex[:8]}"
    payload = {"tool_name": tool, "tool_input": {"command": command}, "session_id": sid}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out, sid


# --- detector unit ------------------------------------------------------------

def test_detect_nohup():
    assert mod.detect("nohup python3 watch.py &")


def test_detect_orchestrator_launch():
    assert mod.detect("nirvana workflow start --id abc")
    assert mod.detect("yt start-op map --src //tmp/a")


def test_detect_silent_on_plain_command():
    assert mod.detect("git status") is None
    assert mod.detect("python3 -m pytest -q") is None
    assert mod.detect("cat nirvana_notes.md") is None  # tool word, no launch verb


# --- hook behaviour -----------------------------------------------------------

def test_fires_once_and_is_advisory(monkeypatch, capsys):
    rc, out, sid = _run(monkeypatch, capsys, "nohup ./train.sh &")
    assert rc == 0
    assert "long-job-arm" in out
    # second identical launch in the same session stays silent
    rc2, out2, _ = _run(monkeypatch, capsys, "nohup ./train.sh &", session=sid)
    assert rc2 == 0
    assert out2 == ""


def test_silent_on_negative(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, "ls -la")
    assert rc == 0
    assert out == ""


def test_ignores_non_bash(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, "nohup x &", tool="Read")
    assert rc == 0
    assert out == ""
