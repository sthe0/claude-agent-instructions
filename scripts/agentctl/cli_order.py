"""Order-agreement subcommands: submit an order draft, record the user's agreement to its
understanding, dismiss a challenge to it.

See cli.py for the command table."""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from lib import ask_evidence, config_root
from . import answer_ledger, delivery, effort_segments, gates
from .directive import Directive
from .dispatch import Runner, subprocess_runner
from .plan import (
    PlanError, executor_side_digest, executor_side_parts, is_two_sided, load_order_draft,
    understanding_digest, understanding_parts,
)
from .state import (
    CHALLENGE_DISMISSED,
    CHALLENGE_OPEN,
    CHALLENGE_REAGREED,
    ORDER_AGREEMENT_ANCHOR_CONFIRMED,
    ORDER_AGREEMENT_ANCHOR_STAMP,
    OrderAgreement,
    PLAN_PRESENTATION_KIND_ORDER,
    SessionState,
    TYPE_ENTRY_EXPERIENCE,
    TYPE_ENTRY_NONE,
    TYPE_ENTRY_REGULARITY,
    order_agree_marker,
    order_dismiss_marker,
)
from .store import StateStore
from .submission import (
    order_customer_side_missing, order_sides_violations, order_understanding_completeness_violations,
    order_understanding_violations,
)
from .cli_common import _renew_order_agreement, _require

REPO_ROOT = Path(__file__).resolve().parents[2]
LEAVES_ROOT = REPO_ROOT / "memory-global" / "leaves"
SEARCH_TIERS = ("experience", "regularities", "principles", "system-knowledge")
BINDING_DECISIONS = ("keep", "drop")
_SEARCH_HIT_RE = re.compile(r"^\s*\[\s*\d+\]\s+(\S+)", re.MULTILINE)
_TYPE_ENTRY_AREA = {TYPE_ENTRY_EXPERIENCE: "experience", TYPE_ENTRY_REGULARITY: "regularities"}


def _search_recorded(state: SessionState, search_id: str) -> bool:
    """Whether this session holds a recorded search under `search_id` (a module attribute so
    the no-search-record mutant can replace it)."""
    return any(s.get("id") == search_id for s in state.order_searches)


def _record_exists(kind: str, ref: str) -> bool:
    area = LEAVES_ROOT / _TYPE_ENTRY_AREA[kind]
    name = ref[:-3] if ref.endswith(".md") else ref
    return bool(name) and "/" not in name and any(p.name == f"{name}.md" for p in area.rglob("*.md"))


def _executor_side_violations(meta, state: SessionState) -> list[str]:
    """The executor-side checks that need the session: a type entry naming a record must name
    an existing one, and a none entry must name a search recorded in THIS session's state. Each
    session resolves its own entries at its own submission; no search is read across a link.
    Whether the recorded results fit the order is not judged here."""
    order = meta.order
    executor = order.executor_side if order is not None else None
    entry = executor.type_entry if executor is not None else None
    if entry is None:
        return []
    if entry.kind == TYPE_ENTRY_NONE:
        if entry.search_id and not _search_recorded(state, entry.search_id):
            return [f"[meta.order.executor_side] the none type_entry names search "
                    f"{entry.search_id!r}, which this session has not recorded: run order-search first"]
    elif entry.kind in _TYPE_ENTRY_AREA and entry.ref and not _record_exists(entry.kind, entry.ref):
        return [f"[meta.order.executor_side] the {entry.kind} type_entry names {entry.ref!r}, "
                f"which is not a record under {LEAVES_ROOT / _TYPE_ENTRY_AREA[entry.kind]}"]
    return []


def _beside_records(doc) -> dict:
    """The records beside the order, taken from the draft: binding conditions, method ideas with
    their verdicts, conditions on means and the planning budget. In neither digest."""
    order = doc.meta.order
    budget = order.planning_budget if order is not None else None
    return {
        "binding_conditions": [
            {"id": b.id, "text": b.text, "set_by": b.set_by,
             "objection": {"text": b.objection_text, "raised_by": b.objection_by}
             if b.objection_text else None}
            for b in (order.binding_conditions if order is not None else ())],
        "method_ideas": [
            {"id": p.id, "text": p.text, "verdict": p.verdict, "critique": p.critique,
             "against": list(p.against)} for p in (order.method_ideas if order is not None else ())],
        "means_conditions": [
            {"kind": "means_condition", "id": c.id, "text": c.text}
            for c in (order.means_conditions if order is not None else ())],
        "planning_budget": ({"active_minutes": budget.active_minutes, "usd": budget.usd,
                             "derivation": budget.derivation} if budget is not None else None),
    }


