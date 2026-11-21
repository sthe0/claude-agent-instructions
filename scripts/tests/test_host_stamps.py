"""Host, harness-version, model and root-session stamps on quality and spawn-cost rows."""
import importlib.util
import json
from pathlib import Path

import pytest

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


def _main_session(monkeypatch, tmp_path, harness, host=None):
    """Run agentctl's `main` as a root that sets only AGENTCTL_HARNESS_SESSION_ID, under a tmp
    agent home so its states and binding files never reach the developer's."""
    from agentctl import store
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(store, "DEFAULT_ROOT", tmp_path / "home" / "agentctl" / "state")
    for name in ("CLAUDE_CODE_SESSION_ID", "AGENTCTL_OWN_SESSION", "AGENTCTL_RUNTIME_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCTL_HARNESS_SESSION_ID", harness)


def _classify_args(engine, host):
    return ["classify", "--session", engine, "--changed-lines", "200", "--architectural", "--host", host]


def _engine_state(tmp_path, engine):
    from agentctl.store import FileStateStore
    return FileStateStore(tmp_path / "home" / "agentctl" / "state").load(engine)


def test_bind_appends_host_binds_per_root_session(tmp_path, monkeypatch):
    from agentctl import cli
    _main_session(monkeypatch, tmp_path, "ses_one")
    assert cli.main(["start", "--session", "eng-1", "--task", "t", "--host", "cursor"]) == 0
    cli.main(_classify_args("eng-1", "cursor"))
    for harness in ("ses_two", "ses_two", "ses_one"):
        monkeypatch.setenv("AGENTCTL_HARNESS_SESSION_ID", harness)
        cli.main(["plan", "--session", "eng-1"])

    state = _engine_state(tmp_path, "eng-1")
    assert [b["root_session_id"] for b in state.host_binds] == ["ses_one", "ses_two"]
    assert [b["host"] for b in state.host_binds] == ["cursor", "cursor"]
    with pytest.raises(runtime_host.HostConflictError):
        runtime_host.bind_runtime_host(state, "claude", root_session_id="ses_three")
    assert state.runtime_host == "cursor" and len(state.host_binds) == 2


def test_harness_session_id_writes_binding_without_claude_env(tmp_path, monkeypatch):
    from agentctl import cli
    _main_session(monkeypatch, tmp_path, "ses_t")

    assert cli.main(["start", "--session", "eng-t", "--task", "t", "--host", "cursor"]) == 0
    cli.main(_classify_args("eng-t", "cursor"))

    binding = tmp_path / "home" / "agentctl" / "engine-session-bindings" / "ses_t.json"
    assert [e["session"] for e in json.loads(binding.read_text(encoding="utf-8"))["driven"]] == ["eng-t"]
    assert [b["root_session_id"] for b in _engine_state(tmp_path, "eng-t").host_binds] == ["ses_t"]
    assert runtime_host.detect_host() != "claude"


def test_binding_store_save_without_harness_id_binds_nothing(tmp_path, monkeypatch):
    from agentctl import cli, cli_common
    _main_session(monkeypatch, tmp_path, "ses_x")
    cli.main(["start", "--session", "eng-n", "--task", "t", "--host", "cursor"])
    monkeypatch.delenv("AGENTCTL_HARNESS_SESSION_ID")
    state = _engine_state(tmp_path, "eng-n")
    before = list(state.host_binds)

    cli_common._BindingStore("", "eng-n").save(state)

    assert _engine_state(tmp_path, "eng-n").host_binds == before
    assert not (tmp_path / "home" / "agentctl" / "engine-session-bindings" / ".json").exists()


def test_a_state_bound_before_the_stamps_loads_with_a_leading_unknown_entry():
    from agentctl.state import HOST_BINDS_SCHEMA, SessionState
    state = SessionState(session_id="old", task_id="t", runtime_host="claude")
    data = state.to_dict()
    data["schema_version"] = HOST_BINDS_SCHEMA - 1
    data["host_binds"] = []

    loaded = SessionState.from_dict(data)
    assert [b["host"] for b in loaded.host_binds] == [None]
    assert loaded.schema_version == HOST_BINDS_SCHEMA

    data["schema_version"] = HOST_BINDS_SCHEMA
    assert SessionState.from_dict(data).host_binds == []


