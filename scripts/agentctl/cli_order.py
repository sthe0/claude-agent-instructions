"""Order-agreement subcommands: submit an order draft, record the user's agreement to its
understanding, dismiss a challenge to it.

See cli.py for the command table."""
from __future__ import annotations

import re
import time
from pathlib import Path
from lib import ask_evidence, config_root
from . import answer_ledger, delivery, effort_segments, gates
from .directive import Directive
from .dispatch import Runner
from .plan import PlanError, load_order_draft, understanding_digest, understanding_parts
from .state import (
    CHALLENGE_DISMISSED,
    CHALLENGE_OPEN,
    CHALLENGE_REAGREED,
    ORDER_AGREEMENT_ANCHOR_CONFIRMED,
    ORDER_AGREEMENT_ANCHOR_STAMP,
    OrderAgreement,
    PLAN_PRESENTATION_KIND_ORDER,
    SessionState,
    order_agree_marker,
    order_dismiss_marker,
)
from .store import StateStore
from .submission import order_understanding_completeness_violations, order_understanding_violations
from .cli_common import _renew_order_agreement, _require


def _order_draft_violations(meta) -> list[str]:
    """Every reason the order in `meta` is not fit to be put to the user: the
    completeness findings (which already contain the understanding tables' own) and
    the understanding findings once more, each reported once. Re-run on the CURRENT
    draft wherever the draft is acted on, never trusted from an earlier submission.
    Also refuses an id the rendering cannot show as an anchor (see `_ORDER_ANCHOR_RE`),
    so the author learns at submit what no presentation could ever satisfy."""
    problems = list(order_understanding_completeness_violations(meta))
    problems.extend(v for v in order_understanding_violations(meta) if v not in problems)
    order = meta.order
    if order is not None:
        for kind, letter, items in (
            ("requirement", "R", order.requirements),
            ("proposal", "P", order.proposals),
            ("constraint", "K", order.constraints),
        ):
            misshapen = sorted({i.id for i in items if i.id and not re.fullmatch(rf"{letter}\d+", i.id)})
            if misshapen:
                problems.append(
                    f"[meta.order] {kind} id(s) {', '.join(misshapen)} do not have the form "
                    f"{letter}<number> (e.g. {letter}1): the user agrees the order by the "
                    f"[{letter}<number>] anchors of its rendering"
                )
    return problems


def _planning_open(state: SessionState) -> bool:
    """Whether the session has not yet had its plan's first approval: the planning segment's
    arming is open, and no migrated window stands in for it."""
    ledger = state.effort_segments
    return (not state.plan_stack and effort_segments.window_arming(ledger) is None
            and effort_segments.open_arming_of(ledger, effort_segments.PLANNING) is not None)


def _agreed_before_migration(state: SessionState, doc) -> bool:
    """Whether the draft is the very understanding the user agreed before this session's
    migration instant: such an order predates the estimate table and is never asked for it again
    just to be presented."""
    agreed = state.order_agreement
    migrated = state.effort_migrated
    return (agreed is not None and bool(migrated) and agreed.at < migrated["instant"]
            and agreed.understanding_sha256 == understanding_digest(doc))


def _planning_estimate_refusal(state: SessionState, doc, *, agreeing: bool = False) -> str | None:
    """The refusal of an order draft that is to be put to the user or agreed before the plan's
    first approval and lacks any of the estimate's three parts, or None. Only presence is
    graded: what the derivation says is the reviewer's to judge. An order put or agreed after the
    first approval carries no estimate and is not refused for lacking one; an order agreed before
    the session's migration may be put again unchanged without it, but a new agreement of it
    (`agreeing`) needs the table like any other."""
    if not _planning_open(state):
        return None
    if not agreeing and _agreed_before_migration(state, doc):
        return None
    order = doc.meta.order
    estimate = order.planning_estimate if order is not None else None
    missing = estimate.missing() if estimate is not None else ["active_minutes", "usd", "derivation"]
    if not missing:
        return None
    return (
        f"the order draft states no usable planning estimate: {', '.join(missing)} missing from "
        "[meta.order.planning_estimate]. Before the plan's first approval the order carries "
        "{ active_minutes = <positive number>, usd = <positive number>, derivation = \"<how both "
        "follow from the work planning needs>\" }, so planning has a budget the user agrees with the order"
    )


def _record_agreed_estimate(state: SessionState, doc) -> None:
    """The estimate an agreement before the first approval puts to planning, kept in the effort
    ledger for the arming that agreement opens (and the renewals after it)."""
    order = doc.meta.order
    estimate = order.planning_estimate if order is not None else None
    if estimate is None or estimate.missing() or not _planning_open(state):
        return
    state.effort_segments["agreed_estimate"] = {
        "active_minutes": estimate.active_minutes, "usd": estimate.usd, "derivation": estimate.derivation,
    }