def _order_draft_violations(doc, state: SessionState) -> list[str]:
    """Every reason the order in `doc` is not fit to be put to the user: the
    completeness findings (which already contain the understanding tables' own) and
    the understanding findings once more, each reported once. Re-run on the CURRENT
    draft wherever the draft is acted on, never trusted from an earlier submission.
    Also refuses an id the rendering cannot show as an anchor (see `_ORDER_ANCHOR_RE`),
    so the author learns at submit what no presentation could ever satisfy.

    An old-form draft (no customer side) is refused unless the user already agreed exactly
    its understanding in this session: that order may be put again unchanged."""
    meta = doc.meta
    problems = list(order_understanding_completeness_violations(meta))
    problems.extend(v for v in order_understanding_violations(meta) if v not in problems)
    if _old_form_unadmitted(state, doc):
        problems.extend(order_customer_side_missing(meta))
    problems.extend(order_sides_violations(meta))
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
    return gates.agreed_before_migration(state, understanding_digest(doc))


def _planning_terms(order):
    """The planning terms an order carries: the budget record beside a new-form order, the
    `planning_estimate` part of an old-form one."""
    if order is None:
        return None
    return order.planning_budget if is_two_sided(order) else order.planning_estimate


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
    estimate = _planning_terms(order)
    missing = estimate.missing() if estimate is not None else ["active_minutes", "usd", "derivation"]
    if not missing:
        return None
    table = "planning_budget" if is_two_sided(order) else "planning_estimate"
    return (
        f"the order draft states no usable planning estimate: {', '.join(missing)} missing from "
        f"[meta.order.{table}]. Before the plan's first approval the order carries "
        "{ active_minutes = <positive number>, usd = <positive number>, derivation = \"<how both "
        "follow from the work planning needs>\" }, so planning has a budget the user agrees with the order"
    )


def _record_agreed_estimate(state: SessionState, doc) -> None:
    """The estimate an agreement before the first approval puts to planning, kept in the effort
    ledger for the arming that agreement opens (and the renewals after it)."""
    estimate = _planning_terms(doc.meta.order)
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
    problems = _order_draft_violations(doc, state)
    problems.extend(_executor_side_violations(doc.meta, state))
    if problems:
        return Directive(
            False, state.node, "noop",
            f"order draft {args.plan} is not fit to put to the user: " + "; ".join(problems),
            data={"problems": problems},
        )
    state.order_draft_path = args.plan
    state.order_draft_sha256 = understanding_digest(doc)
    state.order_beside = _beside_records(doc)
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
    order = doc.meta.order
    if _old_form_unadmitted(state, doc):
        return refuse(
            "cannot record the agreement: the order draft has no customer side and was not agreed "
            "before the order was split into two sides; rewrite it with [meta.order.customer_side] "
            "and the executor side, and submit it again"
        )
    decisions = _parse_binding_decisions(args, order)
    if isinstance(decisions, str):
        return refuse("cannot record the agreement: " + decisions)
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
    if is_two_sided(order):
        beside = _beside_records(doc)
        agreement.customer_side_agreed = True
        agreement.executor_side_sha256 = executor_side_digest(doc)
        agreement.executor_side_parts = executor_side_parts(doc)
        agreement.correction = _agreed_correction(args)
        agreement.binding_decisions = _agreed_binding_decisions(decisions)
        agreement.binding_conditions = beside["binding_conditions"]
        agreement.planning_budget = beside["planning_budget"]
        agreement.criterion_type = order.customer_side.criterion_type
        _adopt_order_criterion_type(state, doc)
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


def _adopt_order_criterion_type(state: SessionState, doc) -> None:
    """The agreed customer side's criterion type becomes the session's, which the closing ask and
    `verify-final` read. An old-form order never reaches here and keeps the session's own."""
    state.overall_criterion_type = doc.meta.order.customer_side.criterion_type


def _old_form_unadmitted(state: SessionState, doc) -> bool:
    """The one statement of the old-form admission rule: a draft with no customer side is
    admitted only when the user already agreed exactly its understanding in this session, so
    that order may be put again unchanged."""
    digest = understanding_digest(doc)
    return not is_two_sided(doc.meta.order) and not any(
        a.understanding_sha256 == digest
        for a in (*state.order_agreement_history, state.order_agreement) if a is not None)


