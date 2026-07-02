"""spawn-specialist.py composed_system_prompt_file: the spawn is the composition
point for the shared marker-protocol — specialization SKILL.md files reference
skills/specializations/_shared/marker-protocol.md instead of repeating the
invocation contract, so the spawned system prompt must inline it (no information
loss at spawn time). Falls back to the bare SKILL.md when no shared file exists."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_compose", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def test_appends_shared_protocol_when_sibling_shared_exists(tmp_path):
    skill = tmp_path / "specializations" / "thinker" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# thinker skill\n", encoding="utf-8")
    shared = tmp_path / "specializations" / "_shared" / "marker-protocol.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("# shared protocol\nCLARIFY format here\n", encoding="utf-8")

    composed = MOD.composed_system_prompt_file(skill)

    assert composed != skill
    text = composed.read_text(encoding="utf-8")
    assert text.startswith("# thinker skill")
    assert "CLARIFY format here" in text
    composed.unlink()


def test_falls_back_to_repo_shared_for_isolated_skill(tmp_path):
    """A skill outside any specializations tree (e.g. project-local without its
    own _shared) still gets the repo's canonical marker-protocol appended."""
    skill = tmp_path / "solo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# solo skill\n", encoding="utf-8")
    repo_shared = MOD.REPO_ROOT / "skills" / "specializations" / "_shared" / "marker-protocol.md"

    composed = MOD.composed_system_prompt_file(skill)

    if repo_shared.exists():
        assert composed != skill
        text = composed.read_text(encoding="utf-8")
        assert text.startswith("# solo skill")
        assert "marker" in text.lower()
        composed.unlink()
    else:
        assert composed == skill


def test_real_specialization_composes_with_repo_shared():
    """End-to-end on the real tree: every stripped specialization SKILL.md must
    compose to a prompt that still carries the CLARIFY:/PERMISSION-REQUEST:
    format blocks (the invariant the _shared extraction relies on)."""
    repo_skills = MOD.REPO_ROOT / "skills" / "specializations"
    shared = repo_skills / "_shared" / "marker-protocol.md"
    assert shared.exists(), "shared marker-protocol.md must exist"
    for kind_dir in repo_skills.iterdir():
        if not (kind_dir / "SKILL.md").exists():
            continue
        composed = MOD.composed_system_prompt_file(kind_dir / "SKILL.md")
        text = composed.read_text(encoding="utf-8")
        assert "CLARIFY:" in text, kind_dir.name
        assert "PERMISSION-REQUEST:" in text, kind_dir.name
        if composed != kind_dir / "SKILL.md":
            composed.unlink()
