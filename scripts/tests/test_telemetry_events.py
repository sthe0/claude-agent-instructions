"""What the live emitters of the telemetry record write: one event per command, transition, gate,
hook decision, judge call, check, landing step and pre-commit run, each under its registry source
key, with provenance live and (for the ledger-mirroring ones) the back-reference of its ledger line."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_land_branch as lands
from agentctl import advisor, cli_common
from agentctl.directive import Directive
from agentctl.state import SessionState
from lib import config_root, hook_telemetry, judge_ledger
from lib.telemetry import emit, live, recorder, store, sync
from telemetry_live_support import (
    agentctl, db, events, no_background_sweep, open_epoch, query, spool_events, start)

SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent


@pytest.fixture(autouse=True)
def _live_env(monkeypatch):
    """A cold bootstrap on a loaded machine can outlast the production 250 ms; a test's
    verdict must not depend on machine load."""
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv(live.SWITCH_ENV, raising=False)
    no_background_sweep(monkeypatch)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "state"


def _ledger_ids(path: Path) -> "dict[str, bytes]":
    """The imported-row id of every line of a file ledger, as stage 2's identity table names it."""
    ids, offset = {}, 0
    for line in path.read_bytes().splitlines(keepends=True):
        ids[store.imported_row_id(str(path), offset, hashlib.sha256(line).hexdigest())] = line
        offset += len(line)
    return ids


def _flow(root, session: str = "s1") -> "list[dict]":
    """start, classify, status and a refused resolve: every kind of act the wrapper records."""
    directives = [
        start(root, session, "task-a"),
        agentctl(root, "classify", "--session", session, "--architectural", "--changed-lines", "900",
                 "--wall-clock-min", "90"),
        agentctl(root, "status", "--session", session),
        agentctl(root, "resolve", "--session", session, "--by", "agent"),
    ]
    store.replay(session)
    return directives


def test_spool_named_by_session_id_under_the_spool_dir(root):
    start(root, "s1", "task-a")

    spool = config_root.telemetry_spool_dir() / "s1.jsonl"
    assert emit.spool_path("s1") == spool and spool.is_file()
    assert live.end_marker_path("s1") == config_root.telemetry_spool_dir() / "s1.end"
    assert live.epoch_path("s1") == config_root.telemetry_spool_dir() / "s1.epoch"
    lines = spool_events("s1")
    assert lines and {line["session_id"] for line in lines} == {"s1"}
    assert [p.name for p in config_root.telemetry_spool_dir().glob("*.jsonl")] == ["s1.jsonl"]
    store.replay("s1")
    assert {e["session_id"] for e in events()} == {"s1"}


def test_live_events_definition_counts_spool_and_end_marker(root):
    assert live.spool_or_end_marker_exists("none") is False
    live.emit_event("with-spool", "note", "telemetry:test", {})
    live.append_line(live.end_marker_path("marker-only"), b"1\n")
    live.append_line(live.epoch_path("sidecar-only"), b"{}")

    assert live.spool_or_end_marker_exists("with-spool") is True
    assert live.spool_or_end_marker_exists("marker-only") is True
    assert live.spool_or_end_marker_exists("sidecar-only") is False
    store.replay("with-spool")
    rows = query("SELECT COUNT(*) FROM events WHERE session_id = 'with-spool' AND provenance = 'live'")
    assert rows[0][0] == 1
    assert query("SELECT COUNT(*) FROM events WHERE session_id = 'marker-only'")[0][0] == 0


def test_every_emitted_event_has_live_provenance(root, tmp_path, monkeypatch):
    _flow(root)
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(tmp_path / "judge.jsonl"))
    monkeypatch.setitem(judge_ledger._state, "source", "s1")
    judge_ledger.call("binary_ask", timed_out=False, duration=0.1, returncode=0, prompt_chars=4)
    hook_telemetry.record("hook-x", "PreToolUse", "silent", "s1")
    live.emit_landing("start", "s1", {"branch": "b", "tip": "t"})
    sync.run()

    assert {line["provenance"] for line in spool_events("s1")} == {"live"}
    rows = events()
    assert {e["kind"] for e in rows} >= {
        "session", "task", "command", "transition", "gate", "decision", "recorder", "judge", "landing"}
    assert {e["provenance"] for e in rows} == {"live"}


