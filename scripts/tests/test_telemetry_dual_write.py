"""The telemetry record is an addition, never a precondition: with it switched off, locked,
read-only, full or failing, an engine task runs to its end with the outcomes of a task without
a record, and the file ledgers it already wrote are the same bytes, so everything that reads
them prints the same."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from agentctl import cli_common
from lib import config_root, hook_telemetry, judge_ledger
from lib.agent_report import run as run_report
from lib.telemetry import emit, live, store
from telemetry_live_support import (
    agentctl, events, make_resolved, no_background_sweep, query, spool_events, start)
from test_telemetry_identity import _run_spawn

SCRIPTS = Path(__file__).resolve().parent.parent
VOLATILE = {"ts", "time", "duration", "duration_s", "duration_ms", "invocation_id", "elapsed_s"}


@pytest.fixture(autouse=True)
def _live_env(monkeypatch):
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv(live.SWITCH_ENV, raising=False)
    no_background_sweep(monkeypatch)


def _task(root, session: str) -> "list[tuple[int, str]]":
    """A task from start to RESOLVED; the (exit code, node) of every verb is what the engine promised."""
    steps = [start(root, session, "task-a"), agentctl(root, "status", "--session", session)]
    make_resolved(root, session)
    steps.append(agentctl(root, "status", "--session", session))
    return [(d["rc"], d["node"]) for d in steps]


def _baseline(monkeypatch, tmp_path, session: str = "s1") -> "list[tuple[int, str]]":
    """What the same task yields with no record at all."""
    with monkeypatch.context() as scoped:
        scoped.setenv(live.SWITCH_ENV, "off")
        return _task(tmp_path / "baseline-state", session)


def test_off_switch_opens_neither_spool_nor_record(tmp_path, monkeypatch):
    monkeypatch.setenv(live.SWITCH_ENV, "off")
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(tmp_path / "judge.jsonl"))
    monkeypatch.setitem(judge_ledger._state, "source", "s1")

    outcomes = _task(tmp_path / "state", "s1")
    hook_telemetry.record("hook-x", "PreToolUse", "deny", "s1")
    judge_ledger.call("binary_ask", timed_out=False, duration=0.1, returncode=0, prompt_chars=4)
    live.emit_landing("start", "s1", {"branch": "b", "tip": "t"})
    store.replay("s1")

    assert outcomes[-1][1] == "RESOLVED" and outcomes[0][0] == 0
    assert not config_root.telemetry_spool_dir().exists()
    assert not config_root.telemetry_db().exists()
    assert not config_root.telemetry_db().parent.exists() or not any(
        config_root.telemetry_db().parent.glob("telemetry.sqlite*"))
    assert emit.drop_count("s1") == 0
    assert (tmp_path / "judge.jsonl").is_file() and config_root.decision_log().is_file()


def test_task_runs_to_end_with_record_locked_and_events_arrive_later_uncounted_as_lost(tmp_path, monkeypatch):
    expected = _baseline(monkeypatch, tmp_path)
    store.open_record().close()
    record = config_root.telemetry_db()
    holder = sqlite3.connect(str(record), timeout=0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(store, "DEADLINE_S", 0.2)
    try:
        began = time.monotonic()
        outcomes = _task(tmp_path / "state", "s1")
        elapsed = time.monotonic() - began
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert outcomes == expected
    assert elapsed < 60
    assert spool_events("s1") and emit.drop_count("s1") == 0
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)
    result = store.replay("s1")
    assert result["drained"] is True and result["failed"] is False
    assert len(events("s1", kind="command")) >= 3
    assert query("SELECT COUNT(*) FROM losses")[0][0] == 0
    assert emit.drop_count("s1") == 0


def test_task_runs_to_end_with_record_readonly(tmp_path, monkeypatch):
    expected = _baseline(monkeypatch, tmp_path)
    store.open_record().close()
    record = config_root.telemetry_db()
    record.chmod(0o444)
    try:
        assert not os.access(record, os.W_OK)
        outcomes = _task(tmp_path / "state", "s1")
        before = emit.spool_path("s1").read_bytes()
        refused = store.replay("s1")
    finally:
        record.chmod(0o600)

    assert outcomes == expected
    assert refused["failed"] is True and refused["inserted"] == 0 and refused["drained"] is False
    assert emit.spool_path("s1").read_bytes() == before and emit.drop_count("s1") == 0
    assert events("s1") == []
    healed = store.replay("s1")
    assert healed["drained"] is True and healed["failed"] is False
    assert len(events("s1", kind="command")) >= 3


def test_enospc_on_record_and_spool_counted_or_named_as_full_disk(tmp_path, monkeypatch):
    expected = _baseline(monkeypatch, tmp_path)

    monkeypatch.setenv(emit.FAULT_ENV, "enospc_record")
    full_record = _task(tmp_path / "record-state", "rec")
    result = store.replay("rec")
    assert full_record == expected
    assert result["failed"] is True and result["inserted"] == 0 and result["drained"] is False
    assert spool_events("rec") and emit.drop_count("rec") == 0
    monkeypatch.delenv(emit.FAULT_ENV)
    assert store.replay("rec")["drained"] is True and len(events("rec", kind="command")) >= 3

    monkeypatch.setenv(emit.FAULT_ENV, "enospc_spool")
    full_spool = _task(tmp_path / "spool-state", "spl")
    assert full_spool == expected
    assert not emit.spool_path("spl").exists()
    counted = emit.drop_count("spl")
    assert counted >= 4
    reasons = {json.loads(line)["reason"]
               for line in emit.drops_path("spl").read_text(encoding="utf-8").splitlines()}
    assert reasons == {"OSError"}

    monkeypatch.setenv(emit.FAULT_ENV, "enospc")
    unnamed = _task(tmp_path / "both-state", "both")
    assert unnamed == expected
    assert not emit.spool_path("both").exists() and emit.drop_count("both") == 0


def test_forced_emission_failure_raises_counted_loss(tmp_path, monkeypatch):
    expected = _baseline(monkeypatch, tmp_path)
    clean = _task(tmp_path / "clean-state", "clean")
    emitted = len(spool_events("clean"))
    assert clean == expected and emitted >= 4 and emit.drop_count("clean") == 0

    monkeypatch.setenv(emit.FAULT_ENV, "emit_fail")
    failing = _task(tmp_path / "failing-state", "failing")

    assert failing == expected
    assert spool_events("failing") == [] and not emit.spool_path("failing").exists()
    assert emit.drop_count("failing") == emitted
    assert store.replay("failing")["inserted"] == 0
    assert events("failing") == []


def _ledgers(base: Path) -> "dict[str, Path]":
    return {"gate": base / "gate-log.jsonl", "decision": base / "decision-log.jsonl",
            "judge": base / "judge-ledger.jsonl", "spawn-cost": base / "spawn-cost.jsonl"}


def _scrub(path: Path, base: Path) -> "list[dict]":
    rows = []
    for line in path.read_text(encoding="utf-8").replace(str(base), "<scenario>").splitlines():
        row = json.loads(line)
        rows.append({k: v for k, v in row.items() if k not in VOLATILE})
    return rows


def _consumer(run, base: Path) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run() == 0
    text = out.getvalue().replace(str(base), "<scenario>")
    text = re.sub(r"^  duration_ms:.*$", "  duration_ms: <timing>", text, flags=re.MULTILINE)
    return re.sub(r"\b\d+ B\b", "<size> B", text)


def _load_judge_report(monkeypatch):
    spec = importlib.util.spec_from_file_location("judge_usage_report", SCRIPTS / "judge-usage-report.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _scenario(monkeypatch, tmp_path: Path, tag: str, recording: bool) -> dict:
    """One fixed scenario writing all four file ledgers (gate log, decision log, judge ledger,
    spawn cost log), under its own state root, with the record on or off."""
    base = tmp_path / tag
    base.mkdir()
    ledgers = _ledgers(base)
    with monkeypatch.context() as scoped:
        scoped.setenv("AGENTCTL_GATE_LOG", str(ledgers["gate"]))
        scoped.setattr(cli_common, "GATE_LOG", ledgers["gate"])
        scoped.setenv("AGENTCTL_DECISION_LOG", str(ledgers["decision"]))
        scoped.setenv("AGENTCTL_JUDGE_LEDGER", str(ledgers["judge"]))
        scoped.setenv("AGENTCTL_TELEMETRY_DB", str(base / "record" / "telemetry.sqlite"))
        scoped.setenv("AGENTCTL_TELEMETRY_SPOOL", str(base / "record" / "spool"))
        if recording:
            scoped.delenv(live.SWITCH_ENV, raising=False)
        else:
            scoped.setenv(live.SWITCH_ENV, "off")
        scoped.setitem(judge_ledger._state, "source", "s1")
        root = base / "state"
        start(root, "s1", "task-a")
        agentctl(root, "classify", "--session", "s1", "--architectural", "--changed-lines", "900",
                 "--wall-clock-min", "90")
        agentctl(root, "status", "--session", "s1")
        agentctl(root, "resolve", "--session", "s1", "--by", "agent")
        hook_telemetry.record("hook-x", "PreToolUse", "deny", "s1")
        judge_ledger.call("binary_ask", timed_out=False, duration=0.1, returncode=0, prompt_chars=4)
        _run_spawn(scoped, base, "s1", "child-1", cost_log=ledgers["spawn-cost"])
        store.replay("s1")
        judge_report = _load_judge_report(scoped)
        printed = {
            "judge-usage-report": _consumer(
                lambda: judge_report.main(["--ledger", str(ledgers["judge"])]), base),
            "cost-report": _consumer(
                lambda: run_report("cost", ["--log", str(ledgers["spawn-cost"]), "--days", "3650"]), base),
        }
    return {"ledgers": {name: _scrub(path, base) for name, path in ledgers.items()}, "printed": printed,
            "recorded": (base / "record").exists()}


def test_file_ledgers_identical_with_record_enabled_and_disabled(tmp_path, monkeypatch):
    recording = _scenario(monkeypatch, tmp_path, "on", True)
    silent = _scenario(monkeypatch, tmp_path, "off", False)

    assert recording["recorded"] is True and silent["recorded"] is False
    assert {name: len(rows) for name, rows in recording["ledgers"].items()}.keys() == {
        "gate", "decision", "judge", "spawn-cost"}
    assert all(len(rows) >= 1 for rows in recording["ledgers"].values())
    assert recording["ledgers"] == silent["ledgers"]


def test_listed_consumers_unchanged_output_on_both_ledger_sets(tmp_path, monkeypatch):
    recording = _scenario(monkeypatch, tmp_path, "on", True)
    silent = _scenario(monkeypatch, tmp_path, "off", False)

    assert set(recording["printed"]) == {"judge-usage-report", "cost-report"}
    assert all(text.strip() for text in recording["printed"].values())
    assert "binary_ask" in recording["printed"]["judge-usage-report"]
    assert recording["printed"] == silent["printed"]
