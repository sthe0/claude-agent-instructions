"""Pre-trial defects #322, #421, #422: the essence delivery proof and the writer gate follow the
engine session a conversation drives, not the harness conversation id.

A harness session H may drive several engine sessions through `agentctl --session`, and a linked
child's turns run in a process whose harness id is not the engine session id the child was handed.
The delivery-gate hook and the present-plan writer gate both resolve "the session" from the
harness id, so a second engine session's delivered essence is never stamped and a linked child's
own tech-writer pass is never found.

Each test builds engine state with the in-process cli surface, binds the harness session to the
engine session through a real `agentctl start` run as the harness (the way a coordinator creates
it), lays down the transcripts a harness would have written, and asserts the repaired behaviour
with a message that starts `BEHAVIOUR:`; every setup assertion lacks that marker.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery as delivery_mod
from agentctl.state import SHOW_FULL_PLAN_MARKER as MARKER
from agentctl.store import FileStateStore
from lib import writer_pass
from test_plan_presentation import _declared_venue_plan, _write_rendering

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLI = SCRIPTS_DIR / "agentctl-cli.py"
HOOK = SCRIPTS_DIR / "hook-plan-delivery-gate.py"
RENDERING_BODY = "Essence of the plan: two stages, one goal."


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """One config root holding state and transcripts, and a HOME that holds neither."""
    root = tmp_path / "cfg"
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(root))
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
    monkeypatch.setenv(writer_pass.OVERRIDE_ENV, "0")
    (root / "projects" / "x").mkdir(parents=True)
    return root


@pytest.fixture
def store(cfg):
    return FileStateStore(cfg / "agentctl" / "state")


def _env(tmp_path: Path, cfg: Path, harness: str | None, *, writer_gate: bool) -> dict:
    bin_dir = cfg / "_fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\necho YES\n")
    stub.chmod(0o755)
    env = {
        "HOME": str(tmp_path / "fakehome"),
        "CLAUDE_CONFIG_DIR": str(cfg),
        "CLAUDE_AGENT_HOME": str(cfg),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "AGENTCTL_PLAN_PRESENTATION": "1",
        "AGENTCTL_PLAN_REVIEW": "0",
    }
    if not writer_gate:
        env[writer_pass.OVERRIDE_ENV] = "0"
    if harness is not None:
        env["CLAUDE_CODE_SESSION_ID"] = harness
    return env


def _agentctl(tmp_path, cfg, harness, *argv, writer_gate=False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *argv], capture_output=True, text=True, timeout=120,
        env=_env(tmp_path, cfg, harness, writer_gate=writer_gate),
    )


def _start_as(tmp_path, cfg, harness: str, engine: str) -> None:
    proc = _agentctl(tmp_path, cfg, harness, "start", "--session", engine, "--task", "driven", "--goal", "goal")
    assert proc.returncode == 0, f"setup: start failed: {proc.stdout[-300:]}"


def _to_plan_ready(store, sid: str, plan: str) -> None:
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    directive = cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert directive.ok, f"setup: submit-plan failed: {directive.detail}"


def _present(store, sid: str, tmp_path: Path, plan: str, body: str) -> None:
    rendering = _write_rendering(tmp_path, body, name=f"{sid}-rendering.txt", plan_path=plan)
    directive = cli.cmd_present_plan(
        ns(session=sid, kind="essence", rendering_file=rendering, emit_skeleton=False), store=store,
    )
    assert directive.ok, f"setup: present-plan failed: {directive.detail}"


def _plan_ready_with_receipt(store, sid, tmp_path, fixtures_dir, body=RENDERING_BODY):
    plan = _declared_venue_plan(fixtures_dir, tmp_path / sid)
    _to_plan_ready(store, sid, plan)
    _present(store, sid, tmp_path, plan, body)
    return store.load(sid).plan_presentations[0]


def _entry(role: str, ts: float, blocks: list[dict]) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"
    return json.dumps({"type": role, "timestamp": stamp, "message": {"role": role, "content": blocks}})


def _delivered_transcript(path: Path, receipt, *extra_lines: str) -> Path:
    """User prompt, the delivered rendering as the final assistant text, a later user prompt."""
    p = receipt.presented_ts
    lines = [
        _entry("user", p - 1.0, [{"type": "text", "text": "plan it"}]),
        *extra_lines,
        _entry("assistant", p + 1.0, [{"type": "text", "text": receipt.rendering_text}]),
        _entry("user", p + 2.0, [{"type": "text", "text": "ok"}]),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_writer_pass_transcript(path: Path, rendering_file: str) -> None:
    """A tech-writer `Skill` call followed by a `Write` of the rendering's exact bytes: the
    witness the present-plan writer gate binds a rendering to."""
    text = Path(rendering_file).read_text(encoding="utf-8")
    witness = _entry("assistant", time.time() - 10, [
        {"type": "tool_use", "id": "w1", "name": "Skill",
         "input": {"skill": "tech-writer", "args": "polish the plan rendering"}},
    ])
    composed = _entry("assistant", time.time() - 9, [
        {"type": "tool_use", "id": "wr1", "name": "Write",
         "input": {"file_path": "/tmp/rendering.txt", "content": text}},
    ])
    path.write_text(witness + "\n" + composed + "\n", encoding="utf-8")


def _transcript_path(cfg: Path, harness: str) -> Path:
    return cfg / "projects" / "x" / f"{harness}.jsonl"


def _ask_payload(harness: str, transcript: Path) -> dict:
    return {
        "tool_name": "AskUserQuestion",
        "session_id": harness,
        "transcript_path": str(transcript),
        "tool_input": {"questions": [{
            "question": "Approve the plan?",
            "options": [{"label": f"Show me the full plan {MARKER}"}, {"label": "No"}],
        }]},
    }


def _run_hook(tmp_path, cfg, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
        timeout=120, env=_env(tmp_path, cfg, None, writer_gate=False),
    )


def _stamp(cfg: Path, session: str):
    return delivery_mod.read_stamp(cfg / "agentctl" / "state" / f"{session}.json")


def test_i322_second_session_essence_delivery_recorded(cfg, store, tmp_path, fixtures_dir):
    harness = "harness-322"
    _start_as(tmp_path, cfg, harness, "engine-first")
    _start_as(tmp_path, cfg, harness, "engine-second")
    _plan_ready_with_receipt(store, "engine-first", tmp_path, fixtures_dir, body="Essence of the first plan.")
    second = _plan_ready_with_receipt(store, "engine-second", tmp_path, fixtures_dir)
    transcript = _delivered_transcript(_transcript_path(cfg, harness), second)

    proc = _run_hook(tmp_path, cfg, _ask_payload(harness, transcript))

    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert _stamp(cfg, "engine-second") is not None, (
        "BEHAVIOUR: the essence the conversation delivered for its second engine session "
        "leaves that session without a delivery stamp"
    )


def test_i421_linked_child_writer_witness_found(cfg, store, tmp_path, fixtures_dir):
    harness, child = "harness-child-421", "engine-child-421"
    _start_as(tmp_path, cfg, harness, child)
    plan = _declared_venue_plan(fixtures_dir, tmp_path / child)
    _to_plan_ready(store, child, plan)
    rendering = _write_rendering(tmp_path, RENDERING_BODY, name="child-rendering.txt", plan_path=plan)
    _write_writer_pass_transcript(_transcript_path(cfg, harness), rendering)

    proc = _agentctl(
        tmp_path, cfg, harness, "present-plan", "--session", child, "--kind", "essence",
        "--rendering-file", rendering, writer_gate=True,
    )

    assert proc.returncode == 0, f"setup: present-plan failed: {proc.stdout[-300:]}"
    gate_events = [h for h in store.load(child).history if h.get("event") == "present_plan_writer_gate"]
    assert gate_events, "setup: present-plan records its writer-gate outcome"
    assert gate_events[-1].get("outcome") == "bound", (
        "BEHAVIOUR: the writer gate does not find the tech-writer pass in the linked child's own "
        f"turns; outcome {gate_events[-1].get('outcome')!r}"
    )


def test_writer_gate_unlinked_session_binds_own_transcript(cfg, store, tmp_path, fixtures_dir):
    session = "single-writer-session"
    cli.cmd_start(
        ns(session=session, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    plan = _declared_venue_plan(fixtures_dir, tmp_path / session)
    _to_plan_ready(store, session, plan)
    rendering = _write_rendering(tmp_path, RENDERING_BODY, name="single-rendering.txt", plan_path=plan)
    _write_writer_pass_transcript(_transcript_path(cfg, session), rendering)

    proc = _agentctl(
        tmp_path, cfg, session, "present-plan", "--session", session, "--kind", "essence",
        "--rendering-file", rendering, writer_gate=True,
    )

    assert proc.returncode == 0, f"present-plan failed: {proc.stdout[-300:]}"
    gate_events = [h for h in store.load(session).history if h.get("event") == "present_plan_writer_gate"]
    assert gate_events and gate_events[-1].get("outcome") == "bound"


def test_i422_linked_child_delivery_looked_up_in_own_transcript(cfg, store, tmp_path, fixtures_dir):
    """The child's essence is delivered in its own transcript (named by its harness id); the
    parent has a transcript of its own that does not carry it. On base the hook resolves the
    session from the harness id alone, so this fails for the same reason as #322 -- the
    harness id names no engine session -- and not on a parent-transcript lookup that base
    does not perform; no base surface distinguishes the two. The parent transcript is here
    as the guard against a fix that finds the child's delivery by reading the parent's."""
    harness, child, parent = "harness-child-422", "engine-child-422", "engine-parent-422"
    _start_as(tmp_path, cfg, harness, child)
    receipt = _plan_ready_with_receipt(store, child, tmp_path, fixtures_dir)
    state = store.load(child)
    state.parent_link = {"parent_session": parent, "stage_index": 1}
    store.save(state)
    parent_transcript = _transcript_path(cfg, parent)
    parent_transcript.write_text(_entry("user", time.time() - 5, [{"type": "text", "text": "parent turn"}]) + "\n")
    own_transcript = _delivered_transcript(_transcript_path(cfg, harness), receipt)

    proc = _run_hook(tmp_path, cfg, _ask_payload(harness, own_transcript))

    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert _stamp(cfg, child) is not None, (
        "BEHAVIOUR: the essence delivered in the linked child's own transcript leaves the child "
        "without a delivery stamp"
    )


def test_delivery_unlinked_session_keeps_base_lookup(cfg, store, tmp_path, fixtures_dir):
    session = "single-session"
    cli.cmd_start(
        ns(session=session, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    receipt = _plan_ready_with_receipt(store, session, tmp_path, fixtures_dir)
    transcript = _delivered_transcript(_transcript_path(cfg, session), receipt)

    proc = _run_hook(tmp_path, cfg, _ask_payload(session, transcript))

    assert proc.returncode == 0
    stamp = _stamp(cfg, session)
    assert stamp is not None, "an unlinked single-session coordinator is stamped under its own id"
    assert stamp.plan_sha256 == receipt.plan_sha256 and stamp.rendering_sha256 == receipt.rendering_sha256


def _touch(path: Path, age_s: float) -> None:
    import os

    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def test_linked_child_transcript_lookup_ignores_driving_root_transcript(cfg, store, tmp_path):
    from lib import engine_session_binding

    root, child, parent = "root-harness-linked", "engine-child-linked", "engine-parent-linked"
    _start_as(tmp_path, cfg, root, child)
    assert child in engine_session_binding.driven_sessions(root), "setup: the root drove the child"
    state = store.load(child)
    state.parent_link = {"parent_session": parent, "stage_index": 1}
    store.save(state)
    own = _transcript_path(cfg, child)
    own.write_text(_entry("user", time.time() - 50, [{"type": "text", "text": "child turn"}]) + "\n")
    root_transcript = _transcript_path(cfg, root)
    root_transcript.write_text(_entry("user", time.time() - 5, [{"type": "text", "text": "root turn"}]) + "\n")
    _touch(own, 100.0)
    _touch(root_transcript, 1.0)

    assert engine_session_binding.transcript_for_session(child) == own, (
        "BEHAVIOUR: a linked child's lookup is taken over by the newer transcript of the root "
        "that drives it"
    )
    assert cli._present_plan_transcript(child) == own


def test_unlinked_session_driven_from_new_conversation_finds_newest_transcript(cfg, store, tmp_path):
    from lib import engine_session_binding

    new, engine = "new-conversation", "engine-continued"
    _start_as(tmp_path, cfg, new, engine)
    old_transcript = _transcript_path(cfg, engine)
    old_transcript.write_text(_entry("user", time.time() - 50, [{"type": "text", "text": "old"}]) + "\n")
    new_transcript = _transcript_path(cfg, new)
    new_transcript.write_text(_entry("user", time.time() - 5, [{"type": "text", "text": "new"}]) + "\n")
    _touch(old_transcript, 100.0)
    _touch(new_transcript, 1.0)

    assert engine_session_binding.transcript_for_session(engine) == new_transcript


def test_receipt_stamped_on_session_owning_presented_bytes(cfg, store, tmp_path, fixtures_dir):
    harness = "harness-three"
    engines = ("engine-a", "engine-b", "engine-c")
    for engine in engines:
        _start_as(tmp_path, cfg, harness, engine)
    receipts = {
        engine: _plan_ready_with_receipt(store, engine, tmp_path, fixtures_dir, body="Essence of plan " + engine + ".")
        for engine in engines
    }
    transcript = _delivered_transcript(_transcript_path(cfg, harness), receipts["engine-b"])

    proc = _run_hook(tmp_path, cfg, _ask_payload(harness, transcript))

    assert proc.returncode == 0, "setup: the hook must never exit non-zero"
    assert _stamp(cfg, "engine-b") is not None, (
        "BEHAVIOUR: the session whose registered essence was delivered, not the most recently "
        "driven one, is left without a delivery stamp"
    )
    assert _stamp(cfg, "engine-a") is None and _stamp(cfg, "engine-c") is None, (
        "BEHAVIOUR: a session whose essence was not delivered received the delivery stamp"
    )
