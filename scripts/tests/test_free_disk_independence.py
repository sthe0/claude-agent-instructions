"""The floor checks of gate_speedup_control.py decide on an injected free-space value.

Every entry point that refuses below MIN_FREE_GB reads the value through ``_free_gb()``, so no
outcome in this file depends on the machine's real free disk — a shared quantity other
sessions move. Below the floor each entry point refuses with the floor message and its own
exit code; above it the run starts (a sentinel past the floor proves the check let it through).
"""
import subprocess

import pytest

import gate_speedup_control as control

BELOW = control.MIN_FREE_GB - 1.0
ABOVE = control.MIN_FREE_GB + 1.0
FLOOR = f"GB free on /; need {control.MIN_FREE_GB}"
PAST = "past the floor"


@pytest.fixture
def repo(tmp_path):
    """A real throwaway git repository: the checks that start past the floor reach git before
    anything else, and Experiment's setup touches the repository's store paths."""
    path = tmp_path / "repo"
    path.mkdir()
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "i"], check=True)
    return path


def refuse_below(repo, tmp_path, monkeypatch):
    """Every entry point, called below the floor: (name, exit code, call). The floor message and
    each function's own refusal code; nothing else runs."""
    monkeypatch.setattr(control, "_free_gb", lambda: BELOW)
    out = tmp_path / "out.json"
    return [
        ("concurrent-gates", 2, lambda: control.concurrent_gates(repo, 1, out)),
        ("sandbox-run", 2, lambda: control.sandbox_run(repo, out, [], None, 60)),
        ("verdict-scenario", control.EXIT_VERDICT_ERROR,
         lambda: control.verdict_scenario(repo, "additive", None, out)),
        ("disk-series", control.EXIT_SERIES_ENVIRONMENT,
         lambda: control.disk_series(repo, ["pass"], 2048, False, out)),
        ("shuffle-pair", control.EXIT_ERROR,
         lambda: control.shuffle_pair(repo, [1, 2], out, tmp_path / "exceptions.toml", 60)),
    ]


def test_below_the_floor_every_entry_point_refuses_with_the_floor_message(repo, tmp_path, monkeypatch, capsys):
    for name, code, call in refuse_below(repo, tmp_path, monkeypatch):
        assert call() == code, name
        assert FLOOR in capsys.readouterr().out, name


def test_below_the_floor_the_experiment_refuses_at_enter(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(control, "_free_gb", lambda: BELOW)
    with pytest.raises(SystemExit, match=FLOOR):
        control.Experiment(repo, "below-floor").__enter__()


def test_above_the_floor_the_run_starts(repo, tmp_path, monkeypatch):
    """Above the floor the entry points whose first act is a git call reach it: the sentinel
    proves the check let the run start. The experiment reaches its first commit."""
    monkeypatch.setattr(control, "_free_gb", lambda: ABOVE)

    def sentinel(*_args, **_kw):
        raise RuntimeError(PAST)

    monkeypatch.setattr(control, "_git", sentinel)
    monkeypatch.setattr(control, "_commit", sentinel)
    out = tmp_path / "out.json"
    with pytest.raises(RuntimeError, match=PAST):
        control.concurrent_gates(repo, 1, out)
    with pytest.raises(RuntimeError, match=PAST):
        control.sandbox_run(repo, out, [], None, 60)
    with pytest.raises(RuntimeError, match=PAST):
        control.shuffle_pair(repo, [1, 2], out, tmp_path / "exceptions.toml", 60)
    with pytest.raises(RuntimeError, match=PAST):
        control.Experiment(repo, "above-floor").__enter__()