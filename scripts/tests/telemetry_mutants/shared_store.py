"""Mutants of the shared live store's defence against rows a writer should not have put there:
the traced not_requested judge writer and its fix bound, the test-run leak guards and their
class, foreign-layer hook rows, the pinned scale measurement, the session a report run is
charged to, the writer marker stamped at production points only, and the sync's commit of a
replayed spool, which never waits out another spool's retries or a deadline counting only sleeps,
and check-latency's second pre-fix window, bounded and serving only the sync-commit cause.

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_PROCEDURE = "scripts/agentctl/procedure.py"
_LEDGER = "scripts/lib/judge_ledger.py"
_TOKENS = "scripts/lib/telemetry/tokens.py"
_REPORT = "scripts/lib/telemetry/report.py"
_LIVE = "scripts/lib/telemetry/live.py"
_EMIT = "scripts/lib/telemetry/emit.py"
_STORE = "scripts/lib/telemetry/store.py"
_SYNC = "scripts/lib/telemetry/sync.py"
_T = "scripts/tests/test_telemetry_shared_store.py::"
_SYNC_T = "scripts/tests/test_telemetry_recorder_cost.py::"

_TEST_RUN_BRANCH = "        if test == 1:\n"
_NAIVE_CHECK = ('    if claimed.tzinfo is None:\n'
                '        raise LatencyInputError(f"landed_at {text!r} carries no UTC offset")\n')

MUTANTS = [
    {"name": "traced_writer_not_requested", "file": _PROCEDURE,
     "anchor": "        argv = advisor._prompt_argv(HOST_CLAUDE, advisor._JUDGE_COMPLEXITY)\n",
     "replacement": '        argv = ["claude", "-p", "--model", "haiku"]\n',
     "tests": [_T + "test_traced_writer_of_not_requested_lines_writes_none_now"]},
    {"name": "judge_ledger_not_isolated", "file": _LEDGER,
     "anchor": "        if _test_run_without_isolated_ledger():\n            return None\n",
     "replacement": "        pass\n",
     "tests": [_T + "test_test_run_writes_no_live_judge_line"]},
    {"name": "not_requested_excuse_ignored", "file": _TOKENS,
     "anchor": "    if obj.get(judge_ledger.WRITER_MARKER) or bound is None:\n",
     "replacement": "    if True:\n",
     "tests": [_T + "test_not_requested_line_before_fix_bound_is_listed_not_failed"]},
    {"name": "not_requested_excuse_unbounded", "file": _TOKENS,
     "anchor": "    if when is not None and when < bound:\n",
     "replacement": "    if True:\n",
     "tests": [_T + "test_row_after_bound_without_writer_marker_is_version_loss"]},
    {"name": "not_requested_excuse_any_reason", "file": _TOKENS,
     "anchor": ('            out["failures"].append({**where, "class": "judge_line_without_usage", '
                '"reason": "judge_line_without_usage"})\n'),
     "replacement": ('            if _not_requested_class(obj, bound) == "fail":\n'
                     '                out["failures"].append({**where, "class": "judge_line_without_usage"})\n'),
     "tests": [_T + "test_not_requested_bound_excuses_no_other_usage_reason"]},
    {"name": "pinned_lines_not_counted", "file": _TOKENS,
     "anchor": '            out["excused"][PINNED_NOT_REQUESTED] += obj.get("invocation_id") in known\n',
     "replacement": "            pass\n",
     "tests": [_T + "test_pinned_not_requested_lines_are_counted_apart"]},
    {"name": "bound_accepts_naive_landed_at", "file": _REPORT, "anchor": _NAIVE_CHECK,
     "replacement": "    if claimed.tzinfo is None:\n        claimed = claimed.replace(tzinfo=sync.timezone.utc)\n",
     "tests": [_T + "test_bound_landing_file_missing_malformed_or_naive_exits_2"]},
    {"name": "bound_landed_at_unchecked", "file": _REPORT,
     "anchor": "    if claimed < landed_event or (claimed - landed_event).total_seconds() > LANDED_AT_MAX_LAG_S:\n",
     "replacement": "    if False:\n",
     "tests": [_T + "test_bound_landed_at_outside_land_event_window_exits_2"]},
    {"name": "foreign_hook_unresolved", "file": _REPORT,
     "anchor": "        name = _foreign_hook_name(source_key, layer, mechanism)\n",
     "replacement": "        name = None\n",
     "tests": [_T + "test_foreign_layer_hook_key_resolves_as_foreign_hook"]},
    {"name": "foreign_hook_ignores_layer_field", "file": _REPORT,
     "anchor": "    prefix = f\"{layer}:\"\n",
     "replacement": "    layer = mechanism.split(\":\")[0]\n    prefix = f\"{layer}:\"\n",
     "tests": [_T + "test_hook_key_without_explicit_layer_stays_unresolved"]},
    {"name": "foreign_hook_admits_core_layer", "file": _REPORT,
     "anchor": ("    if not isinstance(layer, str) or not layer or layer == CORE_LAYER "
                "or not isinstance(mechanism, str):\n"),
     "replacement": "    if not isinstance(layer, str) or not layer or not isinstance(mechanism, str):\n",
     "tests": [_T + "test_unknown_hook_key_of_core_layer_stays_unresolved"]},
    {"name": "foreign_hook_names_unreported", "file": _REPORT,
     "anchor": "            names[name] = names.get(name, 0) + count\n",
     "replacement": "            pass\n",
     "tests": [_T + "test_foreign_hook_report_lists_names_per_layer"]},
    {"name": "test_run_writes_live_row", "file": _LIVE,
     "anchor": "    if not enabled() or leaks_test_run(fields):\n",
     "replacement": "    if not enabled():\n",
     "tests": [_T + "test_test_run_without_isolation_writes_no_live_row"]},
    {"name": "test_leak_ignored", "file": _REPORT,
     "anchor": "        if marked or bound is None or when is None:\n",
     "replacement": "        if True:\n",
     "tests": [_T + "test_leaked_test_row_before_bound_is_named_loss"]},
    {"name": "test_leak_unbounded", "file": _REPORT,
     "anchor": "        name = f\"{TEST_LEAK_PREFIX}{source_key}\" if when < bound else PRE_FIX_WRITER_TEST_RUN\n",
     "replacement": "        name = f\"{TEST_LEAK_PREFIX}{source_key}\"\n",
     "tests": [_T + "test_leaked_test_row_at_or_after_bound_fails"]},
    {"name": "test_leak_excuses_any_key", "file": _REPORT, "anchor": _TEST_RUN_BRANCH,
     "replacement": "        if test == 1 or bound is not None:\n",
     "tests": [_T + "test_test_leak_bound_excuses_no_non_test_key"]},
    {"name": "test_leak_by_key_name", "file": _REPORT, "anchor": _TEST_RUN_BRANCH,
     "replacement": "        if \"test\" in source_key:\n",
     "tests": [_T + "test_test_leak_class_is_structural_not_by_key_name"]},
    {"name": "report_usage_env_before_task", "file": _REPORT,
     "anchor": "        if opener:\n            return opener\n",
     "replacement": "        if opener and not env_session:\n            return opener\n",
     "tests": [_T + "test_report_usage_under_task_goes_to_task_opener_with_session_env_set"]},
    {"name": "json_test_run_constant", "file": _REPORT,
     "anchor": "    return {\"class\": cls, \"source_key\": source_key, \"test_run\": test_run, \"epoch\": epoch,\n",
     "replacement": "    return {\"class\": cls, \"source_key\": source_key, \"test_run\": False, \"epoch\": epoch,\n",
     "tests": [_T + "test_json_failure_test_run_field_is_per_row"]},
    {"name": "bound_offset_dropped", "file": _REPORT, "anchor": _NAIVE_CHECK,
     "replacement": "    claimed = claimed.replace(tzinfo=sync.timezone.utc)\n",
     "tests": [_T + "test_bound_landing_file_with_plus_three_offset_and_whole_seconds"]},
    {"name": "version_excuse_ignored", "file": _REPORT,
     "anchor": "        if marked or bound is None or when is None:\n",
     "replacement": "        if marked or when is None or when >= bound:\n",
     "tests": [_T + "test_row_after_bound_without_writer_marker_is_version_loss"]},
    {"name": "version_excuse_ignores_marker", "file": _REPORT,
     "anchor": "        if marked or bound is None or when is None:\n",
     "replacement": "        if bound is None or when is None:\n",
     "tests": [_T + "test_row_after_bound_with_writer_marker_fails"]},
    {"name": "writer_marker_not_stamped", "file": _EMIT,
     "anchor": "    stamped[WRITER_MARKER] = 1\n",
     "replacement": "    pass\n",
     "tests": [_T + "test_every_live_writer_stamps_writer_marker"]},
    {"name": "bound_truncates_any_precision", "file": _REPORT,
     "anchor": "    if whole_seconds:\n",
     "replacement": "    if True:\n",
     "tests": [_T + "test_whole_second_rule_only_for_fractionless_landed_at"]},
    {"name": "foreign_hook_before_test_run", "file": _REPORT, "anchor": _TEST_RUN_BRANCH,
     "replacement": "        if test == 1 and _foreign_hook_name(source_key, layer, mechanism) is None:\n",
     "tests": [_T + "test_test_run_class_precedes_foreign_hook"]},
    {"name": "marker_stamped_at_replay", "file": _STORE,
     "anchor": "    fields = event.get(\"fields\", {})\n    backref = event.get(\"backref\")\n",
     "replacement": "    fields = {**event.get(\"fields\", {}), \"writer_marker\": 1}\n    backref = event.get(\"backref\")\n",
     "tests": [_T + "test_replayed_unmarked_spool_line_stays_unmarked"]},
    {"name": "scale_window_unchecked", "file": _REPORT,
     "anchor": "    if earliest is None or latest is None or earliest < first or latest > last:\n",
     "replacement": "    if earliest is None or latest is None:\n",
     "tests": [_T + "test_scale_measurement_window_edges_hold_and_a_row_past_them_fails",
               _T + "test_scale_measurement_before_window_fails"]},
    {"name": "scale_window_edge_exclusive", "file": _REPORT,
     "anchor": "    if earliest is None or latest is None or earliest < first or latest > last:\n",
     "replacement": "    if earliest is None or latest is None or earliest <= first or latest >= last:\n",
     "tests": [_T + "test_scale_measurement_window_edges_hold_and_a_row_past_them_fails"]},
    {"name": "scale_count_unchecked", "file": _REPORT,
     "anchor": "    elif group[\"count\"] > SCALE_MEASUREMENT_ROWS:\n",
     "replacement": "    elif False:\n",
     "tests": [_T + "test_scale_measurement_count_above_pinned_fails"]},
    {"name": "sync_commit_after_retries", "file": _SYNC,
     "anchor": "    results = _replay_all(stems, deadline, commit_replayed)\n",
     "replacement": ("    results = _replay_all(stems, deadline, lambda stem, result: None)\n"
                     "    for stem in stems:\n"
                     "        if results[stem][\"ok\"]:\n"
                     "            commit_replayed(stem, results[stem])\n"),
     "tests": [_SYNC_T + "test_replayed_spool_commits_before_retries_of_a_deferred_one_end"]},
    {"name": "sync_budget_counts_only_sleeps", "file": _SYNC,
     "anchor": "    while _monotonic() + wait <= deadline and not all(r[\"ok\"] for r in results.values()):\n",
     "replacement": ("    waited = _monotonic()\n"
                     "    while waited + wait <= deadline and not all(r[\"ok\"] for r in results.values()):\n"
                     "        waited += wait\n"),
     "tests": [_SYNC_T + "test_retry_attempts_count_against_the_wall_clock_deadline"]},
    {"name": "pre_fix_late_second_window_unbounded", "file": _REPORT,
     "anchor": "    start, until = window\n",
     "replacement": "    start, until = window[0], None\n",
     "tests": [_T + "test_pre_fix_late_second_window_open_end_only_without_until",
               _T + "test_pre_fix_late_second_window_edges_exit_2"]},
    {"name": "pre_fix_late_second_window_ignored", "file": _REPORT,
     "anchor": "            refusal = _second_window_refusal(cause, row, second_window, now)\n",
     "replacement": '            refusal = "the second window never accepts"\n',
     "tests": [_T + "test_pre_fix_late_second_window_accepts_entry"]},
    {"name": "pre_fix_late_second_window_any_cause", "file": _REPORT,
     "anchor": "    if cause != PRE_FIX_CAUSE_SYNC_COMMIT_WAITS:\n",
     "replacement": "    if False:\n",
     "tests": [_T + "test_pre_fix_late_second_window_wrong_cause_exit_2"]},
]
