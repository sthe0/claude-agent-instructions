"""The baseline-free R1 mode of r1_control.py and the guard that keeps an aborted run off a stored baseline.

No test here runs pytest under the recorder: run_plain and head are replaced, and the state
directory is a tmp dir, so the real ~/.local/state/disk-guard/r1 is never read or written.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
CANARY = "scripts/tests/canary_arc_launcher.py::test_launcher_run_downloads_nothing"
CANARY_RECORD = {"args": "--version", "pid": "1", "test": CANARY + " (call)"}


@pytest.fixture
def r1(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("r1_control_under_test", HERE / "r1_control.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    heads: list[Path] = []

    def fake_head(tree, ref="HEAD"):
        heads.append(Path(tree))
        return "f" * 40

    monkeypatch.setattr(module, "head", fake_head)
    module.heads = heads
    return module


def a_run(outcomes=None, aborted="", records=(CANARY_RECORD,), arc_paths=()):
    return {"label": "x", "exit": 0 if not aborted else -9, "aborted": aborted, "wall_s": 12.0,
            "launcher_records": list(records), "arc_paths": list(arc_paths),
            "outcomes": {CANARY: "passed", "scripts/tests/test_a.py::test_one": "passed"} if outcomes is None
            else outcomes, "inner": {}}


def stub_runs(r1, monkeypatch, run):
    calls: list[dict] = []

    def fake_run_plain(tree, label, canary, select=None, start_free_gb=r1.START_FREE_GB):
        calls.append({"tree": Path(tree), "canary": canary, "select": select, "start_free_gb": start_free_gb})
        return copy.deepcopy(run)

    monkeypatch.setattr(r1, "run_plain", fake_run_plain)
    return calls


def write_report(path: Path, plain: dict, **extra_entries) -> Path:
    path.write_text(json.dumps({"schema": 1, "commit": "f" * 40, "canary_id": CANARY,
                                "entries": {"plain": plain, **extra_entries}}))
    return path


def good_section(**changes) -> dict:
    sec = {"baseline": "none", "wall_s": 12.0, "exit": 0, "aborted": "", "launcher_records": [CANARY_RECORD],
           "arc_paths": [], "total_ids": 2, "canary_outcome": "passed"}
    sec.update(changes)
    return sec


# ── cmd_baseline: an aborted or empty run never replaces a stored baseline ──

STORED = {"plain": {"commit": "c" * 40, "ids": {"scripts/tests/test_a.py::test_one": "passed"}},
          "gate": {"commit": "c" * 40, "ids": {"scripts/tests/test_b.py::test_two": "failed"}}}


@pytest.mark.parametrize("run", [a_run(aborted="free space under 15 GB"), a_run(outcomes={})],
                         ids=["aborted", "no-outcomes"])
def test_a_failed_baseline_run_keeps_the_stored_entry(r1, monkeypatch, tmp_path, run):
    (r1.STATE).mkdir(parents=True)
    store = r1.STATE / "baseline-ids.json"
    store.write_text(json.dumps(STORED))
    before = store.read_bytes()
    stub_runs(r1, monkeypatch, run)
    assert r1.main(["baseline", "--entry", "plain", "--tree", str(tmp_path)]) == 1
    assert store.read_bytes() == before
    aborted = json.loads((r1.STATE / "baseline-ids.aborted.json").read_text())
    assert aborted["plain"]["aborted"] == run["aborted"] and aborted["plain"]["ids"] == run["outcomes"]


def test_a_finished_baseline_run_replaces_only_its_own_entry(r1, monkeypatch, tmp_path):
    (r1.STATE).mkdir(parents=True)
    (r1.STATE / "baseline-ids.json").write_text(json.dumps(STORED))
    stub_runs(r1, monkeypatch, a_run())
    assert r1.main(["baseline", "--entry", "plain", "--tree", str(tmp_path)]) == 0
    store = json.loads((r1.STATE / "baseline-ids.json").read_text())
    assert store["plain"]["ids"][CANARY] == "passed"
    assert store["gate"] == STORED["gate"]
    assert not (r1.STATE / "baseline-ids.aborted.json").exists()
    assert not list(r1.STATE.glob("*.tmp"))


# ── control --no-baseline ──

def test_control_without_baseline_writes_its_own_report_and_never_reads_the_base_tree(r1, monkeypatch):
    calls = stub_runs(r1, monkeypatch, a_run())
    assert r1.main(["control", "--no-baseline"]) == 0
    assert calls == [{"tree": r1.REPO, "canary": True, "select": None,
                      "start_free_gb": r1.NO_BASELINE_START_FREE_GB}]
    assert r1.NO_BASELINE_START_FREE_GB < r1.START_FREE_GB
    assert r1.BASE_TREE not in r1.heads
    assert not (r1.STATE / "report.json").exists()
    assert not (r1.STATE / "baseline-ids.json").exists()
    report = json.loads((r1.STATE / "report-r1only.json").read_text())
    assert set(report["entries"]) == {"plain"}
    plain = report["entries"]["plain"]
    assert plain["baseline"] == "none" and plain["canary_outcome"] == "passed"
    assert "new_failures" not in plain and "flaky" not in plain


def test_control_without_baseline_report_is_accepted_by_r1_only_verify(r1, monkeypatch, capsys):
    stub_runs(r1, monkeypatch, a_run())
    r1.main(["control", "--no-baseline"])
    assert r1.main(["verify", "--r1-only", "--report", str(r1.STATE / "report-r1only.json")]) == 0
    assert "ACCEPTED:" in capsys.readouterr().out


def test_an_aborted_baseline_free_run_is_written_and_then_refused(r1, monkeypatch, capsys):
    stub_runs(r1, monkeypatch, a_run(aborted="free space under 15 GB"))
    assert r1.main(["control", "--no-baseline"]) == 0
    assert r1.main(["verify", "--r1-only", "--report", str(r1.STATE / "report-r1only.json")]) == 1
    assert "run aborted" in capsys.readouterr().out


def test_control_without_baseline_refuses_deletion_reasons(r1, monkeypatch):
    calls = stub_runs(r1, monkeypatch, a_run())
    assert r1.main(["control", "--no-baseline", "--deleted", "a::b=gone"]) == 2
    assert calls == []


def test_failed_baseline_runs_of_both_entries_are_both_kept(r1, monkeypatch, tmp_path):
    stub_runs(r1, monkeypatch, a_run(aborted="over 3460 s"))
    monkeypatch.setattr(r1, "run_gate", lambda mode, tree, tip: a_run(outcomes={}))
    assert r1.main(["baseline", "--entry", "plain", "--tree", str(tmp_path)]) == 1
    assert r1.main(["baseline", "--entry", "gate", "--tree", str(tmp_path)]) == 1
    failed = json.loads((r1.STATE / "baseline-ids.aborted.json").read_text())
    assert failed["plain"]["aborted"] == "over 3460 s" and failed["gate"]["ids"] == {}


def test_plain_verify_refuses_a_section_without_diff_fields(r1, tmp_path, capsys):
    plain = good_section()
    del plain["baseline"]
    report = write_report(tmp_path / "r.json", plain, gate=dict(plain, wall_s=100.0))
    assert r1.main(["verify", "--report", str(report)]) == 1
    assert "malformed" in capsys.readouterr().out


def test_control_with_baseline_still_requires_one(r1, monkeypatch):
    stub_runs(r1, monkeypatch, a_run())
    assert r1.main(["control"]) == 2


# ── verify --r1-only ──

def test_r1_only_verify_accepts_a_clean_baseline_free_report(r1, tmp_path):
    assert r1.main(["verify", "--r1-only", "--report", str(write_report(tmp_path / "r.json", good_section()))]) == 0


@pytest.mark.parametrize("change, reason", [
    ({"launcher_records": [CANARY_RECORD, {"args": "root", "pid": "2",
                                           "test": "scripts/tests/test_ledger_stamp.py::test_x (call)"}]},
     "not made by the canary"),
    ({"launcher_records": []}, "no launcher record"),
    ({"canary_outcome": "failed"}, "canary outcome"),
    ({"aborted": "over 3460 s", "exit": -9}, "run aborted"),
    ({"exit": None}, "run aborted"),
    ({"arc_paths": [{"path": "tmp/r1-x/home/.arc", "bytes": 10}]}, ".arc left"),
    ({"baseline": "diffed"}, "carries a baseline diff"),
], ids=["foreign-record", "no-record", "canary-failed", "aborted", "no-exit", "stray-arc", "has-baseline"])
def test_r1_only_verify_refuses_each_way_r1_fails(r1, tmp_path, capsys, change, reason):
    report = write_report(tmp_path / "r.json", good_section(**change))
    assert r1.main(["verify", "--r1-only", "--report", str(report)]) == 1
    assert reason in capsys.readouterr().out


def test_r1_only_verify_refuses_a_report_with_a_gate_entry(r1, tmp_path, capsys):
    report = write_report(tmp_path / "r.json", good_section(), gate=good_section())
    assert r1.main(["verify", "--r1-only", "--report", str(report)]) == 1
    assert "want plain only" in capsys.readouterr().out


def test_r1_only_negative_control_fixture_is_refused(r1, capsys):
    assert r1.main(["verify", "--r1-only", "--report", str(FIXTURES / "r1_only_bad_report.json")]) == 1
    assert "test_ledger_stamp.py" in capsys.readouterr().out


# ── plain verify keeps its old meaning ──

def test_plain_verify_refuses_a_baseline_free_section(r1, tmp_path, capsys):
    plain = good_section(new_failures=[], new_skips=[], lost_ids=[], now_passing=[], flaky=[])
    gate = dict(plain, baseline="diffed", wall_s=100.0)
    report = write_report(tmp_path / "r.json", plain, gate=gate)
    assert r1.main(["verify", "--report", str(report)]) == 1
    assert "only `verify --r1-only` judges it" in capsys.readouterr().out


def test_old_negative_control_fixture_is_still_refused(r1):
    assert r1.main(["verify", "--report", str(FIXTURES / "r1_bad_report.json")]) == 1
