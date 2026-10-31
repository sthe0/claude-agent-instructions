"""Replan, effort-fire acknowledgement and coverage subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

from . import effort, gates
from .config import Thresholds
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import Runner
from .plan import change_tier, load_plan
from .state import AGENT_ACTOR, EFFORT_FIRE_FINISHED_WORK, NORMALIZATION_LEVELS
from .store import StateStore
from .cli_common import (
    _load_prior_plan,
    _replan_baseline_path,
    _effort_cross_totals,
    _clear_open_fires_if_customer,
    _difficulty_id,
    _record_backstop_crossing,
    _crossing_data,
    _agent_fire_ack_refusal,
    _effort_fire_escalation_data,
    _require,
    _clear_since_user_decision,
    _effort_fire_window,
    _park_node,
    _cost_rows,
    _refresh_spend,
    _utcnow,
    _effort_now,
    _report_acceptance_staleness,
    _replan_spelling_advisories,
    _cmd_replan,
)


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    crossed: list[dict] = []
    reopened: list[int] = []
    reopen_ignored: list[int] = []
    d = _cmd_replan(args, store=store, runner=runner, crossed=crossed, reopened=reopened,
                    reopen_ignored=reopen_ignored)
    d.data.update(_crossing_data(crossed[0] if crossed else None))
    if reopened and d.ok:
        d.data["reopened_stages"] = reopened
        d.detail += f"; re-armed PASSED stage(s) {reopened} (--reopen)"
    if reopen_ignored and d.ok:
        d.data["reopen_ignored"] = reopen_ignored
        d.detail += (f"; --reopen {reopen_ignored} ignored: not PASSED, so there is no "
                     "verdict to re-arm")
    # Only on something to say: an unconditional setdefault would add an empty
    # `advisories` key to every replan Directive, which test_history_capture's
    # exact-equality pin reads as a changed contract.
    advisories = _replan_spelling_advisories(args.plan)
    if advisories:
        d.data.setdefault("advisories", []).extend(advisories)
    _report_acceptance_staleness(d, args, store)
    return d


def cmd_fire_acknowledge(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The synchronous decision `gates.effort_fire_blockers` refuses dispatch/replan/
    submit_plan without: appends an "ack" onto the LAST entry of state.effort_fires
    (never deletes — the append-only audit trail is preserved) recording who decided
    and what. Three decisions:

    - "continue": accept the overrun and keep executing the current plan as-is. No
      node change — whatever node the fire's own diagnose transition already put the
      session in (always DIAGNOSING, since every record_fire call site forces that
      transition in the same call) is left untouched; the coordinator proceeds
      through the ordinary declare/investigate/critique/normalize/replan cycle next.
    - "abandon": the order no longer warrants continuing. Parks the session at
      BLOCKED — the same bypass-transition() idiom cmd_block itself uses — rather
      than RESOLVED: a mid-execution fire routinely fires with stages still
      PENDING, and check_invariants refuses RESOLVED unless every stage is
      PASSED, so RESOLVED would be a lie for exactly the sessions this decision
      exists to stop. cmd_unblock remains the (audited) way back in, same as an
      ordinary block.
    - "revise": the plan itself needs to change in response to the overrun. No node
      change needed for the same reason as "continue" (already DIAGNOSING); the
      difference is purely in the human decision recorded, informing what the
      coordinator does next.
    """
    state = _require(store, args.session)
    if not state.effort_fires:
        return Directive(False, state.node, "noop", "no effort-divergence fire recorded")
    last = state.effort_fires[-1]
    if last.get("ack") is not None:
        return Directive(True, state.node, "noop", "fire already acknowledged", data={"fire": last})
    if last.get("disposition") == EFFORT_FIRE_FINISHED_WORK:
        return Directive(True, state.node, "noop",
                         "the last fire is a finished-work retier record: it opened no difficulty "
                         "and owes no acknowledgement", data={"fire": last})
    decision = args.decision
    if decision not in ("continue", "abandon", "revise"):
        return Directive(False, state.node, "noop", f"invalid --decision {decision!r}: "
                         "must be one of continue, abandon, revise")
    if not args.by or not args.by.strip():
        return Directive(False, state.node, "noop", "empty --by: must name who decided")
    by_agent = args.by.strip().casefold() == AGENT_ACTOR
    crossing = None
    if by_agent:
        refusal, crossing = _agent_fire_ack_refusal(state, last, decision)
        if refusal is not None:
            return Directive(
                False, state.node, "fire_acknowledge",
                f"fire-acknowledge by the agent refused: {refusal}",
                marker=DIRECTIVE_ESCALATE_TO_USER,
                data={"effort_fire": _effort_fire_escalation_data(state), "reason": refusal},
            )
        state.agent_ack_difficulty_ids.append(_difficulty_id(state.difficulty))
        # The replans-scale fire rebased every scale's baseline; the spend / wall-clock
        # progress it swallowed is still owed to the user's own fire.
        replaced = last.get("replaced_baseline") or {}
        for scale in effort.SCALE_ORDER:
            if scale != effort.SCALE_REPLANS and scale in replaced:
                state.effort_baseline[scale] = replaced[scale]
    last["ack"] = {
        "by": args.by,
        "decision": decision,
        "ts": _utcnow(),
        "note": getattr(args, "note", None),
    }
    state.log("fire_acknowledge", by=args.by, decision=decision)
    _record_backstop_crossing(state, crossing)
    _clear_since_user_decision(state, args.by)
    _clear_open_fires_if_customer(state, args.by)
    if decision == "abandon":
        _park_node(state)
        state.log("block", reason="user-abandoned-after-fire")
        store.save(state)
        return Directive(True, state.node, "unblock",
                         "session abandoned after unacknowledged effort-divergence fire; "
                         "unblock to resume",
                         marker="ESCALATE",
                         data={"fire": last})
    store.save(state)
    next_action = "declare" if state.difficulty is not None else "next_stage"
    return Directive(True, state.node, next_action,
                     f"fire acknowledged (decision={decision}); resuming",
                     data={"fire": last, **_crossing_data(crossing)})


