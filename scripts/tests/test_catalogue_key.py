"""The narrowed result key of a registered mutation catalogue (``scripts/lib/catalogue_key.py``) in the store.

Difficulty removed: a catalogue pass that depends on its module, the files it rewrites and what its killing tests
read must be reused across edits of anything else, rerun on an edit of any of those, and never outlive the
age bound. The end-to-end behaviour on a real suite run is ``gate_speedup_control.py mutation-scenario``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib import catalogue_key as ck
from lib import gate_retention
from lib import result_store as rs

FP = {"dists": [["pytest", "8.0"]], "git": "git version 2.43.0"}
CAND = "c" * 40
CAT = "scripts/tests/test_cat.py::test_every_mutant_is_killed"
NAMED = "scripts/tests/test_named.py::test_kills"
OTHER = "scripts/tests/test_other.py::test_x"
MODULE, REWRITTEN, HELPER, NAMED_READ, UNRELATED = (
    "scripts/tests/cat_control.py", "scripts/lib/target.py", "scripts/lib/helper.py", "scripts/lib/read.py",
    "scripts/lib/unrelated.py")
CONTROL_SOURCE = "import helper\nfrom lib import target\n"


def environment():
    return rs.current_environment(FP)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "core"
    for rel, text in {MODULE: CONTROL_SOURCE, REWRITTEN: "T = 1\n", HELPER: "H = 1\n", NAMED_READ: "R = 1\n",
                      UNRELATED: "U = 1\n", "scripts/tests/test_cat.py": "x\n",
                      "scripts/tests/helper.py": "H = 1\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / ck.REGISTRY_PATH).write_text(
        f'[[catalogue]]\nname = "cat"\nmodule = "{MODULE}"\ntests = ["{CAT}"]\n')
    return root


@pytest.fixture
def registry(tree):
    return ck.load(tree, {}, declared=lambda core, module: ([REWRITTEN], [NAMED]))


@pytest.fixture
def store(tmp_path):
    with rs.ResultStore(tmp_path / "s" / "results.sqlite") as opened:
        yield opened


def deps_of_all(tree, extra=()):
    resolve = rs.tree_resolver(tree)
    paths = [MODULE, REWRITTEN, HELPER, NAMED_READ, UNRELATED, "scripts/tests/test_cat.py", *extra]
    return [(rs.FILE, p, resolve(rs.FILE, p)) for p in [*paths, "scripts/tests/helper.py"]]


def named_deps(tree):
    resolve = rs.tree_resolver(tree)
    return [(rs.FILE, NAMED_READ, resolve(rs.FILE, NAMED_READ)), (rs.FILE, REWRITTEN, resolve(rs.FILE, REWRITTEN))]


def stored(store, tree, registry, now=None, durations=None):
    return store.put(CAND, environment(), {CAT: deps_of_all(tree), NAMED: named_deps(tree)}, now=now,
                     registry=registry, durations=durations)


def lookup(store, tree, registry, max_age=3600.0, now=None):
    return store.lookup([CAT, NAMED], environment(), rs.tree_resolver(tree), registry, max_age, now)


def test_an_edit_outside_the_narrowed_set_keeps_the_catalogue_pass(store, tree, registry):
    stored(store, tree, registry)
    (tree / UNRELATED).write_text("U = 2\n")
    assert CAT in lookup(store, tree, registry)


@pytest.mark.parametrize("edited", [REWRITTEN, MODULE, NAMED_READ, "scripts/tests/helper.py", "scripts/tests/test_cat.py"])
def test_an_edit_inside_the_narrowed_set_reruns_the_catalogue(store, tree, registry, edited):
    stored(store, tree, registry, durations={CAT: 7.0})
    (tree / edited).write_text("changed\n")
    assert CAT not in lookup(store, tree, registry)


def test_the_import_closure_is_part_of_the_set(tree, registry):
    subject = registry.find(CAT)
    assert "scripts/tests/helper.py" in subject.paths and REWRITTEN in subject.paths
    assert UNRELATED not in subject.paths


def test_a_catalogue_whose_named_test_has_no_stored_result_is_not_stored(store, tree, registry):
    written = store.put(CAND, environment(), {CAT: deps_of_all(tree)}, registry=registry)
    assert written == 0 and CAT not in lookup(store, tree, registry)


def test_the_named_test_is_looked_up_by_its_newest_stored_row(store, tree, registry):
    store.put(CAND, environment(), {NAMED: named_deps(tree)})
    assert store.put(CAND, environment(), {CAT: deps_of_all(tree)}, registry=registry) == 1
    assert CAT in lookup(store, tree, registry)


def test_a_pass_older_than_the_bound_is_not_reused_and_a_reuse_keeps_the_pass_time(store, tree, registry):
    stored(store, tree, registry, now=1000.0, durations={CAT: 5.0})
    assert lookup(store, tree, registry, max_age=100.0, now=1050.0)[CAT].duration_s == 5.0
    assert store.rows_of(CAT)[0]["stored_at"] == 1000.0
    assert CAT not in lookup(store, tree, registry, max_age=100.0, now=1101.0)


def test_a_test_the_registry_does_not_name_is_keyed_on_everything_it_read(store, tree, registry):
    store.put(CAND, environment(), {OTHER: deps_of_all(tree)}, registry=registry)
    (tree / UNRELATED).write_text("U = 2\n")
    assert OTHER not in store.lookup([OTHER], environment(), rs.tree_resolver(tree), registry, 3600.0)


def test_the_excluded_path_is_dropped_from_every_set(tree):
    registry = ck.load(tree, {ck.EXCLUDE_ENV: REWRITTEN}, declared=lambda core, module: ([REWRITTEN], [NAMED]))
    subject = registry.find(CAT)
    assert REWRITTEN not in subject.paths and subject.uncovered([REWRITTEN]) == [REWRITTEN]


def test_a_planted_narrowing_is_reported_as_uncovered(tree, registry):
    subject = registry.find(CAT)
    assert subject.uncovered([REWRITTEN]) == []
    narrowed = ck.Subject(subject.name, subject.tests, subject.paths - {REWRITTEN}, subject.named)
    assert narrowed.uncovered([REWRITTEN]) == [REWRITTEN]


def test_the_gate_refuses_the_control_setting_unless_its_store_is_private():
    assert ck.control_refusal({ck.EXCLUDE_ENV: "x"}, private_store=False)
    assert not ck.control_refusal({ck.EXCLUDE_ENV: "x"}, private_store=True)
    assert not ck.control_refusal({}, private_store=False)


def test_a_catalogue_whose_module_cannot_be_read_is_left_out(tree):
    assert ck.load(tree, {}, declared=lambda core, module: None).subjects == ()


def test_the_age_bound_default_equals_the_config_row():
    row = next(line for line in (Path(__file__).resolve().parents[2] / "config.md").read_text().splitlines()
               if line.startswith(f"| `{ck.MAX_AGE_KEY}`"))
    assert f"`{ck.DEFAULT_MAX_AGE_H}`" in row.split("|")[2]
    assert ck.max_age_s("/nonexistent/config.md") == ck.DEFAULT_MAX_AGE_H * 3600


def test_the_stale_hook_drops_only_old_catalogue_rows(store, tree, registry):
    stored(store, tree, registry, now=1000.0)
    store.put(CAND, environment(), {OTHER: deps_of_all(tree)}, now=1000.0)
    assert store.drop_stale_catalogue_rows(500.0, now=2000.0) == 1
    assert store.rows_of(CAT) == [] and store.rows_of(NAMED) and store.rows_of(OTHER)


def test_the_registered_retention_hook_drops_the_old_catalogue_rows_of_the_live_store(tmp_path, tree, registry, monkeypatch):
    directory = tmp_path / "results"
    database = directory / rs.STORE_ENTRY / rs.DB_NAME
    with rs.ResultStore(database) as opened:
        stored(opened, tree, registry, now=1.0)
        opened.put(CAND, environment(), {OTHER: [(rs.FILE, "x", "d" * 64)]}, now=1.0)
    monkeypatch.setattr(ck, "max_age_s", lambda config_path=None: 60.0)
    rs.drop_stale(directory)
    with sqlite3.connect(database) as db:
        assert db.execute("select count(*) from result where catalogue=1").fetchone()[0] == 0
        assert db.execute("select count(*) from result where test_id=?", (OTHER,)).fetchone()[0] == 1
    assert gate_retention._STORES["results"].stale is rs.drop_stale
