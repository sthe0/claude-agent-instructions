"""Tests for hook-memory-accessed-stamp.py (PostToolUse Read stamping hook)."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load():
    path = SCRIPTS_DIR / "hook-memory-accessed-stamp.py"
    spec = importlib.util.spec_from_file_location("hook_memory_accessed_stamp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()

_LEAF = """\
---
name: x
description: d
type: reference
created: 2026-06-01
last_verified: 2026-06-10
---

body
"""

# A memory-leaf path (scope 1) the reused is_memory_leaf classifier accepts.
_LEAF_REL = "memory-global/leaves/sample.md"
_TODAY = "2026-06-29"


def _make_leaf(tmp_path: Path, content: str = _LEAF) -> Path:
    p = tmp_path / "memory-global" / "leaves" / "sample.md"
    p.parent.mkdir(parents=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_stamp_inserts_last_accessed(tmp_path):
    p = _make_leaf(tmp_path)
    assert _mod.stamp(p, _TODAY) is True
    assert f"last_accessed: {_TODAY}" in p.read_text(encoding="utf-8")


def test_same_day_restamp_is_noop(tmp_path):
    p = _make_leaf(tmp_path)
    _mod.stamp(p, _TODAY)
    first = p.read_text(encoding="utf-8")
    assert _mod.stamp(p, _TODAY) is False
    assert p.read_text(encoding="utf-8") == first  # byte-identical


def test_older_last_accessed_is_bumped(tmp_path):
    content = _LEAF.replace("last_verified: 2026-06-10\n",
                            "last_verified: 2026-06-10\nlast_accessed: 2026-05-01\n")
    p = _make_leaf(tmp_path, content)
    assert _mod.stamp(p, _TODAY) is True
    text = p.read_text(encoding="utf-8")
    assert f"last_accessed: {_TODAY}" in text
    assert "2026-05-01" not in text


def test_frontmatterless_file_untouched(tmp_path):
    p = tmp_path / "memory-global" / "leaves" / "nofm.md"
    p.parent.mkdir(parents=True)
    p.write_text("# Just a heading\n\ncontent\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    assert _mod.stamp(p, _TODAY) is False
    assert p.read_text(encoding="utf-8") == before


def _run_hook(monkeypatch, payload: dict, today: str = _TODAY) -> int:
    monkeypatch.setattr(_mod, "_today", lambda: today)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return _mod.main()


def test_main_stamps_on_read_of_leaf(tmp_path, monkeypatch):
    p = _make_leaf(tmp_path)
    rc = _run_hook(monkeypatch, {"tool_name": "Read", "tool_input": {"file_path": str(p)}})
    assert rc == 0
    assert f"last_accessed: {_TODAY}" in p.read_text(encoding="utf-8")


def test_main_noop_on_non_memory_read(tmp_path, monkeypatch):
    src = tmp_path / "src.py"
    src.write_text("print(1)\n", encoding="utf-8")
    before = src.read_text(encoding="utf-8")
    rc = _run_hook(monkeypatch, {"tool_name": "Read", "tool_input": {"file_path": str(src)}})
    assert rc == 0
    assert src.read_text(encoding="utf-8") == before


def test_main_noop_on_non_read_tool(tmp_path, monkeypatch):
    p = _make_leaf(tmp_path)
    before = p.read_text(encoding="utf-8")
    rc = _run_hook(monkeypatch, {"tool_name": "Edit", "tool_input": {"file_path": str(p)}})
    assert rc == 0
    assert p.read_text(encoding="utf-8") == before


def test_main_always_exits_0_on_corrupt_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not-json{{{"))
    assert _mod.main() == 0


def test_main_missing_file_does_not_raise(tmp_path, monkeypatch):
    missing = tmp_path / "memory-global" / "leaves" / "ghost.md"
    rc = _run_hook(monkeypatch, {"tool_name": "Read", "tool_input": {"file_path": str(missing)}})
    assert rc == 0
