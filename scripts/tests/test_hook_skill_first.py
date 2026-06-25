"""Stage 5: skill-first advisory hook — nudges on hand-rolled domain ops,
silent on plain shell, never blocks, once per operation-class per session."""
import importlib.util
import io
import json
import uuid
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "hook_skill_first",
    Path(__file__).resolve().parents[1] / "hook-skill-first.py",
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

def test_detect_vcs():
    names = [n for n, _ in mod.detect("arc commit -m wip && arc push")]
    assert "vcs" in names


def test_detect_vault_and_grep():
    assert any(n == "secrets" for n, _ in mod.detect("ya vault get version sec-xxx"))
    assert any(n == "codesearch" for n, _ in mod.detect("arc grep -n TODO"))


def test_detect_tracker_rest():
    names = [n for n, _ in mod.detect(
        "curl -X PATCH https://st-api.yandex-team.ru/v2/issues/ABC-1")]
    assert "tracker" in names


def test_silent_on_plain_shell():
    assert mod.detect("git status") == []
    assert mod.detect("python3 build.py") == []


# --- hook behaviour -----------------------------------------------------------

def test_fires_once_per_class(monkeypatch, capsys):
    rc, out, sid = _run(monkeypatch, capsys, "arc push")
    assert rc == 0 and "skill-first" in out and "vcs" in out
    # same class again -> silent
    rc2, out2, _ = _run(monkeypatch, capsys, "arc commit -m x", session=sid)
    assert rc2 == 0 and out2 == ""
    # a different class in the same session still fires
    rc3, out3, _ = _run(monkeypatch, capsys, "ya vault get x", session=sid)
    assert rc3 == 0 and "secrets" in out3


def test_silent_on_negative(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, "make all")
    assert rc == 0 and out == ""


def test_ignores_non_bash(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, "arc push", tool="Write")
    assert rc == 0 and out == ""