def cmd_check_coverage(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only pre-flight: would `replan --plan <new>` clear the critique coverage
    gate right now? Calls gates.replan_coverage_blockers directly — the same check
    cmd_replan runs — so a coverage miss surfaces BEFORE the coordinator spends a
    thinker plan-review on a corrected plan that would only bounce off replan's own
    gate afterward. Mirrors cmd_replan's OLD/NEW loading contract exactly (snapshot-
    else-plan_path, lenient OLD / strict NEW) but never saves, never logs a gate,
    never transitions state — a pre-flight that mutated what it inspects would
    corrupt the very state a later real replan diffs against."""
    state = _require(store, args.session)
    if not (state.difficulty and state.difficulty.critique):
        return Directive(True, state.node, "inspect", "no active critique; nothing to cover")
    old_path = _replan_baseline_path(state)
    old = _load_prior_plan(state, old_path, strict=False)
    new = load_plan(args.new)
    blockers = gates.replan_coverage_blockers(
        old, new, state.difficulty.critique, tier=change_tier(old, new).tier,
        cost_tier_window=_effort_fire_window(state))
    if blockers:
        return Directive(False, state.node, "inspect",
                         "coverage blockers: " + "; ".join(blockers),
                         data={"coverage_blockers": blockers})
    return Directive(True, state.node, "inspect", "OK — coverage clear")


def cmd_effort_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only effort-divergence report: where does this session stand against the
    norm its approved plan declared, on each of the four scales?

    Exists because the trigger only ever fires INSIDE a command the coordinator chose
    to run (`record-result`, `verify-final`, or the `gates.effort_fire_blockers` refusal
    on `dispatch`/`submit-plan`/`replan`). A session that runs long WITHOUT reaching one
    of those — a stage the coordinator keeps working, a specialist that never returns —
    diverges unobserved, however far past the multiple it goes. This command is the
    observation that does not depend on such a call arriving, and it is what the
    UserPromptSubmit watch hook drives on every prompt.

    STRICTLY READ-ONLY, and `ok=True` on every path including a refusal-shaped answer:
    it never transitions, never seeds a difficulty, never appends to `state.effort_fires`
    and never calls `store.save`. A read command with a side effect would be a SECOND,
    hidden fire site — it would consume the one-fire-per-replan budget belt 2 keeps
    (`effort._replans_since_last_fire`) without anyone having diagnosed anything, and
    the real fire site would then fall silent. `effort.refresh_spend` does mutate the
    loaded `SessionState` in memory (that is how the spend accumulator is read at all);
    with no save, the on-disk state is untouched, which the test asserts on the bytes.

    Deliberately does NOT call `effort.rederive`: the fire sites compare against the
    STORED estimate, so re-deriving here would report a divergence against a comparand
    no gate uses — a watch that disagrees with the gate it watches is worse than none.
    The same rule is why every per-scale row is computed from `effort.effective_deltas`
    / `effective_ratios` with the same cross-session totals `divergence()` is handed:
    `at_or_past_threshold` (what the hook speaks on) and `would_fire` (what a fire site
    would act on) must be answers about one vector, not two."""
    state = store.load(args.session) if getattr(args, "session", None) else None
    if state is None:
        return Directive(True, "(none)", "start", "no session state; nothing to check",
                         data={"armed": False, "scales": []})
    if not effort.armed(state):
        return Directive(
            True, state.node, "inspect",
            "effort trigger not armed (no approved-plan baseline); no scale can diverge",
            data={"armed": False, "active": gates.effort_active(state), "scales": []},
        )
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    thr = Thresholds()
    multiple = thr.effort_divergence_multiple()
    # The SAME cross-session totals the fire sites pass, read once and used for both
    # halves of this report. Reporting the session-local vector while `would_fire` was
    # decided on the cross-session one is how a watch goes silent on exactly the case it
    # exists for: a resolved re-entry hands the fresh SessionState a replan count of 0
    # while the accumulator still holds the prior laps. See effort.effective_deltas.
    now = _effort_now(store, state)
    cross_totals = _effort_cross_totals(state, now, store=store)
    local = effort.deltas(state, now=now)
    delta = effort.effective_deltas(state, cross_session_totals=cross_totals, now=now)
    comparand = effort.comparands(state, thr)
    ratio = effort.effective_ratios(state, thr, cross_session_totals=cross_totals, now=now)
    scales = []
    for scale in effort.SCALE_ORDER:
        label, unit = effort.describe(scale)
        kind = "ratio" if scale in effort.RATIO_SCALES else "absolute"
        trigger = multiple if kind == "ratio" else 1.0
        observed = ratio[scale]
        record_only = scale in effort.RECORD_ONLY_SCALES
        scales.append({
            "scale": scale, "label": label, "unit": unit, "kind": kind,
            "actual": delta[scale], "comparand": comparand[scale], "ratio": observed,
            # Normalized "how far past its OWN line", so the four scales rank against
            # each other — the same footing effort.divergence puts them on.
            "past_own_trigger": (observed / trigger) if observed is not None else None,
            # RECORD_ONLY_SCALES never fire, so forced False here mirrors
            # effort.divergence's own skip of these scales — a watch that showed
            # at_or_past_threshold=True for a scale that can never diverge would read
            # as an alarm nothing acts on.
            "at_or_past_threshold": (not record_only) and observed is not None and observed >= trigger,
            # Whether the accumulator, not this session's own history, supplied the
            # number — so a reader of the line knows the count is the TASK's, not the
            # session's, without having to open the accumulator to find out.
            "cross_session": delta[scale] > local[scale],
            "record_only": record_only,
        })
    div = effort.divergence(state, thr, cross_session_totals=cross_totals, now=now)
    over = [s["scale"] for s in scales if s["at_or_past_threshold"]]
    # Separate from `over_threshold`: the true crossing state of a record-only scale,
    # reported for visibility but never folded into `over`/`would_fire` — those two
    # stay reserved for scales the divergence trigger can actually act on.
    record_only_over = [
        s["scale"] for s in scales
        if s["record_only"] and s["ratio"] is not None and s["ratio"] >= (
            multiple if s["kind"] == "ratio" else 1.0
        )
    ]
    detail = (
        f"effort divergence on {', '.join(over)}" if over
        else "no scale at or past its threshold"
    )
    return Directive(
        True, state.node, "inspect", detail,
        data={
            "armed": True,
            # Whether a fire site WOULD act on this: gates.effort_active is the same
            # predicate they consult, so a report that omitted it would read as an
            # alarm on a session where the trigger is switched off.
            "active": gates.effort_active(state),
            "over_threshold": over,
            "record_only_over_threshold": record_only_over,
            "would_fire": div.scale if div is not None else None,
            "framing": div.framing if div is not None else None,
            "scales": scales,
        },
    )


