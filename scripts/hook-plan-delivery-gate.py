#!/usr/bin/env python3
"""PreToolUse hook (matcher: AskUserQuestion): deny a plan-approval-node ask
until the plan has actually been SHOWN to the user — not merely submitted, not
merely registered as a presentation receipt, but landed as a completed turn's
final text message. On positive verification it also STAMPS a delivery
receipt (agentctl/delivery.py) that cmd_approve requires before it will record
approval at all.

Two failures this hook now guards against (2026-07-01..02 "Я не вижу плана",
and the finding that motivated this extension):

1. SAME-TURN ask (the original defect): a plan-approval ask fired in the very
   turn the plan was submitted. The primary observable is the session
   transcript's latest turn-boundary (a real user prompt OR a `queued_command`
   attachment entry — the shape a background task-notification uses to open a
   new turn without firing UserPromptSubmit); the legacy last_user_prompt_ts/
   plan_submitted_ts state-timestamp pair is a fallback for when the
   transcript is unavailable. This check is UNCHANGED below.

2. NEVER-SHOWN ask (this extension's reason to exist): the same-turn check
   alone cannot tell "shown in an earlier turn" from "NEVER shown" — both are
   byte-identical to it (turn_start_ts > plan_submitted_ts in both cases), and
   both ALLOW. THE TRAP: the transcript faithfully RECORDS assistant text that
   was never RENDERED — an assistant message can carry [text, tool_use] in one
   entry's content blocks, and that leading text is pre-tool-call text the
   harness may never show. A naive substring search over the transcript would
   therefore ALLOW the very bypass this check exists to kill. So delivery is
   provable only via lib.transcript_turns.delivered_final_texts: TERMINAL
   POSITION, at BLOCK granularity (not entry granularity), in a COMPLETED
   turn, with the landing strictly AFTER the presentation receipt's
   presented_ts (so a re-registration of already-delivered bytes cannot
   substitute for a fresh delivery — see _receipt_stale_reason and the
   post-dating loop in gate_decision).

PERMISSION IS NOT PROOF. The hook ALLOWS on every genuinely missing
observable (no live session, unreadable state, wrong node, absent/unparsable
transcript, inactive session, the AGENTCTL_PLAN_PRESENTATION=0 kill switch) —
a live turn must not wedge on a transient read error, and the user is
waiting. But it may CERTIFY (write a delivery stamp) only what it positively
observed. `gate_decision` therefore returns delivery_verified as a THIRD,
separate value from the allow/deny verdict: a fail-open ALLOW must never
stamp, or the gate becomes theatre — it would certify delivery precisely in
the cases where it could not observe it, and cmd_approve would trust that
certificate. main() stamps iff delivery_verified is True; the pure core never
touches agentctl.delivery itself.

THIS COSTS SOMETHING DOWNSTREAM, and both halves of the trade must be named
together or a later reader "fixes" only the one they see: since Stage 2 made
cmd_approve REQUIRE a stamp, a fail-open ALLOW here (transcript unreadable,
etc.) no longer just permits the ask — it also means no stamp gets written,
so `agentctl approve` will later REFUSE for lack of delivery proof. The
escape is `agentctl confirm-delivery --by <you> --note <reason>`. The posture
is deliberately split: fail OPEN on the live turn (cheap to retry), fail
CLOSED on recording approval (the irreversible act) — and that inversion only
works because the escape stays reachable.

SCOPE: both new checks (receipt presence/freshness, delivery landing, the
show-full-plan option) apply to EVERY AskUserQuestion at node PLAN_READY, not
only a self-identified "approve this plan?" ask. This widening is ACCEPTED,
not an oversight: the hook cannot distinguish "the approval ask" from "any ask
at this node" without a marker the coordinator supplies, and a gate the
coordinator must remember to opt itself into is exactly the forgettable-prose
failure this whole task exists to remove. A second identifying marker was
considered and rejected: the only natural marker IS the show-full-plan
option itself, so an ask carrying it would trivially pass the option check by
construction, and an ask lacking it is by definition not an approval ask —
making a distinct "this is the approval ask" marker either vacuous or visible
to the user as machine noise. The real cost is small and bounded: once a plan
exists, ANY ask at PLAN_READY must now be preceded by a delivered essence and
must offer to show the full plan — which is a coherent norm in its own right,
not an accident. Escape: AGENTCTL_PLAN_PRESENTATION=0.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}

Always exits 0 — a hook crash must never wedge the workflow.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import delivery as _delivery  # noqa: E402
from agentctl import gates as _gates  # noqa: E402
from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE as _KIND_ESSENCE  # noqa: E402
from agentctl.state import PlanPresentation as _PlanPresentation  # noqa: E402
from agentctl.state import SessionState as _SessionState  # noqa: E402
from lib import config_root  # noqa: E402
from lib.transcript_turns import delivered_final_texts, latest_turn_start  # noqa: E402

resolve_state_path = config_root.resolve_agentctl_state_file

# The only node this gate concerns itself with: the plan-approval hard gate.
GATED_NODE = "PLAN_READY"

# Language-independent ASCII marker the coordinator embeds in a "show the full
# plan" option's label (or description). Stage 4/5 renderings are dialogue-
# language prose, so a natural-language keyword match ("покажи план" / "show
# the plan") would break the moment the dialogue is not the language it was
# written against. An ASCII bracketed literal is stable across every dialogue
# language and trivially greppable in coordinator prompts/skills; the
# surrounding option label/description text is free-form.
SHOW_FULL_PLAN_MARKER = "[show-full-plan]"

_SAME_TURN_REASON = (
    "the plan was submitted this same turn — it cannot have rendered to the "
    "user yet (pre-tool-call text may never render); deliver the plan as this "
    "turn's FINAL text message and ask for approval next turn"
)
_NO_RECEIPT_REASON = (
    "no plan presentation is recorded for this plan — the plan must be shown "
    "to the user (present-plan --kind essence) before it can be approved"
)
_NOT_DELIVERED_REASON = (
    "the essence was registered but has not landed as a completed turn's "
    "final message since it was registered — arm the timer FIRST, then emit "
    "the rendering as this turn's FINAL text message, then ask for approval "
    "next turn"
)
_NO_MARKER_REASON = (
    f"this ask has no option carrying the {SHOW_FULL_PLAN_MARKER!r} marker — "
    "an ask at the plan-approval node must always offer to show the full "
    "plan; add an option whose label (or description) embeds the marker"
)


def load_gate_fields(path: Path) -> tuple[str | None, float | None, float | None] | None:
    """Return (node, plan_submitted_ts, last_user_prompt_ts). None on unreadable/
    corrupt state or a missing/non-string node, so main() falls through to allow."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    node = data.get("node")
    if not isinstance(node, str):
        return None
    plan_ts = data.get("plan_submitted_ts")
    plan_ts = plan_ts if isinstance(plan_ts, (int, float)) else None
    prompt_ts = data.get("last_user_prompt_ts")
    prompt_ts = prompt_ts if isinstance(prompt_ts, (int, float)) else None
    return node, plan_ts, prompt_ts


