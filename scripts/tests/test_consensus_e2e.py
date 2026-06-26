"""End-to-end smoke for the consensus architecture (ADR-0001 final verification, stage 14).

Offline, in-memory channel. Demonstrates the ADR's removed difficulty is closed:
a NON-author files a Core-target DifficultyRecord to a channel (no push to Core)
  -> core-difficulty-digest.py pulls + normalizes + clusters-by-functional-ground + FLAGS
  -> consensus-synthesizer.py produces a ranked menu (dry-run)
  -> Core files (CLAUDE.md, config.md, skills/**) are byte-unchanged by the whole run.

The non-author's fast path is file-to-channel, not edit-Core; Core changes only via a distilled,
accumulated, author-approved principle through the human gate.
"""
import hashlib
import importlib.util
import sys
from pathlib import Path

import difficulty_channel as dc
from difficulty_channel import authority

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, REPO_ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


digest = _load("core_difficulty_digest", "core-difficulty-digest.py")
syn = _load("consensus_synthesizer", "consensus-synthesizer.py")

CORE_FILES = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "config.md",
    REPO_ROOT / "skills" / "self-improvement" / "SKILL.md",
    REPO_ROOT / "scripts" / "agentctl" / "cli.py",
]


def _hash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rec(reporter):
    return dc.DifficultyRecord(
        ts="2026-06-26T00:00:00", layer="core", target="CLAUDE.md",
        functional_ground="gate denies a legitimate memory write on a non-author machine",
        severity=dc.Severity.HIGH, reporter=reporter, evidence="session quote",
    )


def test_non_author_to_channel_to_digest_to_synthesizer_core_unchanged():
    before = {f: _hash(f) for f in CORE_FILES if f.exists()}

    # 1. NON-author: routing decision is file-to-channel, never edit-core.
    assert authority.route_for_core_difficulty(author=False) == authority.ROUTE_TO_CHANNEL
    ch = dc.NullChannel()
    dc.register_channel("e2e", lambda: ch)
    authority.file_core_difficulty(_rec("dev-a"), channel="e2e")
    authority.file_core_difficulty(_rec("dev-b"), channel="e2e")  # second independent report

    # 2. digest pulls + clusters-by-functional-ground + flags (mass 4+4=8 >= threshold 8).
    records = digest.pull_all(["e2e"])
    threshold = digest.read_mass_threshold()  # 8 from config
    flagged = digest.digest(records, threshold)
    assert len(flagged) == 1
    assert flagged[0].reporters == {"dev-a", "dev-b"}  # two reports -> one cluster

    # 3. synthesizer produces a ranked menu (dry-run), writes nothing.
    result = syn.run_synthesis(records, threshold)
    assert result.core_written is False
    assert len(result.menu) == 1
    assert result.menu[0].invariant is not None  # critique primitive applied

    # 4. the human gate held: Core is byte-identical after the whole run.
    after = {f: _hash(f) for f in CORE_FILES if f.exists()}
    assert before == after, "the consensus run must leave Core byte-unchanged"
