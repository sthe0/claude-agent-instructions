"""hook-plan-delivery-gate.py: deny a plan-approval AskUserQuestion issued in the
same turn the plan was submitted. The primary observable is the session
transcript's latest turn-boundary (a real user prompt OR a `queued_command`
attachment entry — the shape a background task-notification uses to open a new
turn without firing UserPromptSubmit); the legacy last_user_prompt_ts/
plan_submitted_ts state-timestamp pair is a fallback for when the transcript is
unavailable. Driven end-to-end via subprocess with CLAUDE_CONFIG_DIR pointed at
a tmp tree so the hook's real state-file resolution lands under tmp_path. The
pure gate_decision function is also unit-tested directly via an importlib load."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentctl.state import PlanPresentation  # noqa: E402

HOOK = Path(__file__).resolve().parent.parent / "hook-plan-delivery-gate.py"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def user_prompt_entry(ts: float) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        "timestamp": iso(ts),
    }


def queued_command_entry(ts: float) -> dict:
    return {
        "type": "attachment",
        "attachment": {"type": "queued_command", "prompt": "<task-notification>timer done</task-notification>"},
        "timestamp": iso(ts),
    }


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def _load_module():
    spec = importlib.util.spec_from_file_location("hook_plan_delivery_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(payload: dict, config_dir: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    # HOME also pinned to tmp_path: resolve_agentctl_state_file's legacy-root
    # fallback hardcodes Path.home() (not CLAUDE_CONFIG_DIR) — without this an
    # unset HOME falls back to the real user's home and the hook would read
    # ~/.claude/agentctl/state for a session-id collision with a real file.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(config_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def write_state(
    config_dir: Path,
    session_id: str,
    node: str,
    plan_submitted_ts: float | None = None,
    last_user_prompt_ts: float | None = None,
) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"node": node}
    if plan_submitted_ts is not None:
        data["plan_submitted_ts"] = plan_submitted_ts
    if last_user_prompt_ts is not None:
        data["last_user_prompt_ts"] = last_user_prompt_ts
    (state_dir / f"{session_id}.json").write_text(json.dumps(data))


def ask_payload(session_id: str, transcript_path: Path | None = None) -> dict:
    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": session_id,
        "tool_input": {"questions": [{"question": "Approve the plan?", "options": []}]},
    }
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    return payload


def _is_deny(proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    out = json.loads(proc.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _deny_reason(proc: subprocess.CompletedProcess) -> str:
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


# --- end-to-end via subprocess -----------------------------------------------

def test_deny_when_plan_submitted_same_turn(tmp_path):
    write_state(tmp_path, "sess1", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess1"), tmp_path)
    assert proc.returncode == 0  # hook never raises / never exits non-zero
    assert _is_deny(proc)
    reason = _deny_reason(proc)
    assert "final" in reason.lower() and "plan" in reason.lower()


def test_silent_inside_a_judge_child(tmp_path):
    """A judge subprocess is not a user turn, so this hook must have no opinion
    about it — and, more to the point, must not reach its own judge from inside
    one. The same fixture that denies above is driven twice, so the silence is
    demonstrated against a world that would otherwise deny rather than against a
    fixture that was never going to say anything."""
    write_state(tmp_path, "sessguard", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    unguarded = run_hook(ask_payload("sessguard"), tmp_path)
    assert unguarded.stdout.strip(), "fixture must produce a decision without the marker"

    marker = _load_module().JUDGE_CHILD_ENV_VAR
    proc = run_hook(ask_payload("sessguard"), tmp_path, env_extra={marker: "1"})

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_deny_when_plan_submitted_after_prompt(tmp_path):
    # plan_submitted_ts > last_user_prompt_ts (later in the same turn) is still same-turn
    write_state(tmp_path, "sess2", "PLAN_READY", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess2"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_allow_when_plan_submitted_earlier_turn(tmp_path):
    # a later prompt (new turn) advanced last_user_prompt_ts past plan_submitted_ts
    write_state(tmp_path, "sess3", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=105.0)
    proc = run_hook(ask_payload("sess3"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)
    assert proc.stdout.strip() == ""


def test_allow_when_node_not_plan_ready(tmp_path):
    write_state(tmp_path, "sess4", "EXECUTING", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess4"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_allow_when_no_live_session(tmp_path):
    # session never ran `agentctl start` (no state file) -> no observable, allow
    proc = run_hook(ask_payload("unknown"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_allow_when_timestamps_missing_legacy_state(tmp_path):
    # legacy state predating schema 10 has neither timestamp -> fail open
    write_state(tmp_path, "sess5", "PLAN_READY")
    proc = run_hook(ask_payload("sess5"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_non_ask_tool_is_ignored(tmp_path):
    write_state(tmp_path, "sess6", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=100.0)
    payload = {"tool_name": "Edit", "session_id": "sess6", "tool_input": {"file_path": "/x.py"}}
    proc = run_hook(payload, tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


# --- transcript-driven turn boundary (primary observable) -------------------

def test_allow_timer_split_via_queued_command_boundary(tmp_path):
    # The scenario this stage exists to fix: a timer-notification turn opens
    # with a `queued_command` boundary that never advanced last_user_prompt_ts,
    # so the legacy pair alone (105 >= 100) would wrongly deny. The transcript
    # sees the queued_command boundary at t=110, strictly after submission.
    write_state(tmp_path, "sess8", "PLAN_READY", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(100.0), queued_command_entry(110.0)])
    proc = run_hook(ask_payload("sess8", t), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_deny_same_turn_via_transcript(tmp_path):
    # Plan submitted (105) after the last transcript boundary (the user prompt
    # at 100) with no later boundary yet -> still the submitting turn.
    write_state(tmp_path, "sess9", "PLAN_READY", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
    t = write_transcript(tmp_path / "t.jsonl", [user_prompt_entry(100.0)])
    proc = run_hook(ask_payload("sess9", t), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_transcript_path_absent_falls_back_to_legacy_allow(tmp_path):
    write_state(tmp_path, "sess10", "PLAN_READY", plan_submitted_ts=100.0, last_user_prompt_ts=105.0)
    proc = run_hook(ask_payload("sess10"), tmp_path)  # no transcript_path in payload
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_unreadable_transcript_falls_back_to_legacy_deny(tmp_path):
    write_state(tmp_path, "sess11", "PLAN_READY", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
    proc = run_hook(ask_payload("sess11", tmp_path / "absent.jsonl"), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_transcript_without_boundary_is_the_accepted_residual_risk(tmp_path):
    # Documented residual risk: if the transcript is readable but the boundary
    # entry itself is gone (e.g. compaction dropped it), latest_turn_start
    # returns None and the gate degrades to the legacy last_user_prompt_ts
    # comparison — on a harness/version where a notification turn never fires
    # UserPromptSubmit, that reproduces the ORIGINAL false-deny. This is the
    # accepted degraded behavior (a degrade, not a fail-open hole), not a bug.
    write_state(tmp_path, "sess12", "PLAN_READY", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
    t = write_transcript(tmp_path / "t.jsonl", [{"type": "assistant", "message": {"role": "assistant", "content": []}}])
    proc = run_hook(ask_payload("sess12", t), tmp_path)
    assert proc.returncode == 0
    assert _is_deny(proc)


def test_corrupt_state_file_allows(tmp_path):
    state_dir = tmp_path / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sess7.json").write_text("{not valid json")
    proc = run_hook(ask_payload("sess7"), tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


def test_malformed_stdin_allows(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="this is not json",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_session_id_allows(tmp_path):
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
    proc = run_hook(payload, tmp_path)
    assert proc.returncode == 0
    assert not _is_deny(proc)


# --- pure gate_decision, unit-tested directly --------------------------------

def test_gate_decision_pure_function():
    mod = _load_module()
    assert mod.gate_decision("PLAN_READY", 100.0, 100.0, is_approval_ask=True)[0] == "deny"
    assert mod.gate_decision("PLAN_READY", 105.0, 100.0, is_approval_ask=True)[0] == "deny"
    assert mod.gate_decision("PLAN_READY", 100.0, 105.0, is_approval_ask=True)[0] == "allow"
    assert mod.gate_decision("EXECUTING", 100.0, 100.0, is_approval_ask=True)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", None, 100.0, is_approval_ask=True)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", 100.0, None, is_approval_ask=True)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", None, None, is_approval_ask=True)[0] == "allow"
    # turn_start_ts (transcript) takes priority over the legacy timestamp pair
    assert mod.gate_decision("PLAN_READY", 105.0, 100.0, turn_start_ts=110.0, is_approval_ask=True)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", 105.0, 100.0, turn_start_ts=100.0, is_approval_ask=True)[0] == "deny"
    assert mod.gate_decision("PLAN_READY", 100.0, 105.0, turn_start_ts=None, is_approval_ask=True)[0] == "allow"
    assert mod.gate_decision("PLAN_READY", 105.0, 100.0, turn_start_ts=None, is_approval_ask=True)[0] == "deny"


def test_gate_decision_normalized_match_ts_none_degrades_to_allow():
    # Same content, reformatted (drifted whitespace/case) so only the tier-2
    # normalized comparison matches; the landing timestamp is unparsable
    # (ts=None) -- must degrade to allow+verified, same as the byte-exact
    # ts=None case, not fall through to _NOT_DELIVERED_REASON.
    mod = _load_module()
    receipt = PlanPresentation(
        plan_path="/plan.toml", kind="essence", plan_sha256="a" * 64,
        rendering_sha256="b" * 64, rendering_text="## Stage 1\n...full plan essence...",
        presented_ts=100.0,
    )
    drifted = "##   STAGE 1\n...FULL plan   essence...\n"
    decision, reason, delivery_verified = mod.gate_decision(
        "PLAN_READY", 100.0, 90.0, turn_start_ts=110.0,
        presentation_active=True,
        receipt=receipt,
        receipt_stale_reason=None,
        delivered_texts=[(drifted, None)],
        has_show_full_plan_option=True,
        is_approval_ask=True,
    )
    assert decision == "allow"
    assert delivery_verified is True


# --- the certificate is computed independently of the classifier -------------
#
# The measured failure this covers: the approval-ask judge timed out, the
# classifier failed open to is_approval_ask=False, the old code returned an
# unverified allow from that branch alone, the hook stamped nothing, and
# `agentctl approve` then refused a delivery that had in fact landed. What the
# transcript shows has nothing to do with which ask is in flight, so the
# observation must ride out on the fail-open allow too.

RENDERING = "## Stage 1\n...full plan essence..."


def _receipt(presented_ts: float = 100.0, rendering_text: str = RENDERING) -> PlanPresentation:
    return PlanPresentation(
        plan_path="/plan.toml", kind="essence", plan_sha256="a" * 64,
        rendering_sha256="b" * 64, rendering_text=rendering_text,
        presented_ts=presented_ts,
    )


def _decide(mod, **kwargs):
    base = dict(
        presentation_active=True,
        receipt=_receipt(),
        receipt_stale_reason=None,
        delivered_texts=[(RENDERING, 110.0)],
        has_show_full_plan_option=True,
    )
    base.update(kwargs)
    # turn_start_ts=110 puts the plan submission (100) in an EARLIER turn, so
    # the same-turn check upstream never fires and each case below exercises
    # the branch it names.
    return mod.gate_decision("PLAN_READY", 100.0, 90.0, turn_start_ts=110.0, **base)


def test_non_approval_ask_still_certifies_an_observed_delivery():
    # (a) The defect itself: a fail-open classifier verdict must not destroy
    # the observation.
    assert _decide(_load_module(), is_approval_ask=False) == ("allow", "", True)


def test_non_approval_ask_without_delivery_certifies_nothing():
    # (b) Fail-open the OTHER way is not permitted either: no observation, no
    # stamp -- allow because the classifier said this is not the gated ask.
    mod = _load_module()
    assert _decide(mod, is_approval_ask=False, delivered_texts=[("something else", 110.0)]) == (
        "allow", "", False,
    )


def test_stale_receipt_is_never_certified_on_either_branch():
    # (c) A stale receipt is an OBSERVED NEGATIVE, not a missing observable:
    # a receipt bound to another plan version must never be certified, however
    # convincingly its bytes appear in the transcript.
    mod = _load_module()
    stale = "receipt is for a different plan version"
    assert _decide(mod, is_approval_ask=False, receipt_stale_reason=stale) == ("allow", "", False)
    decision, reason, verified = _decide(mod, is_approval_ask=True, receipt_stale_reason=stale)
    assert (decision, reason, verified) == ("deny", stale, False)


def test_unreadable_transcript_certifies_nothing_on_either_branch():
    # (d) delivered_texts is None -- a MISSING observable. Fails open on both
    # branches (allow), and stamps on neither.
    mod = _load_module()
    assert _decide(mod, is_approval_ask=False, delivered_texts=None) == ("allow", "", False)
    assert _decide(mod, is_approval_ask=True, delivered_texts=None) == ("allow", "", False)


def test_missing_receipt_certifies_nothing_on_the_fail_open_branch():
    mod = _load_module()
    assert _decide(mod, is_approval_ask=False, receipt=None) == ("allow", "", False)


def test_soft_hyphen_drift_still_verifies_the_delivery():
    # (e) The live case: the delivered text differs from the registered
    # rendering only by one U+00AD the client inserted mid-word (and a trailing
    # newline). Both tiers used to fail, so the hook DENIED a delivery that
    # happened.
    mod = _load_module()
    registered = "## Stage 1\nDecouple the certificate"
    delivered = "## Stage 1\nDecouple the certi" + chr(0x00AD) + "ficate\n"
    assert _decide(
        mod, is_approval_ask=True, receipt=_receipt(rendering_text=registered),
        delivered_texts=[(delivered, 110.0)],
    ) == ("allow", "", True)


def test_a_missing_word_still_denies():
    # (f) The property that makes (e) safe: normalization tolerates
    # reformatting, not omission.
    mod = _load_module()
    registered = "## Stage 1\nDecouple the certificate"
    delivered = "## Stage 1\nDecouple the\n"
    decision, reason, verified = _decide(
        mod, is_approval_ask=True, receipt=_receipt(rendering_text=registered),
        delivered_texts=[(delivered, 110.0)],
    )
    assert decision == "deny"
    assert reason == mod._NOT_DELIVERED_REASON
    assert verified is False


def test_approval_ask_deny_reasons_and_their_order_are_unchanged():
    # (g) The DENY ladder the extension must leave exactly as it was. Each case
    # below supplies MORE than one failing condition where it can, so the
    # assertion pins the ORDER and not merely the reason text.
    mod = _load_module()

    # same-turn beats everything downstream, including a missing receipt
    decision, reason, verified = mod.gate_decision(
        "PLAN_READY", 100.0, 90.0, turn_start_ts=95.0,
        presentation_active=True, receipt=None, receipt_stale_reason=None,
        delivered_texts=None, has_show_full_plan_option=False, is_approval_ask=True,
    )
    assert (decision, reason, verified) == ("deny", mod._SAME_TURN_REASON, False)

    # no receipt beats both the missing marker and the missing delivery
    assert _decide(mod, is_approval_ask=True, receipt=None,
                   has_show_full_plan_option=False, delivered_texts=[]) == (
        "deny", mod._NO_RECEIPT_REASON, False,
    )

    # a stale receipt beats the missing marker
    stale = "receipt is for a different plan version"
    assert _decide(mod, is_approval_ask=True, receipt_stale_reason=stale,
                   has_show_full_plan_option=False) == ("deny", stale, False)

    # the missing marker beats the missing delivery
    assert _decide(mod, is_approval_ask=True, has_show_full_plan_option=False,
                   delivered_texts=[]) == ("deny", mod._NO_MARKER_REASON, False)

    # and with all the above satisfied, an absent delivery is the last DENY
    assert _decide(mod, is_approval_ask=True, delivered_texts=[]) == (
        "deny", mod._NOT_DELIVERED_REASON, False,
    )


def test_a_delivery_predating_the_receipt_is_not_the_delivery():
    # The receipt registers the rendering; only a landing AFTER it can be that
    # rendering's delivery. Unchanged by the extension, pinned here because
    # _delivery_observed now owns the comparison.
    mod = _load_module()
    assert _decide(mod, is_approval_ask=True, delivered_texts=[(RENDERING, 90.0)]) == (
        "deny", mod._NOT_DELIVERED_REASON, False,
    )
    assert _decide(mod, is_approval_ask=False, delivered_texts=[(RENDERING, 90.0)]) == (
        "allow", "", False,
    )


def test_load_gate_fields_missing_node_returns_none(tmp_path):
    mod = _load_module()
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"plan_submitted_ts": 1.0}))
    assert mod.load_gate_fields(p) is None


def test_load_gate_fields_unreadable_returns_none(tmp_path):
    mod = _load_module()
    p = tmp_path / "missing.json"
    assert mod.load_gate_fields(p) is None
