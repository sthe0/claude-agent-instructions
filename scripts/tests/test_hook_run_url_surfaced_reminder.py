"""run-url-surfaced Stop hook: fires when a launched run/graph URL appeared in
tool output but never in an assistant text message; silent once surfaced.
Vendor-neutral fixtures — the hook matches by generic run/job path segment."""
import importlib.util
import io
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "hook_run_url_surfaced_reminder",
    Path(__file__).resolve().parents[1] / "hook-run-url-surfaced-reminder.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

_GRAPH = "https://orchestrator.example.com/flow/8389ecb2-abcd/c956ded9-1234/graph"
_BARE = "https://orchestrator.example.com/flow/8389ecb2-abcd/c956ded9-1234"


def _tool_result(text):
    return {"message": {"role": "user",
                        "content": [{"type": "tool_result", "content": text}]}}


def _assistant(text, ttype="text"):
    return {"message": {"role": "assistant",
                        "content": [{"type": ttype, "text": text}]}}


def _write(tmp_path, entries):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def _run(monkeypatch, capsys, transcript_path, stop_active=False):
    payload = {"transcript_path": str(transcript_path), "stop_hook_active": stop_active}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out


# --- detector unit ------------------------------------------------------------

def test_run_ids_matches_flow_graph():
    assert mod._run_ids(f"see {_GRAPH} now")


def test_run_ids_matches_ci_job():
    assert mod._run_ids("https://ci.example.com/builds/998877")


def test_run_ids_ignores_plain_url():
    assert mod._run_ids("https://docs.example.com/guide/intro") == {}


def test_identity_collapses_graph_suffix():
    # "/graph" (tool output) and the bare instance URL (surfaced) are one run.
    assert mod._identity(_GRAPH) == mod._identity(_BARE)


def test_analyze_flags_seen_but_not_surfaced():
    entries = [_tool_result(f"graph created at {_GRAPH}"),
               _assistant("Прогон запущен, идёт.")]
    assert mod.analyze(entries)


def test_analyze_silent_when_surfaced_even_as_bare_form():
    entries = [_tool_result(f"graph created at {_GRAPH}"),
               _assistant(f"Ссылка: {_BARE}")]
    assert mod.analyze(entries) == {}


def test_analyze_thinking_does_not_count_as_surfaced():
    entries = [_tool_result(f"graph {_GRAPH}"),
               _assistant(f"internal note {_GRAPH}", ttype="thinking")]
    assert mod.analyze(entries)


# --- hook behaviour -----------------------------------------------------------

def test_fires_and_is_advisory(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_tool_result(f"graph created at {_GRAPH}"),
                          _assistant("Прогон запущен.")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert "run-url-surfaced" in out


def test_silent_when_surfaced(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_tool_result(f"graph created at {_GRAPH}"),
                          _assistant(f"Ссылка на прогон: {_GRAPH}")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_silent_when_no_run_url(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_tool_result("just some logs, no url"),
                          _assistant("Готово.")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_stop_hook_active_guard(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_tool_result(f"graph created at {_GRAPH}"),
                          _assistant("Прогон запущен.")])
    rc, out = _run(monkeypatch, capsys, p, stop_active=True)
    assert rc == 0
    assert out == ""


def test_missing_transcript_is_silent(monkeypatch, capsys, tmp_path):
    rc, out = _run(monkeypatch, capsys, tmp_path / "does-not-exist.jsonl")
    assert rc == 0
    assert out == ""
