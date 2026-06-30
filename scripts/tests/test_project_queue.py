"""Tests for difficulty_channel.project_queue.resolve_project_queue."""
import json
from pathlib import Path

import pytest

from difficulty_channel.project_queue import resolve_project_queue


def _write_project_json(directory: Path, queue: str) -> None:
    (directory / ".claude").mkdir(exist_ok=True)
    (directory / ".claude" / "agent-project.json").write_text(
        json.dumps({"instruction_queue": queue}), encoding="utf-8"
    )


def test_resolves_from_direct_ancestor(tmp_path):
    _write_project_json(tmp_path, "DEEPAGENT")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert resolve_project_queue(sub) == "DEEPAGENT"


def test_returns_none_when_absent(tmp_path):
    sub = tmp_path / "a"
    sub.mkdir()
    assert resolve_project_queue(sub) is None


def test_returns_none_on_malformed_json(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-project.json").write_text("not json", encoding="utf-8")
    assert resolve_project_queue(tmp_path) is None


def test_nearest_ancestor_wins(tmp_path):
    _write_project_json(tmp_path, "OUTER")
    inner = tmp_path / "inner"
    inner.mkdir()
    _write_project_json(inner, "INNER")
    deep = inner / "deep"
    deep.mkdir()
    assert resolve_project_queue(deep) == "INNER"


def test_resolves_from_file_path(tmp_path):
    _write_project_json(tmp_path, "DEEPAGENT")
    target_file = tmp_path / "CLAUDE.md"
    target_file.write_text("# test", encoding="utf-8")
    assert resolve_project_queue(target_file) == "DEEPAGENT"


def test_ignores_empty_instruction_queue(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-project.json").write_text(
        json.dumps({"instruction_queue": ""}), encoding="utf-8"
    )
    assert resolve_project_queue(tmp_path) is None


def test_ignores_missing_instruction_queue_key(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-project.json").write_text(
        json.dumps({"other_field": "value"}), encoding="utf-8"
    )
    assert resolve_project_queue(tmp_path) is None
