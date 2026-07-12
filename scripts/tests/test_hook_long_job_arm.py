"""Stage 5: long-job-arm advisory hook — fires on a long-job launch, silent
otherwise, never blocks, once per session."""
import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "hook_long_job_arm",
    SCRIPTS_DIR / "hook-long-job-arm.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

import long_job_detect  # noqa: E402


def test_detect_is_shared_object():
    """The advisory re-exports the SHARED detect() (identity, not a copy) so it can
    never drift from the turn-end guardian's launch scan — mirrors the
    timer_arm_detect.py no-drift `is`-identity pins."""
    assert mod.detect is long_job_detect.detect
    assert mod.NOHUP_RE is long_job_detect.NOHUP_RE
    assert mod.DEFAULT_ORCHESTRATORS is long_job_detect.DEFAULT_ORCHESTRATORS


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


# --- configurable orchestrator list ------------------------------------------

def test_default_orchestrator_list_unconfigured(tmp_path):
    """No long_job_orchestrators= key -> built-in Yandex default, behaviour unchanged."""
    idf = tmp_path / "agent-identity.local"
    idf.write_text("difficulty_channel=github\n")
    assert mod._orchestrator_names(idf) == mod.DEFAULT_ORCHESTRATORS
    # the default-built regex still matches a Yandex orchestrator launch
    assert mod.detect("nirvana workflow start --id abc")


def test_operator_override_replaces_list(tmp_path):
    """An operator-supplied list matches its own names, not the Yandex defaults."""
    idf = tmp_path / "agent-identity.local"
    idf.write_text("long_job_orchestrators=airflow, dagster prefect\n")
    names = mod._orchestrator_names(idf)
    assert names == ("airflow", "dagster", "prefect")
    tool_re = mod._build_tool_re(names)
    assert tool_re.search("airflow dags trigger my_dag")
    assert tool_re.search("dagster job launch")
    assert tool_re.search("nirvana workflow start") is None  # Yandex name no longer matched


def test_missing_identity_file_falls_back(tmp_path):
    """A non-existent identity file is fail-open to the default list."""
    assert mod._orchestrator_names(tmp_path / "nope.local") == mod.DEFAULT_ORCHESTRATORS


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