def _parse_binding_decisions(args, order) -> "list[dict] | str":
    """The user's decision on each binding condition of the draft, from repeated
    `--binding-decision ID=keep|drop`, or the refusal text. Every condition needs one."""
    raw = list(getattr(args, "binding_decision", None) or [])
    declared = [b.id for b in (order.binding_conditions if order is not None else ())]
    decisions: list[dict] = []
    for item in raw:
        ident, _, verdict = item.partition("=")
        ident, verdict = ident.strip(), verdict.strip()
        if ident not in declared:
            return f"--binding-decision names {ident!r}, which is not a binding condition of the draft"
        if verdict not in BINDING_DECISIONS:
            return f"--binding-decision {ident} must be one of {'/'.join(BINDING_DECISIONS)}"
        if any(d["id"] == ident for d in decisions):
            return f"--binding-decision names {ident!r} twice"
        decisions.append({"id": ident, "decision": verdict, "decided_by": "user"})
    undecided = [i for i in declared if not any(d["id"] == i for d in decisions)]
    if undecided:
        return ("the draft's binding condition(s) " + ", ".join(undecided) +
                " have no user decision: pass --binding-decision <id>=keep|drop for each")
    return decisions


# Pass-throughs kept as module attributes so the correction-unrecorded and
# binding-decision-unrecorded mutants can replace them.
def _agreed_correction(args) -> str:
    """The user's correction of the reconstruction, recorded with the agreement."""
    return (getattr(args, "correction", "") or "").strip()


def _agreed_binding_decisions(decisions: list[dict]) -> list[dict]:
    return decisions


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


def cmd_order_search(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Run `record-experience.py search` and record, under `--id`, the query and the results it
    returned in the session state, so an order's none type entry can name the search that found
    no record. Changes no production file: it writes engine state and the search's own ledger."""
    state = _require(store, args.session)

    def refuse(msg: str) -> Directive:
        return Directive(False, state.node, "noop", msg)

    ident = (args.id or "").strip()
    query = (args.query or "").strip()
    if not ident or not query:
        return refuse("order-search needs a non-empty --id and --query")
    if any(s.get("id") == ident for s in state.order_searches):
        return refuse(f"a search is already recorded under {ident!r}: pick another --id")
    run = runner or subprocess_runner
    result = run([sys.executable, str(REPO_ROOT / "scripts" / "record-experience.py"), "search",
                  "--tier", args.tier, "--session", state.session_id, query])
    if result.returncode != 0:
        return refuse(f"the search failed (exit {result.returncode}): {(result.stderr or '').strip()[:300]}")
    leaves = _SEARCH_HIT_RE.findall(result.stdout)
    state.order_searches.append({
        "id": ident, "tier": args.tier, "query": query,
        "results": {"leaves": leaves, "output": result.stdout}, "at": time.time(),
    })
    state.log("order_search", id=ident, tier=args.tier, hits=len(leaves))
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"search {ident!r} recorded ({len(leaves)} result(s)); a none type_entry may name it as search_id",
        data={"id": ident, "leaves": leaves},
    )


