"""agent-report.py host-comparison: figures per host and weight class from the two ledgers."""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "host_comparison"


def _report(*extra, quality=FIXTURES / "quality.jsonl", spawns=FIXTURES / "spawns.jsonl"):
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "agent-report.py"), "host-comparison", "--json",
         "--quality-log", str(quality), "--spawn-log", str(spawns), *extra],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _task(report, host, weight):
    return next(f for f in report["tasks"] if f["host"] == host and f["weight_class"] == weight)


def test_figures_split_by_weight_with_task_counts():
    report = _report()
    claude_heavy = _task(report, "claude", "SUBSTANTIVE")
    assert (claude_heavy["tasks"], claude_heavy["total_cost_usd"], claude_heavy["mean_cost_usd"]) == (2, 6.0, 3.0)
    assert (claude_heavy["mean_quality"], claude_heavy["mean_replans"]) == (4.0, 0.5)
    assert _task(report, "claude", "SMALL_CHANGE")["tasks"] == 1
    assert _task(report, "opencode", "SUBSTANTIVE")["total_cost_usd"] == 1.0


def test_prestamp_state_continued_same_host_unknown():
    report = _report()
    unknown = _task(report, "unknown", "SUBSTANTIVE")
    assert unknown["tasks"] == 2
    assert unknown["total_cost_usd"] == 16.0
    assert _task(report, "claude", "SUBSTANTIVE")["tasks"] == 2


def test_unknown_cost_null_not_zero():
    report = _report()
    small = _task(report, "claude", "SMALL_CHANGE")
    assert small["tasks"] == 1
    assert small["cost_known_tasks"] == 0
    assert small["total_cost_usd"] is None and small["mean_cost_usd"] is None
    opencode_spawns = next(f for f in report["spawns"] if f["host"] == "opencode")
    assert opencode_spawns["spawns"] == 1
    assert opencode_spawns["total_cost_usd"] is None


def test_spawns_are_counted_per_host_and_refusals_left_out():
    spawns = {f["host"]: f for f in _report()["spawns"]}
    assert spawns["claude"]["spawns"] == 2 and spawns["claude"]["total_cost_usd"] == 0.5
    assert spawns["claude"]["cost_known_spawns"] == 1
    assert spawns["unknown"]["spawns"] == 1 and spawns["unknown"]["total_cost_usd"] == 3.0


def test_since_accepts_a_bare_date_and_a_full_timestamp():
    by_date = _report("--since", "2026-11-01")
    by_timestamp = _report("--since", "2026-11-01T00:00:00Z")
    assert by_date == by_timestamp
    assert sum(f["tasks"] for f in by_date["tasks"]) == 5
    assert all(f["host"] != "unknown" or f["tasks"] == 1 for f in by_date["tasks"])
    midday = _report("--since", "2026-11-03T10:00:01Z")
    assert [f["host"] for f in midday["spawns"]] == ["opencode"]


def test_a_malformed_since_exits_2():
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "agent-report.py"), "host-comparison", "--since", "last week"],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 2


def test_help_exits_0_and_the_text_report_names_the_table():
    base = [sys.executable, str(SCRIPTS / "agent-report.py"), "host-comparison"]
    assert subprocess.run([*base, "--help"], capture_output=True, text=True, timeout=60).returncode == 0
    text = subprocess.run(
        [*base, "--quality-log", str(FIXTURES / "quality.jsonl"), "--spawn-log", str(FIXTURES / "spawns.jsonl")],
        capture_output=True, text=True, timeout=60).stdout
    assert "| unknown | SUBSTANTIVE | 2 |" in text
    assert "n/a" in text


def test_missing_ledgers_give_an_empty_report(tmp_path):
    report = _report(quality=tmp_path / "none.jsonl", spawns=tmp_path / "none2.jsonl")
    assert report == {"tasks": [], "spawns": []}
