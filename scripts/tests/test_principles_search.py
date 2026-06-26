"""record-experience.py search over the principles generality tier (ADR-0001 S2).

Retrieval-augmented planning needs `search --tier principles` to surface the principle(s)
relevant to a stage. These tests assert:
  - the principles tier is searchable (root resolves to memory-global/leaves/principles);
  - a known seed principle is returned for a matching query;
  - the existing experience-tier behaviour is unchanged when --tier is absent (the invariant:
    no second ranking engine, default path untouched).
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

# record-experience.py is hyphenated — not an importable module name.
_SPEC = importlib.util.spec_from_file_location(
    "record_experience",
    Path(__file__).resolve().parents[1] / "record-experience.py",
)
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _args(keywords, *, tier="experience", scope="global"):
    return SimpleNamespace(keywords=keywords, tier=tier, scope=scope, project_dir=None)


def test_principles_tier_root_resolves():
    root = rec.search_root("global", None, "principles")
    assert root == REPO_ROOT / "memory-global/leaves/principles"
    assert root.is_dir(), "principles tier directory must exist to be searchable"


def test_seed_principle_returned_for_matching_query(capsys):
    # The option-space principle is keyed on "option space" / "axes" / "functional ground".
    rc = rec.cmd_search(_args("option space functional ground axes", tier="principles"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "option-space-spans-axes-from-functional-ground.md" in out
    assert "principle" in out  # tier-aware wording, not "experience leaf"


def test_result_image_principle_searchable(capsys):
    rec.cmd_search(_args("result image declared compare difference", tier="principles"))
    out = capsys.readouterr().out
    assert "result-checked-against-its-result-image.md" in out


def test_experience_tier_is_default_and_uses_experience_dir():
    # Invariant: absent/default tier preserves the original experience search root.
    assert rec.search_root("global", None, "experience") == rec.experience_dir("global", None)
    # getattr fallback in cmd_search means a namespace without .tier still works as experience.
    assert rec.TIER_SECTION["experience"] == "Difficulty"
    assert rec.TIER_SECTION["principles"] == "Principle"
