"""Structure and existence checks of the per-stage effort rule registry (effort_per_stage_rules.toml).

test_registry_structure checks the registry's shape and its coverage of the order's rules, order tests and
assumptions. test_rules_of_stage_have_tests[stageN] checks that every row of stage N names a test that exists in
the stage's per-stage test file; it stays red until that stage lands.
"""

import ast
import copy
import os
import re
import tomllib
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent
DEFAULT_REGISTRY = TESTS_DIR / "effort_per_stage_rules.toml"
REGISTRY_PATH = Path(os.environ.get("EFFORT_RULES_REGISTRY") or DEFAULT_REGISTRY)

ORDER_IDS = ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "K1", "K2", "K3", "K4"]
ASSUMPTION_IDS = [f"A{n}" for n in range(1, 29)]
KNOWN_IDS = set(ORDER_IDS) | set(ASSUMPTION_IDS)
ORDER_TESTS = set(range(1, 15))
SCALES = {"spend", "wall_clock", "both", "replans"}
STAGE_FILES = {
    3: ["test_effort_segments.py", "test_effort_per_stage_judging.py"],
    4: [
        "test_effort_armings_approvals.py",
        "test_effort_planning_segment.py",
        "test_effort_disclosure_migration.py",
    ],
    5: ["test_effort_per_stage_docs.py"],
}
ALL_STAGE_FILES = {f for files in STAGE_FILES.values() for f in files}
STAGE_ORDER_TESTS = {3: {1, 2, 3, 8, 9, 10, 11}, 4: {3, 4, 5, 6, 7, 9, 12, 13}, 5: {14}}
REQUIRED_MECHANICS = {
    3: {
        "two_session_carry", "reindexing", "late_row_retier_update", "migration_armings", "migration_seed",
        "closed_arming_late_booking", "pre_switch_booking_owner", "stamp_time_bound",
        "bounded_refresh_books_row_once", "migrated_stamp_leaves_rows", "window_arming_blocks_stage_armings",
        "v61_open_fire_carrier",
    },
    4: {
        "unfold_budget_lazy_parent", "unfold_budget_unmarked_parent", "unfold_past_new_fire_point",
        "unfolded_parent_rearm_budget", "unfolded_parent_line", "planner_run_end_planning_act",
        "planner_run_end_deciding_act", "booking_before_judging_planning_acts",
        "booking_before_judging_subplan_approve", "booking_before_closing", "planning_stamp_time_bound",
        "closing_arming_on_drop", "migrated_planning_arming_budget", "switch_closes_window_arming",
        "pre_answer_figures", "cycle_minutes_route_kept_under_window", "resolve_books_before_quality_row",
        "pre_switch_no_line", "carried_line_versus_total",
    },
    5: {
        *(f"readme_label:{label}" for label in (
            "Segments", "Armings", "Judging acts", "Spawned planner run end", "Planning fires", "Settlement",
            "Floor", "Open-fire gate", "Unfold", "Disclosure", "Migration",
        )),
        "rationale_anchor:#planning-fire-point-wall-clock",
        "rationale_anchor:#floor-not-binding-planning-spend",
        "rationale_anchor:#floor-keys-not-following-tier-keys",
    },
}
NODE_ID = re.compile(r"^scripts/tests/(?P<file>[A-Za-z0-9_]+\.py)::(?P<name>test_[A-Za-z0-9_]+)$")
RETIRES = re.compile(r"^scripts/tests/(?P<file>test_[A-Za-z0-9_]+\.py)::test_[A-Za-z0-9_]+$")
ROW_FIELDS = {
    "id", "sentence", "sources", "order_test", "stage", "test", "scales", "assumption_backed", "retires",
    "mechanic", "realizes",
}


def load_registry(path=REGISTRY_PATH):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _is_mechanic(row):
    return "mechanic" in row


