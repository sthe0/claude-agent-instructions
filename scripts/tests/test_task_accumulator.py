"""Stage 6 / item B: `agentctl.task_accumulator`, the cross-session per-task
effort accumulator, tested in isolation via a `tmp_path`-rooted store so no
test touches the real `~/.claude-agent/agentctl/task-accumulators/` tree."""
from __future__ import annotations

import json

import pytest

from agentctl import task_accumulator as ta


# --- get() on a missing file ---------------------------------------------------

def test_get_on_missing_file_returns_zeroed_shape(tmp_path):
    data = ta.get("some-task", root=tmp_path)
    assert data["schema_version"] == 1
    assert data["task_id"] == "some-task"
    assert data["per_axis_totals"] == {axis: 0 for axis in ta.AXES}
    assert data["session_ids_contributing"] == []
    assert data["last_updated"] is None


# --- add() / get() round-trip ---------------------------------------------------

def test_add_then_get_round_trips(tmp_path):
    ta.add("task-a", "replan_count", 2, session_id="sess-1", now="2026-08-26T00:00:00Z", root=tmp_path)
    data = ta.get("task-a", root=tmp_path)
    assert data["per_axis_totals"]["replan_count"] == 2
    assert data["session_ids_contributing"] == ["sess-1"]
    assert data["last_updated"] == "2026-08-26T00:00:00Z"


def test_add_accumulates_across_calls(tmp_path):
    ta.add("task-a", "replan_count", 2, root=tmp_path)
    ta.add("task-a", "replan_count", 1, root=tmp_path)
    assert ta.get("task-a", root=tmp_path)["per_axis_totals"]["replan_count"] == 3


def test_add_tracks_distinct_axes_independently(tmp_path):
    ta.add("task-a", "replan_count", 2, root=tmp_path)
    ta.add("task-a", "plan_review_rounds", 5, root=tmp_path)
    totals = ta.get("task-a", root=tmp_path)["per_axis_totals"]
    assert totals["replan_count"] == 2
    assert totals["plan_review_rounds"] == 5
    assert totals["plan_enumerate_rounds"] == 0
    assert totals["code_review_rounds"] == 0


def test_add_appends_each_distinct_session_id_once(tmp_path):
    ta.add("task-a", "replan_count", 1, session_id="sess-1", root=tmp_path)
    ta.add("task-a", "replan_count", 1, session_id="sess-1", root=tmp_path)
    ta.add("task-a", "replan_count", 1, session_id="sess-2", root=tmp_path)
    assert ta.get("task-a", root=tmp_path)["session_ids_contributing"] == ["sess-1", "sess-2"]


def test_add_rejects_unknown_axis(tmp_path):
    with pytest.raises(ValueError):
        ta.add("task-a", "not-a-real-axis", 1, root=tmp_path)


def test_add_rejects_negative_count(tmp_path):
    with pytest.raises(ValueError):
        ta.add("task-a", "replan_count", -1, root=tmp_path)


def test_add_zero_count_is_a_noop_fold(tmp_path):
    ta.add("task-a", "replan_count", 0, session_id="sess-1", root=tmp_path)
    data = ta.get("task-a", root=tmp_path)
    assert data["per_axis_totals"]["replan_count"] == 0
    assert data["session_ids_contributing"] == ["sess-1"]


# --- distinct tasks are isolated -------------------------------------------------

def test_distinct_task_ids_do_not_share_totals(tmp_path):
    ta.add("task-a", "replan_count", 3, root=tmp_path)
    ta.add("task-b", "replan_count", 1, root=tmp_path)
    assert ta.get("task-a", root=tmp_path)["per_axis_totals"]["replan_count"] == 3
    assert ta.get("task-b", root=tmp_path)["per_axis_totals"]["replan_count"] == 1


# --- survives a simulated session restart ---------------------------------------