def _changed_parts(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(name for name in {*before, *after} if before.get(name) != after.get(name))


def _agreed_parts_moved(doc, agreement: OrderAgreement) -> list[str]:
    """The agreed parts a revised draft changes ([] == the agreed digest still holds); a module
    attribute so the revision-unguarded mutant can replace it."""
    if understanding_digest(doc) == agreement.understanding_sha256:
        return []
    return _changed_parts(agreement.parts, understanding_parts(doc)) or ["understanding"]


def _agreed_change(moved: list[str], beside: dict, agreement: OrderAgreement) -> str | None:
    """What of the agreement a revised draft changes, as a phrase for the refusal, or None. The
    binding conditions and the planning budget are compared with the records the agreement
    itself bound, never with the session's current draft, which a later order-submit rewrites."""
    if moved:
        return f"the customer side or an acceptance check the user agreed ({', '.join(moved)})"
    if beside["binding_conditions"] != agreement.binding_conditions:
        return "a binding condition the user decided on"
    if beside["planning_budget"] != agreement.planning_budget:
        return "the planning budget the agreement bound"
    return None


def cmd_order_executor_revise(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a revision of the executor side of the agreed order, with no new agreement.

    The revised draft must keep the agreed digest (so the customer side and every acceptance check
    are as the user agreed them) and pass the executor-side checks order-submit runs. Writes the
    typed revision record (what changed, why, by, the earlier and the new executor-side digest)
    and never moves the agreed digest."""
    state = _require(store, args.session)

    def refuse(msg: str, **data) -> Directive:
        return Directive(False, state.node, "noop", msg, data=data)

    why = (getattr(args, "why", "") or "").strip()
    if not why:
        return refuse("order-executor-revise requires a non-empty --why")
    agreement = state.order_agreement
    if agreement is None or not agreement.customer_side_agreed:
        return refuse("no two-sided order agreement is recorded: there is no executor side to revise")
    path = getattr(args, "plan", "") or state.order_draft_path
    try:
        doc, _ = load_order_draft(path)
    except (OSError, PlanError) as exc:
        return refuse(f"cannot load the revised draft: {exc}")
    if not is_two_sided(doc.meta.order):
        return refuse("the revised draft has no customer side")
    moved = _agreed_parts_moved(doc, agreement)
    beside = _beside_records(doc)
    changed = _agreed_change(moved, beside, agreement)
    if changed:
        return refuse(
            f"the revision touches {changed}: that needs a new agreement (order-submit, "
            "order-agree), not a revision", moved=moved)
    problems = _order_draft_violations(doc, state)
    problems.extend(_executor_side_violations(doc.meta, state))
    if problems:
        return refuse("the revised executor side is not fit: " + "; ".join(problems), problems=problems)
    earlier = state.executor_side_revisions[-1] if state.executor_side_revisions else None
    earlier_digest = earlier["executor_side_sha256"] if earlier else agreement.executor_side_sha256
    earlier_parts = earlier["executor_side_parts"] if earlier else agreement.executor_side_parts
    new_digest = executor_side_digest(doc)
    if new_digest == earlier_digest:
        return refuse("the revised draft leaves the executor side as it was: nothing to record")
    new_parts = executor_side_parts(doc)
    state.executor_side_revisions.append({
        "at": time.time(), "by": (getattr(args, "by", "") or "agent").strip() or "agent", "why": why,
        "changed": _changed_parts(earlier_parts, new_parts), "plan": path,
        "earlier_executor_side_sha256": earlier_digest, "executor_side_sha256": new_digest,
        "executor_side_parts": new_parts, "agreed_understanding_sha256": agreement.understanding_sha256,
    })
    state.order_draft_path = path
    state.order_beside = beside
    state.log("order_executor_revise", executor_side_sha256=new_digest, why=why)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"executor side revised ({earlier_digest[:12]} -> {new_digest[:12]}); the agreed digest "
        f"{agreement.understanding_sha256[:12]} is unchanged",
        data={"executor_side_sha256": new_digest, "changed": state.executor_side_revisions[-1]["changed"]},
    )


def register(sub, add):
    sp = add("order-submit"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", required=True,
                    help="order draft file (a TOML with [meta.order] and no stages) to submit "
                         "for review and presentation; only records it, the node does not move")

    sp = add("order-agree"); sp.add_argument("--session", required=True)
    sp.add_argument("--note", default="",
                    help="optional note recorded with the agreement; the agreement itself rests "
                         "on the user's marked answer in the answer ledger, never on a flag")
    sp.add_argument("--correction", default="",
                    help="the user's correction of the agent's reconstruction of the need, "
                         "recorded with the agreement")
    sp.add_argument("--binding-decision", action="append", default=[], metavar="ID=keep|drop",
                    help="the user's decision on one binding condition of the draft; repeat per "
                         "condition")

    sp = add("order-search"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="id the recorded search is kept under")
    sp.add_argument("--tier", choices=SEARCH_TIERS, default="experience")
    sp.add_argument("--query", required=True, help="keywords handed to record-experience.py search")

    sp = add("order-executor-revise"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", default="", help="the revised order draft (default: the submitted draft)")
    sp.add_argument("--why", required=True, help="why the executor side changes")
    sp.add_argument("--by", default="agent")

    sp = add("order-challenge-dismiss"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="id of the open understanding challenge")
    sp.add_argument("--note", required=True,
                    help="why the user dismisses it; needs the user's answer choosing the "
                         "option marked [order-dismiss:<id>]")
