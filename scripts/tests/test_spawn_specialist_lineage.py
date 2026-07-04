"""spawn-specialist.py lineage composition: the child's AGENT_LINEAGE_IDS is the
ordered, deduped union of the inherited lineage (this spawn's own ancestors) plus
the spawning session's own id (CLAUDE_CODE_SESSION_ID), so a parent and its
synchronously-spawned descendants form one write-lineage that the scope-conflict
hook treats as a single actor.

Also pins _spawn_tags to the harness-exposed env var CLAUDE_CODE_SESSION_ID (the
pre-fix code read the non-existent CLAUDE_SESSION_ID and silently logged null).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load():
    spec = importlib.util.spec_from_file_location("spawn_specialist_lineage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


# ── build_child_lineage: inherited ∪ own id, ordered, deduped ────────────────

def test_build_child_lineage_appends_own_id_to_empty_inherited():
    assert MOD.build_child_lineage("", "P") == "P"


def test_build_child_lineage_appends_own_id_to_inherited_chain():
    assert MOD.build_child_lineage("G", "P") == "G,P"


def test_build_child_lineage_dedups_own_id_already_present():
    assert MOD.build_child_lineage("G,P", "P") == "G,P"


def test_build_child_lineage_preserves_order_and_dedups_inherited():
    assert MOD.build_child_lineage("G,G,P", "P") == "G,P"


def test_build_child_lineage_no_own_id_returns_inherited():
    assert MOD.build_child_lineage("G,P", None) == "G,P"
    assert MOD.build_child_lineage("G,P", "") == "G,P"


def test_build_child_lineage_empty_when_nothing():
    assert MOD.build_child_lineage("", None) == ""
    assert MOD.build_child_lineage(None, None) == ""


# ── _spawn_tags reads the real harness session-id env var ────────────────────

def test_spawn_tags_reads_claude_code_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-xyz")
    monkeypatch.delenv("CLAUDE_TICKET", raising=False)
    assert MOD._spawn_tags()["session_id"] == "sess-xyz"
