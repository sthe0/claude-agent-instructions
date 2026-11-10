"""Mutants of the substrate-adequacy producer (lib/telemetry/substrate.py).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_SUBSTRATE = "scripts/lib/telemetry/substrate.py"
_REPORT = "scripts/lib/telemetry/report.py"
_SCAN = "scripts/improvement-scan.py"
_T = "scripts/tests/test_substrate_adequacy.py::"

_CREDIT = "            slot[key] = (mass, difficulty, case)"
_KEY = '        key = (difficulty["task"], difficulty["difficulty"])'
_ENTRY_ROUTE = "def _entry_route(prior: list, entry: tuple) -> str:\n"


def _mutant(name, anchor, replacement, *tests, file=_SUBSTRATE):
    return {"name": name, "file": file, "anchor": anchor, "replacement": replacement,
            "tests": [_T + t for t in tests]}


MUTANTS = [
    _mutant("open_interval_counts_mass",
            '    return "open_running", severity["open_running"]',
            '    return "never_closed", severity["never_closed"]',
            "test_open_interval_of_running_task_contributes_no_mass"),
    _mutant("never_closed_severity_2",
            '        return "never_closed", severity["never_closed"]',
            '        return "never_closed", severity["interval_unknown"]',
            "test_never_closed_interval_in_ended_task_is_severity_3"),
    _mutant("threshold_strict_greater",
            'row["mass"] >= threshold',
            'row["mass"] > threshold',
            "test_finding_for_every_place_at_or_above_threshold_and_none_below"),
    _mutant("no_part_of_rollup",
            '    for parent in registry[entry]["part_of"]:',
            '    for parent in ():',
            "test_mass_rolls_up_part_of_closure"),
    _mutant("only_failed_route_opens",
            _ENTRY_ROUTE,
            _ENTRY_ROUTE + '    return "failed_stage"\n',
            "test_each_diagnosing_entry_route_opens_interval", file=_REPORT),
    _mutant("replan_only_closes",
            'DIFFICULTY_EXITS = ("replan", "unblock", "reset")',
            'DIFFICULTY_EXITS = ("replan",)',
            "test_each_exit_kind_closes_interval", file=_REPORT),
    _mutant("band_boundary_off_by_one",
            "minutes < below",
            "minutes <= below",
            "test_band_boundaries_9m59s_10m00s_60m00s"),
    _mutant("diamond_double_counts",
            _CREDIT,
            "            slot[key] = ((slot[key][0] + mass) if key in slot else mass, difficulty, case)",
            "test_diamond_part_of_counts_each_difficulty_once"),
    _mutant("shared_place_double_counts",
            _CREDIT,
            "            slot[(key, entry)] = (mass, difficulty, case)",
            "test_shared_place_counts_each_difficulty_once",
            "test_part_and_composite_sharing_a_place_count_once"),
    _mutant("synthetic_counted_in_mass",
            "INCLUDE_SYNTHETIC = False",
            "INCLUDE_SYNTHETIC = True",
            "test_synthetic_tasks_excluded_from_mass"),
    _mutant("none_place_ranked",
            "            if place in (None, NONE_PLACE):",
            "            if place is None:",
            "test_none_place_excluded_from_mass_ranking_and_findings"),
    _mutant("calibrate_counts_nested_places",
            '        nested = sorted(result["related"].get(row["place"], set()) & set(counted))',
            "        nested = []",
            "test_calibrate_skips_places_part_of_related_to_a_higher_ranked_one"),
    _mutant("calibrate_share_over_counted_only",
            '"machinery_induced_share": machinery / total if total else None}',
            '"machinery_induced_share": sum(result["places"][p]["machinery_mass"] for p in counted)'
            ' / counted_mass if total else None}',
            "test_calibrate_prints_total_counted_and_machinery_induced_shares"),
    _mutant("unaligned_rows_collapse",
            _KEY,
            '        key = (difficulty["task"], difficulty["entry_at"])',
            "test_unaligned_imported_rows_each_count"),
    _mutant("key_without_task",
            _KEY,
            '        key = difficulty["difficulty"]',
            "test_two_tasks_of_one_session_each_count_their_difficulty"),
    _mutant("scan_opens_record_writable",
            "    conn = telemetry_store.open_readonly()",
            "    conn = telemetry_store.open_record()",
            "test_report_substrate_section_without_a_record_names_why",
            "test_substrate_leaves_a_populated_record_unchanged", file=_SCAN),
]
