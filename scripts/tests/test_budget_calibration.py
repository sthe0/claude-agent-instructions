"""P3: budget-calibration.py + hook-budget-calibration-due.py.

Grouping of realized spend by (kind x tier) and by task-type, the raise/lower
flag conditions, the spawn_count==0 exclusion contract from P2, --check mode, and
the throttled fail-open nudge hook.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = _load("budget_calibration", "budget-calibration.py")
hook = _load("hook_budget_calibration_due", "hook-budget-calibration-due.py")


# A minimal config.md with the tier knobs the analyzer calibrates against.
CONFIG_MD = """# Coordination constants

| Key | Value | Meaning |
|---|---|---|
| `budget-small-usd` | `1.00` | label |
| `budget-medium-usd` | `3.00` | label |
| `budget-large-usd` | `8.00` | label |
| `spawn-runaway-ceiling-usd` | `25.0` | backstop |
"""


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _spawn(kind, tier, cost, ts="2026-07-20T10:00:00+00:00", **extra):
    row = {"event": "spawn", "kind": kind, "budget_tier": tier, "cost_usd": cost, "ts": ts}
    row.update(extra)
    return row


def _quality(wc, dk, cost, spawn_count=1, ts="2026-07-20T10:00:00+00:00", **extra):
    row = {"ts": ts, "weight_class": wc, "deliverable_kind": dk,
           "total_cost_usd": cost, "spawn_count": spawn_count}
    row.update(extra)
    return row


def _setup(tmp_path, spawn_rows, quality_rows):
    spawn_log = tmp_path / "spawn.jsonl"
    quality_log = tmp_path / "quality.jsonl"
    config = tmp_path / "config.md"
    _write_jsonl(spawn_log, spawn_rows)
    _write_jsonl(quality_log, quality_rows)
    config.write_text(CONFIG_MD, encoding="utf-8")
    return spawn_log, quality_log, config


# ---------- grouping + percentiles ----------

def test_group_by_kind_tier_counts_and_stats():
    rows = [
        _spawn("developer", "medium", 1.0),
        _spawn("developer", "medium", 2.0),
        _spawn("developer", "medium", 3.0),
        _spawn("planner", "large", 5.0),
    ]
    g = bc.group_by_kind_tier(rows)
    assert sorted(g[("developer", "medium")]) == [1.0, 2.0, 3.0]
    assert g[("planner", "large")] == [5.0]
    st = bc._stats(g[("developer", "medium")])
    assert st["n"] == 3
    assert st["median"] == 2.0
    assert abs(st["p90"] - 2.8) < 1e-9  # linear interp between 2.0 and 3.0 at p90


def test_percentile_single_and_empty():
    assert bc._percentile([], 90) is None
    assert bc._percentile([4.2], 90) == 4.2


def test_kind_tier_skips_rows_without_cost():
    rows = [_spawn("developer", "medium", None), _spawn("developer", "medium", 2.0)]
    g = bc.group_by_kind_tier(rows)
    assert g[("developer", "medium")] == [2.0]


# ---------- task-type grouping + spawn_count==0 exclusion (P2 contract) ----------

def test_task_type_excludes_in_thread_rows():
    rows = [
        _quality("substantive", "code", 3.0, spawn_count=2),
        _quality("substantive", "code", 5.0, spawn_count=1),
        _quality("substantive", "code", None, spawn_count=0),   # in-thread: excluded
        _quality("small", "code", 0.0, spawn_count=0),          # in-thread: excluded even if 0.0
    ]
    g = bc.group_by_task_type(rows)
    assert sorted(g[("substantive", "code")]) == [3.0, 5.0]
    assert ("small", "code") not in g


# ---------- flags ----------

def test_flag_raises_when_p90_over_tier(tmp_path):
    _, _, config = _setup(tmp_path, [], [])
    thr = bc._load_thresholds(config)
    # medium tier = $3; three spawns whose p90 exceeds it.
    kt = {("developer", "medium"): [3.5, 4.0, 4.5]}
    flags = bc.calibration_flags(kt, thr, min_samples=3)
    assert any(f.startswith("RAISE medium") for f in flags)


def test_flag_lowers_when_median_far_below_tier(tmp_path):
    _, _, config = _setup(tmp_path, [], [])
    thr = bc._load_thresholds(config)
    # large tier = $8; median $0.5 < 0.3*8 = 2.4.
    kt = {("tech-writer", "large"): [0.4, 0.5, 0.6]}
    flags = bc.calibration_flags(kt, thr, min_samples=3)
    assert any(f.startswith("LOWER large") for f in flags)


def test_no_flag_below_min_samples(tmp_path):
    _, _, config = _setup(tmp_path, [], [])
    thr = bc._load_thresholds(config)
    kt = {("developer", "medium"): [9.0, 9.0]}  # over cap but only n=2
    assert bc.calibration_flags(kt, thr, min_samples=3) == []


def test_no_flag_for_unknown_tier(tmp_path):
    _, _, config = _setup(tmp_path, [], [])
    thr = bc._load_thresholds(config)
    kt = {("developer", "?"): [99.0, 99.0, 99.0]}  # no config knob for '?'
    assert bc.calibration_flags(kt, thr, min_samples=3) == []


# ---------- --check mode (end to end) ----------

def test_check_flags_and_exits_nonzero(tmp_path, capsys):
    spawn_log, quality_log, config = _setup(
        tmp_path,
        [_spawn("developer", "medium", 4.0), _spawn("developer", "medium", 4.5),
         _spawn("developer", "medium", 5.0)],
        [],
    )
    rc = bc.main(["--check", "--spawn-log", str(spawn_log),
                  "--quality-log", str(quality_log), "--config", str(config)])
    out = capsys.readouterr().out.strip()
    assert rc == 1
    assert out.startswith("RAISE medium")


def test_check_silent_when_calibrated(tmp_path, capsys):
    spawn_log, quality_log, config = _setup(
        tmp_path,
        [_spawn("developer", "medium", 1.5), _spawn("developer", "medium", 1.6),
         _spawn("developer", "medium", 1.7)],
        [],
    )
    rc = bc.main(["--check", "--spawn-log", str(spawn_log),
                  "--quality-log", str(quality_log), "--config", str(config)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == ""


def test_report_prints_both_groups(tmp_path, capsys):
    spawn_log, quality_log, config = _setup(
        tmp_path,
        [_spawn("developer", "medium", 2.0)],
        [_quality("substantive", "code", 4.0, spawn_count=1)],
    )
    rc = bc.main(["--all", "--spawn-log", str(spawn_log),
                  "--quality-log", str(quality_log), "--config", str(config)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "by (kind x tier)" in out
    assert "by task type" in out
    assert "developer" in out and "substantive" in out


# ---------- nudge hook ----------

def _run_hook(monkeypatch, stamp_path, check_line=None):
    monkeypatch.setattr(hook, "STAMP", stamp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    class _Proc:
        stdout = check_line or ""
    monkeypatch.setattr(hook, "run_check",
                        lambda: (check_line.strip() if check_line else None))
    return hook.main()


def test_hook_nudges_on_flag(tmp_path, monkeypatch, capsys):
    stamp = tmp_path / "stamp"
    rc = _run_hook(monkeypatch, stamp, check_line="RAISE medium: developer ...")
    err = capsys.readouterr().err
    assert rc == 0
    assert "miscalibrated" in err
    assert "self-improvement" in err
    assert stamp.exists()  # window recorded


def test_hook_silent_when_no_flag(tmp_path, monkeypatch, capsys):
    stamp = tmp_path / "stamp"
    rc = _run_hook(monkeypatch, stamp, check_line=None)
    err = capsys.readouterr().err
    assert rc == 0
    assert err.strip() == ""
    assert stamp.exists()  # clean check still resets the window


def test_hook_throttled_within_window(tmp_path, monkeypatch, capsys):
    import datetime as dt
    stamp = tmp_path / "stamp"
    stamp.write_text(dt.datetime.now().isoformat(), encoding="utf-8")
    called = {"n": 0}

    def _check():
        called["n"] += 1
        return "RAISE medium: x"
    monkeypatch.setattr(hook, "STAMP", stamp)
    monkeypatch.setattr(hook, "run_check", _check)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    rc = hook.main()
    assert rc == 0
    assert called["n"] == 0  # throttled: check never ran
    assert capsys.readouterr().err.strip() == ""


def test_hook_fail_open_on_bad_stdin(tmp_path, monkeypatch, capsys):
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(hook, "STAMP", stamp)
    monkeypatch.setattr(hook, "run_check", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = hook.main()  # must not raise
    assert rc == 0
