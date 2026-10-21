"""The files a spawned specialist's system prompt is composed from.

One resolution shared by `spawn-specialist.py` (which composes and appends them) and
`agentctl.gates` (which sizes them for the whole-plan review ceiling), so the two cannot
drift: a file the spawner starts appending is counted by the gate the same day.
"""
from __future__ import annotations

from pathlib import Path

from .config_root import skills_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def skill_path(kind: str, skills_root: Path | None = None) -> Path:
    """Resolve a specialization's SKILL.md. Global (`<skills_root>/<kind>/`, default the
    config root's skills) wins; project-local (`<cwd>/.claude/skills/specializations/<kind>/`)
    is the documented fallback. Returns the global path when neither exists, so the
    caller's not-found error names it."""
    global_path = (skills_root or skills_dir()) / kind / "SKILL.md"
    if global_path.exists():
        return global_path
    project_path = Path.cwd() / ".claude" / "skills" / "specializations" / kind / "SKILL.md"
    if project_path.exists():
        return project_path
    return global_path


def shared_protocol_path(skill: Path) -> Path | None:
    """The marker protocol appended to `skill`: the one beside its specialization tree,
    else the repo's own; None when neither exists (a project-local skill carrying its
    own contract)."""
    shared = skill.resolve().parent.parent / "_shared" / "marker-protocol.md"
    if not shared.exists():
        shared = _REPO_ROOT / "skills" / "specializations" / "_shared" / "marker-protocol.md"
    return shared if shared.exists() else None


def composed_prompt_files(skill: Path) -> list[Path]:
    """The files whose text makes up the composed system prompt, in order: `skill`, then
    the shared marker protocol when there is one."""
    shared = shared_protocol_path(skill)
    return [skill] if shared is None else [skill, shared]
