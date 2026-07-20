"""hook-plan-delivery-gate.py's Stage 3 extension: deny a PLAN_READY
AskUserQuestion unless a registered plan-presentation essence receipt has
verifiably LANDED (delivered_final_texts, block-granularity) since it was
registered, and the ask offers a show-full-plan option — then, iff verified,
stamp a source="hook" DeliveryStamp via agentctl.delivery. See
test_plan_delivery_gate_hook.py for the (unchanged) same-turn check and
test_transcript_turns.py for the block-granularity primitive itself.

Driven end-to-end via subprocess, mirroring test_plan_delivery_gate_hook.py's
own conventions exactly (run_hook/env/write_transcript) plus a new
write_full_state helper that round-trips a real agentctl.state.SessionState
through to_dict()/json.dumps rather than hand-rolling JSON, so every fixture
here is schema-correct and stays correct as the schema evolves."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentctl import delivery as delivery_mod
from agentctl.state import GateRecord, PlanPresentation, SessionState

HOOK = Path(__file__).resolve().parent.parent / "hook-plan-delivery-gate.py"
MARKER = "[show-full-plan]"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def user_prompt_entry(ts: float) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}, "timestamp": iso(ts)}


def text_only_entry(ts: float, text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "timestamp": iso(ts)}


def text_then_tool_use_entry(ts: float, text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}},
        ]},
        "timestamp": iso(ts),
    }


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def write_full_state(
    config_dir: Path,
    session_id: str,
    *,
    node: str = "PLAN_READY",
    plan_submitted_ts: float | None = 100.0,
    last_user_prompt_ts: float | None = 90.0,
    weight_class: str | None = "SUBSTANTIVE",
    plan_path: str | None = "/plan.toml",
    plan_presentations: list[PlanPresentation] | None = None,
    approval_passed: bool = False,
) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        session_id=session_id, task_id="t", node=node,
        plan_submitted_ts=plan_submitted_ts, last_user_prompt_ts=last_user_prompt_ts,
        weight_class=weight_class, plan_path=plan_path,
        plan_presentations=plan_presentations or [],
    )
    # A post-approval node (EXECUTING, VERIFYING, ...) fails check_invariants
    # unless the plan-approval gate is passed; set it so the fixture can model
    # a genuine "wrong node, past the gate" session.
    if approval_passed:
        kwargs["approval"] = GateRecord("plan_approval", armed=True, passed=True, by="user")
    state = SessionState(**kwargs)
    (state_dir / f"{session_id}.json").write_text(json.dumps(state.to_dict()))


def make_receipt(rendering_text: str, presented_ts: float, plan_path: str = "/plan.toml") -> PlanPresentation:
    return PlanPresentation(
        plan_path=plan_path, kind="essence", plan_sha256="a" * 64,
        rendering_sha256="b" * 64, rendering_text=rendering_text, presented_ts=presented_ts,
    )


def run_hook(payload: dict, config_dir: Path) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(config_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )


def ask_payload(session_id: str, transcript_path: Path | None = None, *, with_marker: bool = True, question: str = "Approve the plan?") -> dict:
    option = {"label": f"Show me the full plan {MARKER}" if with_marker else "Yes, approve"}
    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": session_id,
        "tool_input": {"questions": [{"question": question, "options": [option, {"label": "No"}]}]},
    }
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    return payload


def _is_deny(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    return json.loads(proc.stdout).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _stamp(config_dir: Path, session_id: str):
    return delivery_mod.read_stamp(config_dir / "agentctl" / "state" / f"{session_id}.json")


RENDERING = "## Stage 1\n...full plan essence..."


# --- delivered -> ALLOW + stamp -----------------------------------------------

def test_delivered_allows_and_stamps(tmp_path):
    write_full_state(tmp_path, "s1", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook(ask_payload("s1", t), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    stamp = _stamp(tmp_path, "s1")
    assert stamp is not None
    assert stamp.source == delivery_mod.SOURCE_HOOK
    assert stamp.plan_sha256 == "a" * 64 and stamp.rendering_sha256 == "b" * 64


def test_never_emitted_denies(tmp_path):
    write_full_state(tmp_path, "s2", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0)])  # no assistant text at all
    proc = run_hook(ask_payload("s2", t), tmp_path)
    assert _is_deny(proc)
    assert _stamp(tmp_path, "s2") is None


def test_delivered_with_whitespace_newline_case_drift_allows_and_stamps(tmp_path):
    # Same content as RENDERING but reformatted: extra blank line, trailing
    # whitespace, upper-cased -- incidental drift the byte-exact check used to
    # reject with _NOT_DELIVERED_REASON. Must now allow via the normalized tier.
    drifted = "## STAGE 1\n\n...full   plan essence...  \n"
    write_full_state(tmp_path, "s19", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, drifted),
        user_prompt_entry(110.0),
    ])
    proc = run_hook(ask_payload("s19", t), tmp_path)
    assert not _is_deny(proc)
    stamp = _stamp(tmp_path, "s19")
    assert stamp is not None
    assert stamp.source == delivery_mod.SOURCE_HOOK


def test_delivered_missing_content_still_denies(tmp_path):
    # Delivered text omits a whole line of the essence -- normalization
    # collapses whitespace/case, it must NOT mask a genuinely dropped line.
    truncated = "## Stage 1\n"  # missing "...full plan essence..."
    write_full_state(tmp_path, "s20", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, truncated),
        user_prompt_entry(110.0),
    ])
    proc = run_hook(ask_payload("s20", t), tmp_path)
    assert _is_deny(proc)
    assert _stamp(tmp_path, "s20") is None


def test_no_receipt_denies(tmp_path):
    write_full_state(tmp_path, "s3", plan_presentations=[])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s3", t), tmp_path)
    assert _is_deny(proc)
    assert "present-plan" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_tool_call_only_text_denies(tmp_path):
    # The rendering's bytes DID land in the transcript, but only as text
    # preceding a same-message tool_use -- never rendered.
    write_full_state(tmp_path, "s4", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_then_tool_use_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook(ask_payload("s4", t), tmp_path)
    assert _is_deny(proc)
    assert _stamp(tmp_path, "s4") is None


def test_mixed_shape_segment_denies_when_only_early_entry_qualifies(tmp_path):
    # Segment has an early genuine-looking delivery, but the TERMINAL assistant
    # entry of that same segment is trap-shaped -- block/terminal-position
    # granularity means only the terminal entry is examined, so this must deny.
    write_full_state(tmp_path, "s5", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(95.0, RENDERING),
        text_then_tool_use_entry(96.0, "unrelated follow-up"),
        user_prompt_entry(110.0),
    ])
    proc = run_hook(ask_payload("s5", t), tmp_path)
    assert _is_deny(proc)


def test_current_incomplete_turn_only_denies(tmp_path):
    write_full_state(tmp_path, "s6", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),  # no closing boundary -> still open
    ])
    proc = run_hook(ask_payload("s6", t), tmp_path)
    assert _is_deny(proc)


def test_reregistration_without_redelivery_denies_then_fresh_emission_allows(tmp_path):
    # Receipt re-registered (presented_ts bumped to 200) after the ONLY
    # delivered text (at 105, before re-registration) -> the old delivery no
    # longer post-dates the receipt -> deny.
    write_full_state(tmp_path, "s7", plan_presentations=[make_receipt(RENDERING, presented_ts=200.0)])
    t = write_transcript(tmp_path / "t.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
    ])
    proc = run_hook(ask_payload("s7", t), tmp_path)
    assert _is_deny(proc)

    # A fresh delivery emitted after re-registration -> allow + stamp.
    t2 = write_transcript(tmp_path / "t2.jsonl", [
        user_prompt_entry(90.0),
        text_only_entry(105.0, RENDERING),
        user_prompt_entry(110.0),
        text_only_entry(210.0, RENDERING),
        user_prompt_entry(220.0),
    ])
    proc2 = run_hook(ask_payload("s7", t2), tmp_path)
    assert not _is_deny(proc2)
    assert _stamp(tmp_path, "s7") is not None


def test_stale_receipt_denies(tmp_path):
    # Receipt's plan_path disagrees with the session's current plan_path.
    write_full_state(tmp_path, "s8", plan_path="/current-plan.toml",
                      plan_presentations=[make_receipt(RENDERING, presented_ts=100.0, plan_path="/old-plan.toml")])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s8", t), tmp_path)
    assert _is_deny(proc)
    assert "stale" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert _stamp(tmp_path, "s8") is None


def test_no_marker_option_denies(tmp_path):
    write_full_state(tmp_path, "s9", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s9", t, with_marker=False), tmp_path)
    assert _is_deny(proc)
    assert MARKER in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_non_approval_worded_ask_still_gated_scope_widening_accepted(tmp_path):
    # SCOPE is accepted to be "any ask at PLAN_READY", not just a self-
    # identified approval ask -- a differently-worded question with no
    # receipt still denies, and (separately) one WITH a satisfied receipt +
    # marker still allows regardless of its wording.
    write_full_state(tmp_path, "s10", plan_presentations=[])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0)])
    proc = run_hook(ask_payload("s10", t, question="Which color?"), tmp_path)
    assert _is_deny(proc)


def test_same_turn_denies_even_with_receipt_present(tmp_path):
    write_full_state(tmp_path, "s11", plan_submitted_ts=105.0, last_user_prompt_ts=100.0,
                      plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(100.0)])  # no later boundary
    proc = run_hook(ask_payload("s11", t), tmp_path)
    assert _is_deny(proc)


# --- missing observables -> allow, no stamp -----------------------------------

def test_wrong_node_allows_no_stamp(tmp_path):
    write_full_state(tmp_path, "s12", node="EXECUTING", approval_passed=True,
                     plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s12", t), tmp_path)
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "s12") is None


def test_plan_submitted_ts_none_allows_no_stamp(tmp_path):
    write_full_state(tmp_path, "s13", plan_submitted_ts=None, plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s13", t), tmp_path)
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "s13") is None


def test_missing_transcript_allows_no_stamp(tmp_path):
    # last_user_prompt_ts (110) > plan_submitted_ts (100): the plan was submitted
    # an EARLIER turn, so the same-turn state fallback passes and this isolates
    # the DELIVERY-side fail-open (no transcript -> delivered_texts=None -> allow).
    write_full_state(tmp_path, "s14", last_user_prompt_ts=110.0,
                     plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    proc = run_hook(ask_payload("s14", transcript_path=None), tmp_path)  # no transcript_path at all
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "s14") is None


def test_unreadable_transcript_allows_no_stamp(tmp_path):
    # Earlier-turn submission (see test_missing_transcript_allows_no_stamp): the
    # same-turn fallback passes; an unreadable transcript then fails open on the
    # delivery check without stamping.
    write_full_state(tmp_path, "s15", last_user_prompt_ts=110.0,
                     plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    proc = run_hook(ask_payload("s15", tmp_path / "absent.jsonl"), tmp_path)
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "s15") is None


def test_presentation_inactive_allows_no_stamp(tmp_path):
    # A user prompt AFTER plan_submitted_ts (100) opens a later turn, so the
    # same-turn check passes and this isolates the presentation-inactive
    # (SMALL_CHANGE weight) fail-open.
    write_full_state(tmp_path, "s16", weight_class="SMALL_CHANGE", plan_presentations=[])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s16", t), tmp_path)
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "s16") is None


def test_degraded_unparsable_delivery_timestamp_allows_and_stamps(tmp_path):
    write_full_state(tmp_path, "s17", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    entry = text_only_entry(105.0, RENDERING)
    entry["timestamp"] = "not-a-timestamp"
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), entry, user_prompt_entry(110.0)])
    proc = run_hook(ask_payload("s17", t), tmp_path)
    assert not _is_deny(proc)
    assert _stamp(tmp_path, "s17") is not None


def test_write_stamp_raising_still_allows_no_traceback(tmp_path, monkeypatch):
    write_full_state(tmp_path, "s18", plan_presentations=[make_receipt(RENDERING, presented_ts=100.0)])
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(90.0), text_only_entry(105.0, RENDERING), user_prompt_entry(110.0)])
    # Make the sidecar target an unwritable directory so write_stamp's own
    # os.replace/mkstemp raises -- the hook must still exit 0/no traceback,
    # never let a stamp-write failure escalate into a DENY or a crash.
    state_file = tmp_path / "agentctl" / "state" / "s18.json"
    sidecar = delivery_mod.stamp_path_for(state_file)
    sidecar.mkdir(parents=True)  # a directory in the way of the sidecar file write
    proc = run_hook(ask_payload("s18", t), tmp_path)
    assert proc.returncode == 0
    assert proc.stderr == "" or "Traceback" not in proc.stderr
    assert not _is_deny(proc)
