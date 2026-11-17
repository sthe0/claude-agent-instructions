"""The registry of mutation catalogues the gate reuses by a narrowed key (``mutation_catalogues.toml``).

Difficulty removed: a registered catalogue is reused across edits its narrowed set cannot see, so the set must
hold every file the catalogue's mutants rewrite and every test that must kill them, and a catalogue that offers
that information must not be left unregistered by accident. Each entry is tied to the catalogue module itself,
never to a list kept beside it.
"""
from __future__ import annotations

import ast
import fnmatch
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

from lib import catalogue_key as ck

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
ENTRIES = tomllib.loads((REPO / ck.REGISTRY_PATH).read_text(encoding="utf-8"))["catalogue"]


def module_of(entry: dict):
    tests_dir = str(REPO / Path(entry["module"]).parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    return importlib.import_module(Path(entry["module"]).stem)


def defined_in(file: Path, name: str) -> bool:
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
               for n in ast.walk(ast.parse(file.read_text(encoding="utf-8"))))


@pytest.fixture(scope="module")
def registry():
    return ck.load(REPO, {})


def test_every_entry_is_resolved_by_the_gate(registry):
    assert {s.name for s in registry.subjects} == {e["name"] for e in ENTRIES}


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_the_narrowed_set_holds_every_file_the_catalogue_rewrites(entry, registry):
    module = module_of(entry)
    subject = next(s for s in registry.subjects if s.name == entry["name"])
    rewritten = module.rewritten_files()
    assert rewritten and subject.uncovered(rewritten) == []
    assert all((REPO / path).is_file() for path in rewritten)
    assert entry["module"] in subject.paths


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_the_named_tests_exist(entry):
    named = module_of(entry).named_tests()
    assert named
    for test_id in named:
        file, _, rest = test_id.partition("::")
        assert (REPO / file).is_file(), test_id
        assert defined_in(REPO / file, rest.split("[")[0]), test_id


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_the_in_suite_tests_exist(entry):
    for test in entry["tests"]:
        file, _, name = test.partition("::")
        assert (REPO / file).is_file(), test
        assert not name or defined_in(REPO / file, name), test


NAMING_GLOBS = ("*_mutation_control.py", "*_mutants.py", "*_mutations.py")
EXEMPT = {
    "ag_mutation_control.py": "runs its own mutants and does not report rewritten files or killing tests",
    "gc_mutation_control.py": "runs its own mutants and does not report rewritten files or killing tests",
    "resource_grant_mutation_control.py": "runs its own mutants and does not report rewritten files or killing tests",
    "telemetry_mutation_control.py": "runs its own mutants and does not report rewritten files or killing tests",
    "claude_recover_mutants.py": "a mutant list read by its test, not a catalogue module with a control",
    "order_agreement_mutants.py": "a mutant list read by its test, not a catalogue module with a control",
    "plan_dashboard_mutations.py": "a mutant list read by its test, not a catalogue module with a control",
    "time_profile_mutants.py": "a mutant list read by its test, not a catalogue module with a control",
    "test_claude_recover_mutants.py": "an in-suite test file",
    "test_shell_tokens_mutations.py": "an in-suite test file",
    "test_text_rule_judge_mutations.py": "an in-suite test file",
    "test_time_profile_mutants.py": "an in-suite test file",
}


def test_every_module_with_a_catalogue_name_is_registered_or_exempt_with_a_reason():
    matching = {path.name for path in (SCRIPTS / "tests").iterdir()
                if any(fnmatch.fnmatch(path.name, glob) for glob in NAMING_GLOBS)}
    registered = {Path(e["module"]).name for e in ENTRIES}
    assert matching == registered | set(EXEMPT)
    assert not registered & set(EXEMPT) and all(EXEMPT.values())


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_every_entry_names_one_test_by_id(entry):
    assert all("::" in test for test in entry["tests"])


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_the_set_is_the_declared_files_and_no_more(entry, registry):
    subject = next(s for s in registry.subjects if s.name == entry["name"])
    module = module_of(entry)
    assert subject.rewritten == set(module.rewritten_files())
    assert subject.paths == (subject.rewritten | subject.module_closure | subject.named_closure | subject.literal)
    assert {entry["module"], *(t.partition("::")[0] for t in entry["tests"]),
            *(t.partition("::")[0] for t in module.named_tests())} <= subject.paths
    assert all((REPO / path).is_file() for path in subject.literal)


def test_a_file_named_by_literal_path_joins_the_set_and_a_directory_does_not(tmp_path):
    (tmp_path / "scripts/tests").mkdir(parents=True)
    (tmp_path / "scripts/tests/data.txt").write_text("x")
    (tmp_path / "scripts/tests/sub").mkdir()
    (tmp_path / "scripts/tests/t.py").write_text(
        'A = "scripts/tests/data.txt"\nB = "data.txt"\nC = "scripts/tests/sub"\nD = "/etc/hostname"\nE = "../../../x"\n')
    assert ck.literal_files(tmp_path, ["scripts/tests/t.py"]) == {"scripts/tests/data.txt"}
