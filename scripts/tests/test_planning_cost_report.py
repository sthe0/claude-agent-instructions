"""Tests for scripts/planning-cost-report.py: rows are hashed, aggregates re-derive from the rows alone."""
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "planning-cost-report.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "planning_cost_report"
BASELINE = SCRIPTS.parent / "docs" / "operations" / "planning-cost-baseline.json"

_spec = importlib.util.spec_from_file_location("planning_cost_report", SCRIPT)
pcr = importlib.util.module_from_spec(_spec)
sys.modules["planning_cost_report"] = pcr
_spec.loader.exec_module(pcr)


def build(snapshot=FIXTURE):
    rows, roles, axis, inputs_sha = pcr.build_tables(snapshot)
    return pcr.assemble(rows, roles, axis, inputs_sha)


def run_check(doc, tmp_path, *extra):
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), "--check", str(path), *extra],
                          capture_output=True, text=True)


def retamper_rows(doc):
    doc["rows_sha256"] = pcr.digest(doc["rows"])


def test_document_carries_every_named_field():
    doc = build()
    for key in ("formula", "window", "inputs_sha256", "row_columns", "rows", "rows_sha256", "role_columns",
                "role_rows", "role_rows_sha256", "axis_columns", "axis_rows", "axis_rows_sha256", "totals",
                "axes", "counterfactual", "dossier_comparison", "notes"):
        assert key in doc
    assert any("5 percentage points" in n for n in doc["notes"])


def test_fixture_rows_cut_at_first_approve():
    doc = build()
    cols = doc["row_columns"]
    by_day = {r[cols.index("first_review_day")]: r for r in doc["rows"]}
    assert sorted(by_day) == ["2026-10-10", "2026-10-15"]  # the September session is outside the window
    approved = dict(zip(cols, by_day["2026-10-10"]))
    assert (approved["review_acts"], approved["acts_to_cut"], approved["acts_after_cut"]) == (3, 2, 1)
    assert approved["plan_versions"] == 2 and approved["pair_review_events"] == 2 and approved["approved"] is True
    never = dict(zip(cols, by_day["2026-10-15"]))
    assert never["review_acts"] == 1 and never["acts_to_cut"] is None and never["approved"] is False


def test_fixture_totals_roles_and_axes():
    doc = build()
    totals = doc["totals"]
    assert totals["sessions"] == 2 and totals["sessions_approved"] == 1 and totals["sessions_never_approved"] == 1
    assert totals["review_acts"] == 4 and totals["acts_to_cut"] == 2 and totals["acts_after_cut"] == 1
    assert totals["spend_usd_by_role"] == {"developer": 2.0, "thinker": 2.0}
    assert totals["hours_by_role"] == {"developer": 1.0, "thinker": 1.0}
    axes = doc["axes"]
    assert axes["concerns"] == 5 and axes["builds"] == 1 and axes["builds_pct"] == 20.0
    assert doc["counterfactual"]["acts_if_only_building_concerns_block"] == 0.4


def test_check_passes_on_fresh_build_and_against_snapshot(tmp_path):
    doc = build()
    assert run_check(doc, tmp_path).returncode == 0
    assert run_check(doc, tmp_path, "--inputs", str(FIXTURE)).returncode == 0


def test_tampered_row_with_recomputed_hash_fails_totals(tmp_path):
    doc = build()
    doc["rows"][0][doc["row_columns"].index("review_acts")] += 1
    retamper_rows(doc)
    result = run_check(doc, tmp_path)
    assert result.returncode == 1
    assert result.stdout.startswith("MISMATCH: totals")


def test_stale_hash_is_reported(tmp_path):
    doc = build()
    doc["rows"][0][doc["row_columns"].index("review_acts")] += 1
    result = run_check(doc, tmp_path)
    assert result.returncode == 1
    assert "MISMATCH: rows_sha256" in result.stdout


def test_snapshot_drift_is_reported(tmp_path):
    doc = build()
    doc["rows"][0][doc["row_columns"].index("pair_review_events")] += 1
    retamper_rows(doc)
    result = run_check(doc, tmp_path, "--inputs", str(FIXTURE))
    assert result.returncode == 1
    assert "MISMATCH: rows differ from the snapshot" in result.stdout


def test_unexplained_out_of_tolerance_comparison_fails(tmp_path):
    doc = build()
    out_of_tolerance = [c for c in doc["dossier_comparison"] if c["within"] is False]
    assert out_of_tolerance, "the fixture is far from the dossier figures, so some claim must be out of tolerance"
    explained = copy.deepcopy(doc)
    assert run_check(explained, tmp_path).returncode == 0
    for c in doc["dossier_comparison"]:
        if c["within"] is False:
            c["explanation"] = ""
    result = run_check(doc, tmp_path)
    assert result.returncode == 1
    assert "unexplained" in result.stdout or "MISMATCH: dossier_comparison" in result.stdout


def test_malformed_document_exits_2(tmp_path):
    doc = build()
    del doc["rows"]
    assert run_check(doc, tmp_path).returncode == 2


@pytest.mark.skipif(not BASELINE.is_file(), reason="baseline not generated")
def test_committed_baseline_rederives_from_its_rows(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", str(BASELINE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(not BASELINE.is_file(), reason="baseline not generated")
def test_committed_baseline_tamper_fails_totals(tmp_path):
    doc = json.loads(BASELINE.read_text(encoding="utf-8"))
    doc["rows"][0][doc["row_columns"].index("review_acts")] += 1
    retamper_rows(doc)
    result = run_check(doc, tmp_path)
    assert result.returncode == 1
    assert result.stdout.startswith("MISMATCH: totals")
