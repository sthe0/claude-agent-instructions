"""Mutants of export, merge and round-trip check (lib/telemetry/portability.py and its store calls).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_PORTABILITY = "scripts/lib/telemetry/portability.py"
_STORE = "scripts/lib/telemetry/store.py"
_IMPORTER = "scripts/lib/telemetry/importer.py"
_T = "scripts/tests/test_telemetry_portability.py::"

_APPEND = '        batch.append(line["row"])\n'


def _attach_by_rowid(table: str) -> str:
    """Code re-pointing `table`'s task key at the receiver's task in the exported task's position."""
    return (
        f'        if table == "{table}":\n'
        '            exported = [(l["row"]["origin"], l["row"]["task_id"], l["row"]["epoch"])\n'
        '                        for l in read_export(path) if l.get("table") == "tasks"]\n'
        '            local = conn.execute("SELECT origin, task_id, epoch FROM tasks ORDER BY rowid").fetchall()\n'
        '            r = line["row"]\n'
        '            k = (r.get("task_origin"), r.get("task_id"), r.get("task_epoch"))\n'
        '            if k in exported:\n'
        '                r["task_origin"], r["task_id"], r["task_epoch"] = local[exported.index(k)]\n') + _APPEND


def _mutant(name, anchor, replacement, *tests, file=_PORTABILITY):
    return {"name": name, "file": file, "anchor": anchor, "replacement": replacement,
            "tests": [_T + t for t in tests]}