def test_a_bind_without_a_root_session_records_nothing():
    from agentctl.state import SessionState
    state = SessionState(session_id="s", task_id="t", runtime_host="claude")
    runtime_host.record_root_bind(state, None)
    assert state.host_binds == []


def test_rows_carry_task_key_and_root_session_id(store, venued_fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "root-7")
    monkeypatch.setattr(engine_session_binding, "transcript_for_session", lambda session: None)
    _drive_to_resolved(store, "eng-7", str(venued_fixtures_dir / "plan_two_stage.toml"))
    quality = _read_quality_rows(cli_final.TASK_QUALITY_LOG)[-1]
    assert (quality["session"], quality["root_session_id"]) == ("eng-7", "root-7")

    spawner = _spawner()
    monkeypatch.setattr(spawner, "COST_LOG", tmp_path / "spawn-costs.jsonl")
    monkeypatch.delenv("AGENTCTL_OWN_SESSION", raising=False)
    monkeypatch.setattr(spawner.host_stamp, "task_key", lambda environ=None: "eng-7")
    spawner.log_cost_entry({"event": "spawn", **spawner._spawner_session_field(None),
                            **spawner._spawn_host_stamp({"modelUsage": {"claude-x": {}}}, {"CLAUDE_CODE_SESSION_ID": "root-7"})})
    row = json.loads((tmp_path / "spawn-costs.jsonl").read_text(encoding="utf-8"))
    assert (row["spawner_session"], row["root_session_id"], row["host"], row["models"]) == (
        "eng-7", "root-7", "claude", ["claude-x"])


def test_nested_rows_carry_root_host(tmp_path, monkeypatch):
    spawner = _spawner()
    monkeypatch.setattr(spawner, "COST_LOG", tmp_path / "spawn-costs.jsonl")
    child_env = spawner.build_child_env(
        {"AGENTCTL_RUNTIME_HOST": "opencode", "AGENTCTL_HARNESS_SESSION_ID": "ses_root"}, 1, "ses_root")
    grandchild_env = spawner.build_child_env(
        {**child_env, "AGENTCTL_HARNESS_SESSION_ID": "ses_child", "CLAUDE_CODE_SESSION_ID": "claude-child"}, 2,
        "ses_child")

    spawner.log_cost_entry({"event": "spawn", **spawner._spawn_host_stamp({"modelUsage": {"m-z": {}}}, grandchild_env)})

    row = json.loads((tmp_path / "spawn-costs.jsonl").read_text(encoding="utf-8"))
    assert (row["host"], row["root_session_id"], row["models"]) == ("opencode", "ses_root", ["m-z"])


def test_harness_version_from_env_not_launch(store, venued_fixtures_dir, monkeypatch):
    import subprocess

    from lib import host_stamp

    def refuse(*args, **kwargs):
        raise AssertionError("the harness version must be read, not learned by launching a binary")

    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST_VERSION", "3.4.5")
    with monkeypatch.context() as launch_refused:
        launch_refused.setattr(subprocess, "Popen", refuse)
        launch_refused.setattr(subprocess, "run", refuse)
        assert host_stamp.bind_time_version() == "3.4.5"
        assert host_stamp.version_from_transcript(None) is None

    monkeypatch.setenv("AGENTCTL_RUNTIME_HOST_VERSION", "3.4.5")
    monkeypatch.setattr(engine_session_binding, "transcript_for_session", lambda session: None)
    _drive_to_resolved(store, "ver-1", str(venued_fixtures_dir / "plan_two_stage.toml"))
    assert store.load("ver-1").harness_version == "3.4.5"
    assert _read_quality_rows(cli_final.TASK_QUALITY_LOG)[-1]["harness_version"] == "3.4.5"