def test_every_command_transition_gate_and_hook_decision_has_one_event_row(root):
    directives = _flow(root)

    commands = events("s1", kind="command")
    assert [(c["fields"]["command"], c["fields"]["outcome"]) for c in commands] == [
        ("start", "ok"), ("classify", "ok"), ("status", "ok"), ("resolve", "refused")]
    assert [c["fields"]["node_after"] for c in commands] == [d["node"] for d in directives]
    moved = [(c["fields"]["command"], c["fields"]["node_before"], c["fields"]["node_after"]) for c in commands
             if c["fields"]["node_before"] is not None and c["fields"]["node_before"] != c["fields"]["node_after"]]
    assert moved
    transitions = events("s1", kind="transition")
    assert [(t["fields"]["command"], t["fields"]["from"], t["fields"]["to"]) for t in transitions] == moved

    gate_ids = _ledger_ids(cli_common.GATE_LOG)
    gate_events = events("s1", kind="gate")
    assert len(gate_ids) >= 1 and len(gate_events) == len(gate_ids)
    assert sorted(e["backref_id"] for e in gate_events) == sorted(gate_ids)
    decision_ids = _ledger_ids(config_root.decision_log())
    decision_events = events("s1", kind="decision")
    assert len(decision_ids) >= 1 and len(decision_events) == len(decision_ids)
    assert sorted(e["backref_id"] for e in decision_events) == sorted(decision_ids)
    assert {e["source_key"] for e in decision_events} == {"hook:engine:resolution"}
    assert {e["source_key"] for e in gate_events} == {"gate:resolution"}
    assert {e["source_key"] for e in commands} == {"cli:start", "cli:classify", "cli:status", "cli:resolve"}


def test_decision_row_holds_back_reference_and_no_payload_column(tmp_path):
    hook_telemetry.record("hook-x", "PreToolUse", "deny", "s-dec")
    store.replay("s-dec")

    path = config_root.decision_log()
    (line_id, line), = _ledger_ids(path).items()
    (event,) = events("s-dec", kind="decision")
    assert event["backref_id"] == line_id
    row = json.loads(line)
    assert event["fields"] == {
        "mechanism": "hook-x", "event": "PreToolUse", "decision": "deny", "layer": "core",
        "test": row["test"], "source_key": "hook:hook-x"}
    assert "ts" not in event["fields"] and "session" not in event["fields"]
    conn = db()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    finally:
        conn.close()
    assert "payload" not in columns and columns >= {"event_id", "backref_id", "fields", "source_key"}
    assert event["fields"] != row and event["source_key"] == "hook:hook-x"


def test_gate_event_holds_r2_fields_and_gate_log_offset(root):
    _flow(root)

    lines = cli_common.GATE_LOG.read_bytes().splitlines(keepends=True)
    rows = [json.loads(line) for line in lines]
    gate_events = events("s1", kind="gate")
    assert len(gate_events) == len(rows)
    by_ref = {e["backref_id"]: e for e in gate_events}
    offset = 0
    for line, row in zip(lines, rows):
        event = by_ref[store.imported_row_id(str(cli_common.GATE_LOG), offset, hashlib.sha256(line).hexdigest())]
        offset += len(line)
        assert event["source_key"] == f"gate:{row['gate']}"
        assert {k: v for k, v in event["fields"].items() if k not in ("source_key", "task_epoch")} == {
            "session": "s1", "verb": event["fields"]["verb"], "gate": row["gate"],
            "outcome": "pass" if row["passed"] else "refuse", "time": row["ts"]}
    refused = [e for e in gate_events if e["fields"]["outcome"] == "refuse"]
    assert refused and {e["fields"]["verb"] for e in refused} == {"resolve"}
    assert all(e["fields"]["verb"] for e in gate_events)


def _ledger_calls(path: Path) -> "list[tuple[str, dict]]":
    rows = [(row_id, json.loads(line)) for row_id, line in _ledger_ids(path).items()]
    return [(row_id, row) for row_id, row in rows if row["kind"] == "call"]


def test_one_judge_event_per_judge_call_under_its_source_key(tmp_path, monkeypatch):
    ledger = tmp_path / "judge-ledger.jsonl"
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(ledger))
    monkeypatch.setitem(judge_ledger._state, "source", "judge-sess")
    for name in ("binary_ask", "same_difficulty", None):
        if name:
            judge_ledger.begin_attributed_call(name)
        advisor.subprocess_runner(["true"], timeout=20)
    store.replay("judge-sess")

    judge_events = events("judge-sess", kind="judge")
    calls = _ledger_calls(ledger)
    assert len(judge_events) == 3 == len(calls)
    assert [e["source_key"] for e in judge_events] == [
        "judge:binary_ask", "judge:same_difficulty", "judge:unattributed"]
    assert all(e["source_key"].split(":", 1)[1] in judge_ledger.JUDGE_NAMES for e in judge_events)
    assert [e["backref_id"] for e in judge_events] == [i for i, _ in calls]
    for event, (_, row) in zip(judge_events, calls):
        assert event["fields"]["judge"] == row["judge"] and event["fields"]["invocation_id"] == row["invocation_id"]
        assert event["fields"]["timed_out"] is False and event["fields"]["returncode"] == 0


