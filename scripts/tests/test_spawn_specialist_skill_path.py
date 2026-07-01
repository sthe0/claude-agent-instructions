"""spawn-specialist.py skill_path: a specialization's SKILL.md resolves from the
global catalog (~/.claude/skills/<kind>/) first, then falls back to project-local
(<cwd>/.claude/skills/specializations/<kind>/). The fallback implements the
CLAUDE.md dispatch-table convention (project-local domain experts) that the tooling
previously documented but never resolved. Global precedence keeps existing behavior
byte-identical; a non-existent kind still returns the global path so the caller's
not-found error names it."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _mk(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# skill\n")


def test_global_resolves(tmp_path, monkeypatch):
    skills = tmp_path / "global-skills"
    _mk(skills / "developer" / "SKILL.md")
    monkeypatch.setattr(MOD, "SKILLS_DIR", skills)
    monkeypatch.chdir(tmp_path)  # no project-local dir here
    assert MOD.skill_path("developer") == skills / "developer" / "SKILL.md"


def test_project_local_fallback(tmp_path, monkeypatch):
    skills = tmp_path / "global-skills"
    skills.mkdir()
    project = tmp_path / "proj"
    _mk(project / ".claude" / "skills" / "specializations" / "flights" / "SKILL.md")
    monkeypatch.setattr(MOD, "SKILLS_DIR", skills)
    monkeypatch.chdir(project)
    expected = project / ".claude" / "skills" / "specializations" / "flights" / "SKILL.md"
    assert MOD.skill_path("flights") == expected


def test_global_precedence_over_project_local(tmp_path, monkeypatch):
    # when both exist, the global catalog wins (preserves prior behavior)
    skills = tmp_path / "global-skills"
    _mk(skills / "flights" / "SKILL.md")
    project = tmp_path / "proj"
    _mk(project / ".claude" / "skills" / "specializations" / "flights" / "SKILL.md")
    monkeypatch.setattr(MOD, "SKILLS_DIR", skills)
    monkeypatch.chdir(project)
    assert MOD.skill_path("flights") == skills / "flights" / "SKILL.md"


def test_unknown_returns_global_path_for_error(tmp_path, monkeypatch):
    skills = tmp_path / "global-skills"
    skills.mkdir()
    monkeypatch.setattr(MOD, "SKILLS_DIR", skills)
    monkeypatch.chdir(tmp_path)
    # neither global nor project-local exists -> global path, which caller reports
    result = MOD.skill_path("nope")
    assert result == skills / "nope" / "SKILL.md"
    assert not result.exists()
