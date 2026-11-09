"""The structured cause of a critique: the rules between its four fields, the flags that carry
it, its path into the causes table of the telemetry record, and every place that tells an agent
how to call `agentctl critique`."""
from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import tomllib
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentctl import cli, mandate, runtime_host
from agentctl.state import Critique, Declaration, Difficulty, Investigation, Node, SessionState
from lib import config_root
from lib.telemetry import cause, live, store, sync, tables
from telemetry_live_support import no_background_sweep, query, spool_events
from test_difficulty import _declare, _investigate, _to_failed_stage1
from test_telemetry_tables import tables_root  # noqa: F401  (fixture)

SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent

CRITIQUE_SCAN_TREE = ("scripts", "skills", "docs", "cursor", "CLAUDE.md", "README.md")
CAUSE_FLAGS = ("--cause-category", "--machinery-induced", "--mechanism", "--functional-place")
ENGINE_CLIS = ("agentctl-cli.py", "agentctl-own.py")
INVOCATION = re.compile(r"(?<![\w-])(?:agentctl|agentctl-cli\.py|agentctl-own\.py)\s+critique\b")
MARKDOWN_SUFFIXES = (".md", ".mdc")
SPAN = re.compile(r"`([^`\n]+)`")

VALID_ELSE = dict(cause_category="implementation_defect", machinery_induced="false",
                  mechanism="none", functional_place="verification")
VALID_FIELDS = dict(cause_category="implementation_defect", machinery_induced=False,
                    mechanism="none", functional_place="verification")


@pytest.fixture(autouse=True)
def _live_env(monkeypatch):
    """A cold bootstrap on a loaded machine can outlast the production 250 ms; a test's
    verdict must not depend on machine load."""
    monkeypatch.setattr(store, "DEADLINE_S", 30.0)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(runtime_host, "detect_host", lambda: runtime_host.HOST_CLAUDE)
    monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv(live.SWITCH_ENV, raising=False)
    no_background_sweep(monkeypatch)


def _two_mechanisms_of_different_places() -> tuple:
    entries = {n: m for n, m in tables.load("registry").members.items() if m.attrs.get("place")}
    first = next(iter(entries))
    other = next(n for n, m in entries.items() if m.attrs["place"] != entries[first].attrs["place"])
    return first, entries[first].attrs["place"], other, entries[other].attrs["place"]


def _machinery(mechanism: str, **over) -> dict:
    return {**VALID_FIELDS, "machinery_induced": True, "mechanism": mechanism,
            "functional_place": None, **over}


# --- the rules between the four fields ---------------------------------------------------------

def test_cause_fields_accept_only_table_members(tables_root):
    assert cause.validate_cause(dict(VALID_FIELDS)) == []
    mechanism, _, _, _ = _two_mechanisms_of_different_places()

    category = cause.validate_cause({**VALID_FIELDS, "cause_category": "invented_category"})
    assert category and category[0].startswith("cause_category")
    place = cause.validate_cause({**VALID_FIELDS, "functional_place": "invented_place"})
    assert place and place[0].startswith("functional_place")
    bogus = cause.validate_cause(_machinery("invented:mechanism"))
    assert bogus and bogus[0].startswith("mechanism")
    assert cause.validate_cause(_machinery(mechanism)) == []

    tables.add_member("categories", "brand_new_kind")
    tables.add_member("places", "brand_new_place")
    assert cause.validate_cause({**VALID_FIELDS, "cause_category": "brand_new_kind",
                                 "functional_place": "brand_new_place"}) == []


def test_mechanism_none_iff_not_machinery_induced():
    mechanism, _, _, _ = _two_mechanisms_of_different_places()
    with_registry_id = cause.validate_cause({**VALID_FIELDS, "mechanism": mechanism})
    assert any(v.startswith("mechanism") for v in with_registry_id)
    without = cause.validate_cause(_machinery(cause.NO_MECHANISM))
    assert any(v.startswith("mechanism") for v in without)
    assert cause.validate_cause(_machinery(mechanism)) == []
    flag = cause.validate_cause({**VALID_FIELDS, "machinery_induced": None})
    assert any(v.startswith("machinery_induced") for v in flag)