def _constants(tree: ast.Module) -> "dict[str, str]":
    return {node.targets[0].id: node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}


def _enclosing_function(tree: ast.Module, call: ast.Call):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                inner is call for inner in ast.walk(node)):
            return node
    return None


def _judge_names_passed(path: Path) -> "set[str]":
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _constants(tree)
    claims = ("set_current_judge", "begin_attributed_call")
    found: "set[str]" = set()

    def resolve(node, function) -> "set[str]":
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if not isinstance(node, ast.Name):
            return set()
        if node.id in constants:
            return {constants[node.id]}
        params = [a.arg for a in function.args.args] if function is not None else []
        if node.id not in params:
            return set()
        position = params.index(node.id)
        names: "set[str]" = set()
        for site in ast.walk(tree):
            if isinstance(site, ast.Call) and isinstance(site.func, ast.Name) and site.func.id == function.name:
                argument = site.args[position] if position < len(site.args) else next(
                    (k.value for k in site.keywords if k.arg == node.id), None)
                if argument is not None:
                    names |= resolve(argument, _enclosing_function(tree, site))
        return names

    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in claims
                and call.args):
            found |= resolve(call.args[0], _enclosing_function(tree, call))
    return found


def test_judge_names_cover_every_set_current_judge_call():
    passed: "set[str]" = set()
    sources = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        if "tests" in path.relative_to(SCRIPTS).parts or path.name == "judge_ledger.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "set_current_judge" in text or "begin_attributed_call" in text:
            sources.append(path.name)
            passed |= _judge_names_passed(path)

    assert sources == ["advisor.py", "grounding_judge.py"]
    assert len(passed) >= 18
    assert passed | {"unattributed"} == set(judge_ledger.JUDGE_NAMES)


class _MemoryStore:
    def __init__(self, state):
        self.state = state

    def load(self, session):
        return self.state


def test_check_events_carry_plan_digest_and_code_ref():
    state = SessionState(session_id="s-check", task_id="task-a")
    state.accepted_plan_digest = "digest-1"
    state.stages = [
        SimpleNamespace(index=1, outcome=SimpleNamespace(delivered_head="head-one")),
        SimpleNamespace(index=2, outcome=SimpleNamespace(delivered_head="head-two")),
        SimpleNamespace(index=3, outcome=None),
    ]
    state.log("verify_final_check", check="stage:1", venue="delivery", decision="pass", reason="", duration_s=1.5, exit=0)
    state.log("verify_final_run")
    state.log("verify_final_check", check="stage:2", venue="delivery", decision="pass", reason="", duration_s=2.5, exit=0)
    state.log("verify_final_check", check="final:suite", venue="landing", decision="fail", reason="red", duration_s=3.5, exit=1)
    state.log("verify_final_run")

    memory = _MemoryStore(state)
    ctx = recorder.begin("verify-final", "s-check", memory)
    recorder.finish(ctx, SimpleNamespace(), memory, Directive(True, "VERIFYING", "verify_final"))
    store.replay("s-check")

    checks = events("s-check", kind="check")
    assert [(c["fields"]["check"], c["fields"]["code_ref"]) for c in checks] == [
        ("stage:2", "head-two"), ("final:suite", "head-two")]
    assert {c["fields"]["plan_digest"] for c in checks} == {"digest-1"}
    assert [c["source_key"] for c in checks] == ["cli:verify-final"] * 2
    assert checks[1]["fields"]["decision"] == "fail" and checks[1]["fields"]["exit"] == 1
    assert checks[0]["fields"]["venue"] == "delivery" and checks[1]["fields"]["duration_s"] == 3.5


def test_fulltext_query_over_plan_text_returns_the_task_plan(root, tmp_path, monkeypatch):
    from agentctl.store import FileStateStore
    start(root, "s1", "task-a")
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\nreconcile the zanzibarwidget ledger\n", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text("# Plan\nnothing of interest here\n", encoding="utf-8")
    start(root, "s2", "task-b")
    file_store = FileStateStore(root)
    for session, path, rendering in (("s1", plan, "essence mentions quuxmarker"), ("s2", other, None)):
        state = file_store.load(session)
        state.plan_path = str(path)
        if rendering:
            state.plan_presentations = [{"kind": "essence", "rendering_text": rendering}]
        file_store.save(state)
    monkeypatch.setattr(config_root, "resolve_agentctl_state_file", lambda session: file_store.path(session))

    sync.run()

    conn = db()
    try:
        if store.fts5_available(conn):
            ids = [r[0] for r in conn.execute(
                "SELECT rowid FROM artifacts_fts WHERE artifacts_fts MATCH 'zanzibarwidget'")]
            found = conn.execute(
                f"SELECT kind, task_id FROM artifacts WHERE artifact_id IN ({','.join('?' * len(ids))})", ids).fetchall()
        else:
            found = conn.execute("SELECT kind, task_id FROM artifacts WHERE text LIKE '%zanzibarwidget%'").fetchall()
        renderings = conn.execute(
            "SELECT kind, task_id FROM artifacts WHERE text LIKE '%quuxmarker%'").fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in found] == [("plan", "task-a")]
    assert [tuple(r) for r in renderings] == [("essence_rendering", "task-a")]