def register(sub, add):
    sp = add("replan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp.add_argument("--reason", default=None,
                    help="free-text cause for this replan, captured for history only; used "
                         "only when no active difficulty record supplies one (a bare "
                         "refinement/no_change replan outside DIAGNOSING) — never required, "
                         "and its absence never refuses the command")
    sp.add_argument("--coverage-waiver", dest="coverage_waiver", default=None,
                    help="bypass a failing coverage gate with a recorded reason (refused if empty); "
                         "never bypasses the difficulty-record completeness precondition")
    sp.add_argument("--normalization-waiver", dest="normalization_waiver", default=None,
                    help="close a difficulty WITHOUT a re-norming record when the exposed factor "
                         "is genuinely one-off; a recorded reason (refused if empty)")
    sp.add_argument("--renormalize", action="store_true",
                    help="claim the corrected plan changes only stage `procedure` — the "
                         "SEQUENCE of operations, which is the executor's to replace; skips "
                         "the plan-review and plan_approval gates and is REFUSED the moment "
                         "the edit reaches a method, a criterion, a result image or the goal")
    sp.add_argument("--reopen", dest="reopen", action="append", type=int, default=None,
                    metavar="N",
                    help="re-arm PASSED stage N (repeatable) together with every PASSED stage "
                         "that depends on it, so the replan may edit what N was built from or "
                         "promises; without it such an edit to a PASSED stage is refused")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp.add_argument("--renegotiation-decision", dest="renegotiation_decision", default=None,
                    choices=["continue", "rescope", "abandon"],
                    help="clear the diagnosing_replan round-release ceiling (the "
                         "replan-count threshold out of DIAGNOSING): continue/rescope zero the cross-session "
                         "task accumulator and let this replan proceed; abandon parks the "
                         "session at BLOCKED without applying --plan. Requires "
                         "--renegotiated-by and --renegotiation-note")
    sp.add_argument("--renegotiated-by", dest="renegotiated_by", default=None,
                    help="who made the renegotiation decision; must match "
                         "[meta.order].customer_id when the plan declares one")
    sp.add_argument("--renegotiation-note", dest="renegotiation_note", default=None,
                    help="what the customer decided and why (refused if empty)")
    sp.add_argument("--normalize-factor", dest="normalize_factor", default=None,
                    help="record a normalize event in the SAME replan call, without a "
                         "separate `normalize` invocation -- the reproducible cause being "
                         "re-normed. Optional; its absence never refuses the replan (a "
                         "non-blocking hint names the flag when it's missing)")
    sp.add_argument("--normalize-level", dest="normalize_level", default=None,
                    choices=list(NORMALIZATION_LEVELS),
                    help="paired with --normalize-factor; recording level (payoff-gated), "
                         "omit for an in-head note below the leaf threshold")
    sp = add("fire-acknowledge"); sp.add_argument("--session", required=True)
    sp.add_argument("--by", required=True, help="who decided — a name, not a narrative")
    sp.add_argument("--decision", required=True, choices=["continue", "abandon", "revise"],
                    help="continue: accept the overrun, keep executing; abandon: park the "
                         "session at BLOCKED (via blocked_from, same as cmd_block) with "
                         "reason 'user-abandoned-after-fire' — never RESOLVED, since a "
                         "mid-execution fire routinely leaves stages PENDING and "
                         "check_invariants refuses RESOLVED unless every stage PASSED; "
                         "revise: route to the ordinary DIAGNOSING replan cycle")
    sp.add_argument("--note", default=None)
    sp = add("check-coverage"); sp.add_argument("--session", required=True)
    sp.add_argument("--new", required=True,
                    help="corrected plan file to check against the active critique, "
                         "BEFORE spending a thinker plan-review on it")
    sp = add("effort-check"); sp.add_argument("--session", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="cost log file path (test override)")
