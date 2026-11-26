"""telemetry-bench.py judges CPU time: the hook emission's and each command's verdict follows the
CPU figure and ignores a wall-clock figure far above the bound, which is printed beside it. The
timing source is injected; no test starts 1000 interpreters."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load("telemetry-bench")


def _fake(monkeypatch, hook, command_on, command_off=(0.0, 0.0)):
    """hook_run returns `hook` and each command run `command_on` with recording on and
    `command_off` with it off, all as (CPU ms, wall ms)."""
    monkeypatch.setattr(bench, "hook_run", lambda env: hook)
    monkeypatch.setattr(bench, "command_run", lambda verb, env, root: (
        command_off if env.get(bench.OFF_ENV) == "off" else command_on))


def test_bench_judges_hook_cpu_time_not_wall_clock(monkeypatch):
    _fake(monkeypatch, hook=(15.0, 900.0), command_on=(1.0, 1.0))
    result = bench.bench(5, 20, 20.0, 250.0)
    assert result["ok"] and result["failures"] == []
    assert result["clock"] == "cpu"
    assert (result["hook_p95_ms"], result["hook_wall_p95_ms"]) == (15.0, 900.0)
    _fake(monkeypatch, hook=(25.0, 1.0), command_on=(1.0, 1.0))
    result = bench.bench(5, 20, 20.0, 250.0)
    assert not result["ok"]
    assert result["failures"] == ["hook emission p95 25.0 ms > 20.0 ms"]


def test_bench_judges_command_cpu_excess_not_wall_clock(monkeypatch):
    _fake(monkeypatch, hook=(1.0, 1.0), command_on=(110.0, 5000.0), command_off=(100.0, 10.0))
    result = bench.bench(1, 20, 20.0, 250.0)
    assert result["ok"]
    assert result["command_excess_p95_ms"] == {"status": 10.0, "classify --title bench": 10.0}
    assert result["command_excess_wall_p95_ms"] == {"status": 4990.0, "classify --title bench": 4990.0}
    _fake(monkeypatch, hook=(1.0, 1.0), command_on=(400.0, 10.0), command_off=(100.0, 10.0))
    result = bench.bench(1, 20, 20.0, 250.0)
    assert not result["ok"] and len(result["failures"]) == 2


def test_bench_default_hook_bound_is_20_ms_cpu(monkeypatch, capsys):
    _fake(monkeypatch, hook=(19.0, 500.0), command_on=(1.0, 1.0))
    assert bench.main(["--hook-runs", "1"]) == 0
    _fake(monkeypatch, hook=(21.0, 1.0), command_on=(1.0, 1.0))
    assert bench.main(["--hook-runs", "1"]) == 1
    assert '"clock": "cpu"' in capsys.readouterr().out


def test_hook_snippet_reports_cpu_then_wall(monkeypatch):
    class Done:
        stdout = "noise\n12.5 40.0\n"
    monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: Done())
    assert bench.hook_run({}) == (12.5, 40.0)
    Done.stdout = ""
    assert bench.hook_run({}) == (float("inf"), float("inf"))
    assert "process_time" in bench.EMIT_SNIPPET