def _land(tmp_path, *extra):
    origin, clone = lands.make_bare_and_clone(tmp_path)
    old_tip = lands.git("rev-parse", "refs/heads/main", cwd=origin).stdout.strip()
    tip = lands.make_feature_branch(clone)
    lands.git("push", "--quiet", "-u", "origin", "feature", cwd=clone)
    return clone, old_tip, tip


def test_landing_events_carry_branch_and_tip(tmp_path):
    clone, old_tip, tip = _land(tmp_path)
    env = lands._isolated_home(tmp_path)
    lands._seed_landed_session(Path(env["CLAUDE_CONFIG_DIR"]), "land-sid", clone)

    result = lands.run_cli("--branch", "feature", "--trunk", "main", "--keep-branch", "--session", "land-sid",
                           "--stage", "1", cwd=clone, env=env)
    assert result.returncode == 0, result.stderr
    store.replay("land-sid")

    landing = events("land-sid", kind="landing")
    assert [e["source_key"] for e in landing] == ["land:start", "land:suite_gate", "land:attempt", "land:landed"]
    assert {(e["fields"]["branch"], e["fields"]["tip"]) for e in landing} == {("feature", tip)}
    assert landing[0]["fields"]["trunk"] == "main"
    landed = landing[-1]["fields"]
    assert (landed["pushed_head"], landed["fetched_base"]) == (tip, old_tip)
    assert landing[1]["fields"]["outcome"] == "passed" and landing[2]["fields"]["outcome"] == "pushed"
    assert all(e["fields"].get("duration_s") is not None for e in landing[1:])


def test_land_branch_and_pre_commit_emit_landing_events(tmp_path):
    clone, _, tip = _land(tmp_path)
    result = lands.run_cli("--branch", "feature", "--trunk", "main", cwd=clone,
                           env=lands._isolated_home(tmp_path))
    assert result.returncode == 0, result.stderr
    store.replay(live.LANDING_SESSION)
    landing = events(live.LANDING_SESSION, kind="landing")
    assert [e["source_key"] for e in landing] == ["land:start", "land:suite_gate", "land:attempt", "land:landed"]
    assert {e["fields"]["tip"] for e in landing} == {tip}

    hook = (REPO / "githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "telemetry.py\" now" in hook and "telemetry.py\" emit-precommit --started" in hook
    began = time.time() - 3
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "telemetry.py"), "emit-precommit", "--started", f"{began:.3f}", "--exit", "0"],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    store.replay(live.PRECOMMIT_SESSION)
    (precommit,) = events(live.PRECOMMIT_SESSION, kind="precommit")
    assert precommit["source_key"] == "precommit" and precommit["provenance"] == "live"
    assert precommit["fields"]["exit"] == 0 and precommit["fields"]["duration_s"] >= 3


def test_event_carries_recorder_time_on_the_command_path(root):
    start(root, "s1", "task-a")
    agentctl(root, "status", "--session", "s1")
    store.replay("s1")

    first, second = events("s1", kind="command")
    assert first["fields"]["command"] == "start"
    assert 0 < first["fields"]["recorder_s"] <= first["fields"]["duration_s"]
    assert 0 <= second["fields"]["recorder_s"] <= second["fields"]["duration_s"]
    assert first["fields"]["task_epoch"] == open_epoch("s1")


def test_replay_emits_one_event_under_telemetry_recorder(root):
    start(root, "s1", "task-a")
    agentctl(root, "status", "--session", "s1")
    store.replay("s1")

    recorded = events("s1", kind="recorder")
    assert len(recorded) == 2 == len(events("s1", kind="command"))
    assert {e["source_key"] for e in recorded} == {"telemetry:recorder"}
    assert {e["session_id"] for e in recorded} == {"s1"}
    for event in recorded:
        assert set(event["fields"]) >= {"replay_s", "inserted", "deferred", "drained", "failed"}
        assert event["fields"]["replay_s"] >= 0 and event["fields"]["drained"] is True
    assert recorded[0]["fields"]["inserted"] >= 1

    result = recorder.replay_on_command_path("s1")
    store.replay("s1")
    assert result["drained"] is True and len(events("s1", kind="recorder")) == 3
