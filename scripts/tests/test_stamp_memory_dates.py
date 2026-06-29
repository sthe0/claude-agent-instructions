"""Tests for stamp-memory-dates.py (the temporal-frontmatter backfill)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load():
    path = SCRIPTS_DIR / "stamp-memory-dates.py"
    spec = importlib.util.spec_from_file_location("stamp_memory_dates", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()


def test_inserts_dates_into_existing_frontmatter(tmp_path):
    text = "---\nname: x\ndescription: d\ntype: reference\n---\n\nbody\n"
    new, changed = _mod.stamp_text(text, tmp_path / "x.md", "2026-06-01", "2026-06-29")
    assert changed
    assert "created: 2026-06-01" in new
    assert "last_verified: 2026-06-29" in new
    assert new.endswith("body\n")  # body untouched


def test_idempotent_when_dates_present(tmp_path):
    text = ("---\nname: x\ndescription: d\ntype: reference\n"
            "created: 2026-06-01\nlast_verified: 2026-06-29\n---\n\nbody\n")
    new, changed = _mod.stamp_text(text, tmp_path / "x.md", "2020-01-01", "2020-01-02")
    assert not changed
    assert new == text


def test_synthesizes_block_when_no_frontmatter(tmp_path):
    text = "# My Heading\n\nSome prose here.\n"
    new, changed = _mod.stamp_text(text, tmp_path / "the-slug.md", "2026-06-01", "2026-06-29")
    assert changed
    assert new.startswith("---\n")
    assert "name: the-slug" in new
    assert "description: My Heading" in new
    assert "type: reference" in new
    assert "created: 2026-06-01" in new
    assert "last_verified: 2026-06-29" in new
    # body preserved verbatim after the synthesized block
    assert new.endswith("# My Heading\n\nSome prose here.\n")


def test_derive_created_uses_filename_prefix_when_no_git(tmp_path):
    # A path outside any git repo with a YYYY-MM-DD- filename prefix.
    p = tmp_path / "2025-12-31-some-task.md"
    p.write_text("body\n", encoding="utf-8")
    assert _mod.derive_created(p) == "2025-12-31"


def test_last_verified_clamped_to_created(tmp_path):
    p = tmp_path / "2026-06-29-x.md"
    p.write_text("body\n", encoding="utf-8")
    # created from filename prefix; mtime (today-ish) >= created typically, but
    # clamp guarantees the invariant regardless.
    created = "2026-06-29"
    lv = _mod.derive_last_verified(p, created)
    assert lv >= created


def test_full_run_apply_then_noop(tmp_path):
    root = tmp_path / "memory-global" / "leaves"
    root.mkdir(parents=True)
    (root / "a.md").write_text(
        "---\nname: a\ndescription: d\ntype: reference\n---\n\nbody\n", encoding="utf-8")
    (root / "MEMORY.md").write_text("# index\n", encoding="utf-8")

    # Point the module's REPO_ROOT at the tmp tree and run global apply.
    orig = _mod.REPO_ROOT
    try:
        _mod.REPO_ROOT = tmp_path
        rc = _mod.main(["--scope", "global", "--apply"])
        assert rc == 0
        after = (root / "a.md").read_text(encoding="utf-8")
        assert "created:" in after and "last_verified:" in after
        assert "MEMORY.md" not in after  # index file skipped, never stamped
        # second run changes nothing
        before = after
        _mod.main(["--scope", "global", "--apply"])
        assert (root / "a.md").read_text(encoding="utf-8") == before
    finally:
        _mod.REPO_ROOT = orig


def test_iter_leaves_skips_memory_md(tmp_path):
    root = tmp_path / "memory-global" / "leaves"
    root.mkdir(parents=True)
    (root / "a.md").write_text("x", encoding="utf-8")
    (root / "MEMORY.md").write_text("x", encoding="utf-8")
    orig = _mod.REPO_ROOT
    try:
        _mod.REPO_ROOT = tmp_path
        names = {p.name for p in _mod.iter_leaves("global", None)}
        assert names == {"a.md"}
    finally:
        _mod.REPO_ROOT = orig
