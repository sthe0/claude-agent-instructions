"""`digest_neutrality_check.py` must prove neutrality, never assume it.

Each test builds two throwaway trees (a copy of `scripts/agentctl` + `scripts/lib`), mutates
one, and asserts the check's verdict. A check that passes vacuously, skips a broken loader,
or lets both runs import one module would pass the unmutated case and miss every one of these.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
CHECK = TESTS / "digest_neutrality_check.py"
PLAN = TESTS / "fixtures" / "plan_two_stage.toml"


def _load_check():
    spec = importlib.util.spec_from_file_location("digest_neutrality_check_under_test", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECKER = _load_check()


def _copy_tree(dest: Path) -> Path:
    scripts = dest / "scripts"
    for name in ("agentctl", "lib"):
        shutil.copytree(SCRIPTS / name, scripts / name, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _edit(tree: Path, rel: str, old: str, new: str) -> None:
    target = tree / "scripts" / rel
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"anchor in {rel} must occur exactly once"
    target.write_text(text.replace(old, new), encoding="utf-8")


@pytest.fixture(scope="module")
def base_tree(tmp_path_factory):
    return _copy_tree(tmp_path_factory.mktemp("neutrality-base"))


@pytest.fixture
def work_tree(tmp_path):
    return _copy_tree(tmp_path / "work")


def _compare(base_tree, work_tree, plans=None):
    return CHECKER.compare(base_tree, work_tree, plans if plans is not None else [str(PLAN)])


def test_identical_trees_are_neutral(base_tree, work_tree):
    report = _compare(base_tree, work_tree)
    assert report["compared"] == 1
    assert CHECKER.verdict(report) == 0


def test_a_changed_plan_digest_is_detected(base_tree, work_tree):
    _edit(work_tree, "agentctl/plan.py",
          ") + order_place(doc.meta) + optional_stages_place(doc))",
          ") + order_place(doc.meta) + optional_stages_place(doc) + ('x',))")
    report = _compare(base_tree, work_tree)
    assert CHECKER.verdict(report) == 1
    assert "plan_digest" in report["changed"][0]


def test_a_changed_plan_level_premise_key_is_detected(base_tree, work_tree):
    _edit(work_tree, "agentctl/plan.py", "payload = repr((element, value))", "payload = repr((element, value, 1))")
    report = _compare(base_tree, work_tree)
    assert CHECKER.verdict(report) == 1
    assert "premise_keys" in report["changed"][0]
    assert "plan_digest" not in report["changed"][0]


def test_a_changed_stage_question_key_is_detected(base_tree, work_tree):
    _edit(work_tree, "agentctl/plan.py", "payload = repr((element, values, *extra))",
          "payload = repr((element, values, *extra, 1))")
    report = _compare(base_tree, work_tree)
    assert CHECKER.verdict(report) == 1
    assert "premise_keys" in report["changed"][0]


def test_a_changed_stage_closure_digest_is_detected(base_tree, work_tree):
    _edit(work_tree, "agentctl/plan.py", "tuple((d, memo[d]) for d in deps),",
          "tuple((d, memo[d]) for d in deps), 'x',")
    report = _compare(base_tree, work_tree)
    assert CHECKER.verdict(report) == 1
    assert "stage_closure_digests" in report["changed"][0]
    assert "plan_digest" not in report["changed"][0]


def test_zero_compared_plans_fail(base_tree, work_tree):
    report = _compare(base_tree, work_tree, plans=[])
    assert report["compared"] == 0
    assert CHECKER.verdict(report) == 1


def test_files_that_are_not_plans_are_skipped_and_do_not_count(base_tree, work_tree, tmp_path):
    not_a_plan = tmp_path / "not_a_plan.toml"
    not_a_plan.write_text('title = "no meta here"\n', encoding="utf-8")
    report = _compare(base_tree, work_tree, plans=[str(PLAN), str(not_a_plan)])
    assert report["compared"] == 1 and report["skipped"] == [str(not_a_plan)]
    assert CHECKER.verdict(report) == 0


def test_only_non_plans_fail_as_nothing_compared(base_tree, work_tree, tmp_path):
    not_a_plan = tmp_path / "not_a_plan.toml"
    not_a_plan.write_text('title = "no meta here"\n', encoding="utf-8")
    report = _compare(base_tree, work_tree, plans=[str(not_a_plan)])
    assert report["compared"] == 0
    assert CHECKER.verdict(report) == 1


def test_a_plan_the_working_tree_no_longer_loads_is_a_regression(base_tree, work_tree):
    _edit(work_tree, "agentctl/plan.py",
          "    return parse_plan(data, strict=strict, strict_executor=strict_executor)",
          '    raise PlanError("mutant: refuses every plan")')
    report = _compare(base_tree, work_tree)
    assert len(report["regressed"]) == 1
    assert CHECKER.verdict(report) == 1


def test_a_broken_base_loader_fails_instead_of_skipping(work_tree, tmp_path):
    broken_base = _copy_tree(tmp_path / "broken-base")
    (broken_base / "scripts" / "agentctl" / "plan.py").write_text("raise RuntimeError('broken base')\n", encoding="utf-8")
    report = _compare(broken_base, work_tree)
    assert report["broken"] and "base" in report["broken"][0]
    assert report["skipped"] == []
    assert CHECKER.verdict(report) == 1


def test_a_loader_that_crashes_on_one_plan_is_broken_not_skipped(base_tree, work_tree):
    _edit(work_tree, "agentctl/plan.py",
          "    return parse_plan(data, strict=strict, strict_executor=strict_executor)",
          '    raise KeyError("mutant: loader bug")')
    report = _compare(base_tree, work_tree)
    assert report["broken"] and "KeyError" in report["broken"][0]
    assert CHECKER.verdict(report) == 1


def test_each_tree_is_loaded_from_its_own_agentctl(base_tree, work_tree):
    for tree in (base_tree, work_tree):
        entry = CHECKER.load_under(tree, [str(PLAN)])[str(PLAN)]
        assert Path(entry["loaded_from"]).is_relative_to(tree)


def test_main_reports_unusable_base_as_exit_2(capsys):
    assert CHECKER.main(["--base", "0000000000000000000000000000000000000000"]) == 2
    assert "cannot extract base" in capsys.readouterr().err


def test_main_reports_missing_plans_dir_as_exit_2(tmp_path, capsys):
    assert CHECKER.main(["--plans-dir", str(tmp_path / "absent")]) == 2


def test_default_base_is_the_pinned_fixture_commit():
    pinned = CHECKER.DEFAULT_BASE_FILE.read_text(encoding="utf-8").strip()
    assert len(pinned) == 40 and set(pinned) <= set("0123456789abcdef")
