"""Tests for verify-readme.py sentinel-region verifier."""
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
    """Build minimal FS tree; return (scripts_set, skills_set, specs_set)."""
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "foo.py").write_text("")
    (tmp / "scripts" / "bar.sh").write_text("")
    (tmp / "cursor" / "scripts").mkdir(parents=True)
    (tmp / "cursor" / "scripts" / "baz.py").write_text("")
    (tmp / "skills" / "my-skill").mkdir(parents=True)
    (tmp / "skills" / "specializations" / "my-spec").mkdir(parents=True)
    return (
        {"scripts/foo.py", "scripts/bar.sh", "cursor/scripts/baz.py"},
        {"my-skill"},
        {"my-spec"},
    )


def _make_readme(
    scripts: set[str],
    skills: set[str],
    specs: set[str],
    *,
    extra_script_row: str = "",
    missing_marker: str = "",
) -> str:
    """Build a README string with three sentinel regions."""

    def scripts_table() -> str:
        rows = "| Script | Purpose |\n|---|---|\n"
        for s in sorted(scripts):
            base = Path(s).name
            rows += f"| [{base}]({s}) | test purpose |\n"
        return rows + extra_script_row

    def skills_table() -> str:
        rows = "| name | Triggers | File |\n|---|---|---|\n"
        for sk in sorted(skills):
            rows += f"| `{sk}` | test trigger | [skills/{sk}/SKILL.md](skills/{sk}/SKILL.md) |\n"
        return rows

    def specs_table() -> str:
        rows = "| name | Spawns when | File |\n|---|---|---|\n"
        for sp in sorted(specs):
            rows += f"| `{sp}` | test | [skills/specializations/{sp}/SKILL.md](skills/specializations/{sp}/SKILL.md) |\n"
        return rows

    sections: dict[str, str] = {
        "scripts": f"<!-- inventory:scripts:begin -->\n{scripts_table()}<!-- inventory:scripts:end -->\n",
        "skills": f"<!-- inventory:skills:begin -->\n{skills_table()}<!-- inventory:skills:end -->\n",
        "specializations": f"<!-- inventory:specializations:begin -->\n{specs_table()}<!-- inventory:specializations:end -->\n",
    }
    if missing_marker:
        sections[missing_marker] = ""

    return (
        f"# Test README\n\n"
        f"{sections['scripts']}\n"
        f"{sections['skills']}\n"
        f"{sections['specializations']}\n"
    )


def test_match_returns_0(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    (tmp_path / "README.md").write_text(_make_readme(scripts, skills, specs))
    assert main(["--root", str(tmp_path)]) == 0


def test_missing_script_returns_1(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    (tmp_path / "README.md").write_text(_make_readme(scripts, skills, specs))
    (tmp_path / "scripts" / "extra.py").write_text("")
    rc = main(["--root", str(tmp_path)])
    assert rc == 1


def test_dangling_script_returns_1(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    dangling = "| [ghost.py](scripts/ghost.py) | does not exist |\n"
    (tmp_path / "README.md").write_text(_make_readme(scripts, skills, specs, extra_script_row=dangling))
    assert main(["--root", str(tmp_path)]) == 1


def test_fix_reconciles(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    dangling = "| [ghost.py](scripts/ghost.py) | my custom purpose |\n"
    readme_path = tmp_path / "README.md"
    readme_path.write_text(_make_readme(scripts, skills, specs, extra_script_row=dangling))
    # Add a new file to FS (not yet in README)
    (tmp_path / "scripts" / "extra.py").write_text("")
    # Before fix: fails
    assert main(["--root", str(tmp_path)]) == 1
    # Fix
    assert main(["--root", str(tmp_path), "--fix"]) == 0
    # Re-check
    assert main(["--root", str(tmp_path)]) == 0
    content = readme_path.read_text()
    assert "extra.py" in content
    assert "ghost.py" not in content
    assert "test purpose" in content


def test_missing_marker_returns_1(tmp_path):
    scripts, skills, specs = _make_tree(tmp_path)
    (tmp_path / "README.md").write_text(_make_readme(scripts, skills, specs, missing_marker="scripts"))
    assert main(["--root", str(tmp_path)]) == 1
