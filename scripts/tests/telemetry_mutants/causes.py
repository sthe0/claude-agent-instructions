"""Mutants of the historical causes fixture: its alignment (lib/telemetry/align.py) and its loading
by the importer (lib/telemetry/importer.py).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_ALIGN = "scripts/lib/telemetry/align.py"
_IMPORTER = "scripts/lib/telemetry/importer.py"
_F = "scripts/tests/test_telemetry_fixture.py::"

_LOAD = _F + "test_fixture_load_is_idempotent"
_REPLAYED = _F + "test_fixture_replayed_through_telemetry_import_loads_235_rows"
_SINGLE_MATCH = _F + "test_mechanism_assigned_only_on_a_single_registry_match"
_MAPPING = _F + "test_mapping_rules_null_alignment_and_place_derivation"
_NON_MACHINERY = _F + "test_non_machinery_rows_get_place_none"
_COUNT = _F + "test_alignment_refused_when_the_replan_count_differs_from_235"
_HASH = _F + "test_alignment_refused_on_a_snapshot_hash_mismatch"
_CUTOFF = _F + "test_alignment_ignores_replans_after_the_cutoff"
_SHARE = _F + "test_alignment_refused_when_disagreement_share_exceeds_ten_percent"
_RUN = _F + "test_alignment_refused_on_a_contiguous_run_of_six_disagreements"
_OFFSET = _F + "test_alignment_refused_when_offset_shares_not_above_offset_zero"
_DRAFT = _F + "test_alignment_draft_holds_no_hand_category"
_REPORT = _F + "test_alignment_report_equals_fixture_counts"
_IDS = _F + "test_fixture_ids_from_content_sha256_and_row_index"
_REVISED = _F + "test_loading_a_revised_fixture_replaces_the_235_rows"
_PROVENANCE = _F + "test_check_causes_counts_only_fixture_provenance"

MUTANTS = [
    {"name": "fixture_drops_rows", "file": _IMPORTER,
     "anchor": "        for index, row in enumerate(rows):",
     "replacement": "        for index, row in enumerate(rows[:-1]):",
     "tests": [_LOAD, _REPLAYED]},
    {"name": "fixture_flips_hand_classification", "file": _IMPORTER,
     "anchor": '"machinery_induced": row["machinery_induced"],',
     "replacement": '"machinery_induced": not row["machinery_induced"],',
     "tests": [_REPLAYED, _LOAD]},
    {"name": "unknown_without_imported_flag", "file": _IMPORTER,
     "anchor": "stored, violated = cause.complete_cause(fields, imported=True)",
     "replacement": "stored, violated = cause.complete_cause(fields, imported=False)",
     "tests": [_LOAD, _REPLAYED]},
    {"name": "mechanism_without_alignment", "file": _ALIGN,
     "anchor": "    if machinery and aligned:",
     "replacement": "    if machinery:",
     "tests": [_SINGLE_MATCH]},
    {"name": "place_not_derived", "file": _ALIGN,
     "anchor": '            place = tables.load("registry", root).members[mechanism].attrs["place"]',
     "replacement": '            place = base["place"]',
     "tests": [_MAPPING]},
    {"name": "ambiguous_match_assigned", "file": _ALIGN,
     "anchor": "        if len(named) == 1:",
     "replacement": "        if len(named) >= 1:",
     "tests": [_SINGLE_MATCH]},
    {"name": "default_marked_observed", "file": _ALIGN,
     "anchor": "    basis = DEFAULTED\n",
     "replacement": "    basis = OBSERVED\n",
     "tests": [_SINGLE_MATCH, _NON_MACHINERY]},
    {"name": "alignment_count_unchecked", "file": _ALIGN,
     "anchor": "    if found != EXPECTED_COUNT or len(hand) != EXPECTED_COUNT:",
     "replacement": "    if False:",
     "tests": [_COUNT]},
    {"name": "snapshot_hash_unchecked", "file": _ALIGN,
     "anchor": "        if sha256_file(inputs / name) != premises.get(key):",
     "replacement": "        if False:",
     "tests": [_HASH]},
    {"name": "cutoff_ignored", "file": _ALIGN,
     "anchor": "        if at is None or at > cutoff_ms:",
     "replacement": "        if at is None:",
     "tests": [_CUTOFF]},
    {"name": "alignment_uses_analysis_cutoff", "file": _ALIGN,
     "anchor": '    cutoff_ms = _iso_ms(cutoff or premises["fixture_analysis_at"])',
     "replacement": '    cutoff_ms = _iso_ms(cutoff or premises["analysis_cutoff"])',
     "tests": [_CUTOFF]},
    {"name": "alignment_category_agreement_unchecked", "file": _ALIGN,
     "anchor": "    wrong = [aligned[p] != hand[p] for p in range(len(hand))]",
     "replacement": "    wrong = [False for p in range(len(hand))]",
     "tests": [_SHARE]},
    {"name": "non_machinery_row_gets_category_place", "file": _ALIGN,
     "anchor": '(UNKNOWN, base["place"]) if machinery else (NONE, defaults["non_machinery_place"])',
     "replacement": '(UNKNOWN, base["place"]) if machinery else (NONE, base["place"])',
     "tests": [_NON_MACHINERY]},
    {"name": "fixture_id_from_path", "file": _IMPORTER,
     "anchor": 'return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{fixture_sha256}:{index}"))',
     "replacement": 'return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{__file__}:{index}"))',
     "tests": [_IDS]},
    {"name": "revised_fixture_rows_kept", "file": _IMPORTER,
     "anchor": ('        conn.execute("DELETE FROM causes WHERE provenance = ? AND fixture_sha256 != ?", '
                "(align.SOURCE, digest))"),
     "replacement": "        pass",
     "tests": [_REVISED]},
    {"name": "alignment_shifted_by_one", "file": _ALIGN,
     "anchor": "draft[i] if i < len(draft) else None",
     "replacement": "draft[i - 1] if 0 < i <= len(draft) else None",
     "tests": [_MAPPING]},
    {"name": "draft_carries_hand_category", "file": _ALIGN,
     "anchor": '"aligned_category": judged.get((replan["session"], replan["difficulty_index"], digest))})',
     "replacement": ('"aligned_category": judged.get((replan["session"], replan["difficulty_index"], digest)),\n'
                     '                     "category": "hand"})'),
     "tests": [_DRAFT]},
    {"name": "refusal_reason_unchecked", "file": _ALIGN,
     "anchor": '"alignment_refusal": refusal,',
     "replacement": '"alignment_refusal": None,',
     "tests": [_COUNT, _F + "test_zero_observed_mechanisms_require_a_refusal_reason"]},
    {"name": "alignment_report_disagrees_with_fixture", "file": _ALIGN,
     "anchor": '    aligned = sum(1 for r in rows if r["alignment"] is not None)',
     "replacement": '    aligned = sum(1 for r in rows if r.get("aligned_category") is not None)',
     "tests": [_REPORT]},
    {"name": "alignment_run_rule_skipped", "file": _ALIGN,
     "anchor": "    if run > MAX_RUN:",
     "replacement": "    if False:",
     "tests": [_RUN]},
    {"name": "alignment_offset_check_skipped", "file": _ALIGN,
     "anchor": '    for side in ("-1", "1"):',
     "replacement": "    for side in ():",
     "tests": [_OFFSET]},
    {"name": "fixture_source_unregistered", "file": _ALIGN,
     "anchor": 'SOURCE = f"fixture:{STEM}"',
     "replacement": "SOURCE = STEM",
     "tests": [_PROVENANCE]},
]
