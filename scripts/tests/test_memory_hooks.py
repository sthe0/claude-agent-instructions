"""Tests for hook-memory-consistency.py and hook-experience-record-reminder.py."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load(name: str):
    """Load a scripts/ hook module by filename, returning a fresh module object."""
    path = SCRIPTS_DIR / name
    slug = name.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(slug, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_GOOD_LEAF = """\
---
name: test-leaf
description: a test memory leaf for unit tests
type: reference
created: 2026-06-01
last_verified: 2026-06-29
---

Body content here.
"""

_BAD_LEAF_MISSING_TYPE = """\
---
name: test-leaf
description: a test memory leaf for unit tests
---

Body content here.
"""

_BAD_LEAF_NO_FRONTMATTER = "# Just a heading\n\nSome content.\n"

_BAD_LEAF_INVALID_TYPE = """\
---
name: test-leaf
description: a test leaf
type: bogus
---

Body.
"""

# One representative path per scope
_SCOPE1 = "/home/user/.claude/memory-global/leaves/some-leaf.md"
_SCOPE2 = "/home/user/project/.claude/agent-memory/some-leaf.md"
_SCOPE3 = "/home/user/.claude/projects/abc123def456/memory/some-leaf.md"
_NON_MEMORY_TMP = "/tmp/x.md"
_NON_MEMORY_SRC = "/home/user/project/src/main.py"


def _write_payload(file_path: str, content: str) -> str:
    return json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}
    )


def _edit_payload(file_path: str, new_string: str) -> str:
    return json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": file_path, "new_string": new_string}}
    )


# ---------------------------------------------------------------------------
# hook-memory-consistency.py
# ---------------------------------------------------------------------------

class TestIsMemoryLeaf:
    def _mod(self):
        return _load("hook-memory-consistency.py")

    def test_scope1_detected(self):
        assert self._mod().is_memory_leaf(_SCOPE1)

    def test_scope2_detected(self):
        assert self._mod().is_memory_leaf(_SCOPE2)

    def test_scope3_detected(self):
        assert self._mod().is_memory_leaf(_SCOPE3)

    def test_memory_md_excluded(self):
        assert not self._mod().is_memory_leaf(
            "/home/user/.claude/memory-global/leaves/MEMORY.md"
        )

    def test_tmp_excluded(self):
        assert not self._mod().is_memory_leaf(_NON_MEMORY_TMP)

    def test_source_file_excluded(self):
        assert not self._mod().is_memory_leaf(_NON_MEMORY_SRC)

    def test_experience_subdir_scope1_detected(self):
        assert self._mod().is_memory_leaf(
            "/home/user/.claude/memory-global/leaves/experience/task-123.md"
        )


class TestMemoryConsistencyHook:
    def _run(self, monkeypatch, payload_str: str) -> tuple[str, int]:
        mod = _load("hook-memory-consistency.py")
        monkeypatch.setattr("sys.stdin", io.StringIO(payload_str))
        # capture stdout via capsys is not available here; patch print instead
        printed = []
        monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(" ".join(str(x) for x in a)))
        rc = mod.main()
        return "\n".join(printed), rc

    def test_scope1_missing_type_emits_reminder(self, monkeypatch):
        out, rc = self._run(monkeypatch, _write_payload(_SCOPE1, _BAD_LEAF_MISSING_TYPE))
        assert rc == 0
        assert "[memory-consistency]" in out

    def test_scope2_missing_type_emits_reminder(self, monkeypatch):
        out, rc = self._run(monkeypatch, _write_payload(_SCOPE2, _BAD_LEAF_MISSING_TYPE))
        assert rc == 0
        assert "[memory-consistency]" in out

    def test_scope3_missing_type_emits_reminder(self, monkeypatch):
        out, rc = self._run(monkeypatch, _write_payload(_SCOPE3, _BAD_LEAF_MISSING_TYPE))
        assert rc == 0
        assert "[memory-consistency]" in out

    def test_no_frontmatter_emits_reminder_all_scopes(self, monkeypatch):
        for path in (_SCOPE1, _SCOPE2, _SCOPE3):
            out, rc = self._run(monkeypatch, _write_payload(path, _BAD_LEAF_NO_FRONTMATTER))
            assert rc == 0
            assert "[memory-consistency]" in out, f"expected reminder for {path}"

    def test_invalid_type_emits_reminder(self, monkeypatch):
        out, rc = self._run(monkeypatch, _write_payload(_SCOPE1, _BAD_LEAF_INVALID_TYPE))
        assert rc == 0
        assert "[memory-consistency]" in out
        assert "bogus" in out

    def test_well_formed_leaf_is_silent(self, monkeypatch):
        for path in (_SCOPE1, _SCOPE2, _SCOPE3):
            out, rc = self._run(monkeypatch, _write_payload(path, _GOOD_LEAF))
            assert rc == 0
            assert out == "", f"expected silence for well-formed leaf at {path}"

    def test_tmp_path_is_silent(self, monkeypatch):
        out, rc = self._run(monkeypatch, _write_payload(_NON_MEMORY_TMP, _BAD_LEAF_MISSING_TYPE))
        assert rc == 0
        assert out == ""

    def test_source_file_is_silent(self, monkeypatch):
        out, rc = self._run(monkeypatch, _write_payload(_NON_MEMORY_SRC, "print('hello')"))
        assert rc == 0
        assert out == ""

    def test_never_exits_2_on_malformed(self, monkeypatch):
        for path in (_SCOPE1, _SCOPE2, _SCOPE3):
            _, rc = self._run(monkeypatch, _write_payload(path, _BAD_LEAF_NO_FRONTMATTER))
            assert rc != 2, f"hook must not exit 2 for {path}"

    def test_edit_tool_bad_content_emits_reminder(self, monkeypatch):
        out, rc = self._run(monkeypatch, _edit_payload(_SCOPE1, _BAD_LEAF_MISSING_TYPE))
        assert rc == 0
        assert "[memory-consistency]" in out

    def test_edit_tool_good_content_is_silent(self, monkeypatch):
        out, rc = self._run(monkeypatch, _edit_payload(_SCOPE1, _GOOD_LEAF))
        assert rc == 0
        assert out == ""

    def test_corrupt_json_input_is_silent(self, monkeypatch):
        out, rc = self._run(monkeypatch, "not-json{{{")
        assert rc == 0
        assert out == ""


# ---------------------------------------------------------------------------
# hook-experience-record-reminder.py
# ---------------------------------------------------------------------------

def _make_state(tmp_path: Path, session_id: str, node: str, bag: dict | None) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"node": node}
    if bag is not None:
        data["plugins"] = {"experience": bag}
    (state_dir / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")
    return state_dir


def _run_exp(monkeypatch, tmp_path: Path, session_id: str, node: str,
             bag: dict | None) -> tuple[str, int]:
    mod = _load("hook-experience-record-reminder.py")
    state_dir = _make_state(tmp_path, session_id, node, bag)
    monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": session_id})))
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(" ".join(str(x) for x in a)))
    rc = mod.main()
    return "\n".join(printed), rc


_EMPTY_BAG = {"searched": False, "decision": "", "recorded": False, "skipped": False, "skip_reason": ""}


class TestExperienceRecordReminder:

    def test_loud_at_resolution_when_not_searched(self, monkeypatch, tmp_path):
        out, rc = _run_exp(monkeypatch, tmp_path, "s1", "RESOLUTION", _EMPTY_BAG)
        assert rc == 0
        assert "[experience-record-reminder]" in out
        assert "BLOCKED" in out
        assert "searched" in out

    def test_loud_at_resolution_when_searched_but_no_decision(self, monkeypatch, tmp_path):
        bag = {**_EMPTY_BAG, "searched": True}
        out, rc = _run_exp(monkeypatch, tmp_path, "s2", "RESOLUTION", bag)
        assert rc == 0
        assert "[experience-record-reminder]" in out
        assert "BLOCKED" in out
        assert "recorded|skipped" in out

    def test_soft_nudge_at_executing_when_incomplete(self, monkeypatch, tmp_path):
        out, rc = _run_exp(monkeypatch, tmp_path, "s3", "EXECUTING", _EMPTY_BAG)
        assert rc == 0
        assert "[experience-record-reminder]" in out
        assert "BLOCKED" not in out

    def test_silent_when_searched_and_recorded(self, monkeypatch, tmp_path):
        bag = {**_EMPTY_BAG, "searched": True, "recorded": True}
        out, rc = _run_exp(monkeypatch, tmp_path, "s4", "RESOLUTION", bag)
        assert rc == 0
        assert out == ""

    def test_silent_when_searched_and_skipped(self, monkeypatch, tmp_path):
        bag = {**_EMPTY_BAG, "searched": True, "skipped": True, "skip_reason": "trivial"}
        out, rc = _run_exp(monkeypatch, tmp_path, "s5", "RESOLUTION", bag)
        assert rc == 0
        assert out == ""

    def test_silent_when_experience_plugin_absent(self, monkeypatch, tmp_path):
        out, rc = _run_exp(monkeypatch, tmp_path, "s6", "RESOLUTION", None)
        assert rc == 0
        assert out == ""

    def test_silent_when_no_state_file(self, monkeypatch, tmp_path):
        mod = _load("hook-experience-record-reminder.py")
        state_dir = tmp_path / "empty-state"
        state_dir.mkdir()
        monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "nosuchsession"})))
        printed = []
        monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(str(a)))
        rc = mod.main()
        assert rc == 0
        assert printed == []

    def test_always_exits_0(self, monkeypatch, tmp_path):
        _, rc = _run_exp(monkeypatch, tmp_path, "s7", "RESOLUTION", _EMPTY_BAG)
        assert rc == 0

    def test_corrupt_json_input_is_silent(self, monkeypatch, tmp_path):
        mod = _load("hook-experience-record-reminder.py")
        state_dir = tmp_path / "s"
        state_dir.mkdir()
        monkeypatch.setattr(mod, "STATE_ROOT", state_dir)
        monkeypatch.setattr("sys.stdin", io.StringIO("not-json{{{"))
        printed = []
        monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(str(a)))
        rc = mod.main()
        assert rc == 0
        assert printed == []
