"""consensus-synthesizer deterministic stages + propose-only invariant (ADR-0001 S4 stage 11).

Tests the pure stages (normalize / cluster / detect-conflict / induce-invariant) and proves a
dry-run leaves Core byte-unchanged. The AskUserQuestion menu + promotion are human-gated (named
non-testable escape) — only the no-auto-write invariant is asserted for them.
"""
import hashlib
import importlib.util
import sys
from pathlib import Path

import difficulty_channel as dc

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "consensus_synthesizer", REPO_ROOT / "scripts" / "consensus-synthesizer.py"
)
syn = importlib.util.module_from_spec(_SPEC)
sys.modules["consensus_synthesizer"] = syn
_SPEC.loader.exec_module(syn)


def _rec(ground, sev=dc.Severity.MEDIUM, reporter="startrek", target="CLAUDE.md"):
    return dc.DifficultyRecord(
        ts="2026-06-26T00:00:00", layer="core", target=target,
        functional_ground=ground, severity=sev, reporter=reporter, evidence="e",
    )


def test_normalize_rejects_non_records():
    assert syn.normalize([_rec("g")])
    import pytest
    with pytest.raises(TypeError):
        syn.normalize(["not a record"])


def test_cluster_reuses_digest_stage9():
    recs = [_rec("same ground here", reporter="startrek"),
            _rec("same ground here", reporter="external")]
    clusters = syn.cluster_records(recs)
    assert len(clusters) == 1 and clusters[0].channels == {"startrek", "external"}


def test_detect_conflict_on_a_vs_not_a():
    a = syn.Edit(target="CLAUDE.md", directive="side-effect-free actions are pre-authorized", assertion=True)
    not_a = syn.Edit(target="CLAUDE.md", directive="side-effect-free actions are pre-authorized", assertion=False)
    assert syn.detect_conflict(a, not_a) is True
    # different target -> not a conflict
    other = syn.Edit(target="config.md", directive="side-effect-free actions are pre-authorized", assertion=False)
    assert syn.detect_conflict(a, other) is False
    # same assertion -> not a conflict
    assert syn.detect_conflict(a, syn.Edit(target="CLAUDE.md", directive=a.directive)) is False


def test_induce_invariant_returns_commonality_and_difference():
    a = syn.Edit(target="x", directive="prefer skill first dispatch")
    b = syn.Edit(target="x", directive="prefer direct bash dispatch")
    inv = syn.induce_invariant(a, b)
    assert set(inv) == {"commonality", "difference"}
    assert "prefer" in inv["commonality"] and "dispatch" in inv["commonality"]
    assert "skill" in inv["difference"]["a_only"]
    assert "bash" in inv["difference"]["b_only"]


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_leaves_core_byte_unchanged():
    core_files = [REPO_ROOT / "CLAUDE.md", REPO_ROOT / "config.md",
                  REPO_ROOT / "skills" / "self-improvement" / "SKILL.md"]
    before = {f: _hash(f) for f in core_files if f.exists()}
    recs = [_rec("gate too coarse", dc.Severity.HIGH, "startrek"),
            _rec("gate too coarse", dc.Severity.HIGH, "external")]
    result = syn.run_synthesis(recs, threshold=4)
    assert result.core_written is False
    assert len(result.menu) == 1  # flagged (mass 8 >= 4)
    assert result.menu[0].invariant is not None  # critique applied to the 2-record cluster
    after = {f: _hash(f) for f in core_files if f.exists()}
    assert before == after, "synthesizer dry-run must not modify any Core file"


def test_promote_is_handoff_not_write():
    recs = [_rec("some ground", dc.Severity.CRITICAL, "startrek")]
    [prop] = syn.run_synthesis(recs, threshold=1000).menu  # critical flags it
    route = syn.promote_to_layer(prop)
    assert route["action"] == "handoff"
    assert route["core_written"] is False
    assert "planner" in route["route"] and "developer" in route["route"]
