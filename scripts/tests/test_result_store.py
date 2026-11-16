"""The content-keyed result store and the way the gate uses it (``scripts/lib/result_store.py``).

Difficulty removed: a stored pass must be reused exactly when nothing the test depends on changed. These tests
pin each component of the key (a changed dependency, a new file in a listed directory, a deleted file a test
only stat'ed, the interpreter, a distribution), that a failure is never stored and a test with no stored pass
always runs, and the pytest plugin that leaves the reused tests out. The end-to-end behaviour on a real
suite run is ``gate_speedup_control.py reuse-scenario``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from lib import gate_retention
from lib import result_store as rs
from lib import suite_gate as gate

TESTS = Path(__file__).resolve().parent
FP = {"dists": [["pytest", "8.0"], ["pytest-xdist", "3.5"]], "git": "git version 2.43.0"}
CAND = "c" * 40
T1, T2 = "scripts/tests/test_a.py::test_one", "scripts/tests/test_a.py::test_two"


def environment(fp=FP):
    return rs.current_environment(fp)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "core"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "a.py").write_text("A = 1\n")
    (root / "data.txt").write_text("d\n")
    return root


def deps_in(tree_root):
    resolve = rs.tree_resolver(tree_root)
    return [(rs.FILE, "lib/a.py", resolve(rs.FILE, "lib/a.py")),
            (rs.LISTING, "lib", resolve(rs.LISTING, "lib")),
            (rs.PROBE, "gone.txt", resolve(rs.PROBE, "gone.txt"))]


def hits(store, tree_root, ids=(T1,), env=None):
    return store.lookup(ids, env or environment(), rs.tree_resolver(tree_root))


@pytest.fixture
def store(tmp_path):
    with rs.ResultStore(tmp_path / "s" / "results.sqlite") as opened:
        yield opened


def test_pass_is_reused_while_nothing_it_depends_on_changed(store, tree):
    store.put(CAND, environment(), {T1: deps_in(tree)})
    found = hits(store, tree)
    assert found[T1].candidate_sha == CAND


def test_test_without_a_stored_pass_is_not_a_hit(store, tree):
    store.put(CAND, environment(), {T1: deps_in(tree)})
    assert T2 not in hits(store, tree, (T1, T2))


def test_changed_dependency_changes_the_key_and_the_hit_goes(store, tree):
    deps = deps_in(tree)
    key = rs.compute_key(T1, deps, environment())
    store.put(CAND, environment(), {T1: deps})
    (tree / "lib" / "a.py").write_text("A = 2\n")
    assert rs.compute_key(T1, deps_in(tree), environment()) != key
    assert hits(store, tree) == {}


def test_new_file_in_a_listed_directory_changes_the_key(store, tree):
    store.put(CAND, environment(), {T1: deps_in(tree)})
    (tree / "lib" / "new.py").write_text("")
    assert hits(store, tree) == {}


def test_deleting_a_file_the_test_only_statted_changes_the_key(store, tree):
    (tree / "gone.txt").write_text("x")
    store.put(CAND, environment(), {T1: deps_in(tree)})
    assert T1 in hits(store, tree)
    (tree / "gone.txt").unlink()
    assert hits(store, tree) == {}


def test_changed_interpreter_changes_the_key(monkeypatch, tree):
    deps = deps_in(tree)
    here = rs.compute_key(T1, deps, environment())
    monkeypatch.setattr(sys, "version_info", (3, 11, 0, "final", 0))
    assert rs.compute_key(T1, deps, environment()) != here


def test_a_chmod_with_unchanged_content_changes_the_key_and_the_hit_goes(store, tree):
    (tree / "lib" / "a.py").chmod(0o755)
    deps = deps_in(tree)
    key = rs.compute_key(T1, deps, environment())
    store.put(CAND, environment(), {T1: deps})
    assert T1 in hits(store, tree)
    (tree / "lib" / "a.py").chmod(0o644)
    assert rs.compute_key(T1, deps_in(tree), environment()) != key
    assert hits(store, tree) == {}


def test_changed_distribution_version_changes_the_key_and_the_hit_goes(store, tree):
    store.put(CAND, environment(), {T1: deps_in(tree)})
    newer = {**FP, "dists": [["pytest", "8.0"], ["pytest-xdist", "3.6"]]}
    assert hits(store, tree, env=environment(newer)) == {}
    assert T1 in hits(store, tree)


def test_changed_git_version_changes_the_key(tree):
    other = {**FP, "git": "git version 2.44.0"}
    deps = deps_in(tree)
    assert rs.compute_key(T1, deps, environment()) != rs.compute_key(T1, deps, environment(other))


def test_dependency_order_does_not_matter(tree):
    deps = deps_in(tree)
    assert rs.compute_key(T1, deps, environment()) == rs.compute_key(T1, reversed(deps), environment())


def test_at_most_three_rows_per_test_and_the_older_row_still_matches(store, tree):
    original = deps_in(tree)
    for n in range(4):
        (tree / "data.txt").write_text("x" * (n + 1) + "\n")  # sizes differ: the digest cache keys on (mtime, size)
        resolve = rs.tree_resolver(tree)
        store.put(CAND, environment(), {T1: [*original, (rs.FILE, "data.txt", resolve(rs.FILE, "data.txt"))]},
                  now=100 + n)
    assert len(store.rows_of(T1)) == rs.ROWS_PER_TEST
    (tree / "data.txt").write_text("xxx\n")
    assert T1 in hits(store, tree)


def test_forged_row_drops_a_dependency(store, tree):
    deps = deps_in(tree)
    store.put(CAND, environment(), {T1: deps})
    store.replace_deps(T1, deps[1:], environment())
    (tree / "lib" / "a.py").write_text("A = 3\n")
    assert T1 in hits(store, tree)


def test_store_registers_with_retention_and_eviction_drops_the_whole_entry(tmp_path):
    assert "results" in gate_retention._STORES
    entry = tmp_path / "suite-gate" / "results" / rs.STORE_ENTRY
    with rs.ResultStore(entry / rs.DB_NAME) as opened:
        opened.put(CAND, environment(), {T1: []})
    gate_retention._STORES["results"].evict(entry)
    assert not entry.exists()
    assert gate_retention._STORES["results"].path(tmp_path) == tmp_path / "suite-gate" / "results"


def test_store_path_follows_the_override(monkeypatch, tmp_path):
    assert rs.store_path(tmp_path) == tmp_path / "suite-gate" / "results" / rs.STORE_ENTRY / rs.DB_NAME
    monkeypatch.setenv(rs.STORE_ENV, str(tmp_path / "private.sqlite"))
    assert rs.store_path(tmp_path) == tmp_path / "private.sqlite"


def test_trim_drops_the_least_recently_used_rows(store, tree):
    for n in range(40):
        store.put(CAND, environment(), {f"t{n}": deps_in(tree)}, now=100 + n)
    assert store.trim(max_bytes=0) > 0
    assert store.count() == 0


def test_copy_store_gives_the_same_rows(store, tree, tmp_path):
    store.put(CAND, environment(), {T1: deps_in(tree)})
    rs.copy_store(store.path, tmp_path / "copy" / "results.sqlite")
    with rs.ResultStore(tmp_path / "copy" / "results.sqlite") as copy:
        assert T1 in hits(copy, tree)


def test_rows_naming_the_same_files_share_their_paths_and_digests(store):
    shared = [(rs.FILE, f"lib/m{n}.py", f"{n:064x}") for n in range(400)]
    for n in range(50):
        store.put(CAND, environment(), {f"t{n}": shared})
    assert store.rows_of("t7")[0]["deps"] == sorted(shared)
    assert len(store._db.execute("SELECT 1 FROM digest").fetchall()) == 400
    assert max(len(blob) for (blob,) in store._db.execute("SELECT deps FROM result")) < 2000


def test_a_database_of_another_schema_version_is_opened_empty(store, tree):
    store.put(CAND, environment(), {T1: deps_in(tree)})
    store.set_meta("schema", str(rs.SCHEMA_VERSION - 1))
    with rs.ResultStore(store.path) as reopened:
        assert reopened.count() == 0
        assert reopened.get_meta("schema") == str(rs.SCHEMA_VERSION)


def test_fingerprint_sees_a_distribution_on_the_given_pythonpath(tmp_path):
    dist = tmp_path / "site" / "core_control_dummy-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: core-control-dummy\nVersion: 1.0\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
    assert ["core-control-dummy", "1.0"] in rs.fingerprint(env)["dists"]


# ── the gate's side: what is stored after a run ───────────────────────────

def traced(path, ids, always=()):
    path.mkdir(parents=True, exist_ok=True)
    lines = [{"kind": "file", "id": 0, "path": "lib/a.py", "digest": "sha256:aa"}]
    lines += [{"kind": "test", "id": i, "files": [0], "listings": {}, "probes": {},
               "always_rerun": ["git-repo"] if i in always else []} for i in ids]
    (path / "main.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))


def finished_run(tmp_path, collected, failed=(), exit_code=1, hermetic=True):
    lines = [{"kind": "collected", "nodeid": i} for i in collected]
    lines += [{"kind": "report", "nodeid": i, "when": "call", "outcome": "failed" if i in failed else "passed"}
              for i in collected]
    report = tmp_path / "tests.jsonl"
    report.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return gate.SuiteRun(exit_code, tuple(failed), CAND, report_path=str(report), hermetic=hermetic)


def plan_in(tmp_path, hits_=None):
    return gate.ReusePlan(tmp_path / "db" / "r.sqlite", environment(), tmp_path / "trace", tmp_path / "out",
                          hits_ or {})


def test_a_failure_and_an_always_rerun_test_are_never_stored(tmp_path):
    ids = ["t::pass", "t::fail", "t::git"]
    traced(tmp_path / "trace", ids, always={"t::git"})
    plan = plan_in(tmp_path)
    settled = gate.settle_reuse(plan, CAND, finished_run(tmp_path, ids, failed={"t::fail"}))
    with rs.ResultStore(plan.store) as opened:
        assert opened.test_ids() == ["t::pass"]
        assert json.loads(opened.get_meta(rs.ALWAYS_RERUN_META)) == ["t::git"]
        assert json.loads(opened.get_meta(rs.FAILED_META)) == ["t::fail"]
    assert settled.stored_results == 1 and settled.always_rerun_tests == 1 and settled.reuse_active


def test_meta_round_trips_and_a_missing_name_is_none(tmp_path):
    with rs.ResultStore(tmp_path / "m.sqlite") as opened:
        assert opened.get_meta("nothing") is None
        opened.set_meta("name", "one")
        opened.set_meta("name", "two")
        assert opened.get_meta("name") == "two"


def test_a_run_that_did_not_finish_stores_nothing(tmp_path):
    traced(tmp_path / "trace", ["t::pass"])
    plan = plan_in(tmp_path)
    run = finished_run(tmp_path, ["t::pass"], exit_code=-9)
    assert gate.settle_reuse(plan, CAND, run).stored_results == 0


def test_a_run_that_was_not_hermetic_stores_nothing_and_says_so(tmp_path):
    traced(tmp_path / "trace", ["t::pass"])
    plan = plan_in(tmp_path)
    settled = gate.settle_reuse(plan, CAND, finished_run(tmp_path, ["t::pass"], exit_code=0, hermetic=False))
    assert settled.stored_results == 0 and "not hermetic" in settled.store_note
    assert not plan.store.exists()


@pytest.mark.parametrize("error", [zlib.error("bad blob"), KeyError(7), json.JSONDecodeError("x", "", 0)])
def test_a_store_that_fails_on_lookup_costs_the_reuse_not_the_run(monkeypatch, tmp_path, error):
    def failing_lookup(self, ids, environment, resolve):
        raise error

    monkeypatch.setattr(rs.ResultStore, "lookup", failing_lookup)
    monkeypatch.setattr(gate, "_common_dir", lambda repo: str(tmp_path / "git"))
    plan, note = gate.plan_reuse(tmp_path, tmp_path / "sandbox", {}, tmp_path)
    assert plan is None and "every test runs" in note and type(error).__name__ in note


@pytest.mark.parametrize("error", [zlib.error("bad blob"), KeyError(7)])
def test_a_store_that_fails_on_put_leaves_the_verdict_and_a_note(monkeypatch, tmp_path, error):
    def failing_put(self, sha, environment, passing):
        raise error

    monkeypatch.setattr(rs.ResultStore, "put", failing_put)
    traced(tmp_path / "trace", ["t::pass"])
    settled = gate.settle_reuse(plan_in(tmp_path), CAND, finished_run(tmp_path, ["t::pass"], exit_code=0))
    assert settled.pytest_exit == 0 and settled.stored_results == 0
    assert "not stored" in settled.store_note and type(error).__name__ in settled.store_note


def test_a_run_of_another_commit_stores_nothing(tmp_path):
    traced(tmp_path / "trace", ["t::pass"])
    plan = plan_in(tmp_path)
    assert gate.settle_reuse(plan, "d" * 40, finished_run(tmp_path, ["t::pass"], exit_code=0)).stored_results == 0


def test_only_ids_the_plugin_left_out_count_as_reused(tmp_path):
    traced(tmp_path / "trace", ["t::ran"])
    plan = plan_in(tmp_path, {"t::gone": rs.Hit("e" * 40, "k"), "t::out": rs.Hit("f" * 40, "k")})
    (tmp_path / "out.123").write_text("t::out\n")
    settled = gate.settle_reuse(plan, CAND, finished_run(tmp_path, ["t::ran"], exit_code=0))
    assert dict(settled.reused) == {"t::out": "f" * 40}


def test_record_lists_every_collected_id_with_its_source(tmp_path):
    traced(tmp_path / "trace", ["t::a", "t::b"])
    plan = plan_in(tmp_path, {"t::c": rs.Hit("e" * 40, "k")})
    (tmp_path / "out.1").write_text("t::c\n")
    run = finished_run(tmp_path, ["t::a", "t::b"], exit_code=0)
    settled = gate.settle_reuse(plan, CAND, run)
    Path(run.report_path).unlink()
    fields = gate.reuse_fields(settled)
    assert fields["test_sources"] == {"t::a": "ran", "t::b": "ran", "t::c": "reused"}
    assert fields["reused_from"] == {"t::c": "e" * 40}
    assert fields["results"]["ran"] == 2 and fields["results"]["reused"] == 1
    assert fields["results"]["always_rerun_reasons"] == {}


def test_a_run_that_left_every_test_out_as_reused_is_a_pass_not_no_tests(tmp_path):
    traced(tmp_path / "trace", [])
    plan = plan_in(tmp_path, {"t::c": rs.Hit("e" * 40, "k")})
    (tmp_path / "out.1").write_text("t::c\n")
    settled = gate.settle_reuse(plan, CAND, finished_run(tmp_path, [], exit_code=5))
    assert settled.pytest_exit == 0
    assert gate.observe(settled, CAND) == gate.Observation()


def test_a_run_that_collected_nothing_and_left_nothing_out_proves_nothing(tmp_path):
    traced(tmp_path / "trace", [])
    settled = gate.settle_reuse(plan_in(tmp_path), CAND, finished_run(tmp_path, [], exit_code=5))
    assert settled.pytest_exit == 5
    assert gate.observe(settled, CAND).reason


def test_suite_env_forwards_only_the_opt_in_extra_pythonpath(tmp_path):
    base = {"PYTHONPATH": "/elsewhere", rs.STORE_ENV: "/x", gate.EXTRA_PYTHONPATH_ENV: "/extra"}
    env = gate.suite_env(tmp_path, "/u", tmp_path / "f", (), base)
    assert env["PYTHONPATH"] == f"{tmp_path / 'plugin'}{os.pathsep}/extra"
    assert rs.STORE_ENV not in env and gate.EXTRA_PYTHONPATH_ENV not in env
    plain = gate.suite_env(tmp_path, "/u", tmp_path / "f", (), {"PYTHONPATH": "/elsewhere"})
    assert plain["PYTHONPATH"] == str(tmp_path / "plugin")


# ── the plugin ────────────────────────────────────────────────────────────

def test_plugin_leaves_out_the_named_tests_and_writes_the_ones_it_left_out(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a(): pass\n\ndef test_b(): assert False\n")
    ids = tmp_path / "ids.txt"
    ids.write_text("test_x.py::test_b\ntest_x.py::test_removed\n")
    env = {**os.environ, "PYTHONPATH": str(TESTS), "CORE_RESULT_REUSE_IDS": str(ids),
           "CORE_RESULT_REUSE_OUT": str(tmp_path / "out"), "PYTEST_ADDOPTS": ""}
    env.pop("CORE_TEST_REPORT", None)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "result_reuse_plugin", "-p", "no:cacheprovider",
                           "--rootdir", str(tmp_path), "-c", os.devnull, str(tmp_path / "test_x.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    assert "1 passed, 1 deselected" in proc.stdout
    left_out = [line for part in tmp_path.glob("out.*") for line in part.read_text().splitlines()]
    assert left_out == ["test_x.py::test_b"]


def test_plugin_does_nothing_without_the_gate_variables(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a(): pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORE_RESULT")}
    env["PYTHONPATH"] = str(TESTS)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "result_reuse_plugin", "-p", "no:cacheprovider",
                           "-c", os.devnull, "--rootdir", str(tmp_path), str(tmp_path / "test_x.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0 and "1 passed" in proc.stdout and not list(tmp_path.glob("out.*"))