def _load_session_state(path: Path) -> _SessionState | None:
    """Full SessionState reconstruction — needed for plan_presentations,
    plan_path and weight_class, none of which load_gate_fields carries. This
    hook must import agentctl.delivery regardless (it is delivery.py's SOLE
    writer, per Stage 2/3's design); reading state through the same agentctl
    import family, rather than a second hand-rolled JSON reader, is then the
    cheaper consistency — the same choice hook-state-gate.py already made for
    its own heavier (gates.difficulty_blockers) path. Any read/parse/schema
    error -> None, same fail-open posture as load_gate_fields."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _SessionState.from_dict(data)
    except Exception:
        return None


def _receipt_stale_reason(state: _SessionState) -> str | None:
    """Delegate staleness to gates.plan_presentation_blockers rather than
    re-implementing the sha256 comparison — but filtered to RECEIPT-side
    reasons only. Every delivery-side reason gates.py emits contains the
    substring "delivery" ("no delivery proof recorded", "delivery proof is
    stale", "delivery override requires...", "delivery stamp source is...");
    neither receipt-side reason does ("no plan presentation recorded", "plan
    presentation is stale"). This hook is delivery proof's SOLE PRODUCER —
    plan_presentation_blockers will report "no delivery proof recorded" on
    every first call, before this hook has ever verified anything, and that
    must not be mistaken for a receipt problem; the substring filter is what
    keeps the two from colliding. Only called when a receipt is already known
    to exist, so the "no plan presentation recorded" reason can never surface
    here in practice."""
    for reason in _gates.plan_presentation_blockers(state, state.plan_path):
        if "delivery" not in reason:
            return reason
    return None


def _has_show_full_plan_option(tool_input: dict) -> bool:
    """True iff ANY option, across every question in this ask, carries
    SHOW_FULL_PLAN_MARKER in its label or description. Tolerant of missing or
    malformed keys — schema drift contributes nothing rather than raising."""
    if not isinstance(tool_input, dict):
        return False
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return False
    for q in questions:
        if not isinstance(q, dict):
            continue
        options = q.get("options")
        if not isinstance(options, list):
            continue
        for opt in options:
            if not isinstance(opt, dict):
                continue
            for key in ("label", "description"):
                val = opt.get(key)
                if isinstance(val, str) and SHOW_FULL_PLAN_MARKER in val:
                    return True
    return False


def gate_decision(
    node: str,
    plan_submitted_ts: float | None,
    last_user_prompt_ts: float | None,
    turn_start_ts: float | None = None,
    *,
    presentation_active: bool = False,
    receipt: _PlanPresentation | None = None,
    receipt_stale_reason: str | None = None,
    delivered_texts: list[tuple[str, float | None]] | None = None,
    has_show_full_plan_option: bool = False,
) -> tuple[str, str, bool]:
    """Pure decision. Returns ("allow"|"deny", reason, delivery_verified).

    ALLOW != VERIFIED: delivery_verified is True ONLY when this call actually
    observed the rendering land (byte-present in a delivered_final_texts entry
    that either post-dates the receipt's presented_ts, or — degraded — has an
    unparsable landing timestamp; see the loop below). It is False on every
    other path: every fail-open allow (wrong node, no plan yet, presentation
    inactive, unreadable transcript) AND every deny. main() must stamp iff
    delivery_verified, never merely `decision == "allow"` — the existing core
    already allows for node != PLAN_READY and for plan_submitted_ts is None,
    and stamping on either would manufacture proof of a delivery that was
    never observed, for a plan the session may not even have.

    The same-turn check (unchanged from before this extension) runs first and
    can only DENY; the presentation/delivery checks below are ADDITIVE — they
    can add a further DENY but never relax the same-turn one.
    """
    if node != GATED_NODE:
        return "allow", "", False
    if plan_submitted_ts is None:
        return "allow", "", False

    if turn_start_ts is not None:
        if plan_submitted_ts >= turn_start_ts:
            return "deny", _SAME_TURN_REASON, False
    elif last_user_prompt_ts is not None and plan_submitted_ts >= last_user_prompt_ts:
        return "deny", _SAME_TURN_REASON, False
    # Both turn_start_ts and last_user_prompt_ts missing: the same-turn check
    # itself has no observable, but delivered_texts below is an INDEPENDENT
    # observable (it re-reads the transcript on its own traversal), so we do
    # not fail open here — we fall through and let it decide.

    if not presentation_active:
        return "allow", "", False
    if receipt is None:
        return "deny", _NO_RECEIPT_REASON, False
    if receipt_stale_reason is not None:
        return "deny", receipt_stale_reason, False
    if not has_show_full_plan_option:
        return "deny", _NO_MARKER_REASON, False
    if delivered_texts is None:
        # Transcript unreadable/absent: a missing observable, not an observed
        # negative — fail open, and (per the docstring above) do NOT stamp.
        return "allow", "", False

    verified = False
    degraded_match = False
    for text, ts in delivered_texts:
        if receipt.rendering_text not in text:
            continue
        if ts is None:
            # The delivery landed but its own timestamp couldn't be parsed —
            # a missing observable on the DELIVERY side only (presented_ts
            # itself is a required PlanPresentation field and can never be
            # missing). Degrade to the plain byte check rather than wedging.
            degraded_match = True
            continue
        if ts > receipt.presented_ts:
            verified = True
            break
    if verified or degraded_match:
        return "allow", "", True
    return "deny", _NOT_DELIVERED_REASON, False


def deny_with(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _stamp_delivery(state_file: Path, receipt: _PlanPresentation) -> None:
    """Write the hook's positive-verification stamp — the ONLY call site
    allowed to create a source="hook" DeliveryStamp, and only ever reached
    when gate_decision returned delivery_verified=True. Hashes are copied
    FROM the receipt actually verified, never recomputed here: recomputing
    would open a TOCTOU seam (a plan edited between the staleness check and
    this write would stamp a hash never actually checked against delivery).
    cmd_approve re-derives and re-compares at read time regardless, so a plan
    edited after the stamp still invalidates it — the stamp only has to
    record honestly what this hook saw. Failures are swallowed: a stamp that
    cannot be written must degrade to a later refusal-with-escape
    (confirm-delivery), never to a crash on an otherwise-legitimate ALLOW."""
    try:
        _delivery.write_stamp(
            state_file,
            _delivery.DeliveryStamp(
                plan_path=receipt.plan_path,
                plan_sha256=receipt.plan_sha256,
                rendering_sha256=receipt.rendering_sha256,
                verified_ts=time.time(),
                source=_delivery.SOURCE_HOOK,
            ),
        )
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    session_id = payload.get("session_id") or ""
    sp = resolve_state_path(session_id)
    if sp is None:
        return 0
    fields = load_gate_fields(sp)
    if fields is None:
        return 0
    node, plan_ts, prompt_ts = fields

    turn_start_ts = None
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        turn_start_ts = latest_turn_start(Path(transcript_path))

    presentation_active = False
    receipt: _PlanPresentation | None = None
    receipt_stale_reason: str | None = None
    delivered_texts: list[tuple[str, float | None]] | None = None
    has_marker = False

    # Only do the heavier state/transcript work when the same-turn check's
    # own cheap fields say this is even potentially a gated ask — mirrors the
    # pure core's own early-outs so a non-PLAN_READY / no-plan-yet ask never
    # pays for a second state load or a transcript re-scan.
    if node == GATED_NODE and plan_ts is not None:
        state = _load_session_state(sp)
        if state is not None:
            presentation_active = _gates.plan_presentation_active(state)
            if presentation_active:
                receipt = _gates._plan_presentation_for(state, _KIND_ESSENCE)
                if receipt is not None:
                    receipt_stale_reason = _receipt_stale_reason(state)
                    has_marker = _has_show_full_plan_option(payload.get("tool_input") or {})
                    if isinstance(transcript_path, str) and transcript_path:
                        delivered_texts = delivered_final_texts(Path(transcript_path))

    decision, reason, delivery_verified = gate_decision(
        node, plan_ts, prompt_ts, turn_start_ts,
        presentation_active=presentation_active,
        receipt=receipt,
        receipt_stale_reason=receipt_stale_reason,
        delivered_texts=delivered_texts,
        has_show_full_plan_option=has_marker,
    )
    if decision == "deny":
        deny_with(reason)
    elif delivery_verified:
        assert receipt is not None  # delivery_verified is only ever True via a real receipt
        _stamp_delivery(sp, receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