def test_old_rows_untouched_unstamped_unknown(store, venued_fixtures_dir, monkeypatch):
    old = json.dumps({"ts": "2026-10-01T00:00:00+00:00", "session": "old-1", "quality": 3}) + "\n"
    cli_final.TASK_QUALITY_LOG.write_text(old, encoding="utf-8")
    monkeypatch.setattr(engine_session_binding, "transcript_for_session", lambda session: None)

    _drive_to_resolved(store, "new-1", str(venued_fixtures_dir / "plan_two_stage.toml"))

    text = cli_final.TASK_QUALITY_LOG.read_text(encoding="utf-8")
    assert text.startswith(old) and len(text.splitlines()) == 2
    assert "host" not in json.loads(text.splitlines()[0])


def test_the_spawn_row_stamp_and_spawner_fallback(monkeypatch):
    spawner = _spawner()
    env = {"AGENTCTL_RUNTIME_HOST": "opencode", "AGENTCTL_RUNTIME_HOST_VERSION": "1.2.3",
           "AGENTCTL_HARNESS_SESSION_ID": "oc-9"}
    assert spawner._spawn_host_stamp({"modelUsage": {"m-x": {}, "m-y": {}}}, env) == {
        "host": "opencode", "harness_version": "1.2.3", "models": ["m-x", "m-y"], "root_session_id": "oc-9"}
    assert spawner._spawn_host_stamp(None, {})["host"] is None
    monkeypatch.delenv("AGENTCTL_OWN_SESSION", raising=False)
    monkeypatch.setattr(spawner.host_stamp, "task_key", lambda environ=None: "eng-1")
    assert spawner._spawner_session_field(None) == {"spawner_session": "eng-1"}
    assert spawner._spawner_session_field("given") == {"spawner_session": "given"}


def test_models_of_every_root_session_are_kept_in_order(tmp_path, monkeypatch):
    from lib import host_stamp
    first = _transcript(tmp_path, ["m-a", "m-b"])
    second = tmp_path / "root-2.jsonl"
    second.write_text(json.dumps({"type": "assistant", "message": {"model": "m-c"}}) + "\n", encoding="utf-8")
    monkeypatch.setattr(engine_session_binding, "transcript_for_session",
                        lambda session: {"root-1": first, "root-2": second}.get(session))

    assert host_stamp.models_from_transcript("root-1", "root-2", "eng") == ["m-a", "m-b", "m-c"]


def test_telemetry_fills_host_only_for_stamped_single_host_sessions(tmp_path, monkeypatch):
    from lib.telemetry import store as telemetry_store, sync
    from test_telemetry_sync_scale import _replayed
    state_dir = tmp_path / "home" / "agentctl" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    states = {
        "one-host": {"schema_version": 63, "host_binds": [{"host": "opencode"}, {"host": "opencode"}]},
        "two-hosts": {"schema_version": 63, "host_binds": [{"host": "claude"}, {"host": "opencode"}]},
        "prestamp": {"schema_version": 62, "runtime_host": "claude"},
        "null-bind": {"schema_version": 63, "host_binds": [{"host": None}, {"host": "claude"}]},
    }
    for name, body in states.items():
        (state_dir / f"{name}.json").write_text(json.dumps({"session_id": name, **body}), encoding="utf-8")
    for name in [*states, "no-state"]:
        _replayed(name)

    assert sync.materialize([*states, "no-state"])["committed"] is True

    conn = telemetry_store.open_record()
    try:
        hosts = dict(conn.execute("SELECT session_id, host FROM sessions"))
    finally:
        conn.close()
    assert hosts == {"one-host": "opencode", "two-hosts": None, "prestamp": None, "null-bind": None,
                     "no-state": None}


def test_the_telemetry_sessions_table_gains_a_nullable_host_column():
    from lib.telemetry import store as telemetry_store
    conn = telemetry_store.open_record()
    try:
        columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(sessions)")}
    finally:
        conn.close()
    assert "host" in columns and columns["host"] == 0
