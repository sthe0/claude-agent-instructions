"""Host, harness-version, model and root-session stamps on quality and spawn-cost rows."""
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from agentctl import cli_final, runtime_host
from lib import engine_session_binding
from test_quality_row_task_type import _drive_to_resolved, _read_quality_rows

SCRIPTS = Path(__file__).resolve().parents[1]


def _transcript(tmp_path, models, version="2.1.0"):
    path = tmp_path / "root-1.jsonl"
    entries = [{"type": "user", "version": version}]
    entries += [{"type": "assistant", "message": {"model": m}} for m in models]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _spawner():
    spec = importlib.util.spec_from_file_location("spawn_specialist_stamps", SCRIPTS / "spawn-specialist.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_rows_carry_host_harness_models_in_order(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "root-1")
    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST_VERSION", "9.9.9")
    transcript = _transcript(tmp_path, ["m-a", "m-b", "m-a"])
    monkeypatch.setattr(engine_session_binding, "transcript_for_session", lambda session: transcript)

    _drive_to_resolved(store, "stamp-1", str(venued_fixtures_dir / "plan_two_stage.toml"))

    row = _read_quality_rows(cli_final.TASK_QUALITY_LOG)[-1]
    assert row.get("host") == "claude"
    assert row.get("harness_version") == "9.9.9"
    assert row.get("models") == ["m-a", "m-b"]
    assert row.get("root_session_id") == "root-1"
    assert row.get("hosts") == ["claude"]


def test_harness_version_falls_back_to_the_transcript(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUNTIME_HOST_VERSION", raising=False)
    transcript = _transcript(tmp_path, ["m-a"], version="2.7.1")
    monkeypatch.setattr(engine_session_binding, "transcript_for_session", lambda session: transcript)

    _drive_to_resolved(store, "stamp-2", str(venued_fixtures_dir / "plan_two_stage.toml"))

    assert _read_quality_rows(cli_final.TASK_QUALITY_LOG)[-1].get("harness_version") == "2.7.1"


def test_an_unstamped_session_leaves_the_unknowns_null(store, venued_fixtures_dir, monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUNTIME_HOST_VERSION", raising=False)
    monkeypatch.setattr(engine_session_binding, "transcript_for_session", lambda session: None)

    _drive_to_resolved(store, "stamp-3", str(venued_fixtures_dir / "plan_two_stage.toml"))

    row = _read_quality_rows(cli_final.TASK_QUALITY_LOG)[-1]
    assert row.get("harness_version") is None
    assert row.get("models") == []


def test_the_harness_session_id_is_read_from_the_agentctl_variable_when_claude_has_none(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_HARNESS_SESSION_ID", "oc-7")
    assert engine_session_binding.harness_session_id() == "oc-7"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-1")
    assert engine_session_binding.harness_session_id() == "cc-1"


def test_a_state_bound_before_stamps_gets_a_leading_unknown_entry(monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUNTIME_HOST_VERSION", raising=False)
    state = SimpleNamespace(runtime_host="claude", host_binds=[], harness_version=None)

    runtime_host.record_root_bind(state, "root-1")
    runtime_host.record_root_bind(state, "root-1")
    runtime_host.record_root_bind(state, "root-2")

    assert [b["root_session_id"] for b in state.host_binds] == [None, "root-1", "root-2"]
    assert [b["host"] for b in state.host_binds] == [None, "claude", "claude"]


def test_a_bind_without_a_root_session_records_nothing():
    state = SimpleNamespace(runtime_host="claude", host_binds=[], harness_version=None)
    runtime_host.record_root_bind(state, None)
    assert state.host_binds == []


def test_the_spawn_row_stamp_and_spawner_fallback(monkeypatch):
    spawner = _spawner()
    env = {"AGENTCTL_RUNTIME_HOST": "opencode", "AGENTCTL_RUNTIME_HOST_VERSION": "1.2.3",
           "AGENTCTL_HARNESS_SESSION_ID": "oc-9"}
    assert spawner._spawn_host_stamp("m-x", env) == {
        "host": "opencode", "harness_version": "1.2.3", "models": ["m-x"], "root_session_id": "oc-9"}
    assert spawner._spawn_host_stamp("m-x", {})["host"] is None
    monkeypatch.delenv("AGENTCTL_OWN_SESSION", raising=False)
    monkeypatch.setattr(spawner.host_stamp, "task_key", lambda environ=None: "eng-1")
    assert spawner._spawner_session_field(None) == {"spawner_session": "eng-1"}
    assert spawner._spawner_session_field("given") == {"spawner_session": "given"}


def test_the_telemetry_sessions_table_gains_a_nullable_host_column(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    migrations = sorted((SCRIPTS / "lib" / "telemetry" / "migrations").glob("*.sql"))
    assert migrations[-1].name == "011_session_host.sql"
    for path in migrations:
        if path.name.startswith(("001", "011")):
            conn.executescript(path.read_text(encoding="utf-8"))
    columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "host" in columns and columns["host"] == 0