MUTANTS = [
    _mutant("export_includes_text_without_flag",
            "(with_text or t not in TEXT_TABLES)",
            "(True or t not in TEXT_TABLES)",
            "test_export_without_text_flag_has_no_text_rows"),
    _mutant("merge_by_rowid",
            """cursor = conn.execute(f'SELECT {origin_sql} AS "__origin", t.* FROM "{table}" AS t ORDER BY t.rowid')""",
            """cursor = conn.execute(f'SELECT {origin_sql} AS "__origin", t.rowid AS rowid, t.* FROM "{table}" AS t """
            """ORDER BY t.rowid')""",
            "test_reimport_adds_no_row", "test_merge_keeps_unrelated_same_slug_tasks_of_two_origins_apart",
            file=_STORE),
    _mutant("roundtrip_check_ignores_counts",
            "for t in expected if counts[t] != expected[t]}",
            "for t in expected if False}",
            "test_check_roundtrip_fails_on_count_mismatch"),
    _mutant("merge_joins_tasks_ignoring_origin_or_epoch",
            '    "tasks": ("origin", "task_id", "epoch"),',
            '    "tasks": ("task_id",),',
            "test_merge_keeps_unrelated_same_slug_tasks_of_two_origins_apart"),
    _mutant("import_coerces_unknown_table_version",
            '            raise Refused(f"unknown version {version!r} of table {name!r}")',
            "            pass",
            "test_import_refuses_unknown_table_version"),
    _mutant("export_mode_0644",
            "EXPORT_MODE = 0o600",
            "EXPORT_MODE = 0o644",
            "test_export_file_mode_0600"),
    _mutant("origin_from_hostname",
            "origin = _side_file_origin(side) or str(uuid.uuid4())",
            'origin = _side_file_origin(side) or __import__("socket").gethostname()',
            "test_origin_is_random_not_hostname", file=_STORE),
    _mutant("roundtrip_writes_outside_workdir",
            '    export_path = workdir / "export.jsonl.gz"',
            '    export_path = workdir.parent / "export.jsonl.gz"',
            "test_check_roundtrip_writes_only_into_workdir"),
    _mutant("table_without_merge_key_ignored",
            "    if undeclared:",
            "    if False:",
            "test_every_table_has_a_merge_key_or_is_local_only"),
    _mutant("roundtrip_opens_live_record_read_write",
            '    store.backup_record(record, workdir / "copy.sqlite")',
            '    store.backup(store.open_for_merge(record), workdir / "copy.sqlite")',
            "test_check_roundtrip_opens_the_live_record_read_only"),
    _mutant("merge_keeps_backreferenced_imported_row",
            "    superseded = store.delete_superseded_imported_events(conn)",
            "    superseded = 0",
            "test_merge_drops_an_imported_row_a_live_row_back_references"),
    _mutant("merge_older_export_overwrites_losses",
            '        watermark is None or _instant(header["taken_at"]) > _instant(watermark))',
            "        True)",
            "test_merge_two_exports_of_one_machine_taken_at_different_times"),
    _mutant("known_version_current_only",
            "self.seen[(name, version)] = tables.known_version(name, version)",
            "self.seen[(name, version)] = tables.load(name).version == version",
            "test_import_accepts_an_older_version_in_the_snapshot_history"),
    _mutant("portability_imports_sqlite3",
            "import gzip\n",
            "import gzip\nimport sqlite3\n",
            "test_portability_opens_the_record_only_through_store"),
    _mutant("merge_insert_under_command_deadline",
            '    _set_busy(conn, MERGE_BUSY_MS)\n    conn.execute("BEGIN IMMEDIATE")',
            '    _set_busy(conn, int(DEADLINE_S * 1000))\n    conn.execute("BEGIN IMMEDIATE")',
            "test_merge_insert_runs_past_the_command_path_deadline", file=_STORE),
    _mutant("upsert_ignores_row_origin",
            '        upsert = fresh and MERGE_MODE[table] == "upsert" and line["origin"] == source',
            '        upsert = fresh and MERGE_MODE[table] == "upsert"',
            "test_merge_three_origins_keeps_the_newest_copy_of_each_origins_rows",
            "test_unresolved_row_merges_insert_or_ignore_where_the_receiver_resolves_it"),
    _mutant("session_row_merged_insert_or_ignore",
            '"tasks": "upsert", "sessions": "upsert", "contours": "upsert", "stages": "upsert",',
            '"tasks": "upsert", "sessions": "ignore", "contours": "upsert", "stages": "upsert",',
            "test_merge_two_exports_of_one_machine_taken_at_different_times"),
    _mutant("token_row_merged_insert_or_ignore",
            '"token_usage": "upsert",',
            '"token_usage": "ignore",',
            "test_merge_two_exports_of_one_machine_taken_at_different_times"),
    _mutant("fixture_causes_keyed_by_origin",
            "fixture_cause_id(digest, index)",
            "fixture_cause_id(digest + run.origin, index)",
            "test_merge_of_two_origins_keeps_235_fixture_causes", file=_IMPORTER),
    _mutant("merge_accepts_other_fixture_revision",
            "    if len(theirs) > 1 or (theirs and ours and theirs != ours):",
            "    if False:",
            "test_merge_refuses_an_export_with_another_fixture_revision"),
    _mutant("merge_keeps_superseded_spawn_cost_row",
            "\" OR (kind = 'spawn_cost' AND child_session_id IN (SELECT child_session_id FROM events \"",
            "\" OR (0 AND child_session_id IN (SELECT child_session_id FROM events \"",
            "test_merge_drops_an_imported_row_a_live_row_back_references", file=_STORE),
    _mutant("merge_replaces_own_origin_rows",
            "    fresh = source != store.record_origin(conn) and (",
            "    fresh = (",
            "test_merge_of_own_older_export_never_replaces_own_rows"),
    _mutant("merge_attaches_session_to_task_by_rowid",
            _APPEND, _attach_by_rowid("sessions"),
            "test_merged_session_attaches_to_its_own_task_across_origins"),
    _mutant("merge_attaches_token_row_to_task_by_rowid",
            _APPEND, _attach_by_rowid("token_usage"),
            "test_merged_session_attaches_to_its_own_task_across_origins"),
    _mutant("merge_restores_row_its_own_replay_deleted",
            "\"event_id IN (SELECT backref_id FROM events WHERE provenance <> 'imported' AND backref_id IS NOT NULL)\"",
            "\"event_id IN (SELECT backref_id FROM events WHERE 0)\"",
            "test_merge_of_own_older_export_restores_no_row_its_replay_deleted", file=_STORE),
    _mutant("read_export_materializes_lines",
            "            for line in handle:\n                if line.strip():\n                    yield json.loads(line)\n",
            "            yield from [json.loads(line) for line in handle if line.strip()]\n",
            "test_merge_memory_stays_bounded_as_the_export_grows"),
    _mutant("merge_rows_one_transaction",
            "        chunk = list(itertools.islice(rows, MERGE_CHUNK_ROWS))",
            "        chunk = list(rows)",
            "test_merge_commits_in_chunks", file=_STORE),
    _mutant("merge_ignores_line_counts",
            "    if counts != declared:",
            "    if False:",
            "test_merge_refuses_a_truncated_export"),
    _mutant("merge_checks_columns_only_while_writing",
            "        if unknown:\n            raise Refused(",
            "        if False:\n            raise Refused(",
            "test_merge_refuses_an_unknown_column_before_writing"),
    _mutant("header_fields_unchecked",
            "    if missing:",
            "    if False:",
            "test_merge_refuses_a_malformed_header"),
    _mutant("unresolved_row_takes_exporter_origin",
            '"origin": row_origin, "row"',
            '"origin": row_origin or origin, "row"',
            "test_unresolved_row_round_trip_leaves_its_owner_row_unchanged"),
    _mutant("upsert_ignores_stored_row_origin",
            'action, guard = f"UPDATE SET {updates} WHERE ({owner[0]}) IS ?", [owner[1]]',
            'action, guard = f"UPDATE SET {updates}", []',
            "test_upsert_never_overwrites_a_stored_row_of_another_origin", file=_STORE),
    _mutant("immutable_read_error_not_retried",
            "        except sqlite3.DatabaseError:\n            pass\n",
            "        except sqlite3.DatabaseError:\n            raise\n",
            "test_read_quietly_retries_after_an_immutable_read_error", file=_STORE),
    _mutant("quiet_read_keeps_a_changed_result",
            "            if _file_identity(record) == before and not wal.exists():",
            "            if True:",
            "test_read_quietly_discards_a_read_when_the_record_changes", file=_STORE),
    _mutant("export_keeps_a_partial_file",
            "        except BaseException:\n            out.unlink(missing_ok=True)\n            raise",
            "        except BaseException:\n            raise",
            "test_export_removes_a_partial_file_on_error"),
    _mutant("roundtrip_ignores_free_disk",
            "    if free < needed:",
            "    if False:",
            "test_check_roundtrip_refuses_without_free_disk"),
]