def test_functional_place_derived_from_registry_and_mismatch_refused():
    mechanism, place, _, other_place = _two_mechanisms_of_different_places()

    derived, violated = cause.complete_cause(_machinery(mechanism))
    assert violated == [] and derived["functional_place"] == place

    refused, violated = cause.complete_cause(_machinery(mechanism, functional_place=other_place))
    assert refused is None and any(v.startswith("functional_place") for v in violated)
    refused, violated = cause.complete_cause(_machinery(mechanism, functional_place=cause.NO_PLACE))
    assert refused is None and any(v.startswith("functional_place") for v in violated)
    accepted, violated = cause.complete_cause(_machinery(mechanism, functional_place=place))
    assert violated == [] and accepted["functional_place"] == place

    unplaced, violated = cause.complete_cause({**VALID_FIELDS, "functional_place": cause.NO_PLACE})
    assert violated == [] and unplaced["functional_place"] == cause.NO_PLACE


def test_other_category_requires_note():
    other = {**VALID_FIELDS, "cause_category": cause.OTHER_CATEGORY}
    assert any(v.startswith("cause_note") for v in cause.validate_cause(other))
    assert any(v.startswith("cause_note") for v in cause.validate_cause({**other, "cause_note": "  "}))
    stored, violated = cause.complete_cause({**other, "cause_note": "  a stray cause "})
    assert violated == [] and stored["cause_note"] == "a stray cause"


def test_validate_cause_accepts_unknown_only_when_imported():
    row = _machinery(cause.UNKNOWN_MECHANISM, functional_place="execution")
    live_violations = cause.validate_cause(row)
    assert live_violations and live_violations[0].startswith("mechanism")
    assert cause.validate_cause(row, imported=True) == []


def test_validate_cause_unknown_mechanism_needs_a_non_none_place():
    for place in (cause.NO_PLACE, None, "invented_place"):
        row = _machinery(cause.UNKNOWN_MECHANISM, functional_place=place)
        violated = cause.validate_cause(row, imported=True)
        assert violated and violated[0].startswith("functional_place"), place


def test_cause_row_stores_table_version(tables_root):
    mechanism, _, _, _ = _two_mechanisms_of_different_places()
    first, violated = cause.complete_cause(_machinery(mechanism))
    assert violated == []
    assert first["cause_category_table_version"] == tables.load("categories").version
    assert first["mechanism_table_version"] == tables.load("registry").version
    assert first["functional_place_table_version"] == tables.load("places").version

    version = tables.add_member("categories", "brand_new_kind")
    later, _ = cause.complete_cause({**VALID_FIELDS, "cause_category": "brand_new_kind"})
    assert later["cause_category_table_version"] == version > first["cause_category_table_version"]


