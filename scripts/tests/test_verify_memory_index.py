"""Tests for verify-memory-index.py.

Builds a minimal memory-global/ tree under tmp_path and checks both invariants:
every leaf is referenced from some index, and every leaf carries a valid
top-level `type:` frontmatter key (not a nested metadata.type).

The trees that are not `git init`-ed exercise the rglob fallback (a root that
is not a git work tree); the `_git_tree` ones exercise the git-index
enumeration, where an untracked draft is out of scope and a staged one is not.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "verify-memory-index.py"
    spec = importlib.util.spec_from_file_location("verify_memory_index", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_mod()
main = _mod.main


def _leaf(text_type: str = "type: reference") -> str:
    return (f"---\nname: x\ndescription: d\n{text_type}\n"
            "created: 2026-06-01\nlast_verified: 2026-06-29\n---\n\nbody\n")


def _make_tree(tmp: Path) -> Path:
    leaves = tmp / "memory-global" / "leaves"
    leaves.mkdir(parents=True)
    (leaves / "alpha.md").write_text(_leaf())
    (leaves / "beta.md").write_text(_leaf("type: project"))
    (tmp / "memory-global" / "MEMORY.md").write_text(
        "# Global memory\n\n- [Alpha](leaves/alpha.md) — a\n- [Beta](leaves/beta.md) — b\n"
    )
    return leaves


def _git(tmp: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(tmp), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        check=True, capture_output=True,
    )


def _git_tree(tmp: Path) -> Path:
    """A _make_tree whose leaves and index are tracked in a fresh git repo."""
    leaves = _make_tree(tmp)
    _git(tmp, "init", "-q")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-qm", "base")
    return leaves


def test_all_indexed_valid_returns_0(tmp_path):
    _make_tree(tmp_path)
    assert main(["--root", str(tmp_path)]) == 0


def test_unindexed_leaf_returns_1(tmp_path):
    leaves = _make_tree(tmp_path)
    (leaves / "orphan.md").write_text(_leaf())
    assert main(["--root", str(tmp_path)]) == 1


def test_sub_index_reference_counts(tmp_path):
    leaves = _make_tree(tmp_path)
    sub = leaves / "experience"
    sub.mkdir()
    (sub / "gamma.md").write_text(_leaf())
    (sub / "MEMORY.md").write_text("# Experience\n\n- [Gamma](gamma.md) — g\n")
    assert main(["--root", str(tmp_path)]) == 0


def test_nested_metadata_type_returns_1(tmp_path):
    leaves = _make_tree(tmp_path)
    (leaves / "nested.md").write_text(
        "---\nname: x\ndescription: d\nmetadata:\n  type: reference\n---\n\nbody\n"
    )
    (tmp_path / "memory-global" / "MEMORY.md").write_text(
        "# Global memory\n\n- [Alpha](leaves/alpha.md) — a\n"
        "- [Beta](leaves/beta.md) — b\n- [Nested](leaves/nested.md) — n\n"
    )
    assert main(["--root", str(tmp_path)]) == 1


def test_bad_type_value_returns_1(tmp_path):
    leaves = _make_tree(tmp_path)
    (leaves / "weird.md").write_text(_leaf("type: bogus"))
    (tmp_path / "memory-global" / "MEMORY.md").write_text(
        "# Global memory\n\n- [Alpha](leaves/alpha.md) — a\n"
        "- [Beta](leaves/beta.md) — b\n- [Weird](leaves/weird.md) — w\n"
    )
    assert main(["--root", str(tmp_path)]) == 1


def test_missing_tree_returns_0(tmp_path):
    assert main(["--root", str(tmp_path)]) == 0


def test_missing_temporal_dates_returns_1(tmp_path):
    leaves = _make_tree(tmp_path)
    # A leaf with valid type but no created/last_verified must be rejected.
    (leaves / "alpha.md").write_text(
        "---\nname: x\ndescription: d\ntype: reference\n---\n\nbody\n")
    (tmp_path / "memory-global" / "MEMORY.md").write_text(
        "# Global memory\n\n- [Alpha](leaves/alpha.md) — a\n"
        "- [Beta](leaves/beta.md) — b\n")
    assert main(["--root", str(tmp_path)]) == 1


def test_tracked_unindexed_leaf_returns_1(tmp_path):
    leaves = _git_tree(tmp_path)
    (leaves / "orphan.md").write_text(_leaf())
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "orphan")
    assert main(["--root", str(tmp_path)]) == 1


def test_untracked_leaf_is_out_of_scope(tmp_path):
    leaves = _git_tree(tmp_path)
    # Never git-added: a local draft is not repo content the gate may judge.
    (leaves / "orphan.md").write_text(_leaf())
    assert main(["--root", str(tmp_path)]) == 0


def test_staged_unindexed_leaf_returns_1(tmp_path):
    leaves = _git_tree(tmp_path)
    (leaves / "orphan.md").write_text(_leaf())
    _git(tmp_path, "add", "memory-global/leaves/orphan.md")
    assert main(["--root", str(tmp_path)]) == 1


def test_non_git_root_falls_back_to_rglob(tmp_path):
    leaves = _make_tree(tmp_path)
    if _mod._tracked_leaf_files(tmp_path) is not None:
        pytest.skip("tmp_path is inside a git work tree")
    (leaves / "orphan.md").write_text(_leaf())
    assert main(["--root", str(tmp_path)]) == 1


def test_last_verified_before_created_returns_1(tmp_path):
    leaves = _make_tree(tmp_path)
    (leaves / "alpha.md").write_text(
        "---\nname: x\ndescription: d\ntype: reference\n"
        "created: 2026-06-29\nlast_verified: 2026-06-01\n---\n\nbody\n")
    (tmp_path / "memory-global" / "MEMORY.md").write_text(
        "# Global memory\n\n- [Alpha](leaves/alpha.md) — a\n"
        "- [Beta](leaves/beta.md) — b\n")
    assert main(["--root", str(tmp_path)]) == 1
