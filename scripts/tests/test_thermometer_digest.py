"""Tests for thermometer-digest.py — the read-only σ build-trigger instrument (S3, ADR-0002).

Spec requirements:
  (i)   a tier-1 leaf whose ground matches a promoted principle counts as one re-refutation
  (ii)  ≥ threshold re-refutations of ONE principle → condition (A) fires/flags
  (iii) below threshold → does not fire
  (iv)  tier-0 (and untagged) leaves are NOT σ-fuel — ignored by (A)
  (v)   the cheap (C) proxy reports corpus size / near-duplicate pairs (report-only, never fires)
  (vi)  build_digest / --json shape carries condition_a, cheap_c, deferred, decides=False, builds=False
  (vii) _read_leaf parses the optional tier frontmatter (absent ⇒ 0)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "thermometer_digest",
    Path(__file__).resolve().parents[1] / "thermometer-digest.py",
)
td = importlib.util.module_from_spec(_SPEC)
# Register before exec so the module's @dataclass decorators can resolve __module__.
sys.modules[_SPEC.name] = td
_SPEC.loader.exec_module(td)


def _leaf(name: str, ground: str, tier: int = 0) -> "td.Leaf":
    return td.Leaf(name=name, ground=ground, tier=tier)


# ---------------------------------------------------------------------------
# condition (A)
# ---------------------------------------------------------------------------
GROUND = "the coordinator must execute through specialists not edit directly from the root"


def test_tier1_match_counts_as_refutation() -> None:
    """(i) one tier-1 leaf matching a promoted principle → exactly one re-refutation."""
    principles = [_leaf("p-coordinator.md", GROUND)]
    experience = [_leaf("e1.md", GROUND, tier=1)]
    hits = td.measure_condition_a(experience, principles, threshold=3)
    assert len(hits) == 1
    assert hits[0].principle == "p-coordinator.md"
    assert hits[0].count == 1


def test_fires_at_threshold() -> None:
    """(ii) ≥ threshold re-refutations of one principle → fired."""
    principles = [_leaf("p-coordinator.md", GROUND)]
    experience = [_leaf(f"e{i}.md", GROUND, tier=1) for i in range(3)]
    hits = td.measure_condition_a(experience, principles, threshold=3)
    assert hits[0].count == 3
    flagged = [h for h in hits if h.count >= 3]
    assert flagged and flagged[0].principle == "p-coordinator.md"


def test_below_threshold_does_not_fire() -> None:
    """(iii) two re-refutations with threshold 3 → not fired."""
    principles = [_leaf("p-coordinator.md", GROUND)]
    experience = [_leaf("e1.md", GROUND, tier=1), _leaf("e2.md", GROUND, tier=1)]
    hits = td.measure_condition_a(experience, principles, threshold=3)
    assert hits[0].count == 2
    assert not [h for h in hits if h.count >= 3]


def test_tier0_leaves_ignored() -> None:
    """(iv) tier-0 / untagged leaves are not σ-fuel even if they match a principle."""
    principles = [_leaf("p-coordinator.md", GROUND)]
    experience = [_leaf("e1.md", GROUND, tier=0), _leaf("e2.md", GROUND)]  # default tier 0
    hits = td.measure_condition_a(experience, principles, threshold=3)
    assert hits == []


def test_no_match_when_ground_unrelated() -> None:
    """A tier-1 leaf about an unrelated difficulty does not match the principle."""
    principles = [_leaf("p-coordinator.md", GROUND)]
    experience = [_leaf("e1.md", "yaml parser chokes on tab indentation in deeply nested maps", tier=1)]
    hits = td.measure_condition_a(experience, principles, threshold=3)
    assert hits == []


# ---------------------------------------------------------------------------
# cheap (C) proxy
# ---------------------------------------------------------------------------
def test_cheap_c_counts_corpus_and_near_dups() -> None:
    """(v) corpus size + near-duplicate pairs; two identical grounds → one near-dup pair."""
    experience = [_leaf("a.md", GROUND), _leaf("b.md", GROUND),
                  _leaf("c.md", "an entirely different difficulty about disk quota exhaustion")]
    c = td.measure_cheap_c(experience)
    assert c.corpus_size == 3
    assert c.near_duplicate_pairs == 1
    assert c.largest_cluster == 2


# ---------------------------------------------------------------------------
# digest shape (end-to-end on a tmp project corpus)
# ---------------------------------------------------------------------------
def _write_leaf(d: Path, name: str, desc: str, difficulty: str, tier: int | None = None) -> None:
    tier_line = f"tier: {tier}\n" if tier is not None else ""
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: {desc}\ntype: reference\n"
        f"schema: difficulty/v1\n{tier_line}"
        f'resolution_confirmed_by_user: "tester"\n---\n'
        f"\n# T\n\n## Difficulty\n{difficulty}\n"
        f"\n## Order & criterion\no\n\n**Acceptance check:** c\n"
        f"\n## Contexts\n\n### 2026-01-01 — ctx\n- Where it arose: x\n- Working plan: y\n"
        f"\n## Cost\nfree\n",
        encoding="utf-8",
    )


def test_build_digest_shape_and_invariants(tmp_path: Path) -> None:
    """(vi) build_digest carries the documented keys and is read-only (decides/builds False)."""
    exp = tmp_path / ".claude" / "agent-memory" / "experience"
    _write_leaf(exp, "e1.md", "coordinator edits directly instead of dispatching", GROUND, tier=1)
    _write_leaf(exp, "e2.md", "a totally separate caching bug", "cache invalidation race on eviction")
    d = td.build_digest("project", str(tmp_path), threshold=3)
    assert set(d) >= {"threshold", "condition_a", "cheap_c", "deferred", "decides", "builds"}
    assert d["decides"] is False and d["builds"] is False
    assert d["condition_a"]["tier1_leaves"] == 1
    assert d["cheap_c"]["corpus_size"] == 2
    # deferred signals are always surfaced (no silent cap)
    assert len(d["deferred"]) >= 2
    assert any("(B)" in s["signal"] for s in d["deferred"])


def test_read_leaf_parses_tier(tmp_path: Path) -> None:
    """(vii) _read_leaf reads the optional tier field; absent ⇒ 0."""
    exp = tmp_path / "e"
    _write_leaf(exp, "tagged.md", "d", "some difficulty", tier=1)
    _write_leaf(exp, "untagged.md", "d", "some difficulty")
    assert td._read_leaf(exp / "tagged.md", "Difficulty").tier == 1
    assert td._read_leaf(exp / "untagged.md", "Difficulty").tier == 0


def test_format_mentions_deferred_and_readonly() -> None:
    """The human digest names the deferred signals and the read-only contract (verify grep)."""
    d = td.build_digest("project", None, threshold=3) if False else {
        "threshold": 3,
        "condition_a": {"tier1_leaves": 0, "promoted_principles": 0, "hits": [],
                        "flagged": [], "fired": False},
        "cheap_c": {"corpus_size": 0, "near_duplicate_pairs": 0, "largest_cluster": 0,
                    "note": "report-only; never flags on its own"},
        "deferred": td.DEFERRED, "decides": False, "builds": False,
    }
    out = td._format(d)
    assert "deferred" in out.lower()
    assert "read-only" in out.lower()
