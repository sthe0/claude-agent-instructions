"""Tests for verify-leaf-structure.py.

Covers:
  - leaf/v1 with all 3 sections passes
  - leaf/v1 missing any one section denies
  - grandfathered ordinary leaf (no schema) passes unconditionally
  - grandfathered system-knowledge leaf without difficulty-lead denies
  - grandfathered system-knowledge leaf with difficulty-lead passes
  - --hook mode exits 2 on violation
  - experience/ leaf is out of scope (ignored)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "verify-leaf-structure.py"
    spec = importlib.util.spec_from_file_location("verify_leaf_structure", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_mod()
check_content = _mod.check_content
is_leaf = _mod.is_leaf
main = _mod.main


# ---------------------------------------------------------------------------
# is_leaf path filter
# ---------------------------------------------------------------------------

class TestIsLeaf:
    def test_flat_leaf(self):
        assert is_leaf("memory-global/leaves/foo.md")

    def test_sk_leaf(self):
        assert is_leaf("memory-global/leaves/system-knowledge/bar.md")

    def test_memory_md_excluded(self):
        assert not is_leaf("memory-global/leaves/MEMORY.md")
        assert not is_leaf("memory-global/leaves/system-knowledge/MEMORY.md")

    def test_experience_excluded(self):
        assert not is_leaf("memory-global/leaves/experience/2026-01-01-task.md")

    def test_non_md_excluded(self):
        assert not is_leaf("memory-global/leaves/foo.txt")

    def test_unrelated_path_excluded(self):
        assert not is_leaf("scripts/verify-leaf-structure.py")

    def test_agent_memory_leaf(self):
        assert is_leaf(".claude/agent-memory/my-leaf.md")

    def test_agent_memory_experience_excluded(self):
        assert not is_leaf(".claude/agent-memory/experience/task.md")

    def test_abs_sk_path(self):
        assert is_leaf("/home/user/.claude/memory-global/leaves/system-knowledge/x.md")


# ---------------------------------------------------------------------------
# leaf/v1 schema — section checks
# ---------------------------------------------------------------------------

def _v1_leaf(sections: list[str]) -> str:
    body = "\n".join(f"## {s}\n\ncontent\n" for s in sections)
    return f"---\nname: x\ndescription: d\ntype: reference\nschema: leaf/v1\n---\n\n# Title\n\n{body}"


class TestLeafV1:
    def test_all_sections_pass(self):
        content = _v1_leaf(["Difficulty", "Guidance", "See also"])
        assert check_content(content, "leaves/x.md") is None

    def test_missing_difficulty_denied(self):
        content = _v1_leaf(["Guidance", "See also"])
        err = check_content(content, "leaves/x.md")
        assert err is not None
        assert "## Difficulty" in err

    def test_missing_guidance_denied(self):
        content = _v1_leaf(["Difficulty", "See also"])
        err = check_content(content, "leaves/x.md")
        assert err is not None
        assert "## Guidance" in err

    def test_missing_see_also_denied(self):
        content = _v1_leaf(["Difficulty", "Guidance"])
        err = check_content(content, "leaves/x.md")
        assert err is not None
        assert "## See also" in err

    def test_missing_all_three_lists_all(self):
        content = "---\nname: x\ndescription: d\ntype: reference\nschema: leaf/v1\n---\n\n# Title\n\nbody\n"
        err = check_content(content, "leaves/x.md")
        assert err is not None
        assert "## Difficulty" in err
        assert "## Guidance" in err
        assert "## See also" in err

    def test_see_also_case_insensitive(self):
        # "See Also" should also pass
        content = _v1_leaf(["Difficulty", "Guidance", "See Also"])
        assert check_content(content, "leaves/x.md") is None


# ---------------------------------------------------------------------------
# Grandfathered ordinary leaves (non-SK, no schema)
# ---------------------------------------------------------------------------

class TestGrandfatheredOrdinary:
    def test_passes_unconditionally(self):
        content = "---\nname: x\ndescription: bare fact about stuff\ntype: reference\n---\n\n# Title\n\n## Section\n\nbody\n"
        assert check_content(content, "memory-global/leaves/acting-without-asking.md") is None

    def test_no_frontmatter_passes(self):
        content = "# Title\n\nbody\n"
        assert check_content(content, "memory-global/leaves/foo.md") is None


# ---------------------------------------------------------------------------
# Grandfathered system-knowledge leaves — difficulty-lead baseline
# ---------------------------------------------------------------------------

SK_PATH = "memory-global/leaves/system-knowledge/foo.md"


def _sk_leaf(description: str = "bare fact", body_lead: str = "") -> str:
    body = f"{body_lead}\n\n# Title\n\n## Section\n\ncontent\n" if body_lead else "# Title\n\n## Section\n\ncontent\n"
    return f"---\nname: x\ndescription: {description!r}\ntype: reference\n---\n\n{body}"


class TestGrandfatheredSK:
    def test_difficulty_in_description_passes(self):
        content = _sk_leaf(description="Difficulty it removes — you don't know X")
        assert check_content(content, SK_PATH) is None

    def test_functional_ground_in_description_passes(self):
        content = _sk_leaf(description="functional ground of this leaf")
        assert check_content(content, SK_PATH) is None

    def test_body_blockquote_passes(self):
        content = _sk_leaf(body_lead="> **Difficulty (functional ground):** desired X; actual Y.")
        assert check_content(content, SK_PATH) is None

    def test_zatrudneniye_passes(self):
        content = _sk_leaf(body_lead="> **Затруднение:** some text")
        assert check_content(content, SK_PATH) is None

    def test_bare_description_denied(self):
        content = _sk_leaf(description="Some plain fact about a system")
        err = check_content(content, SK_PATH)
        assert err is not None
        assert "difficulty not named" in err

    def test_skeleton_no_heading_passes(self):
        # Only frontmatter + a few lines, no H1 — skeleton, fail open
        content = "---\nname: x\ndescription: 'bare'\ntype: reference\n---\n\npartial note\n"
        assert check_content(content, SK_PATH) is None

    def test_stub_under_10_lines_passes(self):
        content = "---\nname: x\ndescription: 'bare'\ntype: reference\n---\n\n# Title\n\na\nb\n"
        assert check_content(content, SK_PATH) is None


# ---------------------------------------------------------------------------
# --hook mode
# ---------------------------------------------------------------------------

class TestHookMode:
    def _make_payload(self, file_path: str, content: str) -> str:
        return json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": file_path, "content": content},
        })

    def test_hook_ok_returns_0(self, monkeypatch):
        content = _v1_leaf(["Difficulty", "Guidance", "See also"])
        payload = self._make_payload("memory-global/leaves/x.md", content)
        monkeypatch.setattr("sys.stdin", StringIO(payload))
        assert main(["--hook"]) == 0

    def test_hook_violation_returns_2(self, monkeypatch, capsys):
        content = _v1_leaf(["Difficulty", "Guidance"])  # missing See also
        payload = self._make_payload("memory-global/leaves/x.md", content)
        monkeypatch.setattr("sys.stdin", StringIO(payload))
        rc = main(["--hook"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "BLOCK" in err

    def test_hook_non_write_ignored(self, monkeypatch):
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "leaves/x.md"}})
        monkeypatch.setattr("sys.stdin", StringIO(payload))
        assert main(["--hook"]) == 0

    def test_hook_experience_ignored(self, monkeypatch):
        # experience/ is out of scope — must not block even if content is bad
        content = _v1_leaf(["Difficulty"])  # missing 2 sections
        payload = self._make_payload(
            "memory-global/leaves/experience/2026-01-01-task.md", content)
        monkeypatch.setattr("sys.stdin", StringIO(payload))
        assert main(["--hook"]) == 0

    def test_hook_bad_json_returns_0(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", StringIO("not json"))
        assert main(["--hook"]) == 0


# ---------------------------------------------------------------------------
# --root batch mode (project agent-memory layout)
# ---------------------------------------------------------------------------

class TestRootMode:
    def _mem(self, tmp_path: Path) -> Path:
        # is_leaf requires "agent-memory" (or "leaves") in the path parts.
        d = tmp_path / "agent-memory"
        d.mkdir()
        return d

    def test_valid_and_grandfathered_pass(self, tmp_path):
        mem = self._mem(tmp_path)
        (mem / "good.md").write_text(
            _v1_leaf(["Difficulty", "Guidance", "See also"]), encoding="utf-8")
        (mem / "grandfathered.md").write_text(
            "---\nname: x\ndescription: bare fact\ntype: reference\n---\n\n# T\n\nbody\n",
            encoding="utf-8")
        assert main(["--root", str(mem)]) == 0

    def test_opted_in_violation_fails(self, tmp_path):
        mem = self._mem(tmp_path)
        (mem / "bad.md").write_text(
            _v1_leaf(["Guidance", "See also"]), encoding="utf-8")  # missing Difficulty
        assert main(["--root", str(mem)]) == 1

    def test_memory_md_and_experience_ignored(self, tmp_path):
        mem = self._mem(tmp_path)
        # A bad opted-in leaf placed in MEMORY.md and under experience/ must NOT
        # fail the scan — both are out of scope per is_leaf.
        (mem / "MEMORY.md").write_text(_v1_leaf(["Guidance"]), encoding="utf-8")
        exp = mem / "experience"
        exp.mkdir()
        (exp / "task.md").write_text(_v1_leaf(["Guidance"]), encoding="utf-8")
        assert main(["--root", str(mem)]) == 0

    def test_empty_root_ok(self, tmp_path):
        mem = self._mem(tmp_path)
        assert main(["--root", str(mem)]) == 0

    def test_missing_root_fails(self, tmp_path):
        assert main(["--root", str(tmp_path / "nope")]) == 1

    def test_root_recurses_subdirs(self, tmp_path):
        mem = self._mem(tmp_path)
        sub = mem / "system-knowledge"
        sub.mkdir()
        # A grandfathered SK leaf missing a difficulty-lead must be caught even
        # nested under the root.
        (sub / "foo.md").write_text(
            "---\nname: x\ndescription: 'Some plain fact about a system'\ntype: reference\n---\n\n"
            "# Title\n\n## Section\n\ncontent\ncontent\ncontent\ncontent\ncontent\ncontent\ncontent\ncontent\n",
            encoding="utf-8")
        assert main(["--root", str(mem)]) == 1
