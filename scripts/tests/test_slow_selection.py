"""Tests for the change-aware slow selection (slow_selection.py + slow_tests.toml).

slow_selection is imported inside the tests, not at module level, so that without it
this file fails on its assertions rather than at collection.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent

REQUIRED_NODES = (
    "scripts/tests/test_review_exhaustion_reflection.py::test_rx_mutation_catalogue_kills_every_mutant",
    "scripts/tests/test_reaper_runner.py::test_every_catalogue_mutant_is_killed_and_the_control_is_green",
    "scripts/tests/test_spawn_specialist_grants.py::test_mutation_control_is_collected_and_discriminates",
    "scripts/tests/test_instruction_smoke_gate.py::test_every_catalogue_mutant_is_killed_and_the_control_is_green",
    "scripts/tests/test_mandate_mutation_catalogue.py::test_the_control_is_green_on_the_unmutated_copy",
    "scripts/tests/test_mandate_mutation_catalogue.py::test_mutant_is_killed",
    "scripts/tests/test_phase3_trigger.py::test_check_due_exits_zero_when_the_surface_source_is_broken",
    "scripts/tests/test_phase3_trigger.py::test_check_due_against_the_real_repo_exits_zero",
    "scripts/tests/test_phase3_trigger.py::test_surface_breakdown_total_is_the_number_the_report_prints",
)
CATALOGUE_NODES = REQUIRED_NODES[:6]
GUARDED_NODE = "scripts/tests/test_guarded.py::test_guarded"


@pytest.fixture(autouse=True)
def _no_ambient_selection_env(monkeypatch):
    monkeypatch.delenv("AGENTCTL_SLOW_TESTS", raising=False)
    monkeypatch.delenv("AGENTCTL_SLOW_BASE", raising=False)


def _sel():
    import slow_selection

    return slow_selection


def _clean_env(**extra: str) -> "dict[str, str]":
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTCTL_SLOW_", "GIT_", "PYTEST_"))}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.update(extra)
    return env


class SyntheticRepo:
    """A git repo holding a copy of slow_selection.py, a one-entry registry guarding
    `guarded/*`, a conftest importing the hooks and a guarded test."""

    def __init__(self, root: Path):
        self.root = root
        tests = root / "scripts" / "tests"
        tests.mkdir(parents=True)
        shutil.copy(TESTS_DIR / "slow_selection.py", tests / "slow_selection.py")
        (tests / "slow_tests.toml").write_text(
            f'[[slow]]\nnodes = ["{GUARDED_NODE}"]\nguards = ["guarded/*"]\n', encoding="utf-8")
        (tests / "conftest.py").write_text(
            "from slow_selection import pytest_addoption, pytest_collection_modifyitems  # noqa: F401\n",
            encoding="utf-8")
        (tests / "test_guarded.py").write_text(
            "def test_guarded():\n    pass\n\n\ndef test_plain():\n    pass\n", encoding="utf-8")
        self.write("guarded/a.txt", "a\n")
        self.write("other/b.txt", "b\n")
        self.git("init", "-q", "-b", "main")
        self.commit("initial")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True, env=_clean_env()).stdout

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-verify", "-m", message)

    def pytest_outcome(self, *args: str, **env: str) -> "tuple[str, str]":
        xml = self.root.parent / "junit.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:cacheprovider",
             f"--rootdir={self.root}", f"--junitxml={xml}", *args, "scripts/tests/test_guarded.py"],
            cwd=self.root, capture_output=True, text=True, env=_clean_env(**env), timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        for case in ET.parse(xml).getroot().iter("testcase"):
            if case.get("name") == "test_guarded":
                skipped = case.find("skipped")
                return ("skip", skipped.get("message", "")) if skipped is not None else ("run", "")
        raise AssertionError(f"test_guarded was not reported (deselected?):\n{proc.stdout}")

    def cli_decision(self, **env: str) -> str:
        proc = subprocess.run([sys.executable, "scripts/tests/slow_selection.py", "--format", "lines"],
                              cwd=self.root, capture_output=True, text=True, env=_clean_env(**env),
                              check=True, timeout=60)
        decision, node = proc.stdout.split()
        assert node == GUARDED_NODE
        return decision


def _unrelated_change(repo):
    repo.write("other/c.txt", "c\n")
    repo.commit("unrelated")


def _guard_commit(repo):
    repo.write("guarded/a.txt", "changed\n")
    repo.commit("guard")


def _uncommitted_guard_edit(repo):
    _unrelated_change(repo)
    repo.write("guarded/a.txt", "edited, not committed\n")


def _untracked_guard_file(repo):
    _unrelated_change(repo)
    repo.write("guarded/new.txt", "new\n")


def _guard_then_unrelated_commit(repo):
    _guard_commit(repo)
    _unrelated_change(repo)


def _merge_base_is_head(repo):
    repo.write("guarded/a.txt", "unchanged in substance\n")
    repo.commit("guard, then landed")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")


def _no_upstream(repo):
    _unrelated_change(repo)
    repo.git("update-ref", "-d", "refs/remotes/origin/main")


SCENARIOS = {
    # name: (setup, env, extra pytest args, expected decision)
    "guard_change_runs": (_guard_commit, {}, (), "run"),
    "unrelated_change_skips": (_unrelated_change, {}, (), "skip"),
    "uncommitted_tracked_guard_change_runs": (_uncommitted_guard_edit, {}, (), "run"),
    "untracked_guard_file_runs": (_untracked_guard_file, {}, (), "run"),
    "merge_base_equal_to_head_runs_all": (_merge_base_is_head, {}, (), "run"),
    "no_origin_main_runs_all": (_no_upstream, {}, (), "run"),
    # Against the merge base the guard changed (run); against HEAD~1 only other/ did.
    "explicit_base_overrides_merge_base": (_guard_then_unrelated_commit, {"AGENTCTL_SLOW_BASE": "HEAD~1"}, (),
                                           "skip"),
    "explicit_head_base_on_clean_tree_skips": (_guard_commit, {"AGENTCTL_SLOW_BASE": "HEAD"}, (), "skip"),
    "run_all_env_runs": (_unrelated_change, {"AGENTCTL_SLOW_TESTS": "all"}, (), "run"),
    "run_slow_flag_runs": (_unrelated_change, {}, ("--run-slow",), "run"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_synthetic_repo_decision_and_cli_agree(tmp_path, name):
    setup, env, args, expected = SCENARIOS[name]
    repo = SyntheticRepo(tmp_path / "repo")
    setup(repo)
    decision, message = repo.pytest_outcome(*args, **env)
    assert decision == expected
    if decision == "skip":
        assert "--run-slow" in message and "AGENTCTL_SLOW_TESTS=all" in message
    if not args:
        assert repo.cli_decision(**env) == decision


def test_a_skipped_slow_test_is_still_collected(tmp_path):
    repo = SyntheticRepo(tmp_path / "repo")
    _unrelated_change(repo)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:xdist", "-p", "no:cacheprovider",
         f"--rootdir={repo.root}", "scripts/tests/test_guarded.py"],
        cwd=repo.root, capture_output=True, text=True, env=_clean_env(), timeout=120)
    assert "test_guarded.py::test_guarded" in proc.stdout
    assert "deselected" not in proc.stdout


def _fake_runner(fail_on: str, exc: "BaseException | None" = None):
    def run(argv, **kwargs):
        if fail_on in argv:
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(argv, 128, "", "fatal")
        return subprocess.CompletedProcess(argv, 0, "abc\n" if "rev-parse" in argv else "", "")
    return run


@pytest.mark.parametrize("runner", [
    _fake_runner("merge-base"),
    _fake_runner("diff"),
    _fake_runner("merge-base", OSError("git missing")),
], ids=["git_error", "diff_error", "git_missing"])
def test_a_git_failure_runs_every_slow_test(runner):
    sel = _sel()
    selection = sel.compute_selection(env={}, runner=runner)
    assert selection.changed is None


def test_a_git_timeout_runs_every_slow_test():
    sel = _sel()
    runner = _fake_runner("merge-base", subprocess.TimeoutExpired(["git"], sel.GIT_TIMEOUT_S))
    assert sel.compute_selection(env={}, runner=runner).changed is None


def test_an_unresolvable_explicit_base_runs_every_slow_test():
    sel = _sel()
    selection = sel.compute_selection(env={"AGENTCTL_SLOW_BASE": "no-such-ref"}, runner=_fake_runner("--verify"))
    assert selection.changed is None


def test_the_real_conftest_uses_the_selection_hooks():
    sel = _sel()
    import conftest

    assert conftest.pytest_addoption is sel.pytest_addoption
    assert conftest.pytest_collection_modifyitems is sel.pytest_collection_modifyitems


def test_every_required_node_is_registered():
    registered = {e.node for e in _sel().load_registry()}
    assert set(REQUIRED_NODES) <= registered


def test_every_registry_entry_collects_a_test():
    sel = _sel()
    entries = sel.load_registry()
    files = sorted({e.node.partition("::")[0] for e in entries})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:xdist", "-p", "no:cacheprovider",
         f"--rootdir={REPO_ROOT}", *files],
        cwd=REPO_ROOT, capture_output=True, text=True, env=_clean_env(), timeout=300)
    collected = [line.strip() for line in proc.stdout.splitlines() if "::" in line]
    uncollected = [e.node for e in entries if not any(e.matches(node) for node in collected)]
    assert uncollected == [], proc.stdout[-2000:] + proc.stderr[-2000:]


def _repo_files() -> "list[str]":
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "-c", "core.quotepath=off", "ls-files", "-co",
                          "--exclude-standard"], check=True, capture_output=True, text=True,
                         env=_clean_env()).stdout
    return out.splitlines()


def test_every_guard_glob_matches_a_file():
    from fnmatch import fnmatchcase

    files = _repo_files()
    dead = [(e.node, g) for e in _sel().load_registry() for g in e.guards
            if not any(fnmatchcase(f, g) for f in files)]
    assert dead == []


def test_every_catalogue_target_is_guarded():
    sel = _sel()
    by_node = {e.node: e for e in sel.load_registry()}
    files = set(_repo_files())
    for node in CATALOGUE_NODES:
        entry = by_node[node]
        assert entry.catalogue_targets, node
        targets = sel.resolve_catalogue_targets(entry)
        mutated = {p for p in targets if "/tests/test_" not in p}
        catching = targets - mutated
        assert mutated and catching, (node, sorted(targets))
        assert targets <= files, (node, sorted(targets - files))
        unguarded = sorted(p for p in targets if not entry.guarded(frozenset({p})))
        assert unguarded == [], node


def test_an_empty_changed_set_skips_every_entry():
    sel = _sel()
    selection = sel.Selection(frozenset(), "nothing")
    assert [e.node for e in sel.load_registry() if selection.runs(e)] == []


def _cli(*args: str) -> "list[str]":
    proc = subprocess.run([sys.executable, str(TESTS_DIR / "slow_selection.py"), *args, "--format", "lines"],
                          capture_output=True, text=True, check=True, env=_clean_env(), timeout=60)
    return proc.stdout.splitlines()


def test_the_cli_skips_every_entry_when_nothing_changed():
    entries = _sel().load_registry()
    assert _cli("--changed", os.devnull) == [f"skip {e.node}" for e in entries]


def test_the_cli_runs_the_entry_whose_guard_changed(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text("scripts/reaper/runner.py\n", encoding="utf-8")
    lines = _cli("--changed", str(changed))
    assert f"run {REQUIRED_NODES[1]}" in lines
    assert f"skip {REQUIRED_NODES[0]}" in lines