def test_registry_places_and_mandate_vocabulary_are_one_list():
    assert tables.resolves("places", mandate.ITEM_PLACE)
    assert cause.validate_cause({**VALID_FIELDS, "functional_place": mandate.ITEM_PLACE}) == []
    norm = mandate.item_norm_toml(
        mandate=mandate.new_mandate("core-debt", "alice", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        issue=7, title="A title", body="A body", cycle_id="c1", base_sha="abc123", bound=200,
        worktree="/w/item-7", repo="/r", item_dir="/m/items/7")
    assert tomllib.loads(norm)["meta"]["order"]["place"] == mandate.ITEM_PLACE


# --- the flags of `agentctl critique` ----------------------------------------------------------

def _diagnosing(store_, venued_fixtures_dir, sid: str = "c1") -> None:
    _to_failed_stage1(store_, sid, str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare(store_, sid)
    _investigate(store_, sid)


def _critique(store_, sid: str = "c1", **flags):
    return cli.cmd_critique(Namespace(session=sid, functional_ground="fg", replanning_task="rt",
                                      failure_address="resource", **flags), store=store_)


def _critique_events(sid: str) -> list:
    return [e for e in spool_events(sid) if e["kind"] == "critique"]


def test_critique_refuses_record_without_the_four_cause_fields(store, venued_fixtures_dir):
    _diagnosing(store, venued_fixtures_dir)
    for omitted in ("cause_category", "machinery_induced", "mechanism", "functional_place"):
        flags = {k: v for k, v in VALID_ELSE.items() if k != omitted}
        directive = _critique(store, **flags)
        assert directive.ok is False, omitted
        violated = directive.data["cause_violations"]
        assert violated == [f"{omitted}: required (--{omitted.replace('_', '-')})"]
        assert store.load("c1").difficulty.critique is None
    assert _critique(store).ok is False
    assert _critique_events("c1") == []

    mechanism, place, _, _ = _two_mechanisms_of_different_places()
    placeless = _critique(store, cause_category="implementation_defect", machinery_induced="true",
                          mechanism=mechanism)
    assert placeless.ok is True, placeless
    assert store.load("c1").difficulty.cause["functional_place"] == place


def test_unknown_mechanism_refused_on_live_critique(store, venued_fixtures_dir):
    _diagnosing(store, venued_fixtures_dir)
    directive = _critique(store, cause_category="implementation_defect", machinery_induced="true",
                          mechanism=cause.UNKNOWN_MECHANISM, functional_place="execution")
    assert directive.ok is False
    assert any(v.startswith("mechanism") for v in directive.data["cause_violations"])
    assert store.load("c1").difficulty.critique is None
    assert _critique_events("c1") == []


def test_retired_member_refused_on_new_critique(store, venued_fixtures_dir, tables_root):
    _diagnosing(store, venued_fixtures_dir)
    tables.retire_member("categories", "implementation_defect")
    directive = _critique(store, **VALID_ELSE)
    assert directive.ok is False
    assert any(v.startswith("cause_category") for v in directive.data["cause_violations"])
    assert _critique(store, **{**VALID_ELSE, "cause_category": "spec_shift_or_underspecified"}).ok is True


# --- the path into the causes table -------------------------------------------------------------

def _causes(sid: str = "c1") -> list:
    return query("SELECT * FROM causes WHERE session = ?", sid)


def test_live_critique_cause_reaches_the_causes_table(store, venued_fixtures_dir):
    _diagnosing(store, venued_fixtures_dir)
    mechanism, place, _, _ = _two_mechanisms_of_different_places()
    directive = _critique(store, cause_category="implementation_defect", machinery_induced="true",
                          mechanism=mechanism)
    assert directive.ok is True, directive

    events = _critique_events("c1")
    assert len(events) == 1 and events[0]["fields"]["source_key"] == "cli:critique"
    sync.run(session="c1")
    rows = _causes()
    assert len(rows) == 1
    row = dict(rows[0])
    record_origin = query("SELECT value FROM meta WHERE key = 'origin'")[0][0]
    assert row["cause_id"] == events[0]["event_id"] and row["origin"] == record_origin
    assert (row["cause_category"], row["machinery_induced"], row["mechanism"], row["functional_place"]) == (
        "implementation_defect", 1, mechanism, place)
    assert row["difficulty_index"] == 0
    sync.run(session="c1")
    assert len(_causes()) == 1


def test_cause_note_is_stored(store, venued_fixtures_dir):
    _diagnosing(store, venued_fixtures_dir)
    assert _critique(store, cause_category="other", machinery_induced="false", mechanism="none",
                     functional_place="none", cause_note="the trigger lay outside every category").ok
    sync.run(session="c1")
    assert [r["cause_note"] for r in _causes()] == ["the trigger lay outside every category"]
    assert store.load("c1").difficulty.cause["cause_note"] == "the trigger lay outside every category"


def test_cause_row_carries_the_versions_it_was_validated_against(store, venued_fixtures_dir):
    _diagnosing(store, venued_fixtures_dir)
    assert _critique(store, **VALID_ELSE).ok
    sync.run(session="c1")
    row = dict(_causes()[0])
    assert row["cause_category_table_version"] == tables.load("categories").version
    assert row["mechanism_table_version"] == tables.load("registry").version
    assert row["functional_place_table_version"] == tables.load("places").version


def test_row_with_retired_member_stays_valid(store, venued_fixtures_dir, tables_root):
    _diagnosing(store, venued_fixtures_dir)
    assert _critique(store, **VALID_ELSE).ok
    recorded_in = tables.load("categories").version
    tables.retire_member("categories", "implementation_defect")
    sync.run(session="c1")
    row = dict(_causes()[0])
    assert row["cause_category"] == "implementation_defect"
    assert row["cause_category_table_version"] == recorded_in
    assert tables.resolves("categories", "implementation_defect")
    assert tables.validate("categories", "implementation_defect", core_version=recorded_in) == recorded_in


def test_critique_waits_in_the_spool_while_the_record_is_unavailable(store, venued_fixtures_dir, monkeypatch,
                                                                     tmp_path):
    _diagnosing(store, venued_fixtures_dir)
    usable = config_root.telemetry_db()
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AGENTCTL_TELEMETRY_DB", str(blocker / "telemetry.sqlite"))
    assert _critique(store, **VALID_ELSE).ok
    assert len(_critique_events("c1")) == 1

    monkeypatch.setenv("AGENTCTL_TELEMETRY_DB", str(usable))
    assert config_root.telemetry_db() == usable
    sync.run(session="c1")
    assert [r["cause_category"] for r in _causes()] == ["implementation_defect"]


def test_migration_004_creates_the_causes_table_keyed_by_cause_id():
    conn = store.open_record()
    try:
        assert conn is not None
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(causes)")}
    finally:
        conn.close()
    assert [name for name, row in columns.items() if row[5]] == ["cause_id"]
    for name in (*cause.CAUSE_FIELDS, cause.NOTE_FIELD, "origin", "session", "difficulty_index",
                 *(column for _, column in cause.VERSION_FIELDS.values())):
        assert name in columns, name
    with pytest.raises(sqlite3.IntegrityError):
        record = sqlite3.connect(str(config_root.telemetry_db()))
        try:
            for _ in range(2):
                record.execute(
                    "INSERT INTO causes (cause_id, session, cause_category, machinery_induced, mechanism, "
                    "functional_place, origin) VALUES ('same', 's', 'c', 0, 'none', 'none', 'live')")
        finally:
            record.close()


def test_prose_only_critique_records_stay_readable():
    state = SessionState(
        session_id="legacy", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=Difficulty(
            declaration=Declaration("e", "a", "m"),
            investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
            critique=Critique(functional_ground="fg", replanning_task="rt")))
    data = state.to_dict()
    data["difficulty"].pop("cause", None)
    restored = SessionState.from_dict(data)
    assert restored.difficulty.cause == {}
    assert restored.difficulty.critique.functional_ground == "fg"
    assert restored.to_dict()["difficulty"]["critique"]["replanning_task"] == "rt"
    assert cli._difficulty_id(restored.difficulty) == cli._difficulty_id(state.difficulty)


# --- every invocation of `agentctl critique` shows the cause flags -----------------------------

def tree_files(root: Path, tree=CRITIQUE_SCAN_TREE) -> "list[Path]":
    """The files the scan reads: git-tracked ones when `root` is a checkout, else every file."""
    if (root / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-z", "--", *tree], cwd=root, capture_output=True,
                             text=True, check=True).stdout
        return sorted(root / p for p in out.split("\0") if p and (root / p).is_file())
    found = []
    for name in tree:
        target = root / name
        found += [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    return found


def _joined(lines: "list[str]") -> "list[tuple[int, str]]":
    """Lines of a code block with backslash continuations joined; (first line number, text)."""
    joined, pending, start = [], "", 0
    for number, line in enumerate(lines, 1):
        if not pending:
            start = number
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        joined.append((start, pending + line))
        pending = ""
    if pending:
        joined.append((start, pending))
    return joined


def markdown_invocations(text: str) -> "list[tuple[int, str]]":
    """(line, command text) of every fenced-code line and inline code span holding a critique call."""
    found, fence = [], []
    lines = text.splitlines()
    inside, block_start = False, 0
    for number, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            if inside:
                found += [(block_start + n - 1, t) for n, t in _joined(fence) if INVOCATION.search(t)]
                fence = []
            inside, block_start = not inside, number + 1
        elif inside:
            fence.append(line)
        else:
            found += [(number, m.group(1)) for m in SPAN.finditer(line) if INVOCATION.search(m.group(1))]
    return found


def _names_engine_cli(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith(ENGINE_CLIS)
               for n in ast.walk(node))


def python_invocations(text: str) -> "list[tuple[int, str]]":
    """(line, the strings of the list) of every argv list whose element after the engine CLI is "critique"."""
    found = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.List):
            continue
        elts = node.elts
        for i, element in enumerate(elts[:-1]):
            follower = elts[i + 1]
            if (_names_engine_cli(element) and isinstance(follower, ast.Constant)
                    and follower.value == "critique"):
                strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
                           and isinstance(n.value, str)]
                found.append((node.lineno, " ".join(strings)))
                break
    return found


def invocations_in(path: Path) -> "list[tuple[int, str]]":
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix in MARKDOWN_SUFFIXES:
        return markdown_invocations(text)
    if path.suffix == ".py":
        return python_invocations(text)
    if path.suffix == ".sh":
        return [(n, t) for n, t in _joined(text.splitlines()) if INVOCATION.search(t)]
    return []


def invocations_missing_cause_flags(root: Path, tree=CRITIQUE_SCAN_TREE) -> "tuple[int, list[str]]":
    """(how many invocations were read, the `path:line` of each that lacks a cause flag)."""
    total, missing = 0, []
    for path in tree_files(root, tree):
        for line, text in invocations_in(path):
            total += 1
            absent = [flag for flag in CAUSE_FLAGS if flag not in text]
            if absent:
                missing.append(f"{path.relative_to(root)}:{line} lacks {', '.join(absent)}")
    return total, missing


def test_every_critique_invocation_shows_cause_flags():
    total, missing = invocations_missing_cause_flags(REPO)
    assert total >= 2
    assert missing == []


def test_invocation_rule_needs_the_agentctl_prefix_and_scans_the_tree_constant(tmp_path):
    bare = "run `critique --failure-address resource` now\n"
    full = " ".join(CAUSE_FLAGS)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        bare + "```\nagentctl-cli.py critique --session s \\\n  --functional-ground fg\n```\n", encoding="utf-8")
    total, missing = invocations_missing_cause_flags(tmp_path)
    assert total == 1 and len(missing) == 1 and missing[0].startswith("docs/guide.md:")
    assert "docs" in CRITIQUE_SCAN_TREE

    (tmp_path / "docs" / "guide.md").write_text(
        bare + f"```\nagentctl-cli.py critique --session s \\\n  {full}\n```\n"
        f"and `agentctl critique --session s {full}` inline\n", encoding="utf-8")
    assert invocations_missing_cause_flags(tmp_path) == (2, [])

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "caller.py").write_text(
        'ARGV = ["python3", str(ROOT / "agentctl-cli.py"), "critique", "--session", "s"]\n', encoding="utf-8")
    total, missing = invocations_missing_cause_flags(tmp_path)
    assert total == 3 and len(missing) == 1 and missing[0].startswith("scripts/caller.py:")
    (tmp_path / "scripts" / "caller.py").write_text(
        f'ARGV = ["python3", "agentctl-own.py", "critique", {", ".join(repr(f) for f in CAUSE_FLAGS)}]\n',
        encoding="utf-8")
    assert invocations_missing_cause_flags(tmp_path) == (3, [])
