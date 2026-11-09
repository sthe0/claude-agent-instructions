"""The historical causes fixture: the committed rows and counts, the alignment of the hand rows to
the engine histories and its refusal rules (on synthetic inputs), and the importer's loading of the
fixture and `check-causes`. Every test runs against a tmp record and tmp inputs, never the live ones."""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import telemetry as telemetry_cli
from lib import config_root
from lib.telemetry import align, cause, importer, store, tables
from telemetry_import_support import world  # noqa: F401
from telemetry_live_support import query

COMMITTED = align.FIXTURES
PINNED = {"check_authoring_defect": (43, 7), "flaky_or_loaded_verify": (23, 7),
          "spec_shift_or_underspecified": (8, 5), "trunk_drift_landing": (12, 3)}
CYCLE = ("check_authoring_defect", "review_budget_gate_friction", "effort_trigger_overrun", "wrong_premise_environment")
MACHINERY = {"check_authoring_defect": False, "review_budget_gate_friction": True,
             "effort_trigger_overrun": True, "wrong_premise_environment": False}
ONE_ID, OTHER_ID = "cli:accept", "cli:approve"
ANALYSIS_AT = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
START = ANALYSIS_AT - timedelta(days=1)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def committed_rows() -> list:
    return align.read_jsonl(COMMITTED / f"{align.STEM}.jsonl")


def hand_row(i: int) -> dict:
    category = CYCLE[i % 4]
    machinery = MACHINERY[category] != (i == 4)
    return {"i": i, "category": category, "machinery_induced": machinery, "why": "generic"}


def default_text(position: int) -> tuple:
    if position % 3 == 0:
        return f"the {ONE_ID} gate refused", "tighten it"
    if position % 7 == 0:
        return f"{ONE_ID} and {OTHER_ID} both fired", "split them"
    return "nothing named", "retry"


def make_inputs(tmp_path: Path, count: int = align.EXPECTED_COUNT, late: int = 0, texts=None) -> Path:
    """A synthetic input directory: hand file, state snapshot of `count` replans (one session each,
    5 minutes in DIAGNOSING before it) plus `late` replans after the analysis, and premises.json."""
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True)
    texts = texts or default_text
    members = {}
    moments = [START + timedelta(minutes=3 * p) for p in range(count)]
    moments += [ANALYSIS_AT + timedelta(hours=1), ANALYSIS_AT + timedelta(days=2)][:late]
    for p, moment in enumerate(moments):
        ground, task = texts(p)
        members[f"state/s{p:03d}.json"] = {"session_id": f"s{p:03d}", "history": [
            {"ts": iso(moment - timedelta(minutes=5)), "event": "critique", "node": "DIAGNOSING"},
            {"ts": iso(moment), "event": "replan", "node": "PLANNING", "kind": "substantive",
             "functional_ground": ground, "replanning_task": task}]}
    with tarfile.open(inputs / align.SNAPSHOT_FILE, "w") as tar:
        for name, data in members.items():
            raw = json.dumps(data).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    align.write_jsonl(inputs / align.HAND_FILE, [hand_row(i) for i in range(align.EXPECTED_COUNT)])
    (inputs / align.PREMISES_FILE).write_text(json.dumps({
        "causes_sha256": align.sha256_file(inputs / align.HAND_FILE),
        "state_snapshot_sha256": align.sha256_file(inputs / align.SNAPSHOT_FILE),
        "fixture_analysis_at": iso(ANALYSIS_AT),
        "analysis_cutoff": iso(ANALYSIS_AT + timedelta(days=1))}), encoding="utf-8")
    return inputs


