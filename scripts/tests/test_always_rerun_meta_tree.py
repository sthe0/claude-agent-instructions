"""The always-rerun and failed sets of the result store belong to the tree they were computed on, and the
gate warns when most of a run's tests cannot be reused."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import gate_speedup_control as control
from lib import result_store
from lib import suite_gate as gate


def _git_core(tmp_path: Path, content: str) -> Path:
    core = tmp_path / "core"
    (core / "lib").mkdir(parents=True, exist_ok=True)
    (core / "lib/a.py").write_text(content)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(core), *args], check=True, capture_output=True)
    return core


def _settle(tmp_path: Path, core: Path, ids: list[str]) -> Path:
    environment = result_store.current_environment({"dists": [], "git": "g"})
    store_path = tmp_path / "db" / "r.sqlite"
    trace = tmp_path / "trace"
    trace.mkdir(exist_ok=True)
    lines = [{"kind": "file", "id": 0, "path": "lib/a.py", "digest": "sha256:aa"}]
    lines += [{"kind": "test", "id": i, "files": [0], "listings": {}, "probes": {}, "always_rerun": ["subprocess"]}
              for i in ids]
    (trace / "main.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    report = tmp_path / "tests.jsonl"
    records = [{"kind": "collected", "nodeid": i} for i in ids]
    records += [{"kind": "report", "nodeid": i, "when": "call", "outcome": "passed"} for i in ids]
    report.write_text("".join(json.dumps(line) + "\n" for line in records))
    sha = "c" * 40
    plan = gate.ReusePlan(store_path, environment, trace, tmp_path / "out", {}, core=core)
    settled = gate.settle_reuse(plan, sha, gate.SuiteRun(0, (), sha, report_path=str(report), hermetic=True))
    assert settled.always_rerun_tests == len(ids)
    return store_path


def test_a_settled_run_tags_both_sets_with_the_tree_it_ran_on(tmp_path):
    core = _git_core(tmp_path, "A = 1\n")
    store_path = _settle(tmp_path, core, ["t::one", "t::two"])
    tree = gate.core_tree(core)
    assert len(tree) == 40
    with result_store.ResultStore(store_path) as store:
        assert json.loads(gate.get_tagged_meta(store, result_store.ALWAYS_RERUN_META, tree)) == ["t::one", "t::two"]
        assert json.loads(gate.get_tagged_meta(store, result_store.FAILED_META, tree)) == []


def test_a_set_of_another_tree_or_without_a_tag_reads_as_stale(tmp_path):
    core = _git_core(tmp_path, "A = 1\n")
    store_path = _settle(tmp_path, core, ["t::one"])
    with result_store.ResultStore(store_path) as store:
        assert gate.get_tagged_meta(store, result_store.ALWAYS_RERUN_META, "f" * 40) is None
        assert gate.get_tagged_meta(store, result_store.ALWAYS_RERUN_META, "") is None
        store.set_meta("legacy", json.dumps(["t::one"]))
        assert gate.get_tagged_meta(store, "legacy", gate.core_tree(core)) is None


def test_a_stale_always_rerun_set_does_not_excuse_a_miss_from_the_warm_up(tmp_path):
    core = _git_core(tmp_path, "A = 1\n")
    store_path = _settle(tmp_path, core, ["t::one"])
    ids = ["t::one"]
    with result_store.ResultStore(store_path) as store:
        same = json.loads(gate.get_tagged_meta(store, result_store.ALWAYS_RERUN_META, gate.core_tree(core)) or "[]")
        other = json.loads(gate.get_tagged_meta(store, result_store.ALWAYS_RERUN_META, "e" * 40) or "[]")
    assert not control.needs_warm_up({"ids": ids, "hits": {}, "always_rerun": same, "failed": []})
    assert control.needs_warm_up({"ids": ids, "hits": {}, "always_rerun": other, "failed": []})


def test_the_tree_of_a_directory_that_is_not_a_checkout_is_empty(tmp_path):
    assert gate.core_tree(tmp_path) == ""


def test_the_reuse_share_warning_needs_both_a_jump_and_a_large_share():
    history = [100, 300, 200, 150, 120]
    assert gate.reuse_share_warning(1000, 1200, history)
    assert not gate.reuse_share_warning(1000, 5000, history)  # above twice the lowest, but only 20 percent
    assert not gate.reuse_share_warning(150, 200, history)  # a large share, but under twice the lowest
    assert not gate.reuse_share_warning(1000, 1200, [])  # no settled run to compare with


def test_the_reuse_share_warning_looks_at_the_last_five_runs_only():
    assert not gate.reuse_share_warning(1000, 1200, [10, 900, 900, 900, 900, 900])
    assert gate.reuse_share_warning(1000, 1200, [900, 900, 900, 900, 10])


def test_a_run_with_a_jump_in_always_rerun_tests_carries_the_warning_and_a_normal_one_does_not(tmp_path):
    core = _git_core(tmp_path, "A = 1\n")
    ids = [f"t::{n}" for n in range(8)]
    _settle(tmp_path, core, ids[:2])
    store_path = tmp_path / "db" / "r.sqlite"
    with result_store.ResultStore(store_path) as store:
        store.set_meta(gate.ALWAYS_RERUN_HISTORY_META, json.dumps([2, 3, 2]))
    environment = result_store.current_environment({"dists": [], "git": "g"})
    trace, report = tmp_path / "trace", tmp_path / "tests.jsonl"
    lines = [{"kind": "file", "id": 0, "path": "lib/a.py", "digest": "sha256:aa"}]
    lines += [{"kind": "test", "id": i, "files": [0], "listings": {}, "probes": {}, "always_rerun": ["subprocess"]}
              for i in ids]
    (trace / "main.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    report.write_text("".join(json.dumps(line) + "\n" for line in (
        [{"kind": "collected", "nodeid": i} for i in ids]
        + [{"kind": "report", "nodeid": i, "when": "call", "outcome": "passed"} for i in ids])))
    sha = "c" * 40
    plan = gate.ReusePlan(store_path, environment, trace, tmp_path / "out", {}, core=core)
    settled = gate.settle_reuse(plan, sha, gate.SuiteRun(0, (), sha, report_path=str(report), hermetic=True))
    assert "8 of 8" in settled.reuse_warning
    assert gate.reuse_fields(settled)["results"]["warning"] == settled.reuse_warning
