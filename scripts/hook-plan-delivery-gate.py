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

NORMALIZED MATCH: the delivered-text comparison in _delivery_observed is a
two-tier check — exact substring first, then (only if that fails) a
normalized substring via text_shape.normalize_for_match: casefold, collapse
whitespace, and DROP every Unicode format character (category Cf). The
whitespace tiers cover incidental newline/casefold drift; the Cf tier covers
what a live session hit — a registered rendering of 7629 chars against a
delivered 7627, differing by one U+00AD SOFT HYPHEN the client had inserted
mid-word plus a trailing newline, which failed BOTH tiers and denied a
delivery that had happened. Nobody who authors the text can see such a
character, so no amount of care avoids it. Genuinely missing CONTENT (a
dropped word/line) still fails both tiers and still denies — normalization
tolerates reformatting, not omission.

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
escape is `agentctl confirm-delivery --by <you> --note <why> --escape-reason
<one of delivery.DELIVERY_ESCAPE_REASONS>` — typed so escapes are countable
rather than an archive of free-text notes nobody aggregates. The posture
is deliberately split: fail OPEN on the live turn (cheap to retry), fail
CLOSED on recording approval (the irreversible act) — and that inversion only
works because the escape stays reachable.

SCOPE: the classifier (agentctl.advisor.judge_approval_ask, invoked from
decide()) governs the DENY verdict and nothing else. It decides whether the
receipt/freshness/delivery/marker checks below may deny THIS ask — they apply
only to an ask it identifies as the plan-approval ask, not to every
AskUserQuestion at node PLAN_READY. A second coordinator-supplied "this is the
approval ask" marker was considered and rejected, for the same reason
SHOW_FULL_PLAN_MARKER alone cannot serve this role: the coordinator supplies
it, so it proves only that the coordinator SAID this is the approval ask,
never that it IS one. The classifier is a fail-open model judgment over the
ask's own text instead: on any absent/slow/malformed call it resolves to "not
the approval ask", so an unavailable classifier can only widen what is
ALLOWED, never deny a question the user needed answered — the irreversible act
it protects (recording approval) stays fail-CLOSED one layer down, since
cmd_approve refuses without a delivery stamp regardless of what this hook
allowed through. Escape: AGENTCTL_PLAN_PRESENTATION=0.

THE CERTIFICATE IS NOT THE CLASSIFIER'S TO WITHHOLD. Whether the registered
rendering landed in the transcript is a fact about the transcript; it has
nothing to do with which ask is in flight, and _delivery_observed reads it
without asking the classifier anything. It therefore runs ABOVE the
is_approval_ask short-circuit in gate_decision, and its result rides out on
the fail-open allow too. Coupling the two cost a live session an approval:
the judge timed out, the classifier failed open to "not the approval ask",
the old code returned from that branch before the delivery loop ran, no stamp
was written, and cmd_approve then refused a delivery that had in fact landed
— a fail-open classifier that can only WIDEN the verdict was, through the
certificate, denying downstream.

DENY is signaled with the PreToolUse permissionDecision JSON on stdout:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "permissionDecision": "deny", "permissionDecisionReason": "..."}}

Always exits 0 — a hook crash must never wedge the workflow.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import judge_ledger  # noqa: E402

# judge_ledger itself must import cleanly above for this to record anything —
# it is stdlib-only (see its own module docstring) and is what every other
# import failure here needs a working ledger to be recorded against.
try:
    from agentctl import advisor as _advisor  # noqa: E402
    from agentctl import delivery as _delivery  # noqa: E402
    from agentctl import gates as _gates  # noqa: E402
    from agentctl.state import PLAN_PRESENTATION_KIND_ESSENCE as _KIND_ESSENCE  # noqa: E402
    from agentctl.state import PLAN_PRESENTATION_KIND_REPLAN_DIFF as _KIND_REPLAN_DIFF  # noqa: E402
    from agentctl.state import PlanPresentation as _PlanPresentation  # noqa: E402
    from agentctl.state import SessionState as _SessionState  # noqa: E402
    from agentctl.state import SHOW_FULL_PLAN_MARKER  # noqa: E402
    from agentctl.state import AUTHORIZE_REPLAN_MARKER  # noqa: E402
    # Imported directly rather than through a gates.py re-export (the route
    # _normalize_string takes): gates.py has no use of its own for this
    # normalizer, and a re-export exists to spare a module a second import,
    # not to become the address of everything text_shape holds.
    from agentctl import text_shape as _text_shape  # noqa: E402
    from lib import config_root  # noqa: E402
    from lib import judge_budget  # noqa: E402
    from lib.host_llm import JUDGE_CHILD_ENV_VAR  # noqa: E402
    from lib.ask_text import flat_text  # noqa: E402
    from lib.transcript_turns import delivered_final_texts, latest_turn_start  # noqa: E402
