"""agent-report.py host-comparison: figures per host group and weight from the ledgers and states."""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "host_comparison"
NOW = "2026-11-20T00:00:00Z"
CONFIG = "| `telemetry-abandon-window-days` | `{days}` | window |\n"


def _report(tmp_path, *extra, quality=FIXTURES / "quality.jsonl", spawns=FIXTURES / "spawns.jsonl",
            states=None, days=7):
    state_dir = tmp_path / "states"
    state_dir.mkdir(exist_ok=True)
    for name, body in (states or {}).items():
        (state_dir / f"{name}.json").write_text(json.dumps({"session_id": name, **body}), encoding="utf-8")
    config = tmp_path / "config.md"
    config.write_text(CONFIG.format(days=days), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "agent-report.py"), "host-comparison", "--json",
         "--quality-log", str(quality), "--spawn-log", str(spawns), "--state-dir", str(state_dir),
         "--config", str(config), "--now", NOW, *extra],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _ledger(tmp_path, rows, name="quality.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _row(session, host, hosts=None, ts="2026-11-10T10:00:00+00:00", weight="SUBSTANTIVE", **extra):
    return {"ts": ts, "session": session, "weight_class": weight, "quality": 4, "total_cost_usd": 1.0,
            "host": host, "hosts": hosts if hosts is not None else [host], **extra}


def _figures(report, group, weight):
    return report["groups"][group]["by_weight"][weight]


def test_eight_figures_per_host(tmp_path):
    f = _figures(_report(tmp_path), "claude", "SUBSTANTIVE")
    assert f["share_resolved"] == 1.0
    assert (f["mean_cost_usd"], f["total_cost_usd"]) == (2.0, 2.0)
    assert f["mean_quality"] == 4.0
    assert f["mean_replans"] == 0.5
    assert f["mean_review_rounds"] == 1.0
    assert f["mean_difficulties"] == 1.0
    assert f["mean_effort_fires"] == 0.5
    assert f["mean_active_minutes"] == 15.0
    assert (f["quality_tasks"], f["replans_tasks"], f["active_time_tasks"]) == (2, 2, 2)


def test_figures_split_by_weight_with_task_counts(tmp_path):
    report = _report(tmp_path)
    claude = report["groups"]["claude"]
    assert claude["tasks"] == 3
    assert (_figures(report, "claude", "SUBSTANTIVE")["tasks"], _figures(report, "claude", "SMALL_CHANGE")["tasks"]) == (2, 1)
    assert _figures(report, "opencode", "SUBSTANTIVE")["tasks"] == 1
    assert _figures(report, "opencode", "SUBSTANTIVE")["total_cost_usd"] == 1.0


def test_unknown_cost_null_not_zero(tmp_path):
    report = _report(tmp_path)
    small = _figures(report, "claude", "SMALL_CHANGE")
    assert small["total_cost_usd"] is None and small["mean_cost_usd"] is None
    assert (small["cost_tasks"], small["cost_unknown_tasks"]) == (0, 1)
    heavy = _figures(report, "claude", "SUBSTANTIVE")
    assert (heavy["cost_tasks"], heavy["cost_unknown_tasks"]) == (1, 1)
    assert heavy["total_cost_usd"] == 2.0
    assert report["spawns"]["claude"] == {"spawns": 2, "cost_known_spawns": 1, "total_cost_usd": 0.5}


def test_models_per_host(tmp_path):
    groups = _report(tmp_path)["groups"]
    assert sorted(groups["claude"]["models"]) == ["m-a", "m-c"]
    assert groups["opencode"]["models"] == ["m-b"]


def test_cursor_rows_in_neither_group(tmp_path):
    groups = _report(tmp_path)["groups"]
    assert groups["cursor"]["tasks"] == 1
    assert groups["claude"]["tasks"] == 3 and groups["opencode"]["tasks"] == 1


def test_mixed_and_unknown_groups(tmp_path):
    report = _report(tmp_path)
    assert report["groups"]["mixed"]["tasks"] == 1
    assert report["groups"]["unknown"]["tasks"] == 2
    assert report["spawns"]["mixed"]["spawns"] == 2
    assert report["spawns"]["unknown"] == {"spawns": 1, "cost_known_spawns": 1, "total_cost_usd": 3.0}
    assert "refused" not in json.dumps(report)


def test_mixed_joined_by_root_session(tmp_path):
    quality = _ledger(tmp_path, [_row("t-1", "claude", ts="2026-11-10T10:00:00+00:00"),
                                 _row("t-1", "opencode", hosts=["opencode"], ts="2026-11-11T10:00:00+00:00")])
    states = {"t-1": {"schema_version": 63, "host_binds": [{"host": "claude"}, {"host": "opencode"}]}}
    report = _report(tmp_path, quality=quality, spawns=_ledger(tmp_path, [], "s.jsonl"), states=states)
    assert list(report["groups"]) == ["mixed"]
    assert report["groups"]["mixed"]["tasks"] == 1


def test_prestamp_state_continued_same_host_unknown(tmp_path):
    quality = _ledger(tmp_path, [_row("t-2", "claude")])
    states = {"t-2": {"schema_version": 62, "runtime_host": "claude"}}
    report = _report(tmp_path, quality=quality, spawns=_ledger(tmp_path, [], "s.jsonl"), states=states)
    assert list(report["groups"]) == ["unknown"]


def test_prestamp_state_continued_other_host_unknown(tmp_path):
    quality = _ledger(tmp_path, [_row("t-3", "opencode")])
    states = {"t-3": {"schema_version": 62, "runtime_host": "claude"}}
    report = _report(tmp_path, quality=quality, spawns=_ledger(tmp_path, [], "s.jsonl"), states=states)
    assert list(report["groups"]) == ["unknown"]


def test_stamped_rows_without_host_binds_grouped_by_rows(tmp_path):
    quality = _ledger(tmp_path, [_row("t-4", "opencode")])
    report = _report(tmp_path, quality=quality, spawns=_ledger(tmp_path, [], "s.jsonl"))
    assert list(report["groups"]) == ["opencode"]


def test_prestamp_unknown_after_state_pruned(tmp_path):
    quality = _ledger(tmp_path, [{"ts": "2026-11-10T10:00:00+00:00", "session": "t-5",
                                  "weight_class": "SUBSTANTIVE", "quality": 4, "total_cost_usd": 1.0}])
    report = _report(tmp_path, quality=quality, spawns=_ledger(tmp_path, [], "s.jsonl"))
    assert list(report["groups"]) == ["unknown"]


def test_since_filter(tmp_path):
    report = _report(tmp_path, "--since", "2026-11-05")
    assert sum(g["tasks"] for g in report["groups"].values()) == 4
    assert "claude" not in report["groups"]
    assert _report(tmp_path, "--since", "2026-11-01") == _report(tmp_path, "--since", "2026-11-01T00:00:00Z")
    assert report["spawns"]["opencode"]["spawns"] == 1 and "claude" not in report["spawns"]


def _abandoned_state(weight="SUBSTANTIVE"):
    return {"schema_version": 63, "weight_class": weight, "host_binds": [{"host": "claude"}], "history": []}


def test_abandonment_window_from_config(tmp_path):
    states = {"t-6": _abandoned_state()}
    old = 1763000000  # 2025-11-13, well before NOW
    (tmp_path / "states").mkdir()
    (tmp_path / "states" / "t-6.json").write_text(json.dumps({"session_id": "t-6", **states["t-6"]}), encoding="utf-8")
    os.utime(tmp_path / "states" / "t-6.json", (old, old))
    empty = _ledger(tmp_path, [], "none.jsonl")
    short = _report(tmp_path, quality=empty, spawns=empty, days=7)
    long = _report(tmp_path, quality=empty, spawns=empty, days=100000)
    assert _figures(short, "claude", "SUBSTANTIVE")["abandoned_tasks"] == 1
    assert _figures(short, "claude", "SUBSTANTIVE")["share_resolved"] == 0.0
    assert long["groups"] == {}


def test_a_malformed_since_exits_2():
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "agent-report.py"), "host-comparison", "--since", "last week"],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 2


def test_help_exits_0_and_the_text_report_names_the_table(tmp_path):
    base = [sys.executable, str(SCRIPTS / "agent-report.py"), "host-comparison"]
    assert subprocess.run([*base, "--help"], capture_output=True, text=True, timeout=60).returncode == 0
    (tmp_path / "config.md").write_text(CONFIG.format(days=7), encoding="utf-8")
    text = subprocess.run(
        [*base, "--quality-log", str(FIXTURES / "quality.jsonl"), "--spawn-log", str(FIXTURES / "spawns.jsonl"),
         "--state-dir", str(tmp_path), "--config", str(tmp_path / "config.md")],
        capture_output=True, text=True, timeout=60).stdout
    assert "| unknown | SUBSTANTIVE | 2 |" in text
    assert "n/a" in text


def test_missing_ledgers_give_an_empty_report(tmp_path):
    report = _report(tmp_path, quality=tmp_path / "none.jsonl", spawns=tmp_path / "none2.jsonl")
    assert report == {"groups": {}, "spawns": {}}
