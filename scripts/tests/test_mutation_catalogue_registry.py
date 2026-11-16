"""The registry of mutation catalogues the gate reuses by a narrowed key (``mutation_catalogues.toml``).

Difficulty removed: a registered catalogue is reused across edits its narrowed set cannot see, so the set must
hold every file the catalogue's mutants rewrite and every test that must kill them, and a catalogue that offers
that information must not be left unregistered by accident. Each entry is tied to the catalogue module itself,
never to a list kept beside it.
"""
from __future__ import annotations

import ast
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


def test_a_catalogue_module_that_reports_its_subject_is_registered():
    registered = {Path(e["module"]).name for e in ENTRIES}
    reporting = {path.name for path in (SCRIPTS / "tests").glob("*mutation_control.py")
                 if "def rewritten_files(" in path.read_text(encoding="utf-8")}
    assert reporting == registered


def test_a_planted_narrowing_is_caught(registry):
    for subject in registry.subjects:
        entry = next(e for e in ENTRIES if e["name"] == subject.name)
        victim = module_of(entry).rewritten_files()[0]
        narrowed = ck.Subject(subject.name, subject.tests, subject.paths - {victim}, subject.named)
        assert narrowed.uncovered(module_of(entry).rewritten_files()) == [victim]
