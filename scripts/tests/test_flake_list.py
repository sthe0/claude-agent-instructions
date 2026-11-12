"""The flaky-test list and its journal: append, fold into scripts/tests/flaky.toml, drop once landed."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from lib import flake_list
from lib import suite_gate as gate
from test_suite_gate import build_world, git

TEST_A = "scripts/tests/test_a.py::test_one"
TEST_B = 'scripts/tests/test_b.py::test_two[a "quoted" id]'
FLAKY_TOML = Path(__file__).resolve().parent / "flaky.toml"
NEW_LIST = f"{flake_list.LIST_PATH}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return build_world(tmp_path, monkeypatch)


def entry(test, at, entry_id):
    return {"id": entry_id, "test": test, "candidate": f"{entry_id:c<40}", "tip": "b" * 40, "at": at}


def listed(world, sha):
    return flake_list.parse_list(git(world.work, "show", f"{sha}:{NEW_LIST}"))


def land_fold(world, fold):
    """Make the fold commit what origin/main is, as a landing does."""
    git(world.work, "push", "-q", "origin", f"{fold.sha}:refs/heads/main")
    git(world.work, "fetch", "-q", "origin")


# ── the file ──────────────────────────────────────────────────────────────

def test_the_committed_list_is_the_header_and_an_empty_table():
    text = FLAKY_TOML.read_text(encoding="utf-8")
    assert text == flake_list.render_list({}) and flake_list.parse_list(text) == {}


def test_a_list_round_trips_ids_with_brackets_and_quotes():
    table = {TEST_B: {"count": 2, "first_seen": "2026-11-01T00:00:00Z", "last_seen": "2026-11-02T00:00:00Z"},
             TEST_A: {"count": 1, "first_seen": "2026-11-03T00:00:00Z", "last_seen": "2026-11-03T00:00:00Z"}}
    assert flake_list.parse_list(flake_list.render_list(table)) == table


def test_due_lines_name_only_the_tests_that_reached_the_review_count():
    table = {TEST_A: {"count": flake_list.REVIEW_COUNT, "first_seen": "x", "last_seen": "y"},
             TEST_B: {"count": flake_list.REVIEW_COUNT - 1, "first_seen": "x", "last_seen": "y"}}
    (line,) = flake_list.due_lines(table)
    assert line.startswith(TEST_A)


def test_the_journal_path_is_in_the_common_dir_unless_the_environment_names_one(tmp_path, monkeypatch):
    monkeypatch.delenv(flake_list.JOURNAL_ENV, raising=False)
    assert flake_list.journal_path(tmp_path) == tmp_path / "suite-gate" / "flaky-pending.jsonl"
    monkeypatch.setenv(flake_list.JOURNAL_ENV, str(tmp_path / "private.jsonl"))
    assert flake_list.journal_path(tmp_path) == tmp_path / "private.jsonl"


# ── the journal ───────────────────────────────────────────────────────────

def test_concurrent_appends_keep_every_line_whole(tmp_path):
    journal = tmp_path / "j.jsonl"

    def writer(n):
        for k in range(20):
            flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", f"{n}-{k}")])

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({e["id"] for e in flake_list.read_entries(journal)}) == 120


def test_a_torn_or_malformed_line_is_skipped_and_an_absent_journal_is_empty(tmp_path):
    journal = tmp_path / "j.jsonl"
    assert flake_list.read_entries(journal) == []
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "one")])
    with open(journal, "a", encoding="utf-8") as fh:
        fh.write('{"id": "torn", "test"\n[1, 2]\n')
    assert [e["id"] for e in flake_list.read_entries(journal)] == ["one"]


# ── folding ───────────────────────────────────────────────────────────────

def test_the_fold_adds_an_unlisted_id_with_its_journal_count_and_first_and_last_seen(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-02T00:00:00Z", "e2"),
                                        entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    fold = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    assert listed(world, fold.sha) == {
        TEST_A: {"count": 2, "first_seen": "2026-11-01T00:00:00Z", "last_seen": "2026-11-02T00:00:00Z"}}


def test_the_fold_raises_a_listed_ids_count_by_its_journal_count(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    first = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-05T00:00:00Z", "e2"),
                                        entry(TEST_A, "2026-11-06T00:00:00Z", "e3")])
    second = flake_list.fold(world.work, journal, first.sha, gate.GIT_IDENTITY)
    assert listed(world, second.sha)[TEST_A] == {
        "count": 3, "first_seen": "2026-11-01T00:00:00Z", "last_seen": "2026-11-06T00:00:00Z"}
    assert second.entry_ids == ("e2", "e3")


def test_the_fold_is_one_separate_commit_with_the_entry_ids_in_a_trailer(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1"),
                                        entry(TEST_B, "2026-11-01T00:00:01Z", "e2")])
    fold = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    assert git(world.work, "log", "-1", "--format=%s", fold.sha) == "tests: record flaky tests"
    assert git(world.work, "log", "-1", "--format=%(trailers:key=Flake-Entries,valueonly)", fold.sha) == "e1 e2"
    assert git(world.work, "rev-parse", f"{fold.sha}^") == world.base
    assert git(world.work, "diff", "--name-only", world.base, fold.sha).splitlines() == [NEW_LIST]


def test_the_fold_leaves_the_working_tree_and_the_branch_alone(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    assert git(world.work, "status", "--porcelain") == "" and world.head() == world.base


def test_nothing_pending_is_no_fold(world, tmp_path):
    assert flake_list.fold(world.work, tmp_path / "absent.jsonl", world.base, gate.GIT_IDENTITY) is None


def test_an_entry_folded_twice_is_counted_once(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    first = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    assert flake_list.fold(world.work, journal, first.sha, gate.GIT_IDENTITY) is None
    flake_list.append_entries(journal, [entry(TEST_B, "2026-11-02T00:00:00Z", "e2")])
    second = flake_list.fold(world.work, journal, first.sha, gate.GIT_IDENTITY)
    assert second.entry_ids == ("e2",) and listed(world, second.sha)[TEST_A]["count"] == 1


# ── dropping what landed ──────────────────────────────────────────────────

def test_the_journal_drops_the_entries_origin_main_carries_and_keeps_the_rest(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    fold = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    land_fold(world, fold)
    flake_list.append_entries(journal, [entry(TEST_B, "2026-11-02T00:00:00Z", "e2")])
    assert flake_list.drop_landed(world.work, journal, "origin/main") == 1
    assert [e["id"] for e in flake_list.read_entries(journal)] == ["e2"]


def test_dropping_is_idempotent(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    land_fold(world, flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY))
    assert flake_list.drop_landed(world.work, journal, "origin/main") == 1
    assert flake_list.drop_landed(world.work, journal, "origin/main") == 0
    assert flake_list.read_entries(journal) == []


def test_an_entry_the_trunk_does_not_carry_stays_in_the_journal(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    assert flake_list.drop_landed(world.work, journal, "origin/main") == 0
    assert [e["id"] for e in flake_list.read_entries(journal)] == ["e1"]


# ── reproducible fold, deduplicated journal, torn lines, leftovers ────────

def test_the_same_journal_on_the_same_tip_gives_the_same_fold_commit(world, tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_B, "2026-11-01T00:00:01Z", "e2"),
                                        entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    first = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    time.sleep(1.1)
    second = flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY)
    assert first.sha == second.sha


def test_one_test_of_one_candidate_and_tip_is_journaled_once(tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [flake_list.new_entry(TEST_A, "c" * 40, "b" * 40, "2026-11-01T00:00:00Z")])
    flake_list.append_entries(journal, [flake_list.new_entry(TEST_A, "c" * 40, "b" * 40, "2026-11-01T01:00:00Z"),
                                        flake_list.new_entry(TEST_A, "d" * 40, "b" * 40, "2026-11-01T02:00:00Z"),
                                        flake_list.new_entry(TEST_B, "c" * 40, "b" * 40, "2026-11-01T02:00:00Z")])
    assert [(e["test"], e["candidate"]) for e in flake_list.read_entries(journal)] == [
        (TEST_A, "c" * 40), (TEST_A, "d" * 40), (TEST_B, "c" * 40)]


def test_an_append_after_a_torn_line_does_not_glue_onto_it(tmp_path):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    with open(journal, "a", encoding="utf-8") as fh:
        fh.write('{"id": "torn", "te')
    flake_list.append_entries(journal, [entry(TEST_B, "2026-11-02T00:00:00Z", "e2")])
    assert [e["id"] for e in flake_list.read_entries(journal)] == ["e1", "e2"]


def test_a_failed_rewrite_leaves_no_temporary_file(world, tmp_path, monkeypatch):
    journal = tmp_path / "j.jsonl"
    flake_list.append_entries(journal, [entry(TEST_A, "2026-11-01T00:00:00Z", "e1")])
    land_fold(world, flake_list.fold(world.work, journal, world.base, gate.GIT_IDENTITY))

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(flake_list.os, "replace", broken)
    with pytest.raises(OSError):
        flake_list.drop_landed(world.work, journal, "origin/main")
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-")] == []