except BaseException as exc:
    judge_ledger.import_failed("plan_delivery", f"{type(exc).__name__}: {exc}")
    raise

resolve_state_path = config_root.resolve_agentctl_state_file

# The only node this gate concerns itself with: the plan-approval hard gate.
GATED_NODE = "PLAN_READY"

# Safe-by-default kill-switch: unset or any value other than "0" leaves the
# classifier enabled, matching every other semantic judge's env convention.
_APPROVAL_ASK_KILLSWITCH_ENV = "CLAUDE_APPROVAL_ASK_SEMANTIC"

# This hook's own whole-invocation judge budget — owned here rather than read
# off advisor._APPROVAL_ASK_TIMEOUT_S, mirroring how every sibling hook
# (_JUDGE_BUDGET_S, _ASK_JUDGE_BUDGET_S, _TURN_JUDGE_BUDGET_S) owns its budget
# independently of the judge function's own internal default.
#
# lib/judge_latency.py's ceiling rule, `ceil(max) + 1` over the merged n=64
# population (max 19.14s), is 21s — but this constant is no longer pinned to
# that ceiling by equality (it was, at 13s, and the population moved past it:
# a fresh n=32 sample taken after production started timing out ran
# 14.12-19.14s, entirely above the first sample's 11.42s max). 30s instead:
# this hook now joins the `>=` shape every OTHER single-call hook in this repo
# already uses (e.g. outage_escalation's ceiling of 27 under a budget of 30),
# which is what gives them slack over a ceiling computed from a population that
# has already been observed to move by ~70% within one day. The `>=` rule
# itself is enforced by
# test_a_single_call_hooks_budget_is_never_what_truncates_its_call in
# scripts/tests/test_judge_latency.py.
_APPROVAL_ASK_JUDGE_BUDGET_S = 30

# Below this remaining budget a judge call cannot plausibly finish and would
# only spend the wait on a guaranteed timeout; stop judging and fail open
# instead, exactly like every other unreachable-judge path in this hook. This
# is lib/judge_latency.py's floor rule, `ceil(p90)` over the merged n=64
# population — comfortably above the fastest run observed (5.88s), which is
# what makes a call started with exactly the floor left reachable rather than
# doomed.
_APPROVAL_ASK_JUDGE_MIN_CALL_S = 18

# SHOW_FULL_PLAN_MARKER is defined in agentctl.state (imported above) — the
# coordinator embeds it in a "show the full plan" option's label (or
# description). Stage 4/5 renderings are dialogue-language prose, so a
# natural-language keyword match ("покажи план" / "show the plan") would break
# the moment the dialogue is not the language it was written against. An ASCII
# bracketed literal is stable across every dialogue language and trivially
# greppable in coordinator prompts/skills; the surrounding option
# label/description text is free-form.

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


def _has_marker_option(tool_input: dict, marker: str) -> bool:
    """True iff ANY option, across every question in this ask, carries
    `marker` in its label or description. Tolerant of missing or malformed
    keys — schema drift contributes nothing rather than raising."""
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
                if isinstance(val, str) and marker in val:
                    return True
    return False


def _has_show_full_plan_option(tool_input: dict) -> bool:
    return _has_marker_option(tool_input, SHOW_FULL_PLAN_MARKER)


def _same_turn_denied(
    plan_submitted_ts: float | None,
    last_user_prompt_ts: float | None,
    turn_start_ts: float | None,
) -> bool:
    """The same-turn predicate, shared by gate_decision (which must still deny
    on it) and decide() (which uses it to skip a judge call whose verdict
    gate_decision would discard anyway — see decide()'s own comment on this).
    plan_submitted_ts is None is deliberately NOT handled here: both callers
    already have their own no-plan-yet early-out ahead of this check."""
    if turn_start_ts is not None:
        return plan_submitted_ts >= turn_start_ts
    if last_user_prompt_ts is not None:
        return plan_submitted_ts >= last_user_prompt_ts
    return False


