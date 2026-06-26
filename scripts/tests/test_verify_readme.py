"""Tests for verify-readme.py sentinel-region verifier.

The scripts inventory lives in scripts/README.md (link targets relative to
scripts/); the flat-skills and specializations inventories live in
docs/components/skills.md (link targets relative to docs/components/, i.e.
prefixed with ../../). The tests build that split layout under tmp_path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "verify-readme.py"
    spec = importlib.util.spec_from_file_location("verify_readme", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_mod()
main = _mod.main


def _make_tree(tmp: Path) -> tuple[set[str], set[str], set[str]]:
    """Build minimal FS tree; return (scripts_set, skills_set, specs_set).

    scripts_set holds link targets relative to scripts/ (where the scripts
    sentinel lives), matching what verify-readme._fs_set computes.
    """
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "foo.py").write_text("")
    (tmp / "scripts" / "bar.sh").write_text("")
    (tmp / "cursor" / "scripts").mkdir(parents=True)
    (tmp / "cursor" / "scripts" / "baz.py").write_text("")
    (tmp / "skills" / "my-skill").mkdir(parents=True)
    (tmp / "skills" / "specializations" / "my-spec").mkdir(parents=True)
    return (
        {"foo.py", "bar.sh", "../cursor/scripts/baz.py"},
        {"my-skill"},
        {"my-spec"},
    )


def _write_readmes(
    tmp: Path,
    scripts: set[str],
    skills: set[str],
    specs: set[str],
    *,
    extra_script_row: str = "",
    missing_marker: str = "",
) -> None:
    """Write scripts/README.md (scripts region) and docs/components/skills.md
    (skills + specs regions, File links prefixed ../../ to resolve from
    docs/components/ back to the repo root)."""

    def scripts_region() -> str:
        rows = "| Script | Purpose |\n|---|---|\n"
        for s in sorted(scripts):
            base = Path(s).name
            rows += f"| [{base}]({s}) | test purpose |\n"
        rows += extra_script_row
        return f"<!-- inventory:scripts:begin -->\n{rows}<!-- inventory:scripts:end -->\n"

    def skills_region() -> str:
        rows = "| name | Triggers | File |\n|---|---|---|\n"
        for sk in sorted(skills):
            rows += f"| `{sk}` | test trigger | [../../skills/{sk}/SKILL.md](../../skills/{sk}/SKILL.md) |\n"
        return f"<!-- inventory:skills:begin -->\n{rows}<!-- inventory:skills:end -->\n"

    def specs_region() -> str:
        rows = "| name | Spawns when | File |\n|---|---|---|\n"
        for sp in sorted(specs):
            rows += f"| `{sp}` | test | [../../skills/specializations/{sp}/SKILL.md](../../skills/specializations/{sp}/SKILL.md) |\n"
        return f"<!-- inventory:specializations:begin -->\n{rows}<!-- inventory:specializations:end -->\n"

    (tmp / "README.md").write_text("# Test README\n")

    # docs/components/skills.md: skills + specializations regions.
    comp_skills = "" if missing_marker == "skills" else skills_region()
    comp_specs = "" if missing_marker == "specializations" else specs_region()
    comp = tmp / "docs" / "components"
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "skills.md").write_text(f"# Skills\n\n{comp_skills}\n{comp_specs}\n")

    # scripts/README.md: scripts region (markers omitted when missing_marker).
    if missing_marker == "scripts":
        scripts_body = ""
    else:
        scripts_body = scripts_region()
    (tmp / "scripts" / "README.md").write_text(f"# Scripts\n\n{scripts_body}")


def test_match_returns_0(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    _write_readmes(tmp_path, scripts, skills, specs)
    assert main(["--root", str(tmp_path)]) == 0


def test_missing_script_returns_1(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    _write_readmes(tmp_path, scripts, skills, specs)
    (tmp_path / "scripts" / "extra.py").write_text("")
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_dangling_script_returns_1(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    dangling = "| [ghost.py](ghost.py) | does not exist |\n"
    _write_readmes(tmp_path, scripts, skills, specs, extra_script_row=dangling)
    assert main(["--root", str(tmp_path)]) == 1


def test_fix_reconciles(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    dangling = "| [ghost.py](ghost.py) | my custom purpose |\n"
    _write_readmes(tmp_path, scripts, skills, specs, extra_script_row=dangling)
    scripts_readme = tmp_path / "scripts" / "README.md"
    # Add a new file to FS (not yet in README)
    (tmp_path / "scripts" / "extra.py").write_text("")
    # Before fix: fails
    assert main(["--root", str(tmp_path)]) == 1
    # Fix
    assert main(["--root", str(tmp_path), "--fix"]) == 0
    # Re-check
    assert main(["--root", str(tmp_path)]) == 0
    content = scripts_readme.read_text()
    assert "extra.py" in content
    assert "ghost.py" not in content
    assert "test purpose" in content


def test_missing_marker_returns_1(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    _write_readmes(tmp_path, scripts, skills, specs, missing_marker="scripts")
    assert main(["--root", str(tmp_path)]) == 1


def test_synthesize_row_path_is_region_relative():
    """skills/specializations rows resolve from the region file's own dir.

    REGION_FILES maps both to docs/components/skills.md, two levels below the
    repo root, so a synthesized File link must be prefixed with ../../ to point
    back at the repo-root-relative skills/ tree."""
    sk = _mod._synthesize_row("my-skill", "skills")
    assert "[../../skills/my-skill/SKILL.md](../../skills/my-skill/SKILL.md)" in sk
    sp = _mod._synthesize_row("my-spec", "specializations")
    assert (
        "[../../skills/specializations/my-spec/SKILL.md]"
        "(../../skills/specializations/my-spec/SKILL.md)" in sp
    )


def test_fix_synthesizes_region_relative_skill_row(tmp_path):
    """--fix adds a missing skill with a region-relative (cross-ref-correct) link."""
    scripts, skills, specs = _make_tree(tmp_path)
    _write_readmes(tmp_path, scripts, skills, specs)
    # New skill on FS, not yet in the inventory.
    (tmp_path / "skills" / "fresh-skill").mkdir()
    assert main(["--root", str(tmp_path)]) == 1
    assert main(["--root", str(tmp_path), "--fix"]) == 0
    content = (tmp_path / "docs" / "components" / "skills.md").read_text()
    assert "(../../skills/fresh-skill/SKILL.md)" in content
    # A repo-root-relative path would be a broken cross-ref from docs/components/.
    assert "(skills/fresh-skill/SKILL.md)" not in content
