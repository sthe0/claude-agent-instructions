"""agent-report.py hooks: per-mechanism decision counts from the decision log."""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from lib.agent_report import hooks, run


def _row(mechanism, decision, *, ts="2026-10-20T10:00:00Z", test=False, layer="core", event="PreToolUse"):
    return {"ts": ts, "mechanism": mechanism, "event": event, "decision": decision,
            "session": "s", "test": test, "layer": layer}


def _log(tmp_path, rows):
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n")
    return path


def test_counts_per_mechanism_and_decision(tmp_path, capsys):
    log = _log(tmp_path, [_row("hook-a", "deny"), _row("hook-a", "silent"), _row("hook-a", "silent"),
                          _row("hook-b", "nudge", ts="2026-10-21T10:00:00Z")])
    assert run("hooks", ["--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "| `hook-a` | core | PreToolUse | 3 | 1 | 0 | 2 | 33.3 | 2026-10-20T10:00:00Z |" in out
    assert "| `hook-b` | core | PreToolUse | 1 | 0 | 1 | 0 | 0.0 | 2026-10-21T10:00:00Z |" in out


def test_test_rows_are_left_out_unless_asked_for(tmp_path, capsys):
    log = _log(tmp_path, [_row("hook-a", "deny"), _row("hook-t", "deny", test=True)])
    run("hooks", ["--log", str(log)])
    assert "hook-t" not in capsys.readouterr().out
    run("hooks", ["--log", str(log), "--include-test"])
    assert "hook-t" in capsys.readouterr().out


def test_since_drops_older_rows(tmp_path, capsys):
    log = _log(tmp_path, [_row("old", "deny", ts="2026-09-01T00:00:00Z"), _row("new", "deny")])
    run("hooks", ["--log", str(log), "--since", "2026-10-01"])
    out = capsys.readouterr().out
    assert "`new`" in out and "`old`" not in out


def test_malformed_lines_and_unknown_decisions_are_skipped(tmp_path):
    log = _log(tmp_path, ["not json", "[1]", _row("hook-a", "weird"), _row("hook-a", "deny")])
    table = hooks.summarize(hooks.read_rows(log), since=None, include_test=False)
    assert {k: v["deny"] for k, v in table.items()} == {("hook-a", "core", "PreToolUse"): 1}


def test_a_missing_log_exits_1(tmp_path, capsys):
    assert run("hooks", ["--log", str(tmp_path / "none.jsonl")]) == 1
    assert "No decision log" in capsys.readouterr().err


def test_the_view_is_listed_by_the_dispatcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_report_cli", SCRIPTS / "agent-report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["hooks", "--log", "/nonexistent/x.jsonl"]) == 1
