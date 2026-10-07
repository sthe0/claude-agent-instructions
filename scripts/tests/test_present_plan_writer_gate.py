"""Tests for the writer-pass gate on `agentctl present-plan`: a rendering whose
bytes are not bound (lib.writer_pass.bind) to a tech-writer witness in the
session's own transcript is refused and stamps nothing.

Transcripts are built in-test in the shapes committed under
fixtures/published-text/, and placed where the harness writes them
(<projects root>/<cwd-hash>/<session id>.jsonl) under a redirected config root."""
from __future__ import annotations

import ast
import json
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.store import FileStateStore
from lib import writer_pass
from test_plan_presentation import _to_plan_ready, _write_rendering

_HOOK = Path(__file__).resolve().parent.parent / "hook-published-text-writer-gate.py"
_FULL_BODY = "[stage 1] Scaffold module\nbody1\n[stage 2] Add tests\nbody2\n"


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """One config root for state and transcripts, and a HOME that holds none, so
    `config_root.projects_roots()` sees only this test's directory."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(home))
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    monkeypatch.delenv(writer_pass.OVERRIDE_ENV, raising=False)
    (home / "projects" / "x").mkdir(parents=True)
    return home


@pytest.fixture
def gate_store(roots):
    return FileStateStore(roots / "agentctl" / "state")


def _assistant(block: dict) -> str:
    return json.dumps(
        {"type": "assistant", "timestamp": "2026-10-07T10:00:00Z",
         "message": {"role": "assistant", "content": [block]}}
    )


def _witness() -> str:
    return _assistant(
        {"type": "tool_use", "id": "w1", "name": "Skill",
         "input": {"skill": "tech-writer", "args": "polish the plan rendering"}}
    )


def _write(content: str) -> str:
    return _assistant(
        {"type": "tool_use", "id": "wr1", "name": "Write",
         "input": {"file_path": "/tmp/rendering.txt", "content": content}}
    )


def _edit(new_string: str) -> str:
    return _assistant(
        {"type": "tool_use", "id": "ed1", "name": "Edit",
         "input": {"file_path": "/tmp/rendering.txt", "old_string": "a", "new_string": new_string}}
    )


def _transcript(roots: Path, sid: str, *lines: str) -> Path:
    path = roots / "projects" / "x" / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _present(store, sid, rendering, kind="essence", **extra):
    return cli.cmd_present_plan(
        ns(session=sid, kind=kind, rendering_file=rendering, emit_skeleton=False, **extra),
        store=store,
    )


def _setup(gate_store, fixtures_dir, tmp_path, sid, body="Summary of the plan."):
    _to_plan_ready(gate_store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    rendering = _write_rendering(tmp_path, body)
    return rendering, Path(rendering).read_text(encoding="utf-8")


def _events(store, sid, name):
    return [h for h in store.load(sid).history if h.get("event") == name]


def test_essence_refused_without_witness(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-refused"
    rendering, _text = _setup(gate_store, fixtures_dir, tmp_path, sid)
    _transcript(roots, sid, _assistant({"type": "text", "text": "plain turn"}))

    d = _present(gate_store, sid, rendering)

    assert d.ok is False
    assert writer_pass.NONE_STRENGTH in d.detail
    assert "tech-writer witness" in d.detail
    assert d.data["writer_gate"]["outcome"] == "refused"
    assert gate_store.load(sid).plan_presentations == []
    assert _events(gate_store, sid, "present_plan") == []


def test_essence_bound_post_witness(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-bound"
    rendering, text = _setup(gate_store, fixtures_dir, tmp_path, sid)
    _transcript(roots, sid, _witness(), _write(text))

    d = _present(gate_store, sid, rendering)

    assert d.ok is True
    assert d.data["writer_gate"]["outcome"] == "bound"
    assert d.data["writer_gate"]["strength"] == writer_pass.POST_WITNESS
    assert [p.kind for p in gate_store.load(sid).plan_presentations] == ["essence"]


def test_witness_then_divergent_edit_refused(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-edit"
    rendering, text = _setup(gate_store, fixtures_dir, tmp_path, sid)
    _transcript(roots, sid, _witness(), _edit("a fragment, not the whole rendering"))

    d = _present(gate_store, sid, rendering)

    assert d.ok is False
    assert gate_store.load(sid).plan_presentations == []


def test_full_and_replan_diff_gated(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-kinds"
    rendering, text = _setup(gate_store, fixtures_dir, tmp_path, sid, body=_FULL_BODY)
    _transcript(roots, sid, _assistant({"type": "text", "text": "plain turn"}))

    for kind in ("full", "replan_diff"):
        d = _present(gate_store, sid, rendering, kind=kind)
        assert d.ok is False, kind
        assert "tech-writer witness" in d.detail
    assert gate_store.load(sid).plan_presentations == []

    _transcript(roots, sid, _witness(), _write(text))
    for kind in ("full", "replan_diff"):
        assert _present(gate_store, sid, rendering, kind=kind).ok is True, kind


def test_skeleton_not_gated(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-skeleton"
    _setup(gate_store, fixtures_dir, tmp_path, sid)
    _transcript(roots, sid, _assistant({"type": "text", "text": "plain turn"}))

    d = cli.cmd_present_plan(
        ns(session=sid, kind="full", rendering_file=None, emit_skeleton=True),
        store=gate_store,
    )

    assert d.ok is True
    assert "skeleton" in d.data


def test_override_env_allows_and_logs(gate_store, fixtures_dir, tmp_path, roots, monkeypatch):
    sid = "wg-override"
    rendering, _text = _setup(gate_store, fixtures_dir, tmp_path, sid)
    _transcript(roots, sid, _assistant({"type": "text", "text": "plain turn"}))
    monkeypatch.setenv(writer_pass.OVERRIDE_ENV, "0")

    d = _present(gate_store, sid, rendering)

    assert d.ok is True
    assert d.detail.startswith("writer gate OVERRIDDEN (CLAUDE_PUBLISHED_TEXT_GATE=0)")
    assert d.data["writer_gate"] == {"outcome": "override"}
    logged = _events(gate_store, sid, "present_plan_writer_gate")
    assert [e["outcome"] for e in logged] == ["override"]


def test_no_transcript_is_advisory(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-none"
    rendering, _text = _setup(gate_store, fixtures_dir, tmp_path, sid)

    d = _present(gate_store, sid, rendering)

    assert d.ok is True
    assert d.data["writer_gate"]["outcome"] == "no_transcript"
    assert [p.kind for p in gate_store.load(sid).plan_presentations] == ["essence"]


def test_transcript_derived_from_projects_roots(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-derived"
    rendering, text = _setup(gate_store, fixtures_dir, tmp_path, sid)
    path = _transcript(roots, sid, _witness(), _write(text))

    d = _present(gate_store, sid, rendering)

    assert d.ok is True
    assert d.data["writer_gate"]["outcome"] == "bound"
    assert d.data["writer_gate"]["transcript"] == str(path)


def test_unreadable_transcript_is_advisory(gate_store, fixtures_dir, tmp_path, roots):
    sid = "wg-unreadable"
    rendering, _text = _setup(gate_store, fixtures_dir, tmp_path, sid)
    (roots / "projects" / "x" / f"{sid}.jsonl").mkdir()

    d = _present(gate_store, sid, rendering)

    assert d.ok is True
    assert d.data["writer_gate"]["outcome"] == "unreadable"


def test_override_env_name_matches_hook():
    declared = [
        node.value.value
        for node in ast.parse(_HOOK.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_TEXT_GATE_OVERRIDE_ENV" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert declared == [writer_pass.OVERRIDE_ENV]
