"""agent-stats.py: single local usage report over the three existing ledgers."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "agent-stats.py"
_spec = importlib.util.spec_from_file_location("agent_stats", SCRIPT)
agent_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_stats)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _make_project_dir(projects_dir: Path, project: str, session_ids: list[str]) -> None:
    d = projects_dir / project
    d.mkdir(parents=True, exist_ok=True)
    for sid in session_ids:
        (d / f"{sid}.jsonl").write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# aggregate()
# ---------------------------------------------------------------------------

def test_aggregate_counts_resolved_and_invocations():
    task_rows = [
        {"session": "s1", "quality": 4, "tracker_key": "DEEPAGENT-1"},
        {"session": "s2", "quality": 5, "tracker_key": None},
    ]
    policy_rows = [{"project": "p"}, {"project": "p"}, {"project": "p"}]
    spawn_rows = [
        {"event": "spawn", "cost_usd": 0.5},
        {"event": "spawn", "cost_usd": 0.25},
        {"event": "refused"},
    ]
    stats = agent_stats.aggregate(task_rows, policy_rows, spawn_rows)
    assert stats["resolved"] == 2
    assert stats["invocations"] == 3
    assert stats["spawns"] == 2  # refused excluded
    assert stats["cost"] == pytest.approx(0.75)


def test_aggregate_marked_precedents_equals_rows_with_tracker_key():
    task_rows = [
        {"session": "s1", "quality": 3, "tracker_key": "DEEPAGENT-1"},
        {"session": "s2", "quality": 3, "tracker_key": "org/repo#7"},
        {"session": "s3", "quality": 3, "tracker_key": None},
    ]
    stats = agent_stats.aggregate(task_rows, [], [])
    assert stats["marked_precedents"] == 2
    assert stats["resolved"] == 3


def test_aggregate_mean_quality_ignores_null_quality():
    task_rows = [
        {"session": "s1", "quality": 4, "tracker_key": None},
        {"session": "s2", "quality": None, "tracker_key": None},
        {"session": "s3", "quality": 2, "tracker_key": None},
    ]
    stats = agent_stats.aggregate(task_rows, [], [])
    assert stats["mean_quality"] == pytest.approx(3.0)


def test_aggregate_no_rows_yields_none_mean_and_zero_counts():
    stats = agent_stats.aggregate([], [], [])
    assert stats == {
        "invocations": 0,
        "resolved": 0,
        "marked_precedents": 0,
        "mean_quality": None,
        "cost": 0,
        "spawns": 0,
    }


# ---------------------------------------------------------------------------
# project_sessions() / scope_rows()
# ---------------------------------------------------------------------------

def test_project_sessions_reads_transcript_filenames(tmp_path):
    _make_project_dir(tmp_path, "proj-a", ["s1", "s2"])
    assert agent_stats.project_sessions(tmp_path, "proj-a") == {"s1", "s2"}


def test_project_sessions_missing_dir_returns_empty_set(tmp_path):
    assert agent_stats.project_sessions(tmp_path, "no-such-project") == set()


def test_scope_rows_global_returns_everything_unfiltered():
    task_rows = [{"session": "s1"}]
    policy_rows = [{"project": "p"}]
    spawn_rows = [{"session_id": "s1"}]
    out = agent_stats.scope_rows(
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        project=None, sessions=set(),
    )
    assert out == (task_rows, policy_rows, spawn_rows)


def test_scope_rows_project_filters_by_session_and_project_field():
    task_rows = [{"session": "s1"}, {"session": "s9"}]
    policy_rows = [{"project": "proj-a"}, {"project": "proj-b"}]
    spawn_rows = [{"session_id": "s1"}, {"session_id": "s9"}]
    t, pl, sp = agent_stats.scope_rows(
        task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        project="proj-a", sessions={"s1"},
    )
    assert t == [{"session": "s1"}]
    assert pl == [{"project": "proj-a"}]
    assert sp == [{"session_id": "s1"}]


# ---------------------------------------------------------------------------
# main(): end-to-end --project vs --global, corrupt line handling
# ---------------------------------------------------------------------------

def test_main_project_vs_global_yield_different_counts(tmp_path, capsys):
    projects_dir = tmp_path / "projects"
    _make_project_dir(projects_dir, "proj-a", ["s1"])
    _make_project_dir(projects_dir, "proj-b", ["s2"])

    task_log = _write_jsonl(tmp_path / "task.jsonl", [
        {"ts": "2026-07-01T00:00:00+00:00", "session": "s1", "quality": 5, "tracker_key": "DEEPAGENT-1"},
        {"ts": "2026-07-01T00:00:00+00:00", "session": "s2", "quality": 3, "tracker_key": None},
    ])
    policy_log = _write_jsonl(tmp_path / "policy.jsonl", [
        {"ts": "2026-07-01T00:00:00+00:00", "session_id": "s1", "project": "proj-a"},
        {"ts": "2026-07-01T00:00:00+00:00", "session_id": "s2", "project": "proj-b"},
    ])
    spawn_log = _write_jsonl(tmp_path / "spawn.jsonl", [
        {"ts": "2026-07-01T00:00:00+00:00", "event": "spawn", "session_id": "s1", "cost_usd": 1.0},
        {"ts": "2026-07-01T00:00:00+00:00", "event": "spawn", "session_id": "s2", "cost_usd": 2.0},
    ])

    common = [
        "--days", "3650",
        "--task-log", str(task_log),
        "--policy-log", str(policy_log),
        "--spawn-log", str(spawn_log),
        "--projects-dir", str(projects_dir),
        "--json",
    ]

    rc = agent_stats.main(["--project", "proj-a", *common])
    assert rc == 0
    proj_a = json.loads(capsys.readouterr().out)
    assert proj_a["resolved"] == 1
    assert proj_a["marked_precedents"] == 1
    assert proj_a["invocations"] == 1
    assert proj_a["cost"] == pytest.approx(1.0)

    rc = agent_stats.main(["--global", *common])
    assert rc == 0
    glob = json.loads(capsys.readouterr().out)
    assert glob["resolved"] == 2
    assert glob["marked_precedents"] == 1
    assert glob["invocations"] == 2
    assert glob["cost"] == pytest.approx(3.0)

    assert proj_a != glob


def test_main_corrupt_ledger_line_is_skipped_not_fatal(tmp_path, capsys):
    task_log = tmp_path / "task.jsonl"
    task_log.write_text(
        '{"ts": "2026-07-01T00:00:00+00:00", "session": "s1", "quality": 4, "tracker_key": null}\n'
        "NOT_JSON\n",
        encoding="utf-8",
    )
    policy_log = _write_jsonl(tmp_path / "policy.jsonl", [])
    spawn_log = _write_jsonl(tmp_path / "spawn.jsonl", [])
    projects_dir = tmp_path / "projects"
    _make_project_dir(projects_dir, "proj-a", ["s1"])

    rc = agent_stats.main([
        "--global",
        "--days", "3650",
        "--task-log", str(task_log),
        "--policy-log", str(policy_log),
        "--spawn-log", str(spawn_log),
        "--projects-dir", str(projects_dir),
        "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["resolved"] == 1


def test_main_writes_nothing_to_source_ledgers(tmp_path):
    task_log = _write_jsonl(tmp_path / "task.jsonl", [
        {"ts": "2026-07-01T00:00:00+00:00", "session": "s1", "quality": 4, "tracker_key": None},
    ])
    before = task_log.read_text(encoding="utf-8")
    agent_stats.main([
        "--global", "--days", "3650",
        "--task-log", str(task_log),
        "--policy-log", str(tmp_path / "policy.jsonl"),
        "--spawn-log", str(tmp_path / "spawn.jsonl"),
        "--projects-dir", str(tmp_path / "projects"),
        "--json",
    ])
    assert task_log.read_text(encoding="utf-8") == before
