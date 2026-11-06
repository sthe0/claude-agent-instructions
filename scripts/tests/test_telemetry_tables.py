"""Open enumeration tables of the telemetry record: members are data, versions are recorded."""
from __future__ import annotations

import ast
import shutil
import sqlite3
from pathlib import Path

import pytest

from lib import config_root
from lib.telemetry import store, tables

TELEMETRY_DIR = Path(tables.__file__).resolve().parent


@pytest.fixture
def tables_root(tmp_path, monkeypatch):
    root = tmp_path / "tables-root"
    root.mkdir()
    shutil.copytree(TELEMETRY_DIR / "tables", root / "tables")
    shutil.copy(TELEMETRY_DIR / "tables.snapshot.json", root / "tables.snapshot.json")
    monkeypatch.setenv(tables.ROOT_ENV, str(root))
    return root


def _all_member_names() -> set:
    names = set()
    for name in tables.TABLE_NAMES:
        names |= set(tables.load(name).members)
    return names


def _literal_collections_of_members(path: Path) -> list:
    members = _all_member_names()
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            names = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(names) >= 2 and all(n in members for n in names):
                found.append((path.name, node.lineno, names))
    return found


def _row(table: str, **where):
    conn = sqlite3.connect(str(config_root.telemetry_db()))
    try:
        conn.row_factory = sqlite3.Row
        clause = " AND ".join(f"{k} = ?" for k in where)
        return conn.execute(f"SELECT * FROM {table} WHERE {clause}", list(where.values())).fetchone()
    finally:
        conn.close()


def test_every_gating_check_reads_its_table(tables_root):
    for module in (TELEMETRY_DIR / "store.py", TELEMETRY_DIR / "emit.py"):
        assert _literal_collections_of_members(module) == []
    tables.add_member("roles", "auditor")
    outcome = store.insert_rows("sessions", [{"session_id": "s-new-role", "role": "auditor"}])
    assert outcome == {"inserted": 1, "deferred": 0, "failed": 0}
    assert _row("sessions", session_id="s-new-role")["role"] == "auditor"
    refused = store.insert_rows("sessions", [{"session_id": "s-bad", "role": "not-a-role"}])
    assert refused["failed"] == 1
    assert _row("sessions", session_id="s-bad") is None


def test_added_member_accepted_and_existing_rows_resolve(tables_root):
    store.insert_rows("sessions", [{"session_id": "before", "role": "root"}])
    with pytest.raises(tables.TableError):
        tables.validate("roles", "auditor")
    version = tables.add_member("roles", "auditor")
    assert tables.validate("roles", "auditor") == version
    with pytest.raises(tables.TableError):
        tables.validate("roles", "auditor", core_version=1)
    old = _row("sessions", session_id="before")
    assert tables.resolves("roles", old["role"]) and tables.known_version("roles", old["role_table_version"])


def test_retired_member_refused_on_new_rows_old_rows_resolve(tables_root):
    store.insert_rows("sessions", [{"session_id": "old-judge", "role": "judge"}])
    version = tables.retire_member("roles", "judge")
    with pytest.raises(tables.TableError):
        tables.validate("roles", "judge")
    assert store.insert_rows("sessions", [{"session_id": "new-judge", "role": "judge"}])["failed"] == 1
    assert tables.resolves("roles", "judge")
    assert tables.validate("roles", "judge", core_version=1) == 1
    old = _row("sessions", session_id="old-judge")
    assert old["role"] == "judge" and old["role_table_version"] < version
    assert tables.known_version("roles", old["role_table_version"])


def test_no_member_removed_since_snapshot(tables_root):
    assert tables.check_consistency() == []
    snapshot = tables.snapshot()
    for name in tables.TABLE_NAMES:
        current = set(tables.load(name).members)
        for version, state in snapshot[name].items():
            assert set(state["members"]) <= current, (name, version)
    toml = tables_root / "tables" / "roles.toml"
    text = toml.read_text(encoding="utf-8")
    start = text.index('[[members]]\nname = "judge"')
    end = text.index("[[members]]", start + 1)
    toml.write_text(text[:start] + text[end:], encoding="utf-8")
    assert any("roles" in problem for problem in tables.check_consistency())


def test_rows_store_the_table_version_they_were_validated_against(tables_root):
    store.insert_rows("sessions", [{"session_id": "v1", "role": "root"}])
    assert _row("sessions", session_id="v1")["role_table_version"] == 1
    version = tables.add_member("roles", "auditor")
    assert version == 2
    store.insert_rows("sessions", [{"session_id": "v2", "role": "auditor"}])
    assert _row("sessions", session_id="v2")["role_table_version"] == 2
    store.insert_rows("token_usage", [{"message_id": "m1", "role": "root", "phase": "planning"}])
    usage = _row("token_usage", message_id="m1")
    assert (usage["role_table_version"], usage["phase_table_version"]) == (2, 1)
    tables.add_member("loss_modes", "bespoke_loss")
    store.insert_rows("losses", [{"session_id": "s", "loss_mode": "bespoke_loss", "count": 1}])
    assert _row("losses", session_id="s", loss_mode="bespoke_loss")["loss_mode_table_version"] == 2


def test_table_version_bumped_on_every_add_or_retire_and_recorded_in_snapshot_history(tables_root):
    assert tables.load("phases").version == 1
    assert tables.add_member("phases", "review") == 2
    assert tables.retire_member("phases", "other") == 3
    assert tables.load("phases").version == 3
    history = tables.snapshot()["phases"]
    assert sorted(history, key=int) == ["1", "2", "3"]
    assert "review" in history["2"]["members"] and "other" in history["2"]["members"]
    assert history["3"]["retired"] == ["other"]
    assert tables.check_consistency() == []
    assert tables.load("roles").version == 1


def test_validate_refuses_non_string_and_unknown_pinned_version(tables_root):
    for value in (None, 3, ["root"]):
        with pytest.raises(tables.TableError):
            tables.validate("roles", value)
    with pytest.raises(tables.TableError):
        tables.validate("roles", "root", core_version=99)
    with pytest.raises(tables.TableError):
        tables.load("nope")