def test_survives_simulated_session_restart(tmp_path):
    """Session 1 accumulates 2 replans and "closes" (nothing more than: the
    process exits, no in-memory state carries over). Session 2 opens fresh
    against the same task_id and must see the 2 inherited from session 1
    before adding its own."""
    ta.add("hook-guard-permission-self-grant", "replan_count", 2, session_id="18fb6860", root=tmp_path)

    # Simulate session 2 starting fresh: a brand new call into the module,
    # no shared in-memory state with the block above.
    inherited = ta.get("hook-guard-permission-self-grant", root=tmp_path)
    assert inherited["per_axis_totals"]["replan_count"] == 2

    ta.add("hook-guard-permission-self-grant", "replan_count", 1, session_id="2442a5ac", root=tmp_path)
    final = ta.get("hook-guard-permission-self-grant", root=tmp_path)
    assert final["per_axis_totals"]["replan_count"] == 3
    assert final["session_ids_contributing"] == ["18fb6860", "2442a5ac"]


# --- reset() ---------------------------------------------------------------------

def test_reset_zeroes_all_axes_and_sessions(tmp_path):
    ta.add("task-a", "replan_count", 3, session_id="sess-1", root=tmp_path)
    ta.add("task-a", "code_review_rounds", 2, root=tmp_path)
    ta.reset("task-a", root=tmp_path)
    data = ta.get("task-a", root=tmp_path)
    assert data["per_axis_totals"] == {axis: 0 for axis in ta.AXES}
    assert data["session_ids_contributing"] == []
    assert data["last_updated"] is None


def test_reset_does_not_affect_other_tasks(tmp_path):
    ta.add("task-a", "replan_count", 3, root=tmp_path)
    ta.add("task-b", "replan_count", 5, root=tmp_path)
    ta.reset("task-a", root=tmp_path)
    assert ta.get("task-a", root=tmp_path)["per_axis_totals"]["replan_count"] == 0
    assert ta.get("task-b", root=tmp_path)["per_axis_totals"]["replan_count"] == 5


# --- on-disk shape / forward-migration tolerance --------------------------------

def test_persisted_file_is_valid_json_with_expected_keys(tmp_path):
    ta.add("task-a", "replan_count", 1, root=tmp_path)
    path = ta._path("task-a", root=tmp_path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert set(on_disk["per_axis_totals"]) == set(ta.AXES)


def test_get_degrades_gracefully_on_corrupt_json(tmp_path):
    path = ta._path("task-a", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    data = ta.get("task-a", root=tmp_path)
    assert data["per_axis_totals"] == {axis: 0 for axis in ta.AXES}


def test_get_degrades_gracefully_on_future_schema_version(tmp_path):
    path = ta._path("task-a", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "task_id": "task-a"}), encoding="utf-8")
    data = ta.get("task-a", root=tmp_path)
    assert data["schema_version"] == 1
    assert data["per_axis_totals"] == {axis: 0 for axis in ta.AXES}


def test_add_tolerates_a_missing_axis_key_in_existing_file(tmp_path):
    """A file written before an axis existed (or hand-edited) is missing a key
    the current AXES tuple expects -- add() must fill it in as 0 rather than
    raising a KeyError."""
    path = ta._path("task-a", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "task_id": "task-a",
            "per_axis_totals": {"replan_count": 2},
            "session_ids_contributing": [],
            "last_updated": None,
        }),
        encoding="utf-8",
    )
    ta.add("task-a", "code_review_rounds", 1, root=tmp_path)
    data = ta.get("task-a", root=tmp_path)
    assert data["per_axis_totals"]["replan_count"] == 2
    assert data["per_axis_totals"]["code_review_rounds"] == 1


# --- concurrent-write safety -----------------------------------------------------

def test_concurrent_adds_from_multiple_processes_do_not_drop_increments(tmp_path):
    """Two OS processes racing to increment the same axis on the same task_id
    must not lose an increment to a lost read-modify-write race -- the
    scenario `_FileLock`'s exclusive flock exists to prevent."""
    import subprocess
    import sys

    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from agentctl import task_accumulator as ta; "
        "[ta.add('race-task', 'replan_count', 1, root=sys.argv[2]) for _ in range(20)]"
    )
    scripts_dir = str(ta.__file__.rsplit("/agentctl/", 1)[0])
    procs = [
        subprocess.Popen([sys.executable, "-c", script, scripts_dir, str(tmp_path)])
        for _ in range(5)
    ]
    for p in procs:
        rc = p.wait(timeout=60)
        assert rc == 0
    data = ta.get("race-task", root=tmp_path)
    assert data["per_axis_totals"]["replan_count"] == 100