def _delivery_observed(
    receipt: _PlanPresentation | None,
    receipt_stale_reason: str | None,
    delivered_texts: list[tuple[str, float | None]] | None,
) -> bool:
    """Did the registered rendering LAND in a delivered final text, after the
    receipt registered it? The positive observation, and nothing else.

    Every input here is gathered independently of which ask is in flight, which
    is why this is a module-level function called ABOVE gate_decision's
    is_approval_ask short-circuit rather than a loop inside the approval-ask
    branch (see the module docstring's SCOPE paragraph).

    False on each of the three ways the observation is unavailable or negative,
    and they are not the same kind of thing: receipt is None and
    delivered_texts is None are MISSING observables, while a
    receipt_stale_reason is an observed negative — a receipt bound to a plan
    version other than the current one must never be certified, however
    convincingly its bytes appear in the transcript. All three return False
    because "not observed" and "observed absent" are both grounds not to
    certify; only the DENY verdict needs to tell them apart, and gate_decision
    keeps doing that.
    """
    if receipt is None or receipt_stale_reason is not None or delivered_texts is None:
        return False
    degraded_match = False
    for text, ts in delivered_texts:
        matched = receipt.rendering_text in text
        if not matched:
            # Tier 2: incidental whitespace/newline/casefold drift, and
            # invisible Cf characters inserted or dropped by the rendering
            # pipeline, must not trigger _NOT_DELIVERED_REASON —
            # normalize_for_match removes only things a reader cannot read, so
            # genuinely missing CONTENT (a dropped word/line) still fails this
            # tier too.
            matched = (
                _text_shape.normalize_for_match(receipt.rendering_text)
                in _text_shape.normalize_for_match(text)
            )
        if not matched:
            continue
        if ts is None:
            # The delivery landed but its own timestamp couldn't be parsed —
            # a missing observable on the DELIVERY side only (presented_ts
            # itself is a required PlanPresentation field and can never be
            # missing). Degrade to the match tier already established above
            # rather than wedging.
            degraded_match = True
            continue
        if ts > receipt.presented_ts:
            return True
    return degraded_match


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
    is_approval_ask: bool,
) -> tuple[str, str, bool]:
    """Pure decision. Returns ("allow"|"deny", reason, delivery_verified).

    is_approval_ask is required (no default): main() is the only production
    caller, and it always computes it via the classifier before calling in.
    False short-circuits to an allow — the classifier fails open toward "not
    the approval ask", so an absent/slow/malformed call can only widen what
    passes through, never deny a question the user needed answered. It does
    NOT short-circuit the certificate: _delivery_observed has already run by
    then, and its result rides out on that allow, because what the transcript
    shows has nothing to do with which ask is in flight.

    ALLOW != VERIFIED: delivery_verified is True ONLY when this call actually
    observed the rendering land (present — exact, or normalized for
    whitespace/newline/casefold drift and invisible Cf characters — in a
    delivered_final_texts entry that either post-dates the receipt's
    presented_ts, or — degraded — has an unparsable landing timestamp; see
    _delivery_observed). It is False on every deny, and on every fail-open
    allow that had no observation to carry (wrong node, no plan yet,
    presentation inactive, no receipt, stale receipt, unreadable transcript).
    main() must stamp iff delivery_verified, never merely
    `decision == "allow"` — the core allows for node != PLAN_READY and for
    plan_submitted_ts is None, and stamping on either would manufacture proof
    of a delivery that was never observed, for a plan the session may not even
    have.

    The same-turn check (unchanged from before this extension) runs first and
    can only DENY; the presentation/delivery checks below are ADDITIVE — they
    can add a further DENY but never relax the same-turn one.
    """
    if node != GATED_NODE:
        return "allow", "", False
    if plan_submitted_ts is None:
        return "allow", "", False

    if _same_turn_denied(plan_submitted_ts, last_user_prompt_ts, turn_start_ts):
        return "deny", _SAME_TURN_REASON, False
    # Both turn_start_ts and last_user_prompt_ts missing: the same-turn check
    # itself has no observable, but delivered_texts below is an INDEPENDENT
    # observable (it re-reads the transcript on its own traversal), so we do
    # not fail open here — we fall through and let it decide.

    if not presentation_active:
        return "allow", "", False

    observed = _delivery_observed(receipt, receipt_stale_reason, delivered_texts)

    if not is_approval_ask:
        return "allow", "", observed
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

    if observed:
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