def _session_has_agreement(state: SessionState) -> bool:
    """Whether the user has already agreed the order's understanding in this session."""
    return state.order_agreement is not None


def cmd_order_submit(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Submit an order draft: the first act of the order phase, before any plan exists.

    Refuses a draft whose order is incomplete or ill-formed, and a draft that already
    carries stages while the session has no agreement; otherwise records the draft's
    path and the engine's digest of its understanding for `plan-review --scope order`
    and `present-plan --kind order` to bind to. Only records: `plan_path` is untouched
    and the node never moves, so a session that never calls it is unchanged."""
    state = _require(store, args.session)
    try:
        doc, staged = load_order_draft(args.plan)
    except (OSError, PlanError) as exc:
        return Directive(False, state.node, "noop", f"cannot load order draft: {exc}")
    if staged and not _session_has_agreement(state):
        return Directive(
            False, state.node, "noop",
            f"{args.plan} carries stages but the user has not agreed this session's order: "
            "submit the order draft without [[stage]] entries first",
        )
    problems = _order_draft_violations(doc.meta)
    if problems:
        return Directive(
            False, state.node, "noop",
            f"order draft {args.plan} is not fit to put to the user: " + "; ".join(problems),
            data={"problems": problems},
        )
    state.order_draft_path = args.plan
    state.order_draft_sha256 = understanding_digest(doc)
    state.log("order_submit", plan=args.plan, understanding_sha256=state.order_draft_sha256)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"order draft {args.plan} submitted (understanding {state.order_draft_sha256[:12]}); "
        "next: plan-review --scope order, then present-plan --kind order",
        data={"order_draft_path": args.plan, "understanding_sha256": state.order_draft_sha256},
    )


def _user_answered_after(state_file: Path, since: float, marker: str) -> "tuple[bool, answer_ledger.LedgerRead]":
    """Whether the ledger holds an answer given at or after `since` that chose a marked option
    of an ask offering `marker`. The ledger is the only evidence: no transcript is read."""
    ledger_read = answer_ledger.read(state_file)
    chosen = any(
        record.at >= since and ask_evidence.chose_marked_option(record.tool_input(), record.answers, marker)
        for record in ledger_read.records
    )
    return chosen, ledger_read


def _no_marked_answer_refusal(what: str, marker: str, ledger_read: "answer_ledger.LedgerRead",
                              since: float) -> str:
    text = (
        f"{what} needs the user's own answer: an AskUserQuestion posed after the presentation "
        f"offering an option marked {marker!r} beside an unmarked one, which the user chose; "
        "no such answer is recorded"
    )
    if not any(record.at >= since for record in ledger_read.records):
        text += (
            " (the answer ledger is missing or holds no answer since the presentation — a session "
            "that started before hook-ask-answer-record.py was registered records nothing: "
            "register it with scripts/install-reminder-hooks.sh and restart the session)"
        )
    return text


def _order_agreement_anchor(state: SessionState, receipt, state_file: Path) -> "tuple[float, str] | str":
    """`(time, anchor kind)` from the delivery stamp of the order receipt's own rendering, or the
    refusal text. A sidecar holding another rendering's stamp anchors nothing."""
    stamp = delivery.read_stamp(state_file)
    if stamp is not None and stamp.rendering_sha256 != receipt.rendering_sha256:
        return (
            "the delivery stamp on record belongs to another rendering than the order "
            "presentation: present the order again (present-plan --kind order), let the "
            "delivery hook stamp it, and put a fresh ask to the user, since an earlier answer "
            "predates that stamp"
        )
    blockers = gates._delivery_stamp_blocker(state, receipt, None)
    if blockers:
        return "the order presentation has no usable delivery proof: " + "; ".join(blockers)
    anchor = (ORDER_AGREEMENT_ANCHOR_CONFIRMED if stamp.source == delivery.SOURCE_OVERRIDE
              else ORDER_AGREEMENT_ANCHOR_STAMP)
    return stamp.verified_ts, anchor


def cmd_order_agree(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the user's agreement to the order understanding presented to them.

    Rests on one thing the agent cannot write through a verb: an answer in the session's answer
    ledger, given after the presentation was delivered, choosing the option marked
    `[order-agree:<digest prefix>]`. With no such answer nothing is recorded and no flag
    substitutes for it."""
    state = _require(store, args.session)

    def refuse(msg: str, **data) -> Directive:
        return Directive(False, state.node, "noop", msg, data=data)

    path = state.order_draft_path
    blockers = gates.order_review_blockers(state, path)
    if blockers:
        return refuse("cannot record the agreement: " + "; ".join(blockers), blockers=blockers)
    doc, _ = load_order_draft(path)
    digest = understanding_digest(doc)
    no_estimate = _planning_estimate_refusal(state, doc, agreeing=True)
    if no_estimate is not None:
        return refuse("cannot record the agreement: " + no_estimate)
    receipt = gates._plan_presentation_for(state, PLAN_PRESENTATION_KIND_ORDER)
    if receipt is None:
        return refuse("no order presentation recorded: present-plan --kind order first")
    stale = gates.order_presentation_stale_reason(state, receipt)
    if stale is not None or receipt.understanding_sha256 != digest:
        return refuse(stale or "the order presentation is for another understanding: present it again")
    state_file = config_root.resolve_agentctl_state_file(state.session_id)
    if state_file is None:
        return refuse("no state file for this session to read the answer ledger beside")
    anchored = _order_agreement_anchor(state, receipt, state_file)
    if isinstance(anchored, str):
        return refuse(anchored)
    since, anchor = anchored
    marker = order_agree_marker(digest)
    answered, ledger_read = _agreement_ask_answered(state_file, since, marker)
    if not answered:
        return refuse(_no_marked_answer_refusal("agreeing the order", marker, ledger_read, since))
    previous = state.order_agreement
    agreement = OrderAgreement(
        understanding_sha256=digest, parts=understanding_parts(doc), draft_path=path,
        at=time.time(), note=(getattr(args, "note", "") or "").strip(), anchor=anchor,
        anchor_at=since, rendering_sha256=receipt.rendering_sha256,
    )
    state.order_agreement = agreement
    state.order_agreement_history.append(agreement)
    _record_agreed_estimate(state, doc)
    _renew_order_agreement(state, store, args)
    if previous is None or previous.understanding_sha256 != digest:
        for challenge in state.order_challenges:
            if challenge.status == CHALLENGE_OPEN:
                challenge.status = CHALLENGE_REAGREED
    state.log("order_agree", understanding_sha256=digest, anchor=anchor, draft=path)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"order understanding {digest[:12]} agreed by the user ({anchor} anchor); "
        "next: author the plan and submit-plan",
        data={"understanding_sha256": digest, "anchor": anchor},
    )


# Pass-through kept as a module attribute so the agree_without_ask mutant can replace it.
def _agreement_ask_answered(state_file: Path, since: float, marker: str):
    """The evidence test of `order-agree`: a marked user answer in the ledger since `since`."""
    return _user_answered_after(state_file, since, marker)


def cmd_order_challenge_dismiss(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Dismiss an open challenge to the agreed order, on the user's marked answer to an ask
    posed after the challenge was recorded (`[order-dismiss:<id>]`) and a stated reason."""
    state = _require(store, args.session)

    def refuse(msg: str) -> Directive:
        return Directive(False, state.node, "noop", msg)

    note = (getattr(args, "note", "") or "").strip()
    if not note:
        return refuse("order-challenge-dismiss requires a non-empty --note: why the user dismisses it")
    challenge = next((c for c in state.order_challenges if c.id == args.id), None)
    if challenge is None:
        return refuse(f"no understanding challenge {args.id!r} is recorded")
    if challenge.status != CHALLENGE_OPEN:
        return refuse(f"challenge {args.id!r} is {challenge.status}, not open")
    state_file = config_root.resolve_agentctl_state_file(state.session_id)
    if state_file is None:
        return refuse("no state file for this session to read the answer ledger beside")
    marker = order_dismiss_marker(challenge.id)
    answered, ledger_read = _user_answered_after(state_file, challenge.at, marker)
    if not answered:
        return refuse(_no_marked_answer_refusal("dismissing the challenge", marker, ledger_read, challenge.at))
    challenge.status = CHALLENGE_DISMISSED
    challenge.dismissed_at = time.time()
    challenge.dismiss_note = note
    state.log("order_challenge_dismiss", id=challenge.id, note=note)
    store.save(state)
    return Directive(True, state.node, "continue", f"challenge {challenge.id} dismissed by the user's answer")


def register(sub, add):
    sp = add("order-submit"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", required=True,
                    help="order draft file (a TOML with [meta.order] and no stages) to submit "
                         "for review and presentation; only records it, the node does not move")

    sp = add("order-agree"); sp.add_argument("--session", required=True)
    sp.add_argument("--note", default="",
                    help="optional note recorded with the agreement; the agreement itself rests "
                         "on the user's marked answer in the answer ledger, never on a flag")

    sp = add("order-challenge-dismiss"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="id of the open understanding challenge")
    sp.add_argument("--note", required=True,
                    help="why the user dismisses it; needs the user's answer choosing the "
                         "option marked [order-dismiss:<id>]")
