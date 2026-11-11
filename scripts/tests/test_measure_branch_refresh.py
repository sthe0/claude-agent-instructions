"""Tests for measure-branch-refresh.py (the squash-landing decision measurement)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DECISION_DOC = REPO_ROOT / "docs" / "operations" / "squash-landing-decision.md"
DAY = "2026-10-27"


def _load():
    path = SCRIPTS_DIR / "measure-branch-refresh.py"
    spec = importlib.util.spec_from_file_location("measure_branch_refresh", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve types through sys.modules
    spec.loader.exec_module(mod)
    return mod


_mod = _load()


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{DAY}T12:00:00+0000",
           "GIT_AUTHOR_DATE": f"{DAY}T12:00:00+0000"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-C", str(repo), *args],
        capture_output=True, text=True, check=True, env=env).stdout


def _commit(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def history(tmp_path) -> Path:
    """Three Task values, one trailerless commit, one clean and one conflicting refresh."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "x", "1\n", "base\n\nTask: t1")
    _git(repo, "switch", "-q", "-c", "topic")
    _commit(repo, "y", "topic\n", "topic work\n\nTask: t2")
    _git(repo, "switch", "-q", "main")
    _commit(repo, "z", "main\n", "main work\n\nTask: t3")
    _git(repo, "switch", "-q", "topic")
    _git(repo, "merge", "-q", "--no-edit", "main")  # clean refresh
    _git(repo, "switch", "-q", "main")
    _commit(repo, "x", "3\n", "trailerless main work")
    _git(repo, "switch", "-q", "topic")
    _commit(repo, "x", "topic\n", "topic edits x\n\nTask: t2")
    proc = subprocess.run(["git", "-C", str(repo), "merge", "main"],
                          capture_output=True, text=True)
    assert "CONFLICT" in proc.stdout
    (repo / "x").write_text("resolved\n")
    _git(repo, "add", "x")
    _git(repo, "commit", "-q", "--no-edit")  # conflicting refresh
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "topic")
    return repo


def test_counts_landings_and_refreshes(history):
    # The fast-forward landing puts topic's chain on first-parent: main's own
    # commits (t3, the trailerless one) sit behind the refresh merges' second parents.
    figures = _mod.git_figures(history, "main", DAY, DAY)
    assert figures.landings == 2
    assert figures.refresh_merges == 2
    assert len(figures.conflicting_refreshes) == 1
    graph = _mod.git_figures(history, "main", DAY, DAY, first_parent=False)
    assert (graph.landings, graph.task_values) == (4, 3)
    assert len(graph.conflicting_refreshes) == 1


def test_window_excludes_commits_outside_it(history):
    figures = _mod.git_figures(history, "main", "2026-10-28", "2026-10-29")
    assert (figures.landings, figures.refresh_merges) == (0, 0)


def test_merge_tree_fallback_agrees_with_remerge_diff(history):
    commits = _mod.window_commits(history, "main", DAY, DAY)
    merges = [c for c in commits if c.is_merge]
    assert ([_mod.has_conflict(history, c, True) for c in merges]
            == [_mod.has_conflict(history, c, False) for c in merges])


def _transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _result(stamp: str, text: str) -> dict:
    return {"timestamp": stamp, "message": {"content": [
        {"type": "tool_result", "content": text}]}}


def _call(stamp: str, command: str) -> dict:
    return {"timestamp": stamp, "message": {"content": [
        {"type": "tool_use", "input": {"command": command}}]}}


def test_transcript_samples_time_conflict_to_next_commit(history, tmp_path):
    home = tmp_path / "home"
    _transcript(home / "projects" / "p" / "s.jsonl", [
        _result(f"{DAY}T10:00:00Z", "CONFLICT (content): Merge conflict in x"),
        _call(f"{DAY}T10:05:00Z", "git status"),
        _call(f"{DAY}T10:12:00Z", "git -C repo commit --no-edit"),
        _result(f"{DAY}T11:00:00Z", "CONFLICT (content): Merge conflict in elsewhere/f"),
        _call(f"{DAY}T11:30:00Z", "git commit -m x"),
    ])
    assert _mod.transcript_samples(home, history, "main", DAY, DAY) == [12.0]


def _document(history: Path, **overrides) -> str:
    figures = _mod.git_figures(history, "main", DAY, DAY)
    samples = overrides.pop("samples", [12.0, 30.0])
    median = _mod.median_of(samples)
    conflicting = len(figures.conflicting_refreshes)
    saved = _mod.saving(conflicting, median, figures.landings)
    graph = _mod.git_figures(history, "main", DAY, DAY, first_parent=False)
    graph_conflicting = len(graph.conflicting_refreshes)
    graph_saved = _mod.saving(graph_conflicting, median, graph.task_values)
    values = {
        "Window": f"{DAY}..{DAY}", "Revision": figures.revision,
        "Landings": figures.landings, "Refresh merges": figures.refresh_merges,
        "Conflicting refreshes": conflicting,
        "Median resolution (min)": median, "Saved per landing (min)": f"{saved:.1f}",
        "Threshold (min)": 5, "Task trailers survive squash": "yes",
        "Decision": "adopt" if saved >= 5 else "reject",
        "Whole-graph refresh merges": graph.refresh_merges,
        "Whole-graph conflicting refreshes": graph_conflicting,
        "Whole-graph Task values": graph.task_values,
        "Whole-graph saved per landing, upper bound (min)": f"{graph_saved:.1f}",
    }
    values.update(overrides)
    lines = [f"{key}: {value}" for key, value in values.items()]
    lines += [f"- Sample (min): {s}" for s in samples]
    return "\n".join(lines) + "\n"


def _check(history: Path, text: str) -> tuple[int, list[str]]:
    return _mod.check_document(_mod.parse_document(text), history, None)


def test_true_document_passes(history):
    assert _check(history, _document(history)) == (0, [])


def test_wrong_number_is_refused(history):
    code, problems = _check(history, _document(history, Landings=5))
    assert code == _mod.EXIT_MISMATCH
    assert any("landings" in p for p in problems)


def test_flipped_decision_is_refused_by_the_rule(history):
    code, problems = _check(history, _document(history, Decision="reject"))
    assert code == _mod.EXIT_CONTRADICTS
    assert "DECISION CONTRADICTS RULE" in problems[0]


def test_zero_samples_yield_reject():
    assert _mod.rule_decision(0.0, 5, True, None) == "reject"


def test_trailers_survive_a_composed_squash():
    assert _mod.check_trailers() is True


def _real_revision_present(fixture: Path) -> bool:
    if not fixture.exists():
        return False
    revision = _mod.parse_document(fixture.read_text()).revision
    return subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", revision],
                          capture_output=True).returncode == 0


@pytest.mark.parametrize("name, code, needle", [
    ("squash_decision_bad.md", 1, "landings"),
    ("squash_decision_flipped.md", 3, "DECISION CONTRADICTS RULE"),
])
def test_fixtures_are_refused(name, code, needle):
    fixture = FIXTURES / name
    if not _real_revision_present(fixture):
        pytest.skip("the decision document's revision is not in this clone")
    got, problems = _mod.check_document(
        _mod.parse_document(fixture.read_text()), REPO_ROOT, None)
    assert got == code
    assert any(needle in p for p in problems)
