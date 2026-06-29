"""Tests for the optional `tier` frontmatter field on difficulty/v1 leaves (S2, ADR-0002).

Spec requirements:
  (i)   frontmatter() emits `tier: 1` only when tier == 1
  (ii)  frontmatter() omits the key for tier 0 and tier None (absence implies 0)
  (iii) cmd_new with --tier 1 writes a leaf whose frontmatter carries `tier: 1`
  (iv)  cmd_new without --tier writes a leaf with no `tier:` line (clean run, untagged)
  (v)   a tier-tagged leaf still passes verify-experience-leaf (unknown-but-valid key)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "record_experience",
    Path(__file__).resolve().parents[1] / "record-experience.py",
)
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)


def _exp_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".claude" / "agent-memory" / "experience"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_args(tmp_path: Path, slug: str, *, tier: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        scope="project", project_dir=str(tmp_path),
        date="2026-06-29", slug=slug, title="Test Title",
        description=f"desc for {slug}",
        confirmed_by="tester",
        difficulty=f"a distinct difficulty about {slug} that shares no ground",
        order="order criterion",
        criterion="acceptance check",
        context_where="test env",
        plan="test plan",
        context_label="initial",
        plan_file=None, cost="free", self_critique=None,
        refs=[], justify_new=None, tier=tier,
    )


# ---------------------------------------------------------------------------
# frontmatter() unit tests
# ---------------------------------------------------------------------------

def test_frontmatter_emits_tier_one() -> None:
    """(i) tier == 1 → a `tier: 1` line appears."""
    fm = rec.frontmatter("n", "d", "tester", None, None, None, date="2026-06-29", tier=1)
    assert "tier: 1" in fm


def test_frontmatter_omits_tier_zero_and_none() -> None:
    """(ii) tier 0 and tier None both omit the key — absence implies 0."""
    assert "tier:" not in rec.frontmatter("n", "d", "tester", None, None, None, date="2026-06-29", tier=0)
    assert "tier:" not in rec.frontmatter("n", "d", "tester", None, None, None, date="2026-06-29", tier=None)
    # default arg (caller never passes tier) also omits
    assert "tier:" not in rec.frontmatter("n", "d", "tester", None, None, None, date="2026-06-29")


# ---------------------------------------------------------------------------
# cmd_new end-to-end tests
# ---------------------------------------------------------------------------

def _written_leaf(exp_dir: Path, slug: str) -> str:
    matches = list(exp_dir.glob(f"*{slug}.md"))
    assert len(matches) == 1, f"expected one leaf for {slug}, got {matches}"
    return matches[0].read_text(encoding="utf-8")


def test_cmd_new_tier_one_writes_tag(tmp_path: Path) -> None:
    """(iii) --tier 1 → the written leaf carries `tier: 1` in frontmatter."""
    exp_dir = _exp_dir(tmp_path)
    rc = rec.cmd_new(_new_args(tmp_path, "tier-one-leaf", tier=1))
    assert rc == 0
    body = _written_leaf(exp_dir, "tier-one-leaf")
    assert "tier: 1" in body.split("---", 2)[1]  # in the frontmatter block


def test_cmd_new_untagged_omits_tier(tmp_path: Path) -> None:
    """(iv) no --tier → the leaf has no `tier:` line (clean run stays untagged)."""
    exp_dir = _exp_dir(tmp_path)
    rc = rec.cmd_new(_new_args(tmp_path, "untagged-leaf", tier=None))
    assert rc == 0
    body = _written_leaf(exp_dir, "untagged-leaf")
    assert "tier:" not in body.split("---", 2)[1]


def test_tier_tagged_leaf_passes_verify(tmp_path: Path) -> None:
    """(v) a tier-1 leaf is still schema-valid (the key is accepted, not rejected)."""
    verify_spec = importlib.util.spec_from_file_location(
        "verify_experience_leaf",
        Path(__file__).resolve().parents[1] / "verify-experience-leaf.py",
    )
    vel = importlib.util.module_from_spec(verify_spec)
    verify_spec.loader.exec_module(vel)

    exp_dir = _exp_dir(tmp_path)
    rec.cmd_new(_new_args(tmp_path, "tier-verify-leaf", tier=1))
    leaf = list(exp_dir.glob("*tier-verify-leaf.md"))[0]
    error = vel.check_content(leaf.read_text(encoding="utf-8"))
    assert error is None, f"tier-tagged leaf should validate cleanly, got: {error}"
