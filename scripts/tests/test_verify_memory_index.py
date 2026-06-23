"""Tests for verify-memory-index.py.

Builds a minimal memory-global/ tree under tmp_path and checks both invariants:
every leaf is referenced from some index, and every leaf carries a valid
top-level `type:` frontmatter key (not a nested metadata.type).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    return f"---\nname: x\ndescription: d\n{text_type}\n---\n\nbody\n"


def _make_tree(tmp: Path) -> Path:
    leaves = tmp / "memory-global" / "leaves"
    leaves.mkdir(parents=True)
    (leaves / "alpha.md").write_text(_leaf())
    (leaves / "beta.md").write_text(_leaf("type: project"))
    (tmp / "memory-global" / "MEMORY.md").write_text(
        "# Global memory\n\n- [Alpha](leaves/alpha.md) — a\n- [Beta](leaves/beta.md) — b\n"
    )
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
