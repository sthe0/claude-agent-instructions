"""dep_tracer records, per test, the files it depended on, and marks a test always-rerun when it cannot see them all.

The planted cases run in a nested pytest over a throwaway repository (`_make_repo`), so each case
has exactly one expected dependency set. Run alone: `python3 -m pytest -p no:xdist scripts/tests/test_dep_tracer.py`.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import dep_tracer

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent.parent

OUTCOME_SAMPLE = [
    *(f"scripts/tests/test_argv_text.py::{name}" for name in (
        "test_plain_text_returned_verbatim", "test_at_file_returns_contents", "test_double_at_is_an_escaped_literal",
        "test_escaped_literal_is_never_probed_as_a_path", "test_missing_file_raises_naming_the_path_and_the_escape",
        "test_a_realistically_long_path_is_named_in_full", "test_a_payload_far_above_path_max_is_still_truncated",
        "test_the_two_bounds_are_separate_and_derived", "test_bare_at_raises",
        "test_directory_reference_raises_rather_than_leaking_an_oserror",
        "test_non_utf8_file_raises_cleanly_not_a_unicodedecodeerror", "test_none_stays_none",
        "test_empty_string_stays_empty_string", "test_file_contents_beginning_with_at_are_returned_untouched",
        "test_file_contents_are_rstripped", "test_list_none_stays_none", "test_list_empty_stays_empty",
        "test_list_is_element_wise", "test_list_propagates_a_missing_reference",
        "test_staged_text_round_trips_through_read_arg_text", "test_staged_reference_is_short_enough_for_argv",
    )),
    *(f"scripts/tests/test_backlog_title_parse.py::{name}" for name in (
        "test_multi_span_target_parses_intact", "test_single_span_target_unwrapped", "test_worklist_title_is_issue_title",
    )),
    *(f"scripts/tests/test_band_throttle.py::{name}" for name in (
        "test_records_and_reads_back_a_band", "test_sessions_do_not_share_a_stamp",
        "test_prefix_keeps_stamps_as_files_in_a_shared_directory", "test_session_id_cannot_escape_the_root",
        "test_empty_session_id_gets_a_stable_name", "test_unwritable_root_does_not_raise_and_reads_as_never_fired",
        "test_corrupt_stamp_reads_as_never_fired", "test_stamp_replaced_by_a_directory_reads_as_never_fired",
        "test_write_prunes_stamps_past_the_age_limit", "test_pruning_leaves_recent_stamps_alone",
        "test_pruning_only_touches_its_own_prefix",
    )),
    *(f"scripts/tests/test_budget_calibration.py::{name}" for name in (
        "test_group_by_kind_tier_counts_and_stats", "test_percentile_single_and_empty",
        "test_kind_tier_skips_rows_without_cost", "test_task_type_excludes_in_thread_rows",
        "test_flag_raises_when_p90_over_tier", "test_flag_lowers_when_median_far_below_tier",
        "test_no_flag_below_min_samples", "test_no_flag_for_unknown_tier",
        "test_check_flags_and_exits_nonzero", "test_check_silent_when_calibrated", "test_report_prints_both_groups",
    )),
    "scripts/tests/test_ast_purity.py::test_import_subprocess_is_impure",
    "scripts/tests/test_codeowners.py::test_codeowners_file_exists",
    "scripts/tests/test_codeowners.py::test_protected_core_glob_is_owned[/CLAUDE.md]",
    "scripts/tests/test_codeowners.py::test_protected_core_glob_is_owned[/skills/]",
]

PLANTED = {
    "conftest.py": "",
    # c01: an imported module
    "c01_mod.py": "def f():\n    return 1\n",
    "test_c01_import.py": "import c01_mod\n\n\ndef test_it():\n    assert c01_mod.f() == 1\n",
    # c02: a data file read
    "data/c02.txt": "hello",
    "test_c02_data.py": (
        "from pathlib import Path\n\n\ndef test_it():\n"
        "    assert (Path(__file__).parent / 'data' / 'c02.txt').read_text() == 'hello'\n"
    ),
    # c03: a Python subprocess that imports a helper
    "c03_helper.py": "VALUE = 3\n",
    "c03_script.py": "import c03_helper\nprint(c03_helper.VALUE)\n",
    "test_c03_subproc.py": (
        "import subprocess\nimport sys\nfrom pathlib import Path\n\n\ndef test_it():\n"
        "    here = Path(__file__).parent\n"
        "    out = subprocess.run([sys.executable, str(here / 'c03_script.py')], cwd=here,\n"
        "                         capture_output=True, text=True, check=True)\n"
        "    assert out.stdout.strip() == '3'\n"
    ),
    # c04: a shell script
    "c04.sh": "echo hi\n",
    "test_c04_shell.py": (
        "import subprocess\nfrom pathlib import Path\n\n\ndef test_it():\n"
        "    here = Path(__file__).parent\n"
        "    subprocess.run(['bash', str(here / 'c04.sh')], cwd=here, capture_output=True, check=True)\n"
    ),
    # c05: a Python child started without the tracer variables
    "test_c05_env.py": (
        "import os\nimport subprocess\nimport sys\n\n\ndef test_it():\n"
        "    subprocess.run([sys.executable, '-c', 'print(1)'], env={'PATH': os.environ.get('PATH', '')},\n"
        "                   capture_output=True, check=True)\n"
    ),
    # c06: os.system on a shell script
    "c06.sh": "echo hi\n",
    "test_c06_system.py": (
        "import os\nfrom pathlib import Path\n\n\ndef test_it():\n"
        "    assert os.system(f\"bash {Path(__file__).parent / 'c06.sh'}\") == 0\n"
    ),
    # c07: a multiprocessing child (forked) that reads a file
    "c07.txt": "x",
    "test_c07_fork.py": (
        "import multiprocessing\nfrom pathlib import Path\n\n\n"
        "def _read(path):\n    Path(path).read_text()\n\n\n"
        "def test_it():\n"
        "    proc = multiprocessing.get_context('fork').Process(target=_read, args=(str(Path(__file__).parent / 'c07.txt'),))\n"
        "    proc.start()\n    proc.join()\n    assert proc.exitcode == 0\n"
    ),
    # c08: a subdirectory listing
    "listed/a.txt": "a",
    "listed/b.txt": "b",
    "test_c08_listing.py": (
        "import os\nfrom pathlib import Path\n\n\ndef test_it():\n"
        "    assert sorted(os.listdir(Path(__file__).parent / 'listed')) == ['a.txt', 'b.txt']\n"
    ),
    # c09-c11: tree-wide reads
    "test_c09_root_listing.py": "import os\nfrom pathlib import Path\n\n\ndef test_it():\n    assert os.listdir(Path(__file__).parent)\n",
    "test_c10_walk.py": "import os\nfrom pathlib import Path\n\n\ndef test_it():\n    assert list(os.walk(Path(__file__).parent))\n",
    "test_c11_rglob.py": "from pathlib import Path\n\n\ndef test_it():\n    assert list(Path(__file__).parent.rglob('*.txt'))\n",
    # c12: git against the repository
    "test_c12_git.py": (
        "import subprocess\nfrom pathlib import Path\n\n\ndef test_it():\n"
        "    try:\n        subprocess.run(['git', '-C', str(Path(__file__).parent), 'status', '--short'], capture_output=True)\n"
        "    except FileNotFoundError:\n        pass\n"
    ),
    # c13: a two-level import chain
    "c13_b.py": "LIMIT = 3\n",
    "c13_a.py": "from c13_b import LIMIT\n",
    "test_c13_chain.py": "import c13_a\n\n\ndef test_it():\n    assert c13_a.LIMIT == 3\n",
    # c14: existence probes
    "c14_present.txt": "data",
    "test_c14a_present.py": "from pathlib import Path\n\n\ndef test_it():\n    assert (Path(__file__).parent / 'c14_present.txt').exists()\n",
    "test_c14b_absent.py": "from pathlib import Path\n\n\ndef test_it():\n    assert not (Path(__file__).parent / 'c14_absent.txt').exists()\n",
    "test_c14c_dir.py": "from pathlib import Path\n\n\ndef test_it():\n    assert (Path(__file__).parent / 'listed').is_dir()\n",
    # c15: a size probe
    "c15.bin": "1234",
    "test_c15_size.py": "import os\nfrom pathlib import Path\n\n\ndef test_it():\n    assert os.path.getsize(Path(__file__).parent / 'c15.bin') > 0\n",
    # c16: sqlite
    "test_c16_sqlite.py": (
        "import sqlite3\nfrom pathlib import Path\n\n\ndef test_it():\n"
        "    sqlite3.connect(str(Path(__file__).parent / 'c16.db')).close()\n"
    ),
    "test_c16b_memory.py": "import sqlite3\n\n\ndef test_it():\n    sqlite3.connect(':memory:').close()\n",
    # c17: a conftest imports the module the test's import chain also reaches
    "c17/conftest.py": "import c17_b  # noqa: F401\n",
    "c17/c17_b.py": "LIMIT = 1\n",
    "c17/c17_a.py": "from c17_b import LIMIT\n",
    "c17/test_c17.py": "import c17_a\n\n\ndef test_it():\n    assert c17_a.LIMIT == 1\n",
    # c18: a computed import
    "c18_x.py": "VALUE = 1\n",
    "c18_mod.py": "import importlib\n\n\ndef load(suffix):\n    return importlib.import_module('c18_' + suffix)\n",
    "test_c18_dynamic.py": "import c18_mod\n\n\ndef test_it():\n    assert c18_mod.load('x').VALUE == 1\n",
    # c20: a module-scoped fixture reads a file; only its users depend on it
    "c20/c20.txt": "d",
    "c20/conftest.py": (
        "from pathlib import Path\n\nimport pytest\n\n\n@pytest.fixture(scope='module')\n"
        "def c20_data():\n    return (Path(__file__).parent / 'c20.txt').read_text()\n"
    ),
    "c20/test_c20.py": (
        "def test_first(c20_data):\n    assert c20_data == 'd'\n\n\n"
        "def test_second(c20_data):\n    assert c20_data == 'd'\n\n\ndef test_third():\n    assert True\n"
    ),
    # c21: a file the test writes itself and then reads is an output, not an input
    "test_c21_write.py": (
        "from pathlib import Path\n\n\ndef test_it():\n"
        "    out = Path(__file__).parent / 'c21_out.txt'\n    out.write_text('x')\n    assert out.read_text() == 'x'\n"
    ),
}


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _env(record_dir: Path, root: Path, enabled: bool = True) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in (dep_tracer.CHILD_DIR_ENV, "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
                        "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", dep_tracer.ENABLE_ENV)}
    env["PYTHONPATH"] = os.pathsep.join([str(TESTS_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env[dep_tracer.DIR_ENV] = str(record_dir)
    env[dep_tracer.ROOT_ENV] = str(root)
    if enabled:
        env[dep_tracer.ENABLE_ENV] = "1"
    return env


def _pytest(root: Path, record_dir: Path, *args: str, enabled: bool = True, plugin: bool = True):
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root), *args]
    if plugin:
        cmd[3:3] = ["-p", "dep_tracer"]
    return subprocess.run(cmd, cwd=root, env=_env(record_dir, root, enabled), capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="module")
def traced(tmp_path_factory):
    root = _make_repo(tmp_path_factory.mktemp("planted").resolve(), PLANTED)
    record_dir = tmp_path_factory.mktemp("records")
    run = _pytest(root, record_dir, ".")
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    return {"root": root, "records": dep_tracer.load_records(record_dir)}


def assert_record(traced, nodeid, files, *, reasons=(), probes=None, listings=None):
    record = traced["records"][nodeid]
    assert sorted(record["files"]) == sorted(files)
    assert record["always_rerun"] == sorted(reasons)
    assert record["probes"] == (probes or {})
    assert record["listings"] == (listings or {})
    for rel, digest in record["files"].items():
        assert digest == sha((traced["root"] / rel).read_bytes())


def test_imported_module_is_recorded(traced):
    assert_record(traced, "test_c01_import.py::test_it", {"conftest.py", "test_c01_import.py", "c01_mod.py"})


def test_data_file_read_is_recorded(traced):
    assert_record(traced, "test_c02_data.py::test_it", {"conftest.py", "test_c02_data.py", "data/c02.txt"})


def test_python_subprocess_helper_recorded(traced):
    assert_record(traced, "test_c03_subproc.py::test_it",
                  {"conftest.py", "test_c03_subproc.py", "c03_script.py", "c03_helper.py"})


def test_shell_script_child_forces_rerun(traced):
    assert_record(traced, "test_c04_shell.py::test_it", {"conftest.py", "test_c04_shell.py"}, reasons=["non-python-subprocess"])


def test_python_child_without_tracer_env_forces_rerun(traced):
    assert_record(traced, "test_c05_env.py::test_it", {"conftest.py", "test_c05_env.py"}, reasons=["child-env-dropped"])


def test_os_system_on_a_script_forces_rerun(traced):
    assert_record(traced, "test_c06_system.py::test_it", {"conftest.py", "test_c06_system.py"}, reasons=["untraced-exec"])


def test_forked_multiprocessing_child_forces_rerun(traced):
    assert_record(traced, "test_c07_fork.py::test_it", {"conftest.py", "test_c07_fork.py"}, reasons=["untraced-fork"])


def test_subdirectory_listing_is_keyed_on_its_names(traced):
    assert_record(traced, "test_c08_listing.py::test_it", {"conftest.py", "test_c08_listing.py"},
                  listings={"listed": sha(b"a.txt\nb.txt")})


@pytest.mark.parametrize("case", ["test_c09_root_listing", "test_c10_walk", "test_c11_rglob"])
def test_tree_wide_reads_force_rerun(traced, case):
    record = traced["records"][f"{case}.py::test_it"]
    assert record["always_rerun"] == ["repo-wide-listing"]
    assert "." in record["listings"]


def test_git_against_the_repository_forces_rerun(traced):
    assert_record(traced, "test_c12_git.py::test_it", {"conftest.py", "test_c12_git.py"}, reasons=["git-repo"])


def test_import_chain_reaches_the_second_level(traced):
    assert_record(traced, "test_c13_chain.py::test_it", {"conftest.py", "test_c13_chain.py", "c13_a.py", "c13_b.py"})


def test_existence_probe_records_present_state_as_content(traced):
    assert_record(traced, "test_c14a_present.py::test_it", {"conftest.py", "test_c14a_present.py"},
                  probes={"c14_present.txt": sha(b"data")})


def test_existence_probe_records_absent_state(traced):
    assert_record(traced, "test_c14b_absent.py::test_it", {"conftest.py", "test_c14b_absent.py"},
                  probes={"c14_absent.txt": "absent"})


def test_directory_probe_records_dir(traced):
    assert_record(traced, "test_c14c_dir.py::test_it", {"conftest.py", "test_c14c_dir.py"}, probes={"listed": "dir"})


def test_size_probe_records_the_content_hash(traced):
    assert_record(traced, "test_c15_size.py::test_it", {"conftest.py", "test_c15_size.py"}, probes={"c15.bin": sha(b"1234")})


def test_sqlite_inside_the_repository_forces_rerun(traced):
    assert_record(traced, "test_c16_sqlite.py::test_it", {"conftest.py", "test_c16_sqlite.py"}, reasons=["sqlite3"])


def test_sqlite_in_memory_is_not_a_reason(traced):
    assert_record(traced, "test_c16b_memory.py::test_it", {"conftest.py", "test_c16b_memory.py"})


def test_import_already_loaded_by_a_conftest_is_still_a_dependency(traced):
    assert_record(traced, "c17/test_c17.py::test_it",
                  {"conftest.py", "c17/conftest.py", "c17/test_c17.py", "c17/c17_a.py", "c17/c17_b.py"})


def test_computed_import_name_forces_rerun(traced):
    assert_record(traced, "test_c18_dynamic.py::test_it",
                  {"conftest.py", "test_c18_dynamic.py", "c18_mod.py", "c18_x.py"}, reasons=["dynamic-import"])


def test_scoped_fixture_reads_belong_to_every_user_of_the_fixture(traced):
    base = {"conftest.py", "c20/conftest.py", "c20/test_c20.py"}
    assert_record(traced, "c20/test_c20.py::test_first", base | {"c20/c20.txt"})
    assert_record(traced, "c20/test_c20.py::test_second", base | {"c20/c20.txt"})
    assert_record(traced, "c20/test_c20.py::test_third", base)


def test_a_file_the_test_wrote_before_reading_is_not_a_dependency(traced):
    assert_record(traced, "test_c21_write.py::test_it", {"conftest.py", "test_c21_write.py"})


def test_tracer_off_by_default(tmp_path):
    root = _make_repo((tmp_path / "repo").resolve(), {"test_x.py": "def test_it():\n    pass\n"})
    record_dir = tmp_path / "records"
    run = _pytest(root, record_dir, ".", enabled=False)
    assert run.returncode == 0, run.stdout
    assert not record_dir.exists() or not list(record_dir.iterdir())


def test_per_worker_files_under_xdist(tmp_path):
    pytest.importorskip("xdist")
    tests = {f"test_w{i}.py": "def test_it():\n    pass\n" for i in range(4)}
    root = _make_repo((tmp_path / "repo").resolve(), tests)
    record_dir = tmp_path / "records"
    run = _pytest(root, record_dir, "-n", "2", ".")
    assert run.returncode == 0, run.stdout
    names = sorted(p.name for p in record_dir.glob("*.jsonl"))
    assert names and all(n.startswith("gw") for n in names)
    assert sorted(dep_tracer.load_records(record_dir)) == [f"test_w{i}.py::test_it" for i in range(4)]


def test_outcomes_equal_with_and_without_tracer(tmp_path):
    outcomes = {}
    for enabled in (False, True):
        report = tmp_path / f"report-{enabled}.xml"
        record_dir = tmp_path / f"records-{enabled}"
        env = _env(record_dir, REPO, enabled)
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "--rootdir", str(REPO),
             f"--junitxml={report}", *OUTCOME_SAMPLE],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
        assert run.returncode == 0, run.stdout[-3000:]
        cases = ET.parse(report).getroot().iter("testcase")
        outcomes[enabled] = {f"{c.get('classname')}.{c.get('name')}": [child.tag for child in c] for c in cases}
    assert len(set(OUTCOME_SAMPLE)) == 50
    assert len(outcomes[True]) == len(OUTCOME_SAMPLE)
    assert outcomes[True] == outcomes[False]
    assert sorted(dep_tracer.load_records(tmp_path / "records-True")) == sorted(OUTCOME_SAMPLE)


def test_requires_python_312():
    assert sys.version_info >= (3, 12), "dep_tracer needs Python 3.12 (sys.monitoring)"


def test_a_taken_monitoring_tool_id_makes_the_tracer_unavailable(monkeypatch):
    sys.monitoring.use_tool_id(5, "occupied")
    try:
        monkeypatch.setattr(dep_tracer, "TOOL_ID", 5)
        assert dep_tracer._start_monitoring() is False
    finally:
        sys.monitoring.free_tool_id(5)


def test_docstring_documents_record_format():
    doc = dep_tracer.__doc__
    assert dep_tracer.ENABLE_ENV in doc
    assert "<repo>/.pytest_cache/core-dep-tracer" in doc and dep_tracer.DIR_ENV in doc
    for code in dep_tracer.REASON_CODES:
        assert f"``{code}``" in doc or code in doc, code
    for token in ("listings", "probes", "always_rerun", "listing_digest", "probe_digest", "file_digest", "gw0.jsonl"):
        assert token in doc, token


def test_digests_of_file_listing_and_probe(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"abc")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "g.txt").write_text("g")
    assert dep_tracer.file_digest(str(tmp_path / "f.txt")) == sha(b"abc")
    assert dep_tracer.file_digest(str(tmp_path / "none")) == "absent"
    assert dep_tracer.file_digest(str(tmp_path)) == "dir"
    assert dep_tracer.listing_digest(str(tmp_path)) == sha(b"f.txt\ng.txt")
    assert dep_tracer.listing_digest(str(tmp_path / "none")) == "absent"
    assert dep_tracer.probe_digest(str(tmp_path)) == "dir"
    (tmp_path / "ln").symlink_to("f.txt")
    assert dep_tracer.probe_digest(str(tmp_path / "ln"), follow=False) == "symlink:f.txt"
    assert dep_tracer.probe_digest(str(tmp_path / "ln")) == sha(b"abc")


def test_import_closure_follows_relative_function_level_and_literal_dynamic_imports(tmp_path):
    _make_repo(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/a.py": "from . import b\nfrom .c import X\n\n\ndef f():\n    import d\n",
        "pkg/b.py": "",
        "pkg/c.py": "X = 1\n",
        "d.py": "import importlib\nimportlib.import_module('lit')\n",
        "lit.py": "",
        "other.py": "",
    })
    files, reasons = dep_tracer.import_closure([str(tmp_path / "pkg/a.py")], search=(str(tmp_path),))
    assert {Path(f).relative_to(tmp_path).as_posix() for f in files} == {
        "pkg/a.py", "pkg/__init__.py", "pkg/b.py", "pkg/c.py", "d.py", "lit.py"}
    assert reasons == set()


def test_import_closure_flags_computed_names_and_unparsable_files(tmp_path):
    _make_repo(tmp_path, {"a.py": "__import__(name)\n", "bad.py": "def (:\n"})
    _, reasons = dep_tracer.import_closure([str(tmp_path / "a.py"), str(tmp_path / "bad.py")], search=(str(tmp_path),))
    assert reasons == {"dynamic-import", "unanalysable-source"}


def test_load_records_rejects_an_unknown_line_kind(tmp_path):
    (tmp_path / "main.jsonl").write_text('{"kind": "bogus"}\n')
    with pytest.raises(ValueError, match="unknown record kind"):
        dep_tracer.load_records(tmp_path)
