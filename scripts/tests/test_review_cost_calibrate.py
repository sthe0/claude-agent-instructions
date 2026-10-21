"""review-cost-calibrate.py: act counting, cycle boundaries, minute booking, p90/min_n and --check."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "review-cost-calibrate.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review_cost_calibrate"
COMMITTED = Path(__file__).resolve().parents[2] / "docs" / "operations" / "review-cost-calibration.json"
spec = importlib.util.spec_from_file_location("review_cost_calibrate", SCRIPT)
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)

SESSION_A = "aaaaaaaa-0000-4000-8000-000000000001"
SESSION_B = "bbbbbbbb-0000-4000-8000-000000000002"
COL = {name: i for i, name in enumerate(cal.ROW_COLUMNS)}


def epoch(text: str) -> float:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@pytest.fixture
def snapshot(tmp_path):
    target = tmp_path / "snap"
    shutil.copytree(FIXTURES, target)
    return target


def edit_state(snapshot: Path, session: str, mutate) -> None:
    path = snapshot / "state" / f"{session}.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def rows_of(snapshot: Path, session: str = SESSION_A) -> list[list]:
    doc = cal.build(snapshot, {})
    return [r for r in doc["rows"] if r[0] == session]


def test_cycle_rows_of_the_fixture_session(snapshot):
    assert rows_of(snapshot) == [
        [SESSION_A, 1, "a1a1a1a1a1a1", "submit", "replan", False, 4, 15.0, "exact"],
        [SESSION_A, 2, "b2b2b2b2b2b2", "replan", "approve", False, 1, 40.0, "exact"],
        [SESSION_A, 3, "d4d4d4d4d4d4", "submit", None, True, 2, 10.0, "exact"],
    ]


def test_acts_count_fresh_pass_and_revise_only(snapshot):
    """Cycle 1 holds an override, a composition record and a carried record besides its four acts."""
    cycles, outside = cal.cut_cycles(json.loads((snapshot / "state" / f"{SESSION_A}.json").read_text())["history"])
    assert [c["acts"] for c in cycles] == [4, 1, 2]
    assert outside == 1


def test_same_sha_resubmit_opens_no_cycle_and_unverified_submit_opens_none(snapshot):
    cycles, _ = cal.cut_cycles(json.loads((snapshot / "state" / f"{SESSION_A}.json").read_text())["history"])
    assert [c["plan_sha"][:2] for c in cycles] == ["a1", "b2", "d4"]


def test_post_approval_replan_is_outside_the_population(snapshot):
    cycles, outside = cal.cut_cycles(json.loads((snapshot / "state" / f"{SESSION_A}.json").read_text())["history"])
    assert "c3c3c3c3c3c3c3c3" not in [c["plan_sha"] for c in cycles]
    assert outside == 1


def test_open_cycle_is_flagged(snapshot):
    cycles, _ = cal.cut_cycles(json.loads((snapshot / "state" / f"{SESSION_A}.json").read_text())["history"])
    assert [c["open"] for c in cycles] == [False, False, True]
    assert cycles[2]["closed_by"] is None


def test_session_without_review_records_is_not_in_the_inventory(snapshot):
    doc = cal.build(snapshot, {})
    assert {i["session"] for i in doc["inventory"]} == {SESSION_A, SESSION_B}


def test_session_without_transcript_has_no_minutes(snapshot):
    assert rows_of(snapshot, SESSION_B) == [
        [SESSION_B, 1, "e5e5e5e5e5e5", "submit", "approve", False, 1, None, "unavailable"]]


def test_gap_is_capped_at_the_wall_clock_minimum(snapshot):
    doc = cal.build(snapshot, {})
    inv = next(i for i in doc["inventory"] if i["session"] == SESSION_A)
    assert inv["reconstructed_active_minutes"] == 65.0
    assert rows_of(snapshot)[1][COL["minutes"]] == 40.0


def test_prompt_landing_while_blocked_books_nothing():
    booked = cal.book_prompts([0.0, 600.0, 1200.0], [], [(500.0, 700.0)])
    assert [m for _, m in booked] == [0.0, 0.0, 10.0]


def test_child_run_time_is_not_booked_twice():
    base = epoch("2026-01-01T10:00:00Z")
    run = {"started": base + 600, "ended": base + 3000}
    booked = cal.book_prompts([base, base + 3600], [run], [])
    assert booked[1][1] == pytest.approx((3600 - 2400) / 60)


def test_blocked_prompt_in_a_session_is_excluded_from_the_rows(snapshot):
    def mutate(data):
        data["history"].insert(1, {"event": "block", "ts": "2026-01-01T11:30:00Z"})
        data["history"].insert(2, {"event": "unblock", "ts": "2026-01-01T11:45:00Z"})
        data["effort_actuals"]["active_minutes"] = 35.0
    edit_state(snapshot, SESSION_A, mutate)
    assert rows_of(snapshot)[1][COL["minutes"]] == 10.0
    assert rows_of(snapshot)[1][COL["minutes_bound"]] == "exact"


def test_child_run_in_a_session_shortens_the_gap(snapshot):
    def mutate(data):
        data["effort_child_runs"] = [{"started": epoch("2026-01-01T10:30:00Z"), "ended": epoch("2026-01-01T11:30:00Z")}]
        data["effort_actuals"]["active_minutes"] = 50.0
    edit_state(snapshot, SESSION_A, mutate)
    assert rows_of(snapshot)[1][COL["minutes"]] == 25.0


def test_disagreement_with_the_booked_total_rescales_and_flags_the_rows(snapshot):
    edit_state(snapshot, SESSION_A, lambda d: d["effort_actuals"].update(active_minutes=130.0))
    rows = rows_of(snapshot)
    assert [r[COL["minutes"]] for r in rows] == [30.0, 80.0, 20.0]
    assert {r[COL["minutes_bound"]] for r in rows} == {"scaled"}
    assert cal.build(snapshot, {})["populations"]["review_cycle_active_minutes"]["minutes_bound"] == "scaled"


def test_transcript_with_fewer_prompts_than_the_state_counted_gives_no_minutes(snapshot):
    edit_state(snapshot, SESSION_A, lambda d: d.update(user_prompt_count=9))
    assert {r[COL["minutes_bound"]] for r in rows_of(snapshot)} == {"unavailable"}


def test_counter_started_late_keeps_only_cycles_inside_the_counted_window(snapshot):
    def mutate(data):
        data["user_prompt_count"] = 4
        data["effort_actuals"]["active_minutes"] = 50.0
    edit_state(snapshot, SESSION_A, mutate)
    rows = rows_of(snapshot)
    assert [(r[COL["minutes"]], r[COL["minutes_bound"]]) for r in rows] == [
        (None, "unavailable"), (40.0, "exact"), (10.0, "exact")]


def test_p90_rule():
    assert cal.p90(list(range(1, 11))) == 9
    assert cal.p90(list(range(0, 11))) == 9
    assert cal.p90([5]) == 5
    assert cal.p90([3, 1, 2]) == 3


def test_candidate_rounds_p90_up():
    assert cal.candidate_of([60.2, 10.0]) == (60.2, 61)


def test_below_min_n_the_rule_is_user_decision_and_waits_for_the_answer(snapshot):
    doc = cal.build(snapshot, {})
    entry = doc["derived"]["review-cycle-acts"]
    assert (entry["n"], entry["rule"], entry["value"]) == (4, "user_decision", None)
    assert entry["p90"] == 4 and entry["candidate"] == 4


def test_decision_fills_the_value_under_user_decision(snapshot):
    doc = cal.build(snapshot, {"review-cycle-acts": 7})
    entry = doc["derived"]["review-cycle-acts"]
    assert (entry["rule"], entry["decision"], entry["value"]) == ("user_decision", 7, 7)


def synthetic_doc(n: int = 25) -> dict:
    rows = [[f"s{i % 5}", i, "0" * 12, "submit", "approve", False, 1 + i % 9, float(10 + i), "exact"]
            for i in range(n)]
    return cal.assemble(rows, [], "x", {})


def run_check(tmp_path: Path, doc: dict) -> int:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc))
    return cal.main(["--check", str(path)])


def test_at_min_n_the_rule_is_formula(tmp_path):
    doc = synthetic_doc(cal.MIN_N)
    assert doc["derived"]["review-cycle-acts"]["rule"] == "formula"
    assert doc["derived"]["review-cycle-acts"]["value"] == doc["derived"]["review-cycle-acts"]["candidate"]
    assert synthetic_doc(cal.MIN_N - 1)["derived"]["review-cycle-acts"]["rule"] == "user_decision"


def test_check_accepts_a_formula_document(tmp_path):
    assert run_check(tmp_path, synthetic_doc()) == 0


def test_check_accepts_a_user_decision_document(tmp_path, snapshot):
    doc = cal.build(snapshot, {"review-cycle-acts": 7, "review-cycle-active-minutes": 30})
    assert run_check(tmp_path, doc) == 0


def test_check_rejects_a_mutated_row(tmp_path):
    doc = synthetic_doc()
    doc["rows"][0][COL["acts"]] += 1
    assert run_check(tmp_path, doc) == 1


def test_check_rejects_a_mutated_p90(tmp_path):
    doc = synthetic_doc()
    doc["derived"]["review-cycle-acts"]["p90"] += 1
    assert run_check(tmp_path, doc) == 1


def test_check_rejects_a_mutated_candidate_or_value(tmp_path):
    for field in ("candidate", "value"):
        doc = synthetic_doc()
        doc["derived"]["review-cycle-active-minutes"][field] += 1
        assert run_check(tmp_path, doc) == 1


def test_check_rejects_user_decision_over_a_population_that_meets_min_n(tmp_path):
    doc = synthetic_doc()
    entry = doc["derived"]["review-cycle-acts"]
    entry.update(rule="user_decision", decision=entry["value"])
    assert run_check(tmp_path, doc) == 1


def test_check_rejects_formula_over_a_population_below_min_n(tmp_path, snapshot):
    doc = cal.build(snapshot, {"review-cycle-acts": 7})
    doc["derived"]["review-cycle-acts"]["rule"] = "formula"
    assert run_check(tmp_path, doc) == 1


def test_check_rejects_a_value_that_departs_from_the_decision(tmp_path, snapshot):
    doc = cal.build(snapshot, {"review-cycle-acts": 7})
    doc["derived"]["review-cycle-acts"]["value"] = 8
    assert run_check(tmp_path, doc) == 1


def test_check_rejects_a_stale_rows_digest(tmp_path):
    doc = synthetic_doc()
    doc["rows_sha256"] = "0" * 64
    assert run_check(tmp_path, doc) == 1


def test_check_rejects_a_mutated_population_or_motivating_entry(tmp_path):
    doc = synthetic_doc()
    doc["populations"]["review_cycle_acts"]["sessions"] += 1
    assert run_check(tmp_path, doc) == 1
    doc = synthetic_doc()
    doc["motivating"]["2283939a"]["cycles"] = 3
    assert run_check(tmp_path, doc) == 1


def test_check_exits_2_on_a_malformed_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert cal.main(["--check", str(path)]) == 2
    doc = synthetic_doc()
    del doc["rows"]
    assert run_check(tmp_path, doc) == 2
    doc = synthetic_doc()
    doc["rows"][0] = doc["rows"][0][:3]
    assert run_check(tmp_path, doc) == 2


def test_check_never_reads_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "load_sessions", lambda *_: pytest.fail("snapshot read by --check"))
    assert run_check(tmp_path, synthetic_doc()) == 0


def test_building_twice_is_reproducible(snapshot):
    assert cal.build(snapshot, {}) == cal.build(snapshot, {})


def test_building_modifies_no_input(snapshot):
    before = {p: p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}
    cal.build(snapshot, {})
    assert before == {p: p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}


def test_committed_calibration_checks_clean_and_is_decided():
    assert cal.main(["--check", str(COMMITTED)]) == 0
    doc = json.loads(COMMITTED.read_text())
    for key, entry in doc["derived"].items():
        assert isinstance(entry["value"], int) and entry["value"] >= 1, key
        assert entry["rule"] == ("formula" if entry["n"] >= doc["min_n"] else "user_decision"), key
