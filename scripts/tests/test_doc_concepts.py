"""Tests for verify-doc-concepts.py."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

# Ensure scripts/ on path (conftest also does this, but be explicit)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_verifier():
    path = SCRIPTS_DIR / "verify-doc-concepts.py"
    spec = importlib.util.spec_from_file_location("verify_doc_concepts", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verifier = _load_verifier()


# ---------------------------------------------------------------------------
# Real registry passes
# ---------------------------------------------------------------------------

def test_real_registry_passes():
    errors = verifier.check()
    assert errors == [], f"Real registry has errors: {errors}"


def test_main_returns_zero():
    assert verifier.main([]) == 0


# ---------------------------------------------------------------------------
# Synthetic: missing doc section → fail
# ---------------------------------------------------------------------------

def test_missing_section_fails(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Present heading\n\nsome content\n", encoding="utf-8")

    registry = tmp_path / "doc-bindings.json"
    registry.write_text(json.dumps({
        "concepts": [{
            "id": "test-missing-section",
            "concept": "test",
            "doc": {"file": "doc.md", "section": "Missing heading"},
        }]
    }), encoding="utf-8")

    errors = verifier.check(registry_path=registry, repo_root=tmp_path)
    assert any("Missing heading" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Synthetic: missing anchor symbol → fail
# ---------------------------------------------------------------------------

def test_missing_anchor_symbol_fails(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Present heading\n\nsome content\n", encoding="utf-8")

    registry = tmp_path / "doc-bindings.json"
    registry.write_text(json.dumps({
        "concepts": [{
            "id": "test-missing-symbol",
            "concept": "test",
            "doc": {"file": "doc.md", "section": "Present heading"},
            "anchors": [{"module": "agentctl.state", "symbols": ["NonExistentSymbolXYZ"]}],
        }]
    }), encoding="utf-8")

    errors = verifier.check(registry_path=registry, repo_root=tmp_path)
    assert any("NonExistentSymbolXYZ" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Synthetic: doc-only concept (no anchors) with present section → pass
# ---------------------------------------------------------------------------

def test_doc_only_concept_passes(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# My section\n\nsome content\n", encoding="utf-8")

    registry = tmp_path / "doc-bindings.json"
    registry.write_text(json.dumps({
        "concepts": [{
            "id": "doc-only",
            "concept": "test doc-only",
            "doc": {"file": "doc.md", "section": "My section"},
        }]
    }), encoding="utf-8")

    errors = verifier.check(registry_path=registry, repo_root=tmp_path)
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Synthetic: missing doc file → fail
# ---------------------------------------------------------------------------

def test_missing_doc_file_fails(tmp_path):
    registry = tmp_path / "doc-bindings.json"
    registry.write_text(json.dumps({
        "concepts": [{
            "id": "missing-file",
            "concept": "test",
            "doc": {"file": "no-such-file.md", "section": "Anything"},
        }]
    }), encoding="utf-8")

    errors = verifier.check(registry_path=registry, repo_root=tmp_path)
    assert errors, "Expected errors for missing doc file"
