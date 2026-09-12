"""Tests for cursor/scripts/lint-cursor-mirror-cochange.py (--staged gate)."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

COCHANGE = (
    Path(__file__).resolve().parent.parent.parent
    / "cursor"
    / "scripts"
    / "lint-cursor-mirror-cochange.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("lint_cursor_mirror_cochange", COCHANGE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cochange_mod():
    return _load()


def test_non_staged_is_noop(cochange_mod):
    assert cochange_mod.main([]) == 0


def test_staged_claude_without_relevant_pattern(tmp_path, monkeypatch, cochange_mod):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Title\n\nUnrelated edit.\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
    monkeypatch.setattr(cochange_mod, "REPO_ROOT", repo)
    assert cochange_mod.main(["--staged"]) == 0


def test_staged_claude_runtime_host_requires_mirror(tmp_path, monkeypatch, cochange_mod):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Title\n\nSticky runtime_host binding.\n")
    mirror_dir = repo / "cursor" / "rules"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "claude-code-sync.mdc").write_text("mirror\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
    monkeypatch.setattr(cochange_mod, "REPO_ROOT", repo)
    assert cochange_mod.main(["--staged"]) == 1
    subprocess.run(["git", "add", "cursor/rules/claude-code-sync.mdc"], cwd=repo, check=True)
    assert cochange_mod.main(["--staged"]) == 0


def test_cursor_mirror_na_env(tmp_path, monkeypatch, cochange_mod):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("runtime_host cursor\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
    monkeypatch.setattr(cochange_mod, "REPO_ROOT", repo)
    monkeypatch.setenv("CURSOR_MIRROR_NA", "1")
    assert cochange_mod.main(["--staged"]) == 0
