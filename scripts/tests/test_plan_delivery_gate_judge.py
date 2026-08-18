"""hook-plan-delivery-gate.py's Stage 8 extension: the receipt/freshness/
delivery/marker checks apply only to an ask agentctl.advisor.judge_approval_ask
identifies as the plan-approval ask, not to every AskUserQuestion at node
PLAN_READY. See test_plan_delivery_gate_presentation.py for the (unchanged
in strength) checks themselves, driven there with a constant-YES classifier
stub; this file is what actually exercises the classifier's own YES/NO/
absent-runner outcomes end-to-end.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agentctl import delivery as delivery_mod
from lib import judge_ledger

from test_plan_delivery_gate_presentation import (
    HOOK,
    MARKER,
    RENDERING,
    _is_deny,
    _stamp,
    ask_payload,
    make_receipt,
    text_only_entry,
    user_prompt_entry,
    write_full_state,
    write_transcript,
)


def _ledger_path(config_dir: Path) -> Path:
    """Every test here gets its own config_dir (tmp_path), so keying the
    ledger file off it isolates one test's records from the next without a
    separate fixture -- same per-test isolation the suite-wide
    _isolate_judge_ledger autouse fixture gives in-process callers, applied
    here to a subprocess that does not inherit that fixture's monkeypatched
    env at all (env= below is a full replacement, not additive)."""
    return config_dir / "_ledger.jsonl"


def _ledger_records(config_dir: Path) -> list[dict]:
    return judge_ledger.read_records(_ledger_path(config_dir))


def _run_hook(payload: dict, config_dir: Path, *, bin_dir: Path | None) -> subprocess.CompletedProcess:
    """Like test_plan_delivery_gate_presentation.run_hook, but the caller
    controls what (if anything) PATH exposes as `claude` -- bin_dir=None
    reproduces the pre-Stage-8 default (no claude on PATH at all)."""
    path = f"{bin_dir}:/usr/bin:/bin" if bin_dir is not None else "/usr/bin:/bin"
    env = {
        "PATH": path, "HOME": str(config_dir), "CLAUDE_CONFIG_DIR": str(config_dir),
        "AGENTCTL_JUDGE_LEDGER": str(_ledger_path(config_dir)),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )


def _write_stub(bin_dir: Path, answer: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(f"#!/bin/sh\necho {answer}\n")
    stub.chmod(0o755)


def run_hook_answering(payload: dict, config_dir: Path, answer: str) -> subprocess.CompletedProcess:
    bin_dir = config_dir / "_fakebin"
    _write_stub(bin_dir, answer)
    return _run_hook(payload, config_dir, bin_dir=bin_dir)


def run_hook_no_claude(payload: dict, config_dir: Path) -> subprocess.CompletedProcess:
    return _run_hook(payload, config_dir, bin_dir=None)


# (a) an unrelated ask at PLAN_READY is allowed -------------------------------

def test_unrelated_ask_is_allowed_even_with_a_pending_receipt(tmp_path):
    # A satisfied receipt exists and delivery was never verified for THIS ask
    # -- if the classifier didn't gate scope, this would deny on "not
    # delivered". A NO classifier must instead let it straight through.
    write_full_state(tmp_path, "u1", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook_answering(ask_payload("u1", t, question="Which color?"), tmp_path, "NO")
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "u1") is None


# (b) an identified approval ask still fails every structural check ----------

def test_identified_approval_ask_same_turn_still_denies(tmp_path):
    write_full_state(tmp_path, "b1", plan_submitted_ts=105.0, last_user_prompt_ts=100.0,
                      plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(100.0)])  # no later boundary
    proc = run_hook_answering(ask_payload("b1", t), tmp_path, "YES")
    assert _is_deny(proc)
    assert "same" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_identified_approval_ask_no_receipt_still_denies(tmp_path):
    write_full_state(tmp_path, "b2", plan_presentations=[])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook_answering(ask_payload("b2", t), tmp_path, "YES")
    assert _is_deny(proc)
    assert "present-plan" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_identified_approval_ask_no_marker_still_denies(tmp_path):
    write_full_state(tmp_path, "b3", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook_answering(ask_payload("b3", t, with_marker=False), tmp_path, "YES")
    assert _is_deny(proc)
    assert MARKER in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_identified_approval_ask_never_delivered_still_denies(tmp_path):
    write_full_state(tmp_path, "b4", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0)])  # no assistant text at all
    proc = run_hook_answering(ask_payload("b4", t), tmp_path, "YES")
    assert _is_deny(proc)
    assert _stamp(tmp_path, "b4") is None


# (c) an absent runner allows and does not stamp ------------------------------

def test_absent_claude_binary_allows_without_stamping_even_when_delivered(tmp_path):
    # Receipt + marker + a genuine delivery are all present -- with claude on
    # PATH this would ALLOW + STAMP (see test_delivered_allows_and_stamps).
    # With no `claude` executable reachable, subprocess_runner raises inside
    # the classifier, which fails open to "not the approval ask": still an
    # ALLOW, but the strict/stamping path is never reached.
    write_full_state(tmp_path, "c1", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook_no_claude(ask_payload("c1", t), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "c1") is None


# (d) a judged approval ask with a verified delivery does stamp --------------

def test_identified_approval_ask_with_verified_delivery_stamps(tmp_path):
    write_full_state(tmp_path, "d1", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook_answering(ask_payload("d1", t), tmp_path, "YES")
    assert proc.returncode == 0
    assert not _is_deny(proc)
    stamp = _stamp(tmp_path, "d1")
    assert stamp is not None
    assert stamp.source == delivery_mod.SOURCE_HOOK


# (e) the ledger records a judged invocation, and stays silent on one that
#     never reached the classifier -----------------------------------------

def test_judged_invocation_writes_entered_and_decided_for_approval_ask(tmp_path):
    """Same golden path as test_identified_approval_ask_with_verified_delivery_
    stamps -- node PLAN_READY, an active presentation -- but the assertion is
    on the ledger this Stage 8 pass adds, not on the hook's stdout/stamp."""
    write_full_state(tmp_path, "e1", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook_answering(ask_payload("e1", t), tmp_path, "YES")
    assert proc.returncode == 0

    records = _ledger_records(tmp_path)
    entered = [r for r in records if r["kind"] == "entered" and r["judge"] == "approval_ask"]
    decided = [r for r in records if r["kind"] == "decided" and r["judge"] == "approval_ask"]
    assert len(entered) == 1
    assert entered[0]["prefilter_fired"] is True
    assert len(decided) == 1
    assert decided[0]["stage"] == "call"
    assert decided[0]["verdict"] is True
    assert decided[0]["reason"] == ""


def test_non_judged_invocation_writes_no_approval_ask_records(tmp_path):
    """A node other than PLAN_READY never reaches the classifier at all --
    decide() returns "allow" before the judge branch, mirroring
    gate_decision's own node != GATED_NODE early return. No claude stub is on
    PATH here on purpose: reaching the classifier at all would make this test
    depend on the stub instead of proving the call was skipped."""
    write_full_state(tmp_path, "e2", node="EXECUTING", approval_passed=True,
                      plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook_no_claude(ask_payload("e2", t), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)

    records = _ledger_records(tmp_path)
    assert [r for r in records if r.get("judge") == "approval_ask"] == []