def decide(payload: dict) -> tuple[str, str, Path | None, _PlanPresentation | None]:
    """Returns (decision, reason, state_file_path, receipt-to-stamp).

    Every early exit before gate_decision returns a silent ("allow", "") and
    never calls entered(), so a payload that resolved no session is
    indistinguishable, on the ledger, from one at PLAN_READY with no plan yet.
    The receipt is present iff gate_decision verified delivery.

    Opened as the very first statement, before latest_turn_start below ever
    touches the transcript — mirroring hook-turn-end-gate.py's build_context,
    not hook-deferring-disposition-gate.py's decide (which opens its budget
    after payload parsing, because extracting ask_text from an already-parsed
    tool_input is no file I/O). Here, both latest_turn_start (before the judge
    call) and delivered_final_texts (after it) are unbounded transcript reads,
    so the deadline must be live for the very first one or it quietly spends
    part of its own headroom before the budget ever knows the clock started."""
    budget = judge_budget.JudgeBudget(
        _APPROVAL_ASK_JUDGE_BUDGET_S, _APPROVAL_ASK_JUDGE_MIN_CALL_S, clock=time.monotonic
    )
    if payload.get("tool_name") != "AskUserQuestion":
        return "allow", "", None, None
    session_id = payload.get("session_id") or ""
    sp = resolve_state_path(session_id)
    if sp is None:
        return "allow", "", None, None
    fields = load_gate_fields(sp)
    if fields is None:
        return "allow", "", sp, None
    node, plan_ts, prompt_ts = fields

    # Memoized so the essence block (unconditional on this ask carrying any
    # marker) and the replan-authorization block below (which only ever runs
    # past its own marker check) can each call this without a session that
    # trips both guards paying for two state loads.
    _state_loaded = False
    _state_value: _SessionState | None = None

    def _state() -> _SessionState | None:
        nonlocal _state_loaded, _state_value
        if not _state_loaded:
            _state_value = _load_session_state(sp)
            _state_loaded = True
        return _state_value

    turn_start_ts = None
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        turn_start_ts = latest_turn_start(Path(transcript_path))

    presentation_active = False
    receipt: _PlanPresentation | None = None
    receipt_stale_reason: str | None = None
    delivered_texts: list[tuple[str, float | None]] | None = None
    has_marker = False
    is_approval_ask = False
    replan_receipt_to_stamp: _PlanPresentation | None = None

    # Only do the heavier state/transcript work when the same-turn check's
    # own cheap fields say this is even potentially a gated ask — mirrors two
    # of the pure core's three early-outs (node != GATED_NODE, no plan yet),
    # so such an ask never pays for a second state load or a transcript
    # re-scan. The third early-out (same-turn) is honoured separately below,
    # via same_turn_denied, and skips only the judge call: gate_decision's
    # same-turn deny fires before it ever looks at presentation_active or
    # receipt, so a same-turn ask's judge call would otherwise buy nothing but
    # a verdict gate_decision discards.
    if node == GATED_NODE and plan_ts is not None:
        same_turn_denied = _same_turn_denied(plan_ts, prompt_ts, turn_start_ts)
        state = _state()
        if state is not None:
            presentation_active = _gates.plan_presentation_active(state)
            if presentation_active:
                if not same_turn_denied:
                    ask_text = flat_text(payload.get("tool_input") or {})
                    # Skipping the call on a False prefilter is safe because
                    # judge_approval_ask runs this same prefilter internally
                    # as its own second gate: the skip reproduces the same
                    # False verdict that call would have returned anyway
                    # (though not, when the kill-switch is set, the same
                    # ledger record).
                    prefilter_fired = _advisor.approval_ask_prefilter(ask_text)
                    judge_ledger.entered("approval_ask", prefilter_fired=prefilter_fired)
                    if prefilter_fired:
                        remaining_before_call, call_timeout = budget.remaining_and_timeout(
                            _APPROVAL_ASK_JUDGE_BUDGET_S
                        )
                        if call_timeout is None:
                            judge_ledger.decided(
                                "approval_ask", stage="budget", verdict=False,
                                reason="budget exhausted before call (fail-open)",
                                remaining=remaining_before_call, threshold=None,
                                ceiling=_APPROVAL_ASK_JUDGE_BUDGET_S,
                            )
                        else:
                            is_approval_ask, _reason = _advisor.judge_approval_ask(
                                ask_text,
                                _advisor.subprocess_runner,
                                enabled=os.environ.get(_APPROVAL_ASK_KILLSWITCH_ENV) != "0",
                                timeout=call_timeout,
                                remaining=remaining_before_call,
                                ceiling=_APPROVAL_ASK_JUDGE_BUDGET_S,
                            )
                receipt = _gates._plan_presentation_for(state, _KIND_ESSENCE)
                if receipt is not None:
                    receipt_stale_reason = _receipt_stale_reason(state)
                    has_marker = _has_show_full_plan_option(payload.get("tool_input") or {})
                    if isinstance(transcript_path, str) and transcript_path:
                        delivered_texts = delivered_final_texts(Path(transcript_path))

    # Replan-authorization certificate — a SIBLING to the essence block above,
    # not nested inside it: a replan can be proposed from any session node
    # (EXECUTING, DIAGNOSING, VERIFYING, ...), not only PLAN_READY. The pure,
    # I/O-free marker check is evaluated FIRST and short-circuits every
    # ordinary ask (every ask without this marker, which is nearly all of
    # them) before any state load or transcript scan — see the module
    # docstring's cost-guard note and test_replan_authorization.py's
    # cost-invariant case for what this buys.
    if _has_marker_option(payload.get("tool_input") or {}, AUTHORIZE_REPLAN_MARKER):
        replan_state = _state()
        if replan_state is not None:
            replan_receipt = _gates._plan_presentation_for(replan_state, _KIND_REPLAN_DIFF)
            if replan_receipt is not None:
                replan_stale = _gates._receipt_binding_blocker(
                    replan_receipt, replan_receipt.plan_path, "replan-diff presentation"
                )
                if replan_stale is None:
                    if delivered_texts is None and isinstance(transcript_path, str) and transcript_path:
                        delivered_texts = delivered_final_texts(Path(transcript_path))
                    if _delivery_observed(replan_receipt, None, delivered_texts):
                        replan_receipt_to_stamp = replan_receipt

    decision, reason, delivery_verified = gate_decision(
        node, plan_ts, prompt_ts, turn_start_ts,
        presentation_active=presentation_active,
        receipt=receipt,
        receipt_stale_reason=receipt_stale_reason,
        delivered_texts=delivered_texts,
        has_show_full_plan_option=has_marker,
        is_approval_ask=is_approval_ask,
    )
    essence_receipt_to_stamp = receipt if delivery_verified else None
    return decision, reason, sp, (essence_receipt_to_stamp or replan_receipt_to_stamp)


def main() -> int:
    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0  # a sandboxed judge subprocess, not a real user turn — allow, no opinion
    judge_ledger.hook_start("plan_delivery")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    judge_ledger.source_from_payload(payload)

    try:
        decision, reason, sp, verified_receipt = decide(payload)
        # has_directive means "printed a directive", as in the sibling hooks.
        # The allow-path delivery stamp has no counterpart in it: an allow
        # that stamps and one that does not both report False, since neither
        # printed.
        has_directive = decision == "deny"
        judge_ledger.final(has_directive=has_directive)
        emit_ok = True
        try:
            if has_directive:
                deny_with(reason)
            elif verified_receipt is not None:
                assert sp is not None  # verified_receipt is only ever set alongside sp
                _stamp_delivery(sp, verified_receipt)
        except Exception:
            emit_ok = False
        judge_ledger.emitted(ok=emit_ok, had_directive=has_directive)
    except Exception as exc:
        judge_ledger.discarded(reason=repr(exc))
        return 0  # fail-open — a hook must never wedge the ask
    return 0


if __name__ == "__main__":
    sys.exit(main())
