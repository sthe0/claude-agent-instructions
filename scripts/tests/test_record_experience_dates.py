"""Tests for temporal-frontmatter emission in record-experience.py.

`new`/`ticket` must emit created+last_verified (= --date); `extend` re-confirms
by bumping last_verified (and backfilling created on a legacy leaf); the
`set-last-verified` mode rewrites only last_verified.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load():
    path = SCRIPTS_DIR / "record-experience.py"
    spec = importlib.util.spec_from_file_location("record_experience", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


re_mod = _load()


def test_frontmatter_emits_both_dates():
    fm = re_mod.frontmatter("n", "d", "ok", None, None, None, "2026-06-29")
    assert "created: 2026-06-29" in fm
    assert "last_verified: 2026-06-29" in fm


def test_standalone_body_carries_dates():
    a = SimpleNamespace(
        date="2026-06-29", slug="s", title="T", description="d", confirmed_by="ok",
        refs=None, plan_file=None, difficulty="x", order="o", criterion="c",
        context_label="L", context_where="W", plan="P", cost=None, self_critique=None,
    )
    body = re_mod.standalone_body(a)
    assert "created: 2026-06-29" in body
    assert "last_verified: 2026-06-29" in body


def test_set_fm_field_replaces_existing():
    text = "---\nname: x\nlast_verified: 2026-01-01\n---\nbody\n"
    out = re_mod.set_fm_field(text, "last_verified", "2026-06-29")
    assert "last_verified: 2026-06-29" in out
    assert "2026-01-01" not in out


def test_set_fm_field_appends_when_absent():
    text = "---\nname: x\n---\nbody\n"
    out = re_mod.set_fm_field(text, "created", "2026-06-29")
    assert "created: 2026-06-29" in out
    assert out.endswith("body\n")


def test_set_last_verified_mode(tmp_path):
    leaf = tmp_path / "leaf.md"
    leaf.write_text("---\nname: x\ncreated: 2026-06-01\nlast_verified: 2026-06-01\n---\nbody\n",
                    encoding="utf-8")
    re_mod.cmd_set_last_verified(SimpleNamespace(leaf=str(leaf), date="2026-06-29"))
    text = leaf.read_text(encoding="utf-8")
    assert "last_verified: 2026-06-29" in text
    assert "created: 2026-06-01" in text  # created untouched


def test_extend_backfills_created_and_bumps_last_verified(tmp_path):
    # A legacy leaf predating the contract: no created/last_verified.
    leaf = tmp_path / "leaf.md"
    leaf.write_text(
        "---\nname: x\nschema: difficulty/v1\n---\n\n# T\n\n## Contexts\n\n### 2026-01-01 — old\n",
        encoding="utf-8")
    a = SimpleNamespace(
        leaf=str(leaf), date="2026-06-29", context_label="new", context_where="W",
        plan="P", common=None, variations=None,
    )
    re_mod.cmd_extend(a)
    text = leaf.read_text(encoding="utf-8")
    assert "created: 2026-06-29" in text
    assert "last_verified: 2026-06-29" in text
