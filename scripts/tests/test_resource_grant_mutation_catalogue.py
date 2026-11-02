"""The resource-grant mutation tool: every anchor applies, and a broken control never reads as a kill.

Inside a mutated copy the anchors are already replaced, so the anchor test would fail there for
the wrong reason; the tool exports RESOURCE_GRANT_ACTIVE_MUTANT while it runs a command and the
anchor tests skip themselves when they see it. The behaviour tests stay, so the module never
collects zero tests (pytest exit 5) in a mutated copy.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent / "resource_grant_mutation_control.py"
spec = importlib.util.spec_from_file_location("resource_grant_mutation_control_under_test", TOOL)
CONTROL = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = CONTROL
spec.loader.exec_module(CONTROL)

in_mutated_copy = pytest.mark.skipif(
    bool(os.environ.get(CONTROL.ACTIVE_MUTANT_ENV)), reason="running inside a mutated scratch copy")


@in_mutated_copy
@pytest.mark.parametrize("name", sorted(CONTROL.CATALOGUE))
def test_every_anchor_occurs_exactly_once_in_the_working_tree(name):
    mutant = CONTROL.CATALOGUE[name]
    text = (CONTROL.REPO_ROOT / mutant.relpath).read_text(encoding="utf-8")
    assert text.count(mutant.anchor) == 1
    assert mutant.anchor != mutant.replacement


# --- behaviour on a tiny repository ---------------------------------------------------------

MINI = {"m": CONTROL.Mutant("mod.py", "VALUE = 1", "VALUE = 2", "mini mutant")}


def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "gone.txt").write_text("x\n", encoding="utf-8")
    (root / "kept.txt").write_text("old\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_scratch_copy_carries_modified_deleted_and_untracked_files(repo, tmp_path):
    (repo / "kept.txt").write_text("new\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    (repo / "fresh.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    clone = CONTROL.make_scratch(repo, tmp_path / "scratch")
    assert (clone / "kept.txt").read_text(encoding="utf-8") == "new\n"
    assert not (clone / "gone.txt").exists()
    assert (clone / "fresh.txt").read_text(encoding="utf-8") == "untracked\n"
    assert (clone / "staged.txt").read_text(encoding="utf-8") == "staged\n"
    assert (repo / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_mutation_never_touches_the_source_tree(repo):
    CONTROL.run_mutant("m", "true", repo=repo, catalogue=MINI)
    assert (repo / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_the_command_sees_the_mutated_file_and_exits_with_its_code(repo):
    check = f"{sys.executable} -c \"import sys; sys.exit(1 if 'VALUE = 2' in open('mod.py').read() else 0)\""
    assert CONTROL.run_mutant("m", check, repo=repo, catalogue=MINI) == 1


def test_a_surviving_mutant_exits_zero(repo):
    assert CONTROL.run_mutant("m", "true", repo=repo, catalogue=MINI) == 0


def test_the_active_mutant_marker_is_exported_to_the_command_on_the_mutant_only(repo):
    check = (f"{sys.executable} -c \"import os, sys; "
             f"sys.exit(3 if os.environ.get('{CONTROL.ACTIVE_MUTANT_ENV}') == 'm' else 0)\"")
    assert CONTROL.run_mutant("m", check, repo=repo, catalogue=MINI) == 3


@pytest.mark.parametrize("code", [2, 4, 5])
def test_a_collection_error_on_the_mutant_is_not_the_kill_exit_code(repo, code):
    check = f"{sys.executable} -c \"import sys; sys.exit({code} if 'VALUE = 2' in open('mod.py').read() else 0)\""
    rc = CONTROL.run_mutant("m", check, repo=repo, catalogue=MINI)
    assert rc == code
    assert rc != 1


def test_a_command_red_on_the_unmutated_copy_is_not_a_kill(repo, capsys):
    """CMD=false is red on both copies; that must not read as the mutant being killed."""
    rc = CONTROL.run_mutant("m", "false", repo=repo, catalogue=MINI)
    assert rc != 1
    assert "unmutated" in capsys.readouterr().err


def test_a_command_red_on_the_unmutated_copy_gets_no_second_run(repo):
    count = repo / "runs"
    count.write_text("", encoding="utf-8")
    script = (f"{sys.executable} -c \"open({str(count)!r}, 'a').write('x'); raise SystemExit(1)\"")
    CONTROL.run_mutant("m", script, repo=repo, catalogue=MINI)
    assert count.read_text(encoding="utf-8") == "x"


def test_anchor_miss_exits_zero_and_runs_nothing(repo, capsys):
    miss = {"m": CONTROL.Mutant("mod.py", "NOT THERE", "x", "d")}
    marker = repo / "ran"
    rc = CONTROL.run_mutant("m", f"{sys.executable} -c \"open('ran', 'w')\"", repo=repo, catalogue=miss)
    assert rc == 0
    assert "does not match exactly once" in capsys.readouterr().err
    assert not marker.exists()


def test_a_duplicated_anchor_is_a_miss(repo):
    (repo / "mod.py").write_text("VALUE = 1\nVALUE = 1\n", encoding="utf-8")
    assert CONTROL.run_mutant("m", "false", repo=repo, catalogue=MINI) == 0


def test_a_failed_copy_exits_zero(tmp_path, capsys):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert CONTROL.run_mutant("m", "false", repo=not_a_repo, catalogue=MINI) == 0
    assert "scratch copy failed" in capsys.readouterr().err


def test_an_internal_exception_exits_zero_for_a_mutant(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("tool bug")

    monkeypatch.setattr(CONTROL, "run_mutant", boom)
    assert CONTROL.main(["--mutant", "handoff-resources-carried", "--run", "false"]) == 0
    assert "internal error" in capsys.readouterr().err


def test_an_internal_exception_is_never_a_green_control(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tool bug")

    monkeypatch.setattr(CONTROL, "run_control", boom)
    assert CONTROL.main(["--control"]) == CONTROL.CONTROL_RED


def test_control_without_run_checks_the_anchors(repo):
    assert CONTROL.run_control(repo=repo, catalogue=MINI) == 0
    (repo / "mod.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert CONTROL.run_control(repo=repo, catalogue=MINI) == CONTROL.CONTROL_RED


def test_control_with_run_returns_the_command_code_on_the_unmutated_copy(repo):
    check = f"{sys.executable} -c \"import sys; sys.exit(0 if 'VALUE = 1' in open('mod.py').read() else 5)\""
    assert CONTROL.run_control(check, repo=repo, catalogue=MINI) == 0
    assert CONTROL.run_control("false", repo=repo, catalogue=MINI) == 1


def test_unknown_mutant_is_not_a_kill(capsys):
    assert CONTROL.run_mutant("nope", "false") == 0