@pytest.fixture
def work(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    monkeypatch.setattr(align, "FIXTURES", fixtures)
    return fixtures


def judged_draft(inputs: Path, work: Path, wrong=(), shift: int = 0) -> list:
    """The draft with each position's aligned_category judged: the hand category, except at `wrong`
    positions (a different category) and with the whole sequence moved by `shift`."""
    draft = align.write_draft(inputs)
    hand = align.read_jsonl(inputs / align.HAND_FILE)
    for row in draft:
        p = row["position"]
        row["aligned_category"] = "implementation_defect" if p in wrong else hand[(p - shift) % len(hand)]["category"]
    align.write_jsonl(work / f"{align.STEM}.alignment.jsonl", draft)
    return draft


def accepted_rows(tmp_path, work, wrong=(5, 40, 90, 150, 200)) -> list:
    inputs = make_inputs(tmp_path)
    draft = judged_draft(inputs, work, wrong=set(wrong))
    return align.build_fixture(inputs, draft)


def refusal_of(tmp_path, work, wrong=(), shift: int = 0, **kwargs) -> "str | None":
    inputs = make_inputs(tmp_path, **kwargs)
    draft = judged_draft(inputs, work, wrong=set(wrong), shift=shift)
    return align.build_fixture(inputs, draft)[0]["alignment_refusal"]


def import_fixture(world) -> dict:
    return world.run()


def fixture_rows_in_record() -> list:
    return query("SELECT * FROM causes WHERE provenance = ? ORDER BY fixture_row", align.SOURCE)


# --- the committed fixture ------------------------------------------------------------------

def test_all_235_fixture_rows_pass_the_critique_validator():
    rows = committed_rows()
    assert len(rows) == 235
    for row in rows:
        stored, violated = cause.complete_cause(
            {"cause_category": row["category"], "machinery_induced": row["machinery_induced"],
             "mechanism": row["mechanism"], "functional_place": row["functional_place"]}, imported=True)
        assert violated == [] and stored is not None, (row["i"], violated)


def test_unknown_accepted_only_on_imported_rows():
    fields = {"cause_category": "review_budget_gate_friction", "machinery_induced": True,
              "mechanism": "unknown", "functional_place": "plan_review"}
    assert cause.validate_cause(fields, imported=True) == []
    assert cause.validate_cause(fields, imported=False)
    assert cause.validate_cause({**fields, "functional_place": "none"}, imported=True)


def test_per_row_overrides_preserve_hand_classification():
    defaults = align.load_defaults()["categories"]
    rows = committed_rows()
    assert [r["i"] for r in rows] == list(range(235))
    for row in rows:
        base = defaults[row["category"]]["machinery_induced"]
        assert row["machinery_override"] == (row["machinery_induced"] != base), row["i"]
    counts = json.loads((COMMITTED / f"{align.STEM}.counts.json").read_text(encoding="utf-8"))
    for category, slot in counts["categories"].items():
        mine = [r for r in rows if r["category"] == category]
        assert (sum(1 for r in mine if not r["machinery_induced"]), sum(1 for r in mine if r["machinery_induced"])) == (
            slot["false"], slot["true"])
        majority = slot["true"] > slot["false"]
        assert defaults[category]["machinery_induced"] == majority


def test_hand_values_match_committed_counts():
    counts = json.loads((COMMITTED / f"{align.STEM}.counts.json").read_text(encoding="utf-8"))
    assert counts["total"] == 235 and len(counts["causes_sha256"]) == 64
    for category, (false, true) in PINNED.items():
        assert counts["categories"][category] == {"false": false, "true": true}
    assert sum(s["false"] + s["true"] for s in counts["categories"].values()) == 235
    rows = committed_rows()
    for category, slot in counts["categories"].items():
        assert sum(1 for r in rows if r["category"] == category and r["machinery_induced"]) == slot["true"]


def test_every_row_carries_mechanism_and_place_basis():
    for row in committed_rows():
        assert row["mechanism_basis"] in (align.OBSERVED, align.DEFAULTED)
        assert row["place_basis"] in (align.OBSERVED, align.DEFAULTED)
        assert (row["mechanism_basis"] == align.OBSERVED) == (row["mechanism"] not in (align.UNKNOWN, align.NONE))


def test_zero_observed_mechanisms_require_a_refusal_reason():
    report = json.loads((COMMITTED / f"{align.STEM}.alignment.json").read_text(encoding="utf-8"))
    if report["observed_mechanism_share"] == 0:
        assert report["refusal_reason"]
    rows = [{**r, "mechanism_basis": align.DEFAULTED, "alignment_refusal": None, "alignment": None}
            for r in committed_rows()]
    assert align.report_of(rows)["refusal_reason"]


def test_alignment_draft_holds_no_hand_category(tmp_path, work):
    inputs = make_inputs(tmp_path)
    for rows in (align.write_draft(inputs), align.read_jsonl(COMMITTED / f"{align.STEM}.alignment.jsonl")):
        assert rows
        for row in rows:
            assert set(row) == set(align.DRAFT_FIELDS)
            assert "category" not in row and "text" not in row and "why" not in row
    assert all(r["aligned_category"] is None for r in align.read_jsonl(COMMITTED / f"{align.STEM}.alignment.jsonl"))


def test_alignment_report_equals_fixture_counts(tmp_path, work):
    inputs = make_inputs(tmp_path)
    judged_draft(inputs, work, wrong={5, 40, 90, 150, 200})
    align.run_align(inputs)
    report = align.write_report()
    rows = align.read_jsonl(work / f"{align.STEM}.jsonl")
    assert report["rows"] == 235 and report["count_found"] == 235
    assert report["aligned_share"] == round(sum(1 for r in rows if r["alignment"]) / 235, 4) == round(230 / 235, 4)
    assert report["observed_mechanism_share"] == round(sum(1 for r in rows if r["mechanism_basis"] == "observed") / 235, 4)
    assert report["disagreeing_positions"] == [5, 40, 90, 150, 200]
    assert json.loads((work / f"{align.STEM}.alignment.json").read_text(encoding="utf-8")) == report


def test_alignment_report_writes_offset_shares(tmp_path, work):
    inputs = make_inputs(tmp_path)
    judged_draft(inputs, work, wrong={5, 40, 90, 150, 200})
    align.run_align(inputs)
    report = align.write_report()
    assert set(report["offset_shares"]) == {"-1", "0", "1"}
    assert report["offset_shares"]["0"] == round(5 / 235, 4)
    assert report["offset_shares"]["-1"] == report["offset_shares"]["1"] == 1.0
    hand = [r["category"] for r in align.read_jsonl(work / f"{align.STEM}.jsonl")]
    aligned = [r["aligned_category"] for r in align.read_jsonl(work / f"{align.STEM}.jsonl")]
    assert report["offset_shares"] == align.offset_shares(aligned, hand)


# --- the alignment and its refusals -----------------------------------------------------------

def test_mapping_rules_null_alignment_and_place_derivation(tmp_path, work):
    rows = accepted_rows(tmp_path, work)
    defaults = align.load_defaults()
    by_i = {r["i"]: r for r in rows}
    unaligned = by_i[5]
    assert unaligned["alignment"] is None and unaligned["interval_minutes"] is None and unaligned["interval_unknown"]
    assert unaligned["mechanism_basis"] == unaligned["place_basis"] == align.DEFAULTED
    for position in (1, 2, 6):
        row = by_i[position]
        assert row["alignment"] == {"session": f"s{position:03d}", "difficulty_index": 0}
        assert row["interval_minutes"] == 5.0 and not row["interval_unknown"]
    observed = by_i[9]
    assert observed["mechanism"] == ONE_ID and observed["mechanism_basis"] == align.OBSERVED
    registry = tables.load("registry")
    assert observed["functional_place"] == registry.members[ONE_ID].attrs["place"]
    assert observed["functional_place"] != defaults["categories"][observed["category"]]["place"]
    assert by_i[1]["alignment"]["session"] == "s001", "each row is aligned to the replan at its own position"


def test_mechanism_assigned_only_on_a_single_registry_match(tmp_path, work):
    rows = {r["i"]: r for r in accepted_rows(tmp_path, work)}
    defaults = align.load_defaults()["categories"]
    assert rows[9]["mechanism"] == ONE_ID
    two_ids = rows[14]
    assert two_ids["machinery_induced"] and two_ids["alignment"] is not None
    assert two_ids["mechanism"] == align.UNKNOWN and two_ids["mechanism_basis"] == align.DEFAULTED
    assert two_ids["functional_place"] == defaults[two_ids["category"]]["place"]
    none_named = rows[1]
    assert none_named["mechanism"] == align.UNKNOWN and none_named["mechanism_basis"] == align.DEFAULTED
    unaligned_naming_one = rows[150]
    assert unaligned_naming_one["alignment"] is None and unaligned_naming_one["machinery_induced"] is True
    assert unaligned_naming_one["mechanism"] == align.UNKNOWN
    assert unaligned_naming_one["mechanism_basis"] == align.DEFAULTED
    refused_named = {r["i"]: r for r in accepted_rows(tmp_path / "again", work, wrong=(9,))}[9]
    assert refused_named["alignment"] is None and refused_named["mechanism"] == align.UNKNOWN
    assert refused_named["mechanism_basis"] == align.DEFAULTED


def test_non_machinery_rows_get_place_none(tmp_path, work):
    rows = accepted_rows(tmp_path, work)
    quiet = [r for r in rows if not r["machinery_induced"]]
    assert quiet
    for row in quiet:
        assert row["mechanism"] == align.NONE and row["functional_place"] == "none"
        assert row["mechanism_basis"] == align.DEFAULTED


def test_alignment_refused_when_the_replan_count_differs_from_235(tmp_path, work):
    inputs = make_inputs(tmp_path, count=234)
    draft = align.write_draft(inputs)
    assert len(draft) == 234
    rows = align.build_fixture(inputs, draft)
    assert len(rows) == 235
    assert all(r["alignment"] is None and r["mechanism_basis"] == align.DEFAULTED for r in rows)
    assert "234" in rows[0]["alignment_refusal"] and all(r["replans_found"] == 234 for r in rows)


def test_alignment_refused_on_a_snapshot_hash_mismatch(tmp_path, work, capsys):
    inputs = make_inputs(tmp_path)
    premises = json.loads((inputs / align.PREMISES_FILE).read_text(encoding="utf-8"))
    premises["state_snapshot_sha256"] = "0" * 64
    (inputs / align.PREMISES_FILE).write_text(json.dumps(premises), encoding="utf-8")
    with pytest.raises(align.AlignmentError):
        align.enumerate_replans(inputs)
    assert telemetry_cli.main(["align-causes", "--inputs", str(inputs), "--draft"]) == 1
    assert "sha256" in capsys.readouterr().err


def test_alignment_ignores_replans_after_the_cutoff(tmp_path, work):
    inputs = make_inputs(tmp_path, late=2)
    replans = align.enumerate_replans(inputs)
    assert len(replans) == 235
    assert max(r["at_ms"] for r in replans) < int(ANALYSIS_AT.timestamp() * 1000)
    assert [r["session"] for r in replans] == [f"s{p:03d}" for p in range(235)]


def test_alignment_refused_when_disagreement_share_exceeds_ten_percent(tmp_path, work):
    wrong = set(range(3, 235, 9))
    assert 0.1 < len(wrong) / 235
    reason = refusal_of(tmp_path, work, wrong=wrong)
    assert reason and "share" in reason
    assert refusal_of(tmp_path / "ok", work, wrong={5, 40, 90, 150, 200}) is None


def test_alignment_refused_on_a_shift_by_one(tmp_path, work):
    reason = refusal_of(tmp_path, work, shift=1)
    assert reason and "share" in reason
    assert refusal_of(tmp_path / "unshifted", work, shift=0) is None


def test_alignment_refused_on_a_contiguous_run_of_six_disagreements(tmp_path, work):
    reason = refusal_of(tmp_path, work, wrong=set(range(100, 106)))
    assert reason and "consecutive" in reason
    assert refusal_of(tmp_path / "five", work, wrong=set(range(100, 105))) is None


def test_alignment_refused_when_offset_shares_not_above_offset_zero(tmp_path, work):
    inputs = make_inputs(tmp_path)
    hand = align.read_jsonl(inputs / align.HAND_FILE)
    constant = [{**h, "category": "check_authoring_defect"} for h in hand]
    align.write_jsonl(inputs / align.HAND_FILE, constant)
    premises = json.loads((inputs / align.PREMISES_FILE).read_text(encoding="utf-8"))
    premises["causes_sha256"] = align.sha256_file(inputs / align.HAND_FILE)
    (inputs / align.PREMISES_FILE).write_text(json.dumps(premises), encoding="utf-8")
    draft = align.write_draft(inputs)
    for row in draft:
        row["aligned_category"] = "implementation_defect" if row["position"] in (5, 40, 90) else "check_authoring_defect"
    reason = align.build_fixture(inputs, draft)[0]["alignment_refusal"]
    assert reason and "offset" in reason


# --- loading and checking ---------------------------------------------------------------------

def test_migration_adds_the_fixture_cause_columns():
    conn = store.open_record()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(causes)")}
        assert 5 in store.applied_migrations(conn)
    finally:
        conn.close()
    assert {"imported", "provenance", "fixture_sha256", "fixture_row", "mechanism_basis", "place_basis",
            "alignment_session", "alignment_difficulty_index", "interval_minutes", "interval_unknown"} <= columns


def test_fixture_load_is_idempotent(world):
    first = import_fixture(world)
    assert first["per_source"]["fixture_causes"]["rows"] == 235
    assert len(fixture_rows_in_record()) == 235
    second = import_fixture(world)
    assert second["per_source"].get("fixture_causes", {"rows": 0})["rows"] == 0
    assert len(fixture_rows_in_record()) == 235


def test_fixture_replayed_through_telemetry_import_loads_235_rows(world, capsys):
    assert telemetry_cli.main(["import"]) == 0
    stats = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert stats["per_source"]["fixture_causes"]["rows"] == 235
    rows = fixture_rows_in_record()
    assert len(rows) == 235 and all(r["imported"] == 1 and r["origin"] == store.origin_id() for r in rows)
    counts = json.loads((COMMITTED / f"{align.STEM}.counts.json").read_text(encoding="utf-8"))
    for category, slot in counts["categories"].items():
        mine = [r for r in rows if r["cause_category"] == category]
        assert (sum(1 for r in mine if not r["machinery_induced"]), sum(1 for r in mine if r["machinery_induced"])) == (
            slot["false"], slot["true"])


def test_check_causes_counts_and_validates(world, capsys):
    import_fixture(world)
    capsys.readouterr()
    assert telemetry_cli.main(["check-causes", "--imported", "--expect", "235"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 235
    assert telemetry_cli.main(["check-causes", "--imported", "--expect", "234"]) == 1
    capsys.readouterr()
    conn = sqlite3.connect(str(config_root.telemetry_db()))
    try:
        conn.execute("UPDATE causes SET machinery_induced = 1, mechanism = 'unknown', functional_place = 'none' "
                     "WHERE fixture_row = 3 AND provenance = ?", (align.SOURCE,))
        conn.commit()
    finally:
        conn.close()
    assert telemetry_cli.main(["check-causes", "--imported", "--expect", "235"]) == 1
    assert json.loads(capsys.readouterr().out)["invalid"] == 1
    with pytest.raises(SystemExit) as usage:
        telemetry_cli.main(["check-causes", "--imported"])
    assert usage.value.code == 2


def test_check_causes_counts_only_fixture_provenance(world, capsys):
    import_fixture(world)
    capsys.readouterr()
    assert align.SOURCE == "fixture:replan_causes_2026_11_03"
    conn = sqlite3.connect(str(config_root.telemetry_db()))
    try:
        conn.execute(
            "INSERT INTO causes (cause_id, session, cause_category, cause_category_table_version, machinery_induced, "
            "mechanism, mechanism_table_version, functional_place, functional_place_table_version, origin, imported, provenance) "
            "VALUES ('x', 's', 'implementation_defect', 1, 0, 'none', 1, 'none', 1, 'o', 1, 'imported')")
        conn.commit()
    finally:
        conn.close()
    assert telemetry_cli.main(["check-causes", "--imported", "--expect", "235"]) == 0
    assert {r["provenance"] for r in query("SELECT DISTINCT provenance FROM causes WHERE imported = 1")} == {
        align.SOURCE, "imported"}


def test_fixture_ids_from_content_sha256_and_row_index(world, tmp_path, monkeypatch):
    import_fixture(world)
    digest = align.sha256_file(COMMITTED / f"{align.STEM}.jsonl")
    ids = [r["cause_id"] for r in fixture_rows_in_record()]
    assert ids == [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{digest}:{i}")) for i in range(235)]
    copy = tmp_path / "elsewhere"
    copy.mkdir()
    (copy / f"{align.STEM}.jsonl").write_bytes((COMMITTED / f"{align.STEM}.jsonl").read_bytes())
    monkeypatch.setattr(align, "FIXTURES", copy)
    assert importer.fixture_cause_id(align.sha256_file(copy / f"{align.STEM}.jsonl"), 7) == ids[7]
    assert len(set(ids)) == 235


def test_loading_a_revised_fixture_replaces_the_235_rows(world, tmp_path, monkeypatch):
    copy = tmp_path / "revised"
    copy.mkdir()
    rows = committed_rows()
    align.write_jsonl(copy / f"{align.STEM}.jsonl", rows)
    monkeypatch.setattr(align, "FIXTURES", copy)
    import_fixture(world)
    before = {r["cause_id"] for r in fixture_rows_in_record()}
    assert len(before) == 235
    rows[0]["interval_minutes"] = 3.5
    rows[0]["interval_unknown"] = False
    align.write_jsonl(copy / f"{align.STEM}.jsonl", rows)
    import_fixture(world)
    after = {r["cause_id"] for r in fixture_rows_in_record()}
    assert len(after) == 235 and not (before & after)
    assert len(query("SELECT 1 FROM causes WHERE provenance = ?", align.SOURCE)) == 235
