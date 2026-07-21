"""Tests for hook-readme-currency-reminder.py — concept-registry-driven extension.

Covers:
- brace expansion in object_glob (fnmatch does not expand braces natively)
- _check_concepts: warn when bound doc absent; silent when bound doc present
- _check_concepts: brace-glob expansion matches the right paths
- fallback to nearest-README heuristic for files unmatched by any concept
- malformed/missing registry -> heuristic path, no crash, exit 0
- integration: main() via monkeypatched changeset + registry
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-readme-currency-reminder.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_readme_currency", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Shared module for pure-function tests (no monkeypatching needed)
_mod = _load_module()


# ---------------------------------------------------------------------------
# Unit: _expand_braces
# ---------------------------------------------------------------------------


def test_expand_braces_plain_passthrough():
    assert _mod._expand_braces("foo/bar.py") == ["foo/bar.py"]


def test_expand_braces_two_alternatives():
    result = sorted(_mod._expand_braces("scripts/{state,plan}.py"))
    assert result == ["scripts/plan.py", "scripts/state.py"]


def test_expand_braces_three_alternatives():
    result = sorted(_mod._expand_braces("{a,b,c}.txt"))
    assert result == ["a.txt", "b.txt", "c.txt"]


def test_expand_braces_multiple_groups():
    result = sorted(_mod._expand_braces("{x,y}/{a,b}.py"))
    assert result == ["x/a.py", "x/b.py", "y/a.py", "y/b.py"]


# ---------------------------------------------------------------------------
# Unit: _glob_matches (with brace expansion)
# ---------------------------------------------------------------------------


def test_glob_matches_plain_wildcard():
    assert _mod._glob_matches("scripts/*.py", "scripts/foo.py")
    assert not _mod._glob_matches("scripts/*.py", "docs/foo.md")


def test_glob_matches_brace_first_alternative():
    assert _mod._glob_matches(
        "scripts/agentctl/{state,plan}.py", "scripts/agentctl/state.py"
    )


def test_glob_matches_brace_second_alternative():
    assert _mod._glob_matches(
        "scripts/agentctl/{state,plan}.py", "scripts/agentctl/plan.py"
    )


def test_glob_no_match_outside_brace_alternatives():
    assert not _mod._glob_matches(
        "scripts/agentctl/{state,plan}.py", "scripts/agentctl/other.py"
    )


# ---------------------------------------------------------------------------
# Unit: _load_registry
# ---------------------------------------------------------------------------


def test_load_registry_valid(tmp_path):
    reg = tmp_path / "doc-bindings.json"
    reg.write_text(json.dumps({"concepts": [{"id": "x"}]}), encoding="utf-8")
    result = _mod._load_registry(reg)
    assert result == [{"id": "x"}]


def test_load_registry_missing_file_returns_none(tmp_path):
    assert _mod._load_registry(tmp_path / "nonexistent.json") is None


def test_load_registry_malformed_json_returns_none(tmp_path):
    (tmp_path / "bad.json").write_text("{not valid json}", encoding="utf-8")
    assert _mod._load_registry(tmp_path / "bad.json") is None


def test_load_registry_non_list_concepts_returns_none(tmp_path):
    reg = tmp_path / "doc-bindings.json"
    reg.write_text(json.dumps({"concepts": "not-a-list"}), encoding="utf-8")
    assert _mod._load_registry(reg) is None


# ---------------------------------------------------------------------------
# Unit: _check_concepts
# ---------------------------------------------------------------------------


def _concept(*, glob, doc_file, section="My Section", cid="test-c"):
    return {
        "id": cid,
        "concept": "Test concept description",
        "doc": {"file": doc_file, "section": section},
        "object_glob": glob,
    }


def test_check_concepts_warns_when_doc_absent():
    concepts = [_concept(glob="scripts/agentctl/*.py", doc_file="README.md")]
    paths = ["scripts/agentctl/state.py"]
    changed = {os.path.normpath("scripts/agentctl/state.py")}
    matched, warnings = _mod._check_concepts(paths, changed, concepts)
    assert "scripts/agentctl/state.py" in matched
    assert any("README.md" in w for w in warnings)
    assert any("test-c" in w for w in warnings)


def test_check_concepts_silent_when_doc_in_changeset():
    concepts = [_concept(glob="scripts/agentctl/*.py", doc_file="README.md")]
    paths = ["scripts/agentctl/state.py"]
    changed = {
        os.path.normpath("scripts/agentctl/state.py"),
        os.path.normpath("README.md"),
    }
    matched, warnings = _mod._check_concepts(paths, changed, concepts)
    assert "scripts/agentctl/state.py" in matched
    assert warnings == []


def test_check_concepts_brace_glob_matches_and_warns():
    """A brace-containing object_glob must expand and match correctly."""
    concepts = [_concept(
        glob="scripts/agentctl/{state,plan}.py",
        doc_file="memory-global/leaves/plan-activity-ontology.md",
        cid="plan-activity-model",
    )]
    paths = ["scripts/agentctl/plan.py"]
    changed = {os.path.normpath("scripts/agentctl/plan.py")}
    matched, warnings = _mod._check_concepts(paths, changed, concepts)
    assert "scripts/agentctl/plan.py" in matched
    assert warnings  # doc absent → warn
    assert any("plan-activity-model" in w for w in warnings)


def test_check_concepts_no_glob_match_returns_empty():
    concepts = [_concept(glob="scripts/agentctl/*.py", doc_file="README.md")]
    paths = ["some/other/file.py"]
    changed = {os.path.normpath("some/other/file.py")}
    matched, warnings = _mod._check_concepts(paths, changed, concepts)
    assert matched == set()
    assert warnings == []


def test_check_concepts_section_appears_in_warning():
    concepts = [_concept(
        glob="scripts/*.py", doc_file="README.md", section="Important Section"
    )]
    paths = ["scripts/foo.py"]
    changed = {os.path.normpath("scripts/foo.py")}
    _, warnings = _mod._check_concepts(paths, changed, concepts)
    assert any("Important Section" in w for w in warnings)


def test_check_concepts_no_duplicate_warning_for_same_concept():
    concepts = [_concept(glob="scripts/*.py", doc_file="README.md")]
    paths = ["scripts/foo.py", "scripts/bar.py"]
    changed = {
        os.path.normpath("scripts/foo.py"),
        os.path.normpath("scripts/bar.py"),
    }
    _, warnings = _mod._check_concepts(paths, changed, concepts)
    concept_warnings = [w for w in warnings if "test-c" in w]
    assert len(concept_warnings) == 1


# ---------------------------------------------------------------------------
# Integration: main() via monkeypatched changeset + registry
# ---------------------------------------------------------------------------


def _payload(command: str, cwd: str = "/tmp") -> str:
    return json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    )


def test_main_concept_warns_when_doc_absent(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    concepts = [_concept(glob="scripts/agentctl/*.py", doc_file="README.md")]
    monkeypatch.setattr(mod, "_load_registry", lambda: concepts)
    monkeypatch.setattr(
        mod,
        "_git_changeset",
        lambda cwd, sweep: (str(tmp_path), ["scripts/agentctl/state.py"]),
    )
    monkeypatch.setattr(mod, "_arc_changeset", lambda cwd: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload("git commit -m 'x'")))
    rc = mod.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "test-c" in err
    assert "README.md" in err


def test_main_concept_silent_when_doc_in_changeset(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    concepts = [_concept(glob="scripts/agentctl/*.py", doc_file="README.md")]
    monkeypatch.setattr(mod, "_load_registry", lambda: concepts)
    monkeypatch.setattr(
        mod,
        "_git_changeset",
        lambda cwd, sweep: (
            str(tmp_path),
            ["scripts/agentctl/state.py", "README.md"],
        ),
    )
    monkeypatch.setattr(mod, "_arc_changeset", lambda cwd: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload("git commit -m 'x'")))
    rc = mod.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "test-c" not in err


def test_main_unmatched_file_falls_back_to_nearest_readme(
    monkeypatch, capsys, tmp_path
):
    mod = _load_module()
    (tmp_path / "README.md").write_text("# Root\n")
    monkeypatch.setattr(mod, "_load_registry", lambda: [])  # no concepts
    monkeypatch.setattr(
        mod,
        "_git_changeset",
        lambda cwd, sweep: (str(tmp_path), ["some_file.py"]),
    )
    monkeypatch.setattr(mod, "_arc_changeset", lambda cwd: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload("git commit -m 'x'")))
    rc = mod.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "README.md" in err


def test_main_missing_registry_falls_back_to_heuristic(
    monkeypatch, capsys, tmp_path
):
    mod = _load_module()
    (tmp_path / "README.md").write_text("# Root\n")
    monkeypatch.setattr(mod, "_load_registry", lambda: None)
    monkeypatch.setattr(
        mod,
        "_git_changeset",
        lambda cwd, sweep: (str(tmp_path), ["some_file.py"]),
    )
    monkeypatch.setattr(mod, "_arc_changeset", lambda cwd: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload("git commit -m 'x'")))
    rc = mod.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "README.md" in err


def test_main_malformed_registry_no_crash_exit_zero(monkeypatch, capsys, tmp_path):
    mod = _load_module()
    monkeypatch.setattr(mod, "_load_registry", lambda: None)
    monkeypatch.setattr(
        mod, "_git_changeset", lambda cwd, sweep: (str(tmp_path), [])
    )
    monkeypatch.setattr(mod, "_arc_changeset", lambda cwd: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload("git commit -m 'x'")))
    rc = mod.main()
    assert rc == 0


# ---------------------------------------------------------------------------
# Integration: main() routes the changeset cwd through git_cwd.effective_git_cwd
# so a cross-repo commit (the standard `cd <worktree> && git/arc commit` landing
# pattern) computes the changeset against the tree the command actually targets,
# not the ambient session cwd. Blind-spot fix — see scripts/lib/git_cwd.py.
# ---------------------------------------------------------------------------


def _capture_changeset_cwd(monkeypatch, mod):
    """Record the cwd _git_changeset receives and short-circuit main(): the stub
    MUST return None (dict.update does; dict.setdefault would return the truthy
    string and make main() unpack a path → ValueError). _arc_changeset is stubbed
    to None so the `_git_changeset(...) or _arc_changeset(...)` fallback stays
    None and the hook takes its no-changeset branch after the capture."""
    seen = {}
    monkeypatch.setattr(mod, "_git_changeset", lambda cwd, sweep: seen.update(cwd=cwd))
    monkeypatch.setattr(mod, "_arc_changeset", lambda cwd: None)
    return seen


def test_main_effective_cwd_cd_redirect(monkeypatch):
    """`cd <worktree> && git commit` — the leading-cd redirect makes the changeset
    compute against the worktree the commit targets, not the session cwd."""
    mod = _load_module()
    seen = _capture_changeset_cwd(monkeypatch, mod)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_payload("cd /repo/b && git commit -m x", cwd="/repo/a")),
    )
    assert mod.main() == 0
    assert seen["cwd"] == "/repo/b"


def test_main_effective_cwd_cd_redirect_arc(monkeypatch):
    """The leading-cd branch is command-agnostic, so it also fixes `arc commit`
    (COMMIT_RE fires on `arc commit` too)."""
    mod = _load_module()
    seen = _capture_changeset_cwd(monkeypatch, mod)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_payload("cd /repo/b && arc commit -m x", cwd="/repo/a")),
    )
    assert mod.main() == 0
    assert seen["cwd"] == "/repo/b"


def test_main_effective_cwd_falls_back_without_redirect(monkeypatch):
    """No redirect → the changeset keys off the payload cwd exactly as before the
    fix (strictly non-regressing for same-tree commits)."""
    mod = _load_module()
    seen = _capture_changeset_cwd(monkeypatch, mod)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_payload("git commit -m x", cwd="/repo/a")),
    )
    assert mod.main() == 0
    assert seen["cwd"] == "/repo/a"


def test_main_dash_c_form_does_not_trigger_hook(monkeypatch):
    """Boundary: `git -C <dir> commit` is NOT contiguous `git commit`, so this
    hook's COMMIT_RE never fires on it and main() returns 0 without computing a
    changeset. The `git -C` branch of effective_git_cwd is therefore exercised
    only at the lib level (test_git_cwd.test_dash_c_absolute), where the canon
    hook's tokenized _is_git_commit does reach it. Documents why the -C form is
    absent from this hook's cross-repo cases."""
    mod = _load_module()
    seen = _capture_changeset_cwd(monkeypatch, mod)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_payload("git -C /repo/b commit -m x", cwd="/repo/a")),
    )
    assert mod.main() == 0
    assert seen == {}  # _git_changeset never called — hook did not fire