def structure_problems(registry):
    """Return the list of structural problems of a registry; an empty list means it is well-formed."""
    problems = []
    rows = registry.get("rows", [])
    unused = registry.get("unused_assumptions", [])
    exempt = registry.get("exempt_sources", [])
    if not rows:
        problems.append("registry has no rows")

    seen = set()
    for row in rows:
        rid = row.get("id", "<no id>")
        if rid in seen:
            problems.append(f"duplicate row id {rid}")
        seen.add(rid)
        for key in sorted(set(row) - ROW_FIELDS):
            problems.append(f"{rid}: unknown field {key}")
        required = ("id", "sentence", "stage", "test") if _is_mechanic(row) else ("id", "sentence", "stage", "test", "scales")
        for key in required:
            if key not in row:
                problems.append(f"{rid}: missing field {key}")
        if row.get("stage") not in STAGE_FILES:
            problems.append(f"{rid}: stage {row.get('stage')!r} is not 3, 4 or 5")
        if not _is_mechanic(row) and row.get("scales") not in SCALES:
            problems.append(f"{rid}: scales {row.get('scales')!r} not in {sorted(SCALES)}")
        if not str(row.get("sentence", "")).strip():
            problems.append(f"{rid}: empty sentence")
        problems += _row_reference_problems(row, rid)
        problems += _test_id_problems(row, rid)
        for entry in row.get("retires", []):
            match = RETIRES.match(entry)
            if not match:
                problems.append(f"{rid}: retires entry {entry!r} is not scripts/tests/<file>.py::<test>")
            elif match["file"] in ALL_STAGE_FILES:
                problems.append(f"{rid}: retires entry {entry!r} names a per-stage test file")

    ordinary = [row for row in rows if not _is_mechanic(row)]
    served = {src for row in ordinary for src in row.get("sources", [])}
    exempt_ids = {e.get("id") for e in exempt if str(e.get("reason", "")).strip()}
    for entry in exempt:
        if not str(entry.get("reason", "")).strip():
            problems.append(f"exempt source {entry.get('id')} has no reason")
    for oid in ORDER_IDS:
        if oid not in served and oid not in exempt_ids:
            problems.append(f"order id {oid} is served by no ordinary row")
    served_tests = {row.get("order_test") for row in ordinary}
    for number in sorted(ORDER_TESTS):
        if number not in served_tests:
            problems.append(f"order test {number} is served by no ordinary row")

    unused_ids = {u.get("id") for u in unused if str(u.get("reason", "")).strip()}
    for entry in unused:
        if not str(entry.get("reason", "")).strip():
            problems.append(f"unused assumption {entry.get('id')} has no reason")
    used = {src for row in rows for src in (*row.get("sources", []), *row.get("realizes", []))}
    for aid in ASSUMPTION_IDS:
        if aid not in used and aid not in unused_ids:
            problems.append(f"assumption {aid} appears in no row and no unused entry")

    for stage, allowed in STAGE_ORDER_TESTS.items():
        stage_rows = [row for row in ordinary if row.get("stage") == stage]
        for row in stage_rows:
            number = row.get("order_test")
            if number is not None and number not in allowed:
                problems.append(f"{row.get('id')}: order test {number} does not belong to stage {stage}")
        have = {row.get("order_test") for row in stage_rows}
        for number in sorted(allowed - have):
            problems.append(f"order test {number} has no ordinary row at stage {stage}")

    for stage, required in REQUIRED_MECHANICS.items():
        have = {row.get("mechanic") for row in rows if _is_mechanic(row) and row.get("stage") == stage}
        for key in sorted(required - have):
            problems.append(f"stage {stage} lacks mechanics row {key}")
        for key in sorted(have - required):
            problems.append(f"stage {stage} has unknown mechanics row {key}")
    return problems


def _row_reference_problems(row, rid):
    problems = []
    if _is_mechanic(row):
        if not row.get("realizes"):
            problems.append(f"{rid}: mechanics row has no realizes")
        if "sources" in row or "order_test" in row:
            problems.append(f"{rid}: mechanics row carries sources or order_test")
        for ref in row.get("realizes", []):
            if ref not in KNOWN_IDS:
                problems.append(f"{rid}: realizes unknown id {ref}")
        return problems
    if not row.get("sources"):
        problems.append(f"{rid}: ordinary row has no sources")
    for ref in row.get("sources", []):
        if ref not in KNOWN_IDS:
            problems.append(f"{rid}: unknown source {ref}")
    if "realizes" in row:
        problems.append(f"{rid}: ordinary row carries realizes")
    number = row.get("order_test")
    if number is not None and number not in ORDER_TESTS:
        problems.append(f"{rid}: order_test {number} is not in 1-14")
    backed = row.get("assumption_backed")
    if backed is not None and (backed not in ASSUMPTION_IDS or backed not in row.get("sources", [])):
        problems.append(f"{rid}: assumption_backed {backed} is not an A id listed in sources")
    return problems


def _test_id_problems(row, rid):
    match = NODE_ID.match(str(row.get("test", "")))
    if not match:
        return [f"{rid}: test {row.get('test')!r} is not scripts/tests/<file>.py::test_<name>"]
    if match["file"] not in STAGE_FILES.get(row.get("stage"), []):
        return [f"{rid}: test file {match['file']} is not a stage {row.get('stage')} test file"]
    return []


def test_registry_structure():
    problems = structure_problems(load_registry())
    assert problems == []


def test_empty_registry_yields_problems_not_an_exception():
    assert structure_problems({"schema": 1})


def test_structure_problems_names_a_dropped_row():
    registry = copy.deepcopy(load_registry(DEFAULT_REGISTRY))
    registry["rows"] = [
        row
        for row in registry["rows"]
        if row.get("mechanic") != "reindexing"
        and not (row.get("stage") == 3 and not _is_mechanic(row) and row.get("order_test") == 8)
    ]
    problems = "\n".join(structure_problems(registry))
    assert "reindexing" in problems
    assert "order test 8" in problems


def _defined_tests(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize("stage", [3, 4, 5], ids=["stage3", "stage4", "stage5"])
def test_rules_of_stage_have_tests(stage):
    missing = []
    cache = {}
    for row in load_registry()["rows"]:
        if row.get("stage") != stage:
            continue
        match = NODE_ID.match(row["test"])
        path = REPO_ROOT / "scripts" / "tests" / match["file"]
        if not path.is_file():
            missing.append(f"{row['id']}: {match['file']} does not exist")
            continue
        if path not in cache:
            cache[path] = _defined_tests(path)
        if match["name"] not in cache[path]:
            missing.append(f"{row['id']}: {match['name']} is not defined in {match['file']}")
    assert missing == []
