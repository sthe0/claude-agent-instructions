"""Request-vs-fact calibration: the comparison, the ledger, the engine's post-dispatch recording and the
`resource-estimate-drift` finding.

Decision-log rows come from the real hook (`scripts/hook-resource-grant-check.py`, judge stubbed, one run
through a subprocess), and the transcript denials from the stage-3 replay fixture, so the comparison reads
what the hook and the harness really produce rather than rows written to suit it.
"""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import resource_grant_hook_support as support
import test_tool_contracts_segments as seg_tests
from agentctl import cli, resource_estimation as RE
from agentctl.dispatch import RunResult
from agentctl.plan import ResourceRequest
from agentctl.state import SessionState
from lib import resource_estimation_ledger as ledger
from test_hook_resource_grant_check import CONSTANTS, Rig, _clean_environment, hook, spawn  # noqa: F401
from test_resource_grant_replay import JudgeStub

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "resource_grant_replay" / "denials.jsonl"


def _load_self_diagnose():
    spec = importlib.util.spec_from_file_location("self_diagnose_under_test", SCRIPTS / "self-diagnose.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DIAG = _load_self_diagnose()


@pytest.fixture(autouse=True)
def _ledger_in_tmp(monkeypatch, tmp_path):
    path = tmp_path / "ledger" / "estimation.jsonl"
    monkeypatch.setenv(ledger.LEDGER_ENV, str(path))
    return path


@pytest.fixture
def rig(hook, spawn, tmp_path):  # noqa: F811
    return Rig(hook, spawn, tmp_path)


def _uses(rig, child, *calls, judge=None):
    for call in calls:
        rig.bash(child, call, judge=judge)
    return child.log_rows()


def _compare(rig, uses, allow=(), add_dirs=()):
    return RE.compare(list(allow), list(add_dirs), uses, venue=str(rig.venue), workdir=str(rig.venue))


# --- the comparison over rows the real hook wrote ---------------------------------------------------

def test_a_write_outside_the_request_is_an_under_estimate_and_the_class_is_named(rig):
    child = rig.child()
    uses = _uses(rig, child, f"touch {rig.outside}/f")
    result = _compare(rig, uses)
    assert result["under"] == [{"class": "file", "text": f"{rig.outside}/f (write)"}]


def test_a_write_the_request_covers_is_not_under_and_its_entry_is_not_over(rig):
    child = rig.child()
    uses = _uses(rig, child, f"touch {rig.outside}/f")
    result = _compare(rig, uses, add_dirs=[(str(rig.outside), "write")])
    assert result == {"under": [], "over": []}


def test_a_write_inside_the_workdir_baseline_is_not_under(rig):
    child = rig.child()
    uses = _uses(rig, child, f"touch {rig.venue}/inside")
    assert _compare(rig, uses)["under"] == []


def test_a_write_dir_no_logged_write_matched_is_an_over_estimate(rig):
    child = rig.child()
    uses = _uses(rig, child, f"touch {rig.outside}/f")
    other = rig.tmp / "never-used"
    other.mkdir()
    result = _compare(rig, uses, add_dirs=[(str(rig.outside), "write"), (str(other), "write")])
    assert result["over"] == [f"add_dir {other} (write)"]


def test_a_read_only_add_dir_is_never_graded_as_over(rig):
    child = rig.child()
    uses = _uses(rig, child, f"touch {rig.outside}/f")
    result = _compare(rig, uses, add_dirs=[(str(rig.outside), "write"), (str(rig.tmp), "read")])
    assert result["over"] == []


def test_over_is_judged_by_the_matched_rule_the_hook_logged(rig):
    used = f"Bash(git -C {rig.venue} commit:*)"
    child = rig.child(rules=[used])
    uses = _uses(rig, child, f"git -C {rig.venue} commit -q -m x")
    assert used in [m for row in uses for m in row["matched"]]
    unused = f"Bash(python3 {rig.venue}/never-run.py:*)"
    result = _compare(rig, uses, allow=[used, unused])
    assert result["over"] == [unused]


def test_a_call_a_wildcard_covered_without_a_typed_write_is_not_under(rig):
    rule = f"Bash(git -C {rig.venue} commit:*)"
    child = rig.child(rules=[rule])
    uses = _uses(rig, child, f"git -C {rig.venue} commit -q -m x")
    assert _compare(rig, uses, allow=[rule])["under"] == []
    assert _compare(rig, uses)["under"] == []


def test_an_opaque_write_the_judge_named_counts_through_the_real_hook(rig):
    child = rig.child()
    judge = JudgeStub([{"kind": "file", "path": f"{rig.outside}/o", "op": "write"}])
    uses = _uses(rig, child, f"python3 -c \"open('{rig.outside}/o', 'w')\"", judge=judge)
    assert any(r["resources"] for r in uses)
    assert {"class": "file", "text": f"{rig.outside}/o (write)"} in _compare(rig, uses)["under"]


def test_log_rows_written_by_the_hook_script_in_a_subprocess_feed_the_comparison(rig):
    child = rig.child()
    payload = child.payload("Bash", command=f"touch {rig.outside}/s")
    env = {**os.environ, **child.env}
    proc = subprocess.run([sys.executable, str(support.HOOK_FILE)], input=payload, text=True,
                          capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0
    uses = RE.read_uses(RE.uses_log_path(child.state_root, support.PARENT_SESSION, support.STAGE_INDEX))
    assert uses and uses[-1]["decision"] in ("deny", "none")
    assert {"class": "file", "text": f"{rig.outside}/s (write)"} in _compare(rig, uses)["under"]


def _fixture_denials():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_a_transcript_denial_from_the_replay_fixture_is_under_when_the_request_lacks_it(rig):
    case = next(c for c in _fixture_denials() if c["tool"] == "Bash" and c["expect"] == "deny")
    command = case["command"].replace("{VENUE}", str(rig.venue)).replace("{OUTSIDE}", str(rig.outside)) \
        .replace("{SCRATCH}", str(rig.scratch))
    miss = {"tool_name": "Bash", "command": command}
    result = RE.compare([], [], [], venue=str(rig.venue), workdir=str(rig.venue), denials=[miss])
    assert result["under"], command
    covering = [(str(rig.outside), "write"), (str(rig.venue), "write")]
    assert RE.compare([], covering, [], venue=str(rig.venue), workdir=str(rig.venue),
                      denials=[miss])["under"] == []


def _denials(rig, *misses):
    return RE.compare([], [], [], venue=str(rig.venue), workdir=str(rig.venue), denials=list(misses))


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_a_denied_file_write_tool_is_under_when_the_request_lacks_the_path(rig, tool):
    result = _denials(rig, {"tool_name": tool, "command": f"{rig.outside}/w"})
    assert result["under"] == [{"class": "file", "text": f"{rig.outside}/w (write)"}]


def test_a_write_denial_from_the_replay_fixture_is_under(rig):
    case = next(c for c in _fixture_denials() if c["tool"] == "Write")
    path = case["path"].replace("{VENUE}", str(rig.venue)).replace("{OUTSIDE}", str(rig.outside)) \
        .replace("{SCRATCH}", str(rig.scratch))
    assert _denials(rig, {"tool_name": "Write", "command": path})["under"]


def test_a_denied_read_is_never_an_under_estimate_with_or_without_a_read_dir(rig):
    miss = {"tool_name": "Read", "command": f"{rig.outside}/secret.txt"}
    assert _denials(rig, miss)["under"] == []
    with_dir = RE.compare([], [(str(rig.outside), "read")], [], venue=str(rig.venue),
                          workdir=str(rig.venue), denials=[miss])
    assert with_dir["under"] == [] and with_dir["over"] == []


def test_a_read_class_service_use_is_not_a_change():
    from agentctl import resources
    from agentctl.resource_uses import is_change
    assert not is_change(resources.ServiceResource("svc", "read"))
    assert is_change(resources.ServiceResource("svc", "write"))


def test_the_hook_and_the_calibration_share_one_log_path_and_change_test():
    spec = importlib.util.spec_from_file_location("hook_under_test", SCRIPTS / "hook-resource-grant-check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    link = {"parent_session": "ps", "stage_index": 4}
    assert module._log_path(Path("/r"), link) == RE.uses_log_path("/r", "ps", 4)
    assert module._is_change is RE._is_change


# --- the ledger -------------------------------------------------------------------------------------

def _row(task, stage, under=(), kind="developer", plan_sha="p"):
    return {"ts": "t", "task_id": task, "session": "s", "stage": stage, "kind": kind,
            "under": list(under), "over": [], "plan_sha": plan_sha}


MISSING = [{"class": "file", "text": "/x/f (write)"}]


def test_the_ledger_only_appends_and_the_reader_keeps_the_last_row_of_a_task_and_stage(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger.append_row(_row("t1", 1, plan_sha="v1"), path)
    ledger.append_row(_row("t1", 2), path)
    ledger.append_row(_row("t1", 1, MISSING, plan_sha="v2"), path)
    assert len(path.read_text().splitlines()) == 3
    rows = ledger.read_rows(path)
    assert [(r["stage"], r["plan_sha"]) for r in rows] == [(2, "p"), (1, "v2")]


def test_the_ledger_reader_skips_a_torn_line_and_an_absent_file(tmp_path):
    path = tmp_path / "l.jsonl"
    assert ledger.read_rows(path) == []
    ledger.append_row(_row("t1", 1), path)
    with open(path, "a") as handle:
        handle.write('{"torn": \n')
    assert [r["task_id"] for r in ledger.read_rows(path)] == ["t1"]


# --- recording after a stage ------------------------------------------------------------------------

def _state(tmp_path):
    state = SessionState(session_id=support.PARENT_SESSION, task_id="task-a")
    state.plan_task_id = "task-a"
    state.accepted_plan_digest = "digest-1"
    state.repo_root = str(tmp_path)
    return state


def _doc(*rows):
    return SimpleNamespace(meta=SimpleNamespace(resource_requests=list(rows)))


def _log(root, rows, session=support.PARENT_SESSION, stage=support.STAGE_INDEX):
    path = RE.uses_log_path(root, session, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


WRITE_ROW = {"decision": "deny", "reason_class": "uncovered-write", "matched": [],
             "resources": [{"kind": "file", "path": "/nowhere/f", "mode": "write"}]}


def test_record_stage_estimate_stores_state_and_appends_one_ledger_row(tmp_path, _ledger_in_tmp):
    state, stage = _state(tmp_path), support.stage_for("developer", "feat")
    _log(tmp_path / "root", [WRITE_ROW])
    request = ResourceRequest(kind="developer", stage=1, consult_ref="c")
    record = RE.record_stage_estimate(state, stage, state_root=tmp_path / "root", doc=_doc(request),
                                      venue=str(tmp_path))
    assert state.resource_estimates["1"] == record
    assert record["kind"] == "developer" and record["plan_sha"] == "digest-1"
    assert record["under"] == [{"class": "file", "text": "/nowhere/f (write)"}]
    rows = ledger.read_rows(_ledger_in_tmp)
    assert len(rows) == 1 and rows[0]["task_id"] == "task-a" and rows[0]["stage"] == 1
    assert set(rows[0]) == {"ts", "task_id", "session", "stage", "kind", "under", "over", "plan_sha"}


def test_a_second_dispatch_of_the_same_stage_replaces_its_ledger_row(tmp_path, _ledger_in_tmp):
    state, stage = _state(tmp_path), support.stage_for("developer", "feat")
    request = ResourceRequest(kind="developer", stage=1, consult_ref="c")
    _log(tmp_path / "root", [WRITE_ROW])
    RE.record_stage_estimate(state, stage, state_root=tmp_path / "root", doc=_doc(request), venue=str(tmp_path))
    state.accepted_plan_digest = "digest-2"
    RE.record_stage_estimate(state, stage, state_root=tmp_path / "root", doc=_doc(request), venue=str(tmp_path))
    assert [r["plan_sha"] for r in ledger.read_rows(_ledger_in_tmp)] == ["digest-2"]


def test_decision_log_rows_older_than_this_dispatch_are_not_compared(tmp_path, _ledger_in_tmp):
    state, stage = _state(tmp_path), support.stage_for("developer", "feat")
    request = ResourceRequest(kind="developer", stage=1, consult_ref="c")
    stale = {**WRITE_ROW, "ts": "2000-01-01T00:00:00+0000"}
    fresh = {**WRITE_ROW, "ts": time.strftime(RE.LOG_TS_FORMAT),
             "resources": [{"kind": "file", "path": "/nowhere/fresh", "mode": "write"}]}
    _log(tmp_path / "root", [stale, fresh])
    since = dt.datetime.now().astimezone().replace(microsecond=0) - dt.timedelta(seconds=5)
    record = RE.record_stage_estimate(state, stage, state_root=tmp_path / "root", doc=_doc(request),
                                      venue=str(tmp_path), since=since)
    assert record["under"] == [{"class": "file", "text": "/nowhere/fresh (write)"}]


def test_no_request_row_records_nothing_and_raises_nothing(tmp_path, _ledger_in_tmp):
    state, stage = _state(tmp_path), support.stage_for("developer", "feat")
    _log(tmp_path / "root", [WRITE_ROW])
    other = ResourceRequest(kind="tech-writer", stage=1, consult_ref="c")
    assert RE.record_stage_estimate(state, stage, state_root=tmp_path / "root", doc=_doc(other)) is None
    assert RE.record_stage_estimate(state, stage, state_root=tmp_path / "root", doc=_doc()) is None
    assert state.resource_estimates == {} and not _ledger_in_tmp.exists()


def test_a_missing_decision_log_records_nothing_and_raises_nothing(tmp_path, _ledger_in_tmp):
    state, stage = _state(tmp_path), support.stage_for("developer", "feat")
    request = ResourceRequest(kind="developer", stage=1, consult_ref="c")
    assert RE.record_stage_estimate(state, stage, state_root=tmp_path / "no-such-root", doc=_doc(request)) is None
    assert RE.record_stage_estimate(state, stage, state_root=None, doc=_doc(request)) is None
    assert state.resource_estimates == {} and not _ledger_in_tmp.exists()


def test_an_unreadable_plan_records_nothing_and_raises_nothing(tmp_path, _ledger_in_tmp):
    state, stage = _state(tmp_path), support.stage_for("developer", "feat")
    state.plan_path = str(tmp_path / "gone.toml")
    assert RE.record_stage_estimate(state, stage, state_root=tmp_path) is None


# --- through cmd_dispatch ---------------------------------------------------------------------------

REQUEST_ROW = """
[[meta.resource_request]]
kind = "developer"
stage = 1
consult_ref = "consult"
allow = []
add_dirs = []
"""


def _dispatch(store, fixtures_dir, tmp_path, sid, *, seed_rows):
    from test_stage_grants import _to_executing, ns
    plan = tmp_path / f"{sid}-plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage.toml").read_text() + REQUEST_ROW)
    _to_executing(store, sid, fixtures_dir, plan_path=str(plan))
    if seed_rows is not None:
        _log(store.root, seed_rows, session=sid)
    runner = lambda argv, cwd=None: RunResult(0, stdout="COMPLETED: done\n", stderr="")  # noqa: E731
    directive = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium", dry_run=False),
                                 store=store, runner=runner)
    return directive, store.load(sid)


def test_dispatch_records_the_comparison_in_state_and_ledger(store, fixtures_dir, tmp_path, _ledger_in_tmp):
    directive, state = _dispatch(store, fixtures_dir, tmp_path, "est-records", seed_rows=[WRITE_ROW])
    assert directive.marker == "COMPLETED"
    assert state.resource_estimates["1"]["under"] == [{"class": "file", "text": "/nowhere/f (write)"}]
    assert [r["stage"] for r in ledger.read_rows(_ledger_in_tmp)] == [1]


def test_dispatch_without_a_decision_log_routes_as_before_and_records_nothing(
        store, fixtures_dir, tmp_path, _ledger_in_tmp):
    directive, state = _dispatch(store, fixtures_dir, tmp_path, "est-nolog", seed_rows=None)
    assert directive.marker == "COMPLETED"
    assert state.resource_estimates == {} and not _ledger_in_tmp.exists()


def test_session_state_round_trips_resource_estimates():
    state = SessionState(session_id="s", task_id="t")
    state.resource_estimates["2"] = {"kind": "developer", "under": [], "over": ["x"], "plan_sha": "p"}
    assert SessionState.from_json(state.to_json()).resource_estimates == state.resource_estimates
    older = json.loads(state.to_json())
    del older["resource_estimates"]
    assert SessionState.from_dict(older).resource_estimates == {}


# --- the finding ------------------------------------------------------------------------------------

def _scan(tmp_path, rows, repeat=None):
    path = tmp_path / "scan-ledger.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    config = tmp_path / "config.md"
    config.write_text("| Key | Value |\n|---|---|\n" + (
        f"| `resource-estimate-drift-repeat` | `{repeat}` | m |\n" if repeat else ""))
    return DIAG.scan_resource_estimate_drift(path, config)


def test_two_distinct_tasks_fire_the_finding_and_it_names_kind_classes_and_the_skill_section(tmp_path):
    rows = [_row("task-a", 1, MISSING), _row("task-b", 3, [{"class": "vcs_ref", "text": "origin main push"}])]
    found = _scan(tmp_path, rows)
    assert len(found) == 1
    finding = found[0]
    assert finding.kind == "resource-estimate-drift"
    assert finding.path == "skills/specializations/developer/SKILL.md"
    assert "developer" in finding.detail and "SKILL.md § Resource request" in finding.detail
    assert "file" in finding.detail and "vcs_ref" in finding.detail and "/x/f (write)" in finding.detail


def test_one_task_with_two_plan_versions_and_two_stages_does_not_fire(tmp_path):
    rows = [_row("task-a", 1, MISSING, plan_sha="v1"), _row("task-a", 2, MISSING, plan_sha="v2")]
    assert _scan(tmp_path, rows) == []


def test_counting_is_per_kind(tmp_path):
    rows = [_row("task-a", 1, MISSING, kind="developer"), _row("task-b", 1, MISSING, kind="tech-writer")]
    assert _scan(tmp_path, rows) == []


def test_rows_without_an_under_estimate_do_not_count(tmp_path):
    rows = [_row("task-a", 1, MISSING), _row("task-b", 1, [])]
    assert _scan(tmp_path, rows) == []


def test_the_threshold_is_the_config_key(tmp_path):
    rows = [_row("task-a", 1, MISSING), _row("task-b", 1, MISSING)]
    assert _scan(tmp_path, rows, repeat=3) == []
    assert len(_scan(tmp_path, rows, repeat=2)) == 1


@pytest.mark.parametrize("under", [["not-a-dict"], [None, 3], "text", {"class": "file"}])
def test_under_rows_without_dict_items_are_skipped_not_raised_on(tmp_path, under):
    rows = [{**_row("task-a", 1), "under": under}, _row("task-b", 1, MISSING), _row("task-c", 1, MISSING)]
    found = _scan(tmp_path, rows)
    assert len(found) == 1 and "2 different tasks" in found[0].detail


def test_the_live_config_row_is_two():
    assert DIAG.read_config_int("resource-estimate-drift-repeat", -1) == 2


def test_a_missing_ledger_reports_nothing(tmp_path):
    assert DIAG.scan_resource_estimate_drift(tmp_path / "none.jsonl", tmp_path / "config.md") == []
