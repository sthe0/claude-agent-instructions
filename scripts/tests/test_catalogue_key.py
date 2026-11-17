"""The narrowed result key of a registered mutation catalogue (``scripts/lib/catalogue_key.py``) in the store.

Difficulty removed: a catalogue pass that depends on its module, the files it rewrites, the tests that must kill
its mutants and what they import must be reused across edits of anything else, rerun on an edit of any of those,
never outlive the age bound and never survive a failing run. The end-to-end behaviour on a real suite run is
``gate_speedup_control.py mutation-scenario``.
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
MODULE, REWRITTEN, HELPER, NAMED_IMPORT, LITERAL, UNRELATED = (
    "scripts/tests/cat_control.py", "scripts/lib/target.py", "scripts/tests/helper.py", "scripts/lib/imported.py",
    "scripts/lib/read.txt", "scripts/lib/unrelated.py")
SOURCES = {
    MODULE: "import helper\nfrom lib import target\n",
    REWRITTEN: "T = 1\n",
    HELPER: "H = 1\n",
    "scripts/tests/test_cat.py": "def test_every_mutant_is_killed():\n    pass\n",
    "scripts/tests/test_named.py": f'from lib import imported\n\n\ndef test_kills():\n    return "{LITERAL}"\n',
    NAMED_IMPORT: "I = 1\n",
    LITERAL: "read\n",
    UNRELATED: "U = 1\n",
}
DECLARED = ([REWRITTEN], [NAMED])


def environment():
    return rs.current_environment(FP)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "core"
    for rel, text in SOURCES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / ck.REGISTRY_PATH).write_text(
        f'[[catalogue]]\nname = "cat"\nmodule = "{MODULE}"\ntests = ["{CAT}"]\n')
    return root


@pytest.fixture
def registry(tree):
    return ck.load(tree, {}, declared=lambda core, module: DECLARED)


@pytest.fixture
def store(tmp_path):
    with rs.ResultStore(tmp_path / "s" / "results.sqlite") as opened:
        yield opened


def traced(tree):
    """What a run of the catalogue test traces: the whole tree, as a catalogue that copies it does."""
    resolve = rs.tree_resolver(tree)
    return [(rs.FILE, p, resolve(rs.FILE, p)) for p in sorted(SOURCES)]


def stored(store, tree, registry, now=None, durations=None):
    return store.put(CAND, environment(), {CAT: traced(tree)}, now=now, registry=registry, durations=durations,
                     clean_tree=rs.tree_resolver(tree))


def lookup(store, tree, registry, max_age=3600.0, now=None):
    return store.lookup([CAT], environment(), rs.tree_resolver(tree), registry, max_age, now)


def test_an_edit_outside_the_declared_set_keeps_the_catalogue_pass(store, tree, registry):
    stored(store, tree, registry)
    (tree / UNRELATED).write_text("U = 2\n")
    assert CAT in lookup(store, tree, registry)


@pytest.mark.parametrize("edited", [REWRITTEN, MODULE, HELPER, NAMED_IMPORT, LITERAL, "scripts/tests/test_named.py",
                                    "scripts/tests/test_cat.py"])
def test_an_edit_inside_the_declared_set_reruns_the_catalogue(store, tree, registry, edited):
    stored(store, tree, registry, durations={CAT: 7.0})
    (tree / edited).write_text("def test_kills():\n    pass\n\ndef test_every_mutant_is_killed():\n    pass\n"
                               if edited.endswith(".py") and "test_" in edited else "changed\n")
    assert CAT not in lookup(store, tree, registry)


def test_the_set_is_what_the_registry_declares_and_names_nothing_a_run_traced(tree, registry):
    subject = registry.find(CAT)
    assert subject.paths == {MODULE, REWRITTEN, HELPER, NAMED_IMPORT, LITERAL, "scripts/tests/test_cat.py",
                             "scripts/tests/test_named.py"}
    assert subject.literal == {LITERAL} and subject.rewritten == {REWRITTEN}
    assert UNRELATED not in subject.paths


def test_a_stored_row_holds_the_declared_set_not_the_trace(store, tree, registry):
    stored(store, tree, registry)
    assert {path for _kind, path, _digest in store.rows_of(CAT)[0]["deps"]} == registry.find(CAT).paths


def test_without_a_clean_tree_no_catalogue_row_is_written(store, tree, registry):
    assert store.put(CAND, environment(), {CAT: traced(tree)}, registry=registry) == 0


def test_a_pass_older_than_the_bound_is_not_reused_and_a_reuse_keeps_the_pass_time(store, tree, registry):
    stored(store, tree, registry, now=1000.0, durations={CAT: 5.0})
    assert lookup(store, tree, registry, max_age=100.0, now=1050.0)[CAT].duration_s == 5.0
    assert store.rows_of(CAT)[0]["stored_at"] == 1000.0
    assert CAT not in lookup(store, tree, registry, max_age=100.0, now=1101.0)


def test_a_test_the_registry_does_not_name_is_keyed_on_everything_it_read_and_has_no_age_bound(store, tree, registry):
    store.put(CAND, environment(), {OTHER: traced(tree)}, registry=registry, now=1.0)
    found = store.lookup([OTHER], environment(), rs.tree_resolver(tree), registry, 3600.0, 1e9)
    assert OTHER in found
    (tree / UNRELATED).write_text("U = 2\n")
    assert OTHER not in store.lookup([OTHER], environment(), rs.tree_resolver(tree), registry, 3600.0)


def test_forget_deletes_every_stored_row_of_the_given_ids_only(store, tree, registry):
    stored(store, tree, registry)
    store.put(CAND, environment(), {OTHER: traced(tree)})
    assert store.forget([CAT]) == 1
    assert store.rows_of(CAT) == [] and store.rows_of(OTHER)


def test_a_narrowed_row_is_not_reused_when_the_registry_no_longer_names_its_test(store, tree, registry):
    stored(store, tree, registry)
    lost = ck.load(tree, {}, declared=lambda core, module: None)
    assert lost.find(CAT) is None
    assert CAT not in lookup(store, tree, lost)
    assert CAT in lookup(store, tree, registry)


def test_a_literal_python_file_brings_its_import_closure(tmp_path):
    (tmp_path / "scripts/tests").mkdir(parents=True)
    (tmp_path / "scripts/tests/t.py").write_text('RUN = "scripts/tests/tool.py"\n')
    (tmp_path / "scripts/tests/tool.py").write_text("import deep\n")
    (tmp_path / "scripts/tests/deep.py").write_text("D = 1\n")
    assert ck.literal_files(tmp_path, ["scripts/tests/t.py"]) == {"scripts/tests/tool.py", "scripts/tests/deep.py"}


def test_a_literal_path_into_git_metadata_stays_out_of_the_set(tmp_path):
    # Any git command of the run rewrites .git/index; a set holding it is never held, so never stored.
    (tmp_path / "scripts/tests").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/index").write_bytes(b"DIRC")
    (tmp_path / "config.md").write_text("x\n")
    (tmp_path / "scripts/tests/t.py").write_text('A = ".git/index"\nB = ".git"\nC = "../../.git/index"\nD = "config.md"\n')
    assert ck.literal_files(tmp_path, ["scripts/tests/t.py"]) == {"config.md"}


def _settle_catalogue(tmp_path, tree, registry, digests, edit_during_run):
    from test_result_store import finished_run, plan_in, traced as write_trace

    write_trace(tmp_path / "trace", [CAT])
    plan = plan_in(tmp_path)
    plan = type(plan)(plan.store, plan.environment, plan.tracer_dir, plan.out_prefix, plan.hits, registry, tree, digests)
    if edit_during_run:
        (tree / REWRITTEN).write_text("T = 99\n")
    from lib import suite_gate

    return plan, suite_gate.settle_reuse(plan, CAND, finished_run(tmp_path, [CAT], exit_code=0))


def test_a_catalogue_whose_set_changed_during_the_run_is_not_stored(tmp_path, tree, registry):
    resolve = rs.tree_resolver(tree)
    digests = {(ck.FILE, p): resolve(ck.FILE, p) for p in registry.find(CAT).paths}
    plan, settled = _settle_catalogue(tmp_path, tree, registry, digests, edit_during_run=True)
    assert settled.stored_results == 0
    with rs.ResultStore(plan.store) as opened:
        assert opened.rows_of(CAT) == []


def test_a_catalogue_whose_set_held_is_stored_at_the_digests_taken_before_the_run(tmp_path, tree, registry):
    resolve = rs.tree_resolver(tree)
    digests = {(ck.FILE, p): resolve(ck.FILE, p) for p in registry.find(CAT).paths}
    plan, settled = _settle_catalogue(tmp_path, tree, registry, digests, edit_during_run=False)
    assert settled.stored_results == 1
    with rs.ResultStore(plan.store) as opened:
        assert {(k, p): d for k, p, d in opened.rows_of(CAT)[0]["deps"]} == digests


def test_a_failing_catalogue_run_deletes_its_stored_pass_even_when_the_run_was_not_hermetic(tmp_path, tree, registry):
    from test_result_store import finished_run, plan_in

    from lib import suite_gate

    plan = plan_in(tmp_path)
    plan = type(plan)(plan.store, plan.environment, plan.tracer_dir, plan.out_prefix, plan.hits, registry, tree, {})
    with rs.ResultStore(plan.store) as opened:
        stored(opened, tree, registry)
    suite_gate.settle_reuse(plan, CAND, finished_run(tmp_path, [CAT], failed={CAT}, exit_code=1, hermetic=False))
    with rs.ResultStore(plan.store) as opened:
        assert opened.rows_of(CAT) == []


def test_the_excluded_path_is_dropped_from_every_set(tree):
    registry = ck.load(tree, {ck.EXCLUDE_ENV: REWRITTEN}, declared=lambda core, module: DECLARED)
    subject = registry.find(CAT)
    assert REWRITTEN not in subject.paths and subject.uncovered([REWRITTEN]) == [REWRITTEN]


def test_an_excluded_rewritten_file_no_longer_reruns_the_catalogue(store, tree):
    registry = ck.load(tree, {ck.EXCLUDE_ENV: REWRITTEN}, declared=lambda core, module: DECLARED)
    stored(store, tree, registry)
    (tree / REWRITTEN).write_text("T = 2\n")
    assert CAT in lookup(store, tree, registry)


def test_the_gate_refuses_the_control_setting_unless_its_store_is_private():
    assert ck.control_refusal({ck.EXCLUDE_ENV: "x"}, private_store=False)
    assert not ck.control_refusal({ck.EXCLUDE_ENV: "x"}, private_store=True)
    assert not ck.control_refusal({}, private_store=False)


def test_a_catalogue_whose_module_cannot_be_read_is_left_out(tree):
    assert ck.load(tree, {}, declared=lambda core, module: None).subjects == ()


@pytest.mark.parametrize("named", [["scripts/tests/test_named.py::test_missing"], ["scripts/tests/absent.py::test_kills"],
                                   []])
def test_a_catalogue_whose_named_test_cannot_be_found_is_left_out(tree, named):
    assert ck.load(tree, {}, declared=lambda core, module: ([REWRITTEN], named)).subjects == ()


def test_a_catalogue_whose_test_file_cannot_be_parsed_is_left_out(tree):
    (tree / "scripts/tests/test_cat.py").write_text("def broken(:\n")
    assert ck.load(tree, {}, declared=lambda core, module: DECLARED).subjects == ()


def test_the_age_bound_default_equals_the_config_row():
    row = next(line for line in (Path(__file__).resolve().parents[2] / "config.md").read_text().splitlines()
               if line.startswith(f"| `{ck.MAX_AGE_KEY}`"))
    assert f"`{ck.DEFAULT_MAX_AGE_H}`" in row.split("|")[2]
    assert ck.max_age_s("/nonexistent/config.md") == ck.DEFAULT_MAX_AGE_H * 3600


def test_the_stale_hook_drops_only_old_catalogue_rows(store, tree, registry):
    stored(store, tree, registry, now=1000.0)
    store.put(CAND, environment(), {OTHER: traced(tree)}, now=1000.0)
    assert store.drop_stale_catalogue_rows(500.0, now=2000.0) == 1
    assert store.rows_of(CAT) == [] and store.rows_of(OTHER)


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
