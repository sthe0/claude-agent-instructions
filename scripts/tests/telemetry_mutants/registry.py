"""Mutants of the mechanism registry, the four cause tables and the structured cause of a critique.

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_CAUSE = "scripts/lib/telemetry/cause.py"
_SYNC = "scripts/lib/telemetry/sync.py"
_CLI_DIFFICULTY = "scripts/agentctl/cli_difficulty.py"
_STATE = "scripts/agentctl/state.py"
_MANDATE = "scripts/agentctl/mandate.py"
_REGISTRY_TABLE = "scripts/lib/telemetry/tables/registry.toml"
_CATEGORIES_TABLE = "scripts/lib/telemetry/tables/categories.toml"
_MIGRATION_004 = "scripts/lib/telemetry/migrations/004_cause_fields.sql"
_CAUSE_TESTS = "scripts/tests/test_telemetry_cause.py"
_C = _CAUSE_TESTS + "::"
_R = "scripts/tests/test_telemetry_registry.py::"

_NONE_IFF = _C + "test_mechanism_none_iff_not_machinery_induced"
_PLACE_DERIVED = _C + "test_functional_place_derived_from_registry_and_mismatch_refused"
_OTHER_NOTE = _C + "test_other_category_requires_note"
_UNKNOWN_IMPORTED = _C + "test_validate_cause_accepts_unknown_only_when_imported"
_UNKNOWN_LIVE = _C + "test_unknown_mechanism_refused_on_live_critique"
_VERSION_STORED = _C + "test_cause_row_stores_table_version"
_ONLY_MEMBERS = _C + "test_cause_fields_accept_only_table_members"
_RETIRED_REFUSED = _C + "test_retired_member_refused_on_new_critique"
_RETIRED_VALID = _C + "test_row_with_retired_member_stays_valid"
_NOTE_STORED = _C + "test_cause_note_is_stored"
_REACHES_TABLE = _C + "test_live_critique_cause_reaches_the_causes_table"
_ONE_LIST = _C + "test_registry_places_and_mandate_vocabulary_are_one_list"
_PROSE_ONLY = _C + "test_prose_only_critique_records_stay_readable"
_SCAN_TREE = _C + "test_invocation_rule_needs_the_agentctl_prefix_and_scans_the_tree_constant"
_MIGRATION = _C + "test_migration_004_creates_the_causes_table_keyed_by_cause_id"

MUTANTS = [
    {"name": "accept_registry_id_with_false", "file": _CAUSE,
     "anchor": "        if mechanism != NO_MECHANISM:",
     "replacement": "        if False:",
     "tests": [_NONE_IFF]},
    {"name": "place_not_from_registry", "file": _CAUSE,
     "anchor": "    expected = registry_place(mechanism, root)",
     "replacement": "    expected = place",
     "tests": [_PLACE_DERIVED]},
    {"name": "other_without_note", "file": _CAUSE,
     "anchor": '    if category == OTHER_CATEGORY and not str(fields.get(NOTE_FIELD) or "").strip():',
     "replacement": "    if False:",
     "tests": [_OTHER_NOTE]},
    {"name": "unknown_on_live_critique", "file": _CAUSE,
     "anchor": "        if not imported:",
     "replacement": "        if False:",
     "tests": [_UNKNOWN_LIVE, _UNKNOWN_IMPORTED]},
    {"name": "registry_misses_a_hook", "file": _REGISTRY_TABLE,
     "anchor": 'name = "hook:hook-ask-answer-record"',
     "replacement": 'name = "hook:hook-ask-answer-recorded"',
     "tests": [_R + "test_registry_complete_over_hooks_gates_judges_commands"]},
    {"name": "mandate_place_literal", "file": _MANDATE,
     "anchor": 'ITEM_PLACE = "mandate_cycle"',
     "replacement": 'ITEM_PLACE = "mandate-cycle"',
     "tests": [_ONE_LIST]},
    {"name": "cause_stored_without_table_version", "file": _CAUSE,
     "anchor": "        cause[column] = tables.load(table, root).version",
     "replacement": "        cause[column] = None",
     "tests": [_VERSION_STORED]},
    {"name": "critique_category_literal", "file": _CAUSE,
     "anchor": '    reason = _live_member("categories", category, root)',
     "replacement": ('    reason = None if category in ("implementation_defect", "other") '
                     'else f"{category!r} is not a category"'),
     "tests": [_ONLY_MEMBERS, _RETIRED_REFUSED]},
    {"name": "composite_dropped", "file": _REGISTRY_TABLE,
     "anchor": 'name = "order_phase"',
     "replacement": 'name = "order_phase_dropped"',
     "tests": [_R + "test_every_r5_composite_is_registered"]},
    {"name": "retired_member_breaks_old_rows", "file": _SYNC,
     "anchor": "        versions = [fields.get(column) for _, column in cause.VERSION_FIELDS.values()]",
     "replacement": ("        if cause.validate_cause(fields):\n            continue\n"
                     "        versions = [fields.get(column) for _, column in cause.VERSION_FIELDS.values()]"),
     "tests": [_RETIRED_VALID]},
    {"name": "cause_note_dropped", "file": _SYNC,
     "anchor": "            versions[2], fields.get(cause.NOTE_FIELD), origin))",
     "replacement": "            versions[2], None, origin))",
     "tests": [_NOTE_STORED]},
    {"name": "source_key_unresolved", "file": _CLI_DIFFICULTY,
     "anchor": 'live.emit_event(state.session_id, "critique", "cli:critique",',
     "replacement": 'live.emit_event(state.session_id, "critique", "cli:critique-cause",',
     "tests": [_R + "test_every_emitted_source_key_resolves_to_one_entry", _REACHES_TABLE]},
    {"name": "live_cause_not_synced", "file": _SYNC,
     "anchor": "    _file_causes(conn, origin, session, events)\n",
     "replacement": "    pass\n",
     "tests": [_REACHES_TABLE]},
    {"name": "prose_only_record_refused", "file": _STATE,
     "anchor": 'cause=dict(d.get("cause") or {})',
     "replacement": 'cause=dict(d["cause"])',
     "tests": [_PROSE_ONLY]},
    {"name": "invocation_scan_skips_docs", "file": _CAUSE_TESTS,
     "anchor": 'CRITIQUE_SCAN_TREE = ("scripts", "skills", "docs", "cursor", "CLAUDE.md", "README.md")',
     "replacement": 'CRITIQUE_SCAN_TREE = ("scripts", "skills", "cursor", "CLAUDE.md", "README.md")',
     "tests": [_SCAN_TREE]},
    {"name": "categories_table_drops_other", "file": _CATEGORIES_TABLE,
     "anchor": 'name = "other"',
     "replacement": 'name = "other_kind"',
     "tests": [_R + "test_categories_table_holds_r3_categories_and_other"]},
    {"name": "causes_table_keyed_by_session", "file": _MIGRATION_004,
     "anchor": "    cause_id                       TEXT PRIMARY KEY,\n    session                        TEXT NOT NULL,",
     "replacement": "    cause_id                       TEXT,\n    session                        TEXT PRIMARY KEY,",
     "tests": [_MIGRATION]},
]
