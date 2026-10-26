"""The inline class definition and its size measure (scripts/agentctl/inline_class.py)."""
import ast
import subprocess
from pathlib import Path

import pytest

from agentctl import inline_class as ic
from agentctl.inline_class import Change, Measure

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
GENERATED = frozenset({"scripts/crutch_registry.toml"})


def _lines(*changes):
    return ic.measure(list(changes), GENERATED).production_lines


def test_a_test_file_is_excluded():
    assert _lines(Change("scripts/tests/test_x.py", 40, 2)) == 0
    assert _lines(Change("pkg/test_y.py", 5, 0)) == 0
    assert _lines(Change("pkg/y_test.py", 5, 0)) == 0


def test_a_scripts_tests_helper_is_excluded():
    assert _lines(Change("scripts/tests/_helper.py", 30, 0)) == 0
    assert _lines(Change("scripts/tests/fixtures/plan.toml", 30, 0)) == 0
    assert _lines(Change("svc/tests/data.json", 30, 0)) == 0


def test_a_generated_path_is_excluded():
    assert _lines(Change("scripts/crutch_registry.toml", 297, 17)) == 0
    assert _lines(Change("scripts/agentctl/plan.py", 34, 8)) == 42


def test_an_experience_leaf_is_excluded():
    assert _lines(Change("memory-global/leaves/experience/2026-10-01-x.md", 60, 0)) == 0


def test_a_memory_leaf_excluded():
    assert _lines(
        Change("memory-global/leaves/experience/2026-10-01-x.md", 9, 0),
        Change("memory-global/leaves/acting-without-asking.md", 9, 0),
    ) == 0


def test_the_memory_index_is_counted():
    assert _lines(Change("memory-global/MEMORY.md", 4, 1)) == 5


def test_a_skill_file_and_the_root_instructions_are_counted():
    assert _lines(Change("skills/planner/SKILL.md", 3, 2), Change("CLAUDE.md", 1, 1)) == 7


def test_a_path_is_judged_by_name_not_by_the_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ic.is_counted("scripts/agentctl/plan.py", frozenset())


def test_a_non_production_extension_is_not_counted():
    measured = ic.measure([Change("docs/diagram.png", 0, 0, binary=True), Change("notes.txt", 9, 0)], frozenset())
    assert measured.production_lines == 0
    assert measured.binary_paths == ()
    assert set(measured.excluded["non-production"]) == {"docs/diagram.png", "notes.txt"}


def test_exclusion_reasons_are_reported():
    measured = ic.measure(
        [
            Change("scripts/tests/test_x.py", 1, 0),
            Change("scripts/crutch_registry.toml", 1, 0),
            Change("memory-global/leaves/x.md", 1, 0),
            Change("scripts/a.py", 1, 0),
        ],
        GENERATED,
    )
    assert set(measured.excluded) == {"test", "generated", "memory"}
    assert measured.counted_paths == ("scripts/a.py",)


def test_a_rename_from_test_to_production_is_counted():
    assert _lines(Change("scripts/a.py", 2, 1, old_path="scripts/tests/test_a.py")) == 3


def test_a_rename_from_production_to_test_is_counted():
    assert _lines(Change("scripts/tests/test_a.py", 2, 1, old_path="scripts/a.py")) == 3


def test_a_counted_binary_file_is_never_within_the_bound():
    measured = ic.measure([Change("scripts/blob.json", 0, 0, binary=True)], frozenset())
    assert measured.production_lines == 0
    assert measured.binary_paths == ("scripts/blob.json",)
    assert not ic.within_bound(measured, 20)


def test_equality_at_the_bound_is_within_and_one_over_is_not():
    assert ic.within_bound(Measure(production_lines=20), 20)
    assert not ic.within_bound(Measure(production_lines=21), 20)


def test_the_numstat_parser_reads_plain_binary_and_rename_records():
    output = (
        "3\t1\tscripts/a.py\0"
        "-\t-\tdocs/x.png\0"
        "2\t0\t\0scripts/old.py\0scripts/new.py\0"
    )
    assert ic.parse_numstat_z(output) == [
        Change("scripts/a.py", 3, 1),
        Change("docs/x.png", 0, 0, binary=True),
        Change("scripts/new.py", 2, 0, old_path="scripts/old.py"),
    ]
    assert ic.parse_numstat_z("") == []


def test_the_check_attr_parser_marks_only_set_and_true():
    output = (
        "a.toml\0linguist-generated\0set\0"
        "b.toml\0linguist-generated\0true\0"
        "c.toml\0linguist-generated\0unset\0"
        "d.toml\0linguist-generated\0unspecified\0"
        "e.toml\0linguist-generated\0false\0"
    )
    assert ic.generated_from_check_attr(output) == frozenset({"a.toml", "b.toml"})
    assert ic.generated_from_check_attr("") == frozenset()


def test_check_attr_stdin_lists_both_sides_of_a_rename():
    changes = [Change("b.py", 0, 0, old_path="a.py"), Change("c.py", 1, 0)]
    assert ic.check_attr_stdin(changes) == "a.py\0b.py\0c.py\0"


def test_the_definition_contains_every_criterion_and_no_brace():
    assert ic.missing_criteria(ic.INLINE_DEFINITION) == ()
    assert "{" not in ic.INLINE_DEFINITION and "}" not in ic.INLINE_DEFINITION


def test_criteria_match_on_word_boundaries():
    assert "reversible" in ic.missing_criteria("no irreversible effect")
    assert "reversible" not in ic.missing_criteria("the change is reversible")


def test_the_reason_carries_the_definition_and_the_bound_value():
    reason = ic.small_change_reason(20)
    assert ic.missing_criteria(reason) == ()
    assert "small-change-max-lines=20" in reason


def test_the_module_is_pure_by_ast():
    tree = ast.parse((SCRIPTS_DIR / "agentctl" / "inline_class.py").read_text(encoding="utf-8"))
    stdlib_allowed = {"__future__", "re", "dataclasses"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name in stdlib_allowed for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            ok = (node.level == 1 and node.module == "exempt_paths") or (
                node.level == 0 and node.module in stdlib_allowed)
            assert ok, ast.dump(node)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            assert name not in {"open", "run", "Popen", "system", "read_text", "write_text"}


def _gitattributes_lines():
    return (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(not (REPO_ROOT / ".gitattributes").exists(), reason="needs the repo root")
def test_every_gitattributes_pattern_matches_a_tracked_file_and_names_its_generator():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    lines = _gitattributes_lines()
    patterns = [(i, ln.split()[0]) for i, ln in enumerate(lines) if ln.strip() and not ln.startswith("#")]
    assert patterns
    for index, pattern in patterns:
        assert any(ic._glob_re(pattern).match(t) for t in tracked), pattern
        comment = lines[index - 1]
        assert comment.startswith("# scripts/"), pattern
        assert (REPO_ROOT / comment[2:].split()[0]).exists(), comment


@pytest.mark.skipif(not (REPO_ROOT / ".gitattributes").exists(), reason="needs the repo root")
def test_the_known_generated_files_are_marked_by_check_attr():
    paths = ["scripts/crutch_registry.toml", "docs/operations/lexical-meaning-inventory.toml", "scripts/agentctl/plan.py"]
    out = subprocess.run(
        ic.check_attr_argv(), cwd=REPO_ROOT, input="".join(p + "\0" for p in paths),
        capture_output=True, text=True, check=True,
    ).stdout
    assert ic.generated_from_check_attr(out) == frozenset(paths[:1])
