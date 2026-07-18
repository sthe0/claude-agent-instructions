#!/usr/bin/env python3
"""Standalone runtime check for hook-plan-delivery-gate.py, driven against a
GIVEN hook file path (argv[1]) rather than an importlib-loaded module — this is
the check `agentctl verify-final` runs against the DEPLOYED shared-tree copy of
the hook, the exact file the live PreToolUse registration executes, to confirm
the fix reached the runtime path and not just the worktree's own copy.

Builds a fixture agentctl state file + session transcript in a tempdir, execs
the given hook via subprocess with the real PreToolUse stdin payload shape, and
asserts, against the DEPLOYED hook:

  Same-turn / timer-split (the original turn-boundary check, minimal state):
  - timer-split sequence (user prompt, plan submitted, then a `queued_command`
    turn boundary) produces NO deny;
  - same-turn sequence (plan submitted after the last boundary) produces the
    deny JSON.

  Delivery verification + stamp (the Stage-3 extension, full substantive state
  with a registered essence receipt):
  - a registered essence rendering that LANDED as a completed turn's final text,
    with the ask carrying the show-full-plan marker, is ALLOWED and a
    source="hook" delivery stamp is written;
  - a registered essence that was NEVER delivered is DENIED and nothing is
    stamped (the live bypass this extension kills);

  Degraded / missing-observable (fail-open, and — crucially — NO stamp):
  - a full substantive session whose transcript is unreadable/absent fails OPEN
    (ALLOW) yet writes NO stamp — ALLOW != VERIFIED at the runtime path too.

Exits 0 only if every case holds; prints a one-line diagnosis and exits 1
otherwise.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentctl import delivery as _delivery  # noqa: E402
from agentctl.state import PlanPresentation, SessionState  # noqa: E402

# A stable rendering body the delivered-case transcript echoes verbatim; the
# hook's byte check is exact-substring, so this must round-trip unaltered.
RENDER = "## Plan essence\n(runtime-check rendering body)"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def write_state(config_dir: Path, session_id: str, plan_submitted_ts: float, last_user_prompt_ts: float) -> None:
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "node": "PLAN_READY",
        "plan_submitted_ts": plan_submitted_ts,
        "last_user_prompt_ts": last_user_prompt_ts,
    }
    (state_dir / f"{session_id}.json").write_text(json.dumps(data))


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def make_receipt(rendering_text: str, presented_ts: float) -> PlanPresentation:
    return PlanPresentation(
        plan_path="/plan.toml", kind="essence", plan_sha256="a" * 64,
        rendering_sha256="b" * 64, rendering_text=rendering_text, presented_ts=presented_ts,
    )


def write_full_state(
    config_dir: Path, session_id: str, *,
    receipt: PlanPresentation | None,
    plan_submitted_ts: float = 100.0,
    last_user_prompt_ts: float = 110.0,
) -> Path:
    """A schema-correct substantive PLAN_READY state (round-tripped through the
    real SessionState) so the deployed hook exercises its presentation/delivery
    path, not merely the minimal-state turn-boundary path write_state builds."""
    state_dir = config_dir / "agentctl" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState(
        session_id=session_id, task_id="t", node="PLAN_READY",
        plan_submitted_ts=plan_submitted_ts, last_user_prompt_ts=last_user_prompt_ts,
        weight_class="SUBSTANTIVE", plan_path="/plan.toml",
        plan_presentations=[receipt] if receipt is not None else [],
    )
    state_file = state_dir / f"{session_id}.json"
    state_file.write_text(json.dumps(state.to_dict()))
    return state_file


# The language-independent marker the coordinator embeds in a show-full-plan
# option; the deployed hook denies a PLAN_READY ask that lacks it.
MARKER_OPTION = {"label": "Show me the full plan [show-full-plan]"}


def run_hook(hook_path: Path, config_dir: Path, session_id: str, transcript_path: Path,
             options: list[dict] | None = None) -> dict | None:
    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "tool_input": {"questions": [{"question": "Approve the plan?", "options": options or []}]},
    }
    env = {"PATH": "/usr/bin:/bin", "HOME": str(config_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        print(f"FAIL: hook exited {proc.returncode}, stderr={proc.stderr!r}")
        sys.exit(1)
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        print(f"FAIL: non-JSON hook stdout: {proc.stdout!r}")
        sys.exit(1)


def is_deny(result: dict | None) -> bool:
    return bool(result) and result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: runtime_check_plan_delivery_gate.py <path-to-hook-plan-delivery-gate.py>")
        return 1
    hook_path = Path(sys.argv[1]).resolve()
    if not hook_path.is_file():
        print(f"FAIL: hook file not found: {hook_path}")
        return 1

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp)

        # Timer-split case: user prompt @100, plan submitted @105, the
        # timer-notification queued_command boundary opens the next turn @110.
        write_state(config_dir, "runtime-timer-split", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
        transcript = write_transcript(
            config_dir / "timer-split.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "покажи план"}]}, "timestamp": iso(100.0)},
                {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "<task-notification>timer done</task-notification>"}, "timestamp": iso(110.0)},
            ],
        )
        result = run_hook(hook_path, config_dir, "runtime-timer-split", transcript)
        if is_deny(result):
            print(f"FAIL: timer-split case was denied: {result}")
            ok = False
        else:
            print("PASS: timer-split PLAN_READY ask allowed")

        # Same-turn case: plan submitted @105 with no later turn boundary yet
        # (the last boundary is the user prompt @100) -> still the submitting turn.
        write_state(config_dir, "runtime-same-turn", plan_submitted_ts=105.0, last_user_prompt_ts=100.0)
        transcript = write_transcript(
            config_dir / "same-turn.jsonl",
            [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "покажи план"}]}, "timestamp": iso(100.0)}],
        )
        result = run_hook(hook_path, config_dir, "runtime-same-turn", transcript)
        if not is_deny(result):
            print(f"FAIL: same-turn case was NOT denied: {result}")
            ok = False
        else:
            print("PASS: same-turn PLAN_READY ask denied")

        # --- Stage-3 extension: delivery verification + stamp ---------------

        # Delivered case: a registered essence that LANDED as a completed turn's
        # final text (@105, after the receipt's presented_ts @100), ask carrying
        # the show-full-plan marker -> ALLOW, and a source="hook" stamp written.
        sf = write_full_state(config_dir, "runtime-delivered", receipt=make_receipt(RENDER, 100.0))
        transcript = write_transcript(
            config_dir / "delivered.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}, "timestamp": iso(90.0)},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": RENDER}]}, "timestamp": iso(105.0)},
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "?"}]}, "timestamp": iso(110.0)},
            ],
        )
        result = run_hook(hook_path, config_dir, "runtime-delivered", transcript, options=[MARKER_OPTION, {"label": "No"}])
        stamp = _delivery.read_stamp(sf)
        if is_deny(result):
            print(f"FAIL: delivered case was denied: {result}")
            ok = False
        elif stamp is None or getattr(stamp, "source", None) != _delivery.SOURCE_HOOK:
            print(f"FAIL: delivered case wrote no source=hook stamp: {stamp}")
            ok = False
        else:
            print("PASS: delivered PLAN_READY ask allowed and stamped")

        # Never-delivered case: registered essence, marker present, but the
        # rendering NEVER landed (no assistant text) -> DENY, nothing stamped.
        sf = write_full_state(config_dir, "runtime-never", receipt=make_receipt(RENDER, 100.0))
        transcript = write_transcript(
            config_dir / "never.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}, "timestamp": iso(90.0)},
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "?"}]}, "timestamp": iso(110.0)},
            ],
        )
        result = run_hook(hook_path, config_dir, "runtime-never", transcript, options=[MARKER_OPTION, {"label": "No"}])
        if not is_deny(result):
            print(f"FAIL: never-delivered case was NOT denied: {result}")
            ok = False
        elif _delivery.read_stamp(sf) is not None:
            print("FAIL: never-delivered case wrote a stamp")
            ok = False
        else:
            print("PASS: never-delivered PLAN_READY ask denied, unstamped")

        # Degraded / missing-observable: a full substantive session whose
        # transcript is absent fails OPEN (ALLOW) yet stamps NOTHING — the
        # ALLOW != VERIFIED invariant at the runtime path. plan submitted an
        # earlier turn (last_user_prompt_ts @110 > plan_submitted_ts @100) so
        # the same-turn fallback passes and this isolates the delivery-side
        # fail-open rather than colliding with the turn-boundary deny.
        sf = write_full_state(config_dir, "runtime-degraded", receipt=make_receipt(RENDER, 100.0))
        absent = config_dir / "does-not-exist.jsonl"
        result = run_hook(hook_path, config_dir, "runtime-degraded", absent, options=[MARKER_OPTION, {"label": "No"}])
        if is_deny(result):
            print(f"FAIL: degraded missing-transcript case was denied: {result}")
            ok = False
        elif _delivery.read_stamp(sf) is not None:
            print("FAIL: degraded missing-transcript case wrote a stamp (fail-open must not certify)")
            ok = False
        else:
            print("PASS: degraded missing-transcript ask allowed, unstamped")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
