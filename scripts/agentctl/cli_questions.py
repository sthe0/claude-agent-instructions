"""Question-provenance and order-element subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import re
from dataclasses import dataclass
from . import controls, plugins_premise, premise
from .directive import Directive
from .dispatch import Runner
from .plan import META_PART, PlanDoc, load_plan, plan_stage_digests, stage_part
from .store import StateStore
from .cli_common import (
    _with_advisories,
    _question_bag,
    _bound_stage_key,
    _bound_order_stage_key,
    _materiality_doc,
    _materiality_advisories,
    cmd_question_enumerate_retired,
)


def cmd_question_raise(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record (or re-declare) one OPEN question arising during plan construction.
    Permissive exactly like ledger-add about its TARGET: a malformed one is stored
    as-is and the GATE (premise.validate_questions) reports it, so the moment-of-
    arising record is never lost to an argparse rejection. UPSERT by --id, last
    write wins — re-raising resets the entry to open. state.log stamps the act; that
    timestamp IS the moment-of-arising record and is why questions live in state,
    not the plan file.

    NOT permissive about --control, and this is the one seam that is not: a question
    must name the control of this plan its answer could flip, and a name that
    resolves to nothing here is refused. The refusal lives at this WRITE seam rather
    than at the gate on purpose — every question persisted before the requirement
    existed carries no control name, and a gate demanding one would convert each of
    them into a blocker on a session that can no longer go back and answer it.
    Enforced here, the requirement binds every question raised from now on and none
    raised before.

    `--plan` names the plan the control is resolved against, defaulting to
    `state.plan_path`, for the CORRECTED plan of a replan — the same deadlock
    `question-dispose`/`question-rebind` carry the flag for (#48(b)): without it a
    question about a stage that exists only in the correction could never be
    raised."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to raise "
                         "against the session's own plan, or name a real one")
    control = getattr(args, "control", None)
    if control is not None:
        control = str(control).strip()
        if not control:
            return Directive(False, state.node, "noop",
                             "--control was given an empty name; name the control of "
                             "this plan whose verdict the answer could change")
    doc, refusal = _materiality_doc(state, named_plan)
    if refusal:
        return Directive(False, state.node, "noop", refusal)
    if control and doc is not None:
        problem = controls.resolve_control(
            control, doc, grammars=controls.MATERIALITY_GRAMMARS)
        if problem:
            return Directive(
                False, state.node, "noop",
                f"--control names no control of this plan — {problem}",
                data={"control": control},
            )
    questions = premise.questions_from_dicts(bag.get("questions", []))
    questions = [q for q in questions if q.id != args.id]
    questions.append(premise.Question(id=args.id, target=args.target,
                                      question=args.question or "", control=control or ""))
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_raise", question=args.id, target=args.target)
    store.save(state)
    advisories = (
        _materiality_advisories(control, args.question or "", doc, state, runner)
        if control and doc is not None else []
    )
    return _with_advisories(Directive(
        True, state.node, "continue",
        f"question {args.id!r} raised (open) against {args.target!r}",
        data={"questions": [q.id for q in questions], "control": control or ""},
    ), advisories)


def cmd_question_research(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the own-research attempt for a question — a SEPARATE act from
    disposing, because 'research precedes escalation' is only evidenced by two
    independently-logged acts. Sets own_research; does not disposition. Writes no
    disposed_at_key (only dispose/rebind may)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    match.own_research = args.attempted
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_research", question=args.id)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"own research recorded for question {args.id!r}",
        data={"question": args.id},
    )


def cmd_question_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition a question: researched | escalated | assumed. Stamps
    `disposed_at_key` from the BOUND STAGE's current stage_question_key ("" for
    plan.goal / plan.done_criterion targets and when no plan exists yet) — PER
    ENTRY, at disposition time, on THIS entry only. There is NO bag-level
    plan_sha256 to restamp; v1's laundering defect was exactly such a shared field,
    so one add carried a whole stale bag onto a new plan version. A per-entry stamp
    written only here (and in cmd_question_rebind) has no such path by construction.

    DEVIATES from the permissive-add idiom for ONE case: refuses `--to escalated`
    with empty own_research, naming `question-research` as the route out. This is a
    UX fast-fail, NOT the authority — the authority is premise.validate_questions
    rule 5 at the plan_approval gate (both exist on purpose; the CLI refusal is a
    courtesy, the gate refusal is the control). Other required fields per
    disposition stay permissive here and are enforced only at the gate.

    `--plan` names the plan `disposed_at_key` is stamped against, defaulting to
    `state.plan_path` so every pre-existing invocation is byte-identical — see
    `_bound_stage_key`. An empty `--plan` is rejected rather than silently falling
    back, mirroring the (since retired) `question-enumerate --plan`'s `0fc6313` fix (#48(b))."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to disposition "
                         "against the session's own plan, or name a real one")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    if args.to == "escalated" and not match.own_research:
        return Directive(
            False, state.node, "noop",
            "refusing --to escalated: own research must precede escalation to the "
            "user — record it with `agentctl question-research --id "
            f"{args.id} --attempted ...` first",
        )
    match.disposition = args.to
    if args.answer:
        match.answer = args.answer
    if args.source:
        match.source = args.source
    if args.derivation:
        match.derivation = args.derivation
    if args.basis:
        match.basis = args.basis
    if args.risk:
        match.risk = args.risk
    match.disposed_at_key = _bound_stage_key(state, match, named_plan)
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_dispose", question=args.id, disposition=args.to)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"question {args.id!r} dispositioned as {args.to!r}",
        data={"question": args.id, "disposition": args.to,
              "disposed_at_key": match.disposed_at_key},
    )


def cmd_question_rebind(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Re-stamp a disposed question's `disposed_at_key` to its bound stage's CURRENT
    key, after the coordinator has RE-READ the question against the changed stage
    (--confirm-still-valid carries that reason). This is the reachable route out of
    blocker 12 (bound-stage-definition-changed); without it a refusal with no
    resolution path trains a bypass. Requires a non-empty reason (argparse-enforced).
    The second — and only other — writer of disposed_at_key.

    `--plan` names the plan the re-stamp is read against, defaulting to
    `state.plan_path` so every pre-existing invocation is byte-identical — see
    `_bound_stage_key`. It exists for the CORRECTED plan of a replan (not yet
    `state.plan_path`): `cmd_replan` evaluates the plan_approval gate against
    `args.plan`, so without a way to name it here, a rebind could only ever
    re-stamp the OLD plan's key and blocker 12 would keep firing against the
    corrected one — the very deadlock this verb exists to make unreachable
    (#48(b), the enumeration channel's `752969e`/`0fc6313` fix mirrored here). An
    empty `--plan` is rejected rather than silently falling back."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to rebind "
                         "against the session's own plan, or name a real one")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    match.disposed_at_key = _bound_stage_key(state, match, named_plan)
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_rebind", question=args.id, reason=args.confirm_still_valid)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"question {args.id!r} rebound to its stage's current key",
        data={"question": args.id, "disposed_at_key": match.disposed_at_key},
    )


def cmd_question_retire(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Retire a question whose target stage no longer exists — the route out of the
    dangling-edge blocker (rule 2). Sets disposition='retired' + reason; writes NO
    disposed_at_key (retired is not key-bound — it has walked away from the target)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    match.disposition = "retired"
    match.reason = args.reason
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_retire", question=args.id)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"question {args.id!r} retired",
        data={"question": args.id},
    )


def cmd_question_list(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only render of the question bag. `--format md` is the THINKER'S read
    surface (state is canonical and so invisible to a reviewer who reads only the
    plan): a markdown table of target | control | question | disposition |
    own_research | source | derivation. A PROJECTION, exactly like the plan-render —
    never a second source of truth. Does not mutate state.

    `control` is rendered because it is the column a reviewer can DISAGREE with: it
    claims which of the plan's own controls the answer moves, and the engine only
    checked that the control exists."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    def _cell(v: str) -> str:
        return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")

    if getattr(args, "format", None) == "md":
        rows = [
            "| target | control | question | disposition | own_research | source | derivation |",
            "|---|---|---|---|---|---|---|",
        ]
        for q in questions:
            disp = q.disposition + (f" — {q.stale_note}" if q.stale_note else "")
            rows.append(
                f"| {_cell(q.target)} | {_cell(q.control)} | {_cell(q.question)} | {_cell(disp)} | "
                f"{_cell(q.own_research)} | {_cell(q.source)} | {_cell(q.derivation)} |"
            )
        detail = "\n".join(rows)
    else:
        detail = "; ".join(
            f"{q.id}={q.disposition}" + (" [stale]" if q.stale_note else "")
            for q in questions
        ) or "no questions"
    unresolved = [
        c for c in premise.question_candidates_from_dicts(bag.get("candidates", []))
        if c.disposition == "raised"
    ]
    if unresolved:
        detail += "\ncandidates (undispositioned):\n" + "\n".join(
            f"{c.id}: {c.question or c.statement}" for c in unresolved)
    return Directive(
        True, state.node, "inspect", detail,
        data={"questions": premise.questions_to_dicts(questions),
              "candidates": premise.question_candidates_to_dicts(unresolved)},
    )


def cmd_order_raise(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record (or re-declare) one element of the ORDER the plan answers. Permissive
    UPSERT by --id exactly like question-raise, last write wins — re-raising resets
    the element to 'raised'. state.log stamps the act, and that timestamp is what
    evidences enumeration-before-plan: an element first raised after submit-plan was
    read off the plan, not off the order."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    elements = [e for e in elements if e.id != args.id]
    elements.append(premise.OrderElement(id=args.id, element=args.element))
    bag["order_elements"] = premise.order_elements_to_dicts(elements)
    state.log("order_raise", element=args.id)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"order element {args.id!r} raised (undispositioned)",
        data={"order_elements": [e.id for e in elements]},
    )


def cmd_order_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition one order element: covered (by --stage) or cut (with --reason).
    Fast-fails on the missing field for the named disposition — a courtesy, NOT the
    authority: premise.validate_order_elements at the plan_approval gate is the
    control, and it re-checks both (the dangling-stage case is only decidable there,
    against the plan actually submitted)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    if args.as_ == "covered" and args.stage is None:
        return Directive(False, state.node, "noop", "--stage is required for --as covered")
    if args.as_ == "cut" and not args.reason:
        return Directive(False, state.node, "noop", "--reason is required for --as cut")
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    match = next((e for e in elements if e.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such order element {args.id!r}")
    match.disposition = args.as_
    match.stage = args.stage if args.as_ == "covered" else None
    match.reason = args.reason if args.as_ == "cut" else ""
    match.content_digest = (
        _bound_order_stage_key(state, match, plan_path=getattr(args, "plan", None))
        if args.as_ == "covered" else ""
    )
    match.stale_note = ""
    bag["order_elements"] = premise.order_elements_to_dicts(elements)
    state.log("order_dispose", element=args.id, disposition=args.as_)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"order element {args.id!r} dispositioned as {args.as_!r}",
        data={"element": args.id, "disposition": args.as_, "stage": match.stage},
    )


def cmd_order_list(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only render of the order bag. `--format md` IS the gate's own block,
    via the same plugins_premise.coverage_block the essence check re-derives — so
    the coordinator pastes what the gate will check rather than composing a second
    rendering of its own. Calling render_coverage_block directly here would be that
    second rendering: it would silently omit the live risk acceptances the gate
    demands, and the pasted essence would be rejected for lines this command never
    showed. A PROJECTION, never a source of truth. Does not mutate."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    plan_path = getattr(state, "plan_path", None)
    stage_count = len(load_plan(plan_path).stages) if plan_path else 0
    if getattr(args, "format", None) == "md":
        detail = plugins_premise.coverage_block(state, bag) or premise.render_coverage_block(
            elements, stage_count)
    else:
        detail = "; ".join(
            f"{e.id}={e.disposition}" + (" [stale]" if e.stale_note else "")
            for e in elements
        ) or "no order elements"
    return Directive(
        True, state.node, "inspect", detail,
        data={"order_elements": premise.order_elements_to_dicts(elements),
              "stage_count": stage_count},
    )


def cmd_question_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: report the active premise bag's blockers via the SAME
    plugins_premise.premise_blockers the plan_approval gate uses, so check and gate
    never diverge (the ledger-check precedent). Green (ok=True) iff every raised
    question is closed, every candidate — a `qrev-` one a review act recorded or a
    legacy `qenum-` one — dispositioned, and every element of the order covered or
    cut. Does not mutate."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    blockers = plugins_premise.premise_blockers(state, bag)
    detail = "questions closed" if not blockers else f"questions not closed: {'; '.join(blockers)}"
    return Directive(not blockers, state.node, "inspect", detail, data={"blockers": blockers})


def cmd_question_candidate_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition one question candidate (bag['candidates'], written by a review
    act's `plan-review --customer-question` as `qrev-…`, or by the retired
    question-enumerate as `qenum-…`): 'recorded' (linked to an existing Question via
    --question) or 'dismissed' (with --reason). Mirrors cmd_ledger_dispose's
    shape but operates on the premise bag's OWN candidate store, never
    bag['questions'] — the two stores have different entry shapes and
    disposition referents (a QuestionCandidate points at a Question id, not a
    ledger claim), so this is a dedicated verb rather than an overload of
    question-dispose. Refuses early when --question for --as recorded is
    missing or does not resolve to an existing bag['questions'] entry —
    premise.validate_question_candidates enforces the same resolvability at the
    plan_approval gate; this command owns the CLI-level fast-fail."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    candidates = premise.question_candidates_from_dicts(bag.get("candidates", []))
    match = next((c for c in candidates if c.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such candidate {args.id!r}")
    if args.as_ == "recorded":
        if not args.question:
            return Directive(False, state.node, "noop", "--question is required for --as recorded")
        questions = premise.questions_from_dicts(bag.get("questions", []))
        if not any(q.id == args.question for q in questions):
            return Directive(
                False, state.node, "noop",
                f"--question {args.question!r} does not resolve to an existing "
                "question (dangling edge)",
            )
    if args.as_ == "dismissed" and not args.reason:
        return Directive(False, state.node, "noop", "--reason is required for --as dismissed")
    match.disposition = args.as_
    match.reason = args.reason or ""
    match.question = args.question or ""
    bag["candidates"] = premise.question_candidates_to_dicts(candidates)
    content_hash = premise.dismissal_hash(match.statement)
    if args.as_ == "dismissed":
        # A genuine COORDINATOR dismissal — the only source dismissed_hashes has
        # (the engine's own automatic immaterial dismissals and out-of-scope
        # listings never write here; see plugins_premise.py's state_factory).
        premise.record_dismissed_hash(
            bag.setdefault("dismissed_hashes", {}), content_hash,
            reason=match.reason, from_id=match.id, target=match.target)
    else:
        # The latest ruling countermands any earlier dismissal of this exact
        # (text, target) — a later pass must not keep silently carrying it.
        premise.forget_dismissed_hash(bag.get("dismissed_hashes") or {}, content_hash, match.target)
    state.log("question_candidate_dispose", candidate=args.id, disposition=args.as_)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"candidate {args.id!r} dispositioned as {args.as_!r}",
        data={"candidate": args.id, "disposition": args.as_},
    )


_LEGACY_ENUMERATION_ID = re.compile(r"^qenum-\d+$")


def _enumeration_part(target: str) -> str:
    """Which part of the plan a raised pair belongs to. A target that does not parse
    as a stage address belongs to the plan-level part — including a malformed one,
    which is the safe direction: `meta` is covered by every whole-plan pass, so a
    question the advisor addressed badly is still raised somewhere rather than
    dropped."""
    parsed = premise.parse_target(target)
    if parsed is not None and parsed[0] == "stage":
        return stage_part(parsed[1])
    return META_PART


def _candidate_immateriality(target: str, doc) -> str:
    """The reason to record an enumerated candidate as already dismissed, "" to
    raise it. A candidate the engine can see is addressed to no control of this
    plan is not a question the coordinator has to sit down and disposition: it
    cannot move any verdict this plan will reach.

    Only a STAGE target carries a derivable control — that stage's own
    done_criterion. A plan-level or unparseable target is raised, the same safe
    direction `_enumeration_part` takes on the same input and for the same reason:
    a badly-addressed question is still a question, and dismissing it on the
    strength of an address WE could not parse would discard it silently."""
    parsed = premise.parse_target(target)
    if parsed is None or parsed[0] != "stage":
        return ""
    control = f"stage {parsed[1]} done_criterion"
    unresolved = controls.resolve_control(
        control, doc, grammars=controls.MATERIALITY_GRAMMARS)
    return premise.CANDIDATE_IMMATERIAL if unresolved else ""


def _inherit_disposition(existing: dict, entry: dict, preserve: bool) -> dict:
    # Matched on the statement text, never the (coarser) target: two passes can
    # both address "goal" (same target, same id-slot) with different wording, and
    # only the statement tells them apart. `statement` is populated on every
    # candidate dict regardless of whether "target" is present, so there is no
    # legacy row this would fail to match — see _apply_enumeration_result's own
    # docstring ("Preservation is keyed on the statement being IDENTICAL, not on
    # the id alone").
    match = preserve and existing.get("statement") == entry.get("statement")
    if match and existing.get("disposition") != "raised":
        return dict(existing)
    return entry


def _unread_part_slot(candidates: list, entry: dict) -> str:
    """The id a plan-level pair from a pass that did NOT read the meta part lands
    under. Such a pass has no standing to renumber the meta part, so it must not
    take over a `qenum-meta-N` slot holding a different question: the row whose
    statement is identical keeps its own id (an in-place upsert that the caller runs
    with preserve_disposition, so its recorded/dismissed disposition survives), and a
    new statement gets the next unused `qenum-meta-N`, leaving every existing meta
    candidate — and the disposition recorded against it — exactly as it stands."""
    for existing in candidates:
        if existing.get("statement") == entry["statement"]:
            return existing["id"]
    used = {c.get("id") for c in candidates}
    n = 1
    while f"qenum-{META_PART}-{n}" in used:
        n += 1
    return f"qenum-{META_PART}-{n}"


def _upsert_candidate(
    candidates: list, entry: dict, *, preserve_disposition: bool, carry: dict | None = None,
) -> str:
    """Upsert `entry` into `candidates`, returning the id it actually lands under.

    `carry` is a dismissed_hashes record ({reason, from_id, target}) when `entry`'s
    own (statement hash, target) matches one on file — the SAME-TARGET requirement
    is what makes this a silent carry rather than a mere hint (see
    `premise.dismissed_hash_lookup`); checked by the caller, not here, since the
    caller already holds the bag's dismissed_hashes map. When present it takes
    priority over the ordinary id/legacy-id matching below and writes `entry` as
    'dismissed' with a carried reason, by a 3-tier resolution: (1) an existing row
    at entry['id'] with the SAME statement — an ordinary in-place update, unless
    it is already 'recorded' (a recorded disposition is never converted by a
    carry); (2) failing that, any existing DISMISSED row addressed to the SAME
    target whose own statement hash matches — reused in place, under ITS id, so
    the carry never overwrites an OPEN candidate merely because the two happen to
    share a part/index slot, and never reaches across to a same-text row
    addressed to a DIFFERENT target (that cross-target case is a hint, not a
    carry, and never reaches this function at all); (3) failing both, a fresh row
    under a non-colliding id. This is what keeps the invariant "a carried entry
    never overwrites an open candidate whose text differs, nor a dismissed row
    addressed to a different target" — the ordinary same-id branch below would
    happily overwrite an unrelated open row at the same id, which is exactly the
    case tier (2)/(3) exist to avoid."""
    if carry is not None:
        carried_entry = {
            **entry,
            "disposition": "dismissed",
            "reason": f"carried: {carry.get('reason', '')} (from {carry.get('from_id', '')})",
        }
        for j, existing in enumerate(candidates):
            if existing.get("id") == entry["id"] and existing.get("statement") == entry["statement"]:
                if existing.get("disposition") == "recorded":
                    return existing["id"]
                candidates[j] = carried_entry
                return entry["id"]
        target_hash = premise.dismissal_hash(entry["statement"])
        for j, existing in enumerate(candidates):
            if (existing.get("disposition") == "dismissed"
                    and existing.get("target") == entry.get("target")
                    and premise.dismissal_hash(existing.get("statement", "")) == target_hash):
                candidates[j] = {**carried_entry, "id": existing["id"]}
                return existing["id"]
        existing_ids = {c.get("id") for c in candidates}
        fresh_id = entry["id"]
        suffix = 2
        while fresh_id in existing_ids:
            fresh_id = f"{entry['id']}-carry{suffix}"
            suffix += 1
        candidates.append({**carried_entry, "id": fresh_id})
        return fresh_id

    for j, existing in enumerate(candidates):
        if existing.get("id") == entry["id"]:
            candidates[j] = _inherit_disposition(existing, entry, preserve_disposition)
            return entry["id"]
    # A candidate raised under the pre-part id scheme is the SAME question when its
    # statement is identical, so it is taken over rather than left standing beside its
    # own successor — otherwise a session carried across the change meets both, and the
    # disposition it already recorded protects neither.
    for j, existing in enumerate(candidates):
        if (_LEGACY_ENUMERATION_ID.match(existing.get("id") or "")
                and existing.get("statement") == entry["statement"]):
            taken_over = _inherit_disposition(existing, entry, preserve_disposition)
            candidates[j] = {**taken_over, "id": entry["id"]}
            return entry["id"]
    candidates.append(entry)
    return entry["id"]


def _target_stage_index(target: str) -> int | None:
    """The stage index a target addresses, or None for a plan-level/unparseable one —
    the same parse _enumeration_part and _candidate_immateriality already run, named
    for the one extra caller that needs just the index (the out-of-scope check)."""
    parsed = premise.parse_target(target)
    if parsed is not None and parsed[0] == "stage":
        return parsed[1]
    return None


@dataclass
class EnumerationApplyResult:
    """`_apply_enumeration_result`'s return value: the candidate ids it upserted as
    'raised' or freshly 'dismissed' (`raised`), the stage-addressed pairs a narrowed
    pass declined to disposition because their stage was outside its scope
    (`out_of_scope`, each `{"target", "question"}`), the ids a (text, target) match
    silently carried forward from an earlier coordinator dismissal of that SAME
    target instead of freshly raising (`carried`, a subset of `raised` since a
    carried candidate still lands in bag['candidates']), and the ids left OPEN but
    annotated with a `dismissal_hint_note` because their text matches an earlier
    dismissal recorded against a DIFFERENT (or unknown/legacy) target — a pointer
    for the coordinator to weigh, never a silent disposition (`carried_hint`, a
    subset of `raised`, disjoint from `carried`)."""
    raised: list[str]
    out_of_scope: list[dict]
    carried: list[str]
    carried_hint: list[str]


def _apply_enumeration_result(
    bag: dict, doc: PlanDoc, plan_path, pairs: list[tuple[str, str]], runner_ok: bool | None,
    *, parts: tuple[bool, set[int]] | None = None,
    preserve_disposition: bool = False, stderr: str = "",
    honor_dismissed_hashes: bool = True,
) -> EnumerationApplyResult:
    """RETIRED; kept only as the legacy `qenum-` upsert (below).

    Upsert `pairs` as legacy `qenum-<part>-N` QuestionCandidates (last-wins by a
    deterministic id) — 'raised', except that a pair the engine can see is addressed to
    no control of this plan is written 'dismissed' with the one countable immateriality
    reason (see `_candidate_immateriality`) — and stamp the bag's enumerated/
    enumerated_at/enumerated_plan/enumerated_runner_ok/enumerated_runner_stderr/
    enumerated_count fields from ONE enumeration pass's result.

    No engine path calls this for a new plan: the standalone enumeration is retired
    (amendments-2.md E3) and review questions are recorded as `qrev-` candidates by
    `_record_review_questions`. It stays as the `qenum-` upsert for bags written by an
    earlier engine and for the suites that pin that upsert's carry/preserve semantics;
    the immateriality auto-dismiss applies to `qenum-` candidates only.

    `parts` is what the pass read — `(whole_plan, {stage indices})`, None for a
    whole-plan pass. Only those parts' candidate ids are renumbered; another part's
    candidates, and the dispositions recorded against them, are left exactly as they
    stand. On a narrowed pass, a pair whose target parses to a STAGE outside
    `stage_scope` is not upserted at all — the pass never read that stage — and is
    listed in `EnumerationApplyResult.out_of_scope`. A plan-level/unparseable target is
    exempt and still upserted, but, the meta part being unread, into a slot that never
    displaces a different existing meta question (`_unread_part_slot`).

    `preserve_disposition` keeps a recorded `dismissed`+reason or `recorded`+question
    link across a re-upsert, keyed on the statement being IDENTICAL, not on the id
    alone: `qenum-s1-3` of a later pass is a different question than `qenum-s1-3` of an
    earlier one unless its text says otherwise.

    `honor_dismissed_hashes` (default True): a candidate whose (text, target) matches a
    bag['dismissed_hashes'] record is carried forward dismissed under the ORIGINAL
    reason (see _upsert_candidate) rather than freshly raised. False skips that carry
    for THIS pass AND forgets the matching record via `premise.forget_dismissed_hash`.
    A same-hash record filed against a DIFFERENT (or unknown/legacy) target is never a
    carry candidate — it surfaces as a hint (`premise.dismissal_hint_note`) so the
    coordinator sees the pointer without an unrelated stage silently reusing someone
    else's ruling."""
    live_stages = plan_stage_digests(doc)
    meta_covered, stage_scope = parts if parts is not None else (True, set(live_stages))
    all_dismissed_hashes = bag.get("dismissed_hashes") or {}

    out_of_scope: list[dict] = []
    by_part: dict[str, list[tuple[str, str]]] = {}
    for target, question in pairs:
        if not meta_covered:
            stage_index = _target_stage_index(target)
            if stage_index is not None and stage_index not in stage_scope:
                out_of_scope.append({"target": target, "question": question,
                                    "reason": premise.CANDIDATE_OUT_OF_EDIT_SCOPE})
                continue
        by_part.setdefault(_enumeration_part(target), []).append((target, question))

    candidates = bag.setdefault("candidates", [])
    raised: list[str] = []
    carried: list[str] = []
    carried_hint: list[str] = []
    for part, part_pairs in by_part.items():
        for i, (target, question) in enumerate(part_pairs):
            immaterial = _candidate_immateriality(target, doc)
            entry = {"id": f"qenum-{part}-{i + 1}",
                     "statement": f"[{target}] {question}",
                     "disposition": "dismissed" if immaterial else "raised",
                     "reason": immaterial, "question": "", "target": target}
            carry = hint = None
            if not immaterial and all_dismissed_hashes:
                content_hash = premise.dismissal_hash(entry["statement"])
                found_carry, hint = premise.dismissed_hash_lookup(
                    all_dismissed_hashes, content_hash, target)
                if found_carry is not None:
                    if honor_dismissed_hashes:
                        carry = found_carry
                    else:
                        premise.forget_dismissed_hash(
                            bag.setdefault("dismissed_hashes", {}), content_hash, target)
            if hint is not None and carry is None:
                entry["reason"] = premise.dismissal_hint_note(hint)
            unread_meta = carry is None and part == META_PART and not meta_covered
            if unread_meta:
                entry["id"] = _unread_part_slot(candidates, entry)
            final_id = _upsert_candidate(
                candidates, entry,
                preserve_disposition=preserve_disposition or (unread_meta and honor_dismissed_hashes),
                carry=carry)
            raised.append(final_id)
            if carry is not None:
                carried.append(final_id)
            elif hint is not None:
                carried_hint.append(final_id)

    bag["enumerated"] = True
    bag["enumerated_at"] = plugins_premise._plan_content_digest(doc)
    bag["enumerated_plan"] = str(plan_path)
    bag["enumerated_runner_ok"] = runner_ok
    bag["enumerated_runner_stderr"] = stderr
    bag["enumerated_count"] = len(pairs)
    bag["enumerate_pass"] = int(bag.get("enumerate_pass") or 0) + 1
    return EnumerationApplyResult(
        raised=raised, out_of_scope=out_of_scope, carried=carried, carried_hint=carried_hint)


cmd_question_enumerate = cmd_question_enumerate_worker = cmd_question_enumerate_escape = (
    cmd_question_enumerate_retired)


def register(sub, add):
    sp = add("question-raise"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id; re-raising an existing id resets it to open")
    sp.add_argument("--target", required=True,
                    help="the plan element the question arose against: plan.goal, "
                         "plan.done_criterion, or stage:<n>.<element>")
    sp.add_argument("--question", default="", help="the question text")
    sp.add_argument("--control", required=True,
                    help="the control of this plan whose verdict the answer could flip: "
                         "'stage <n> verify_command', 'stage <n> done_criterion', "
                         "'stage <n> landed assertion', 'final_check <n>', or "
                         "'order requirement <id>'. Refused when it names nothing this "
                         "plan contains")
    sp.add_argument("--plan", default=None,
                    help="resolve --control against this plan instead of the session's "
                         "current plan_path (use when raising against a CORRECTED plan)")

    sp = add("question-research"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id to attach the research attempt to")
    sp.add_argument("--attempted", required=True, help="the own-research attempt (must precede escalation)")

    sp = add("question-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id to disposition")
    sp.add_argument("--to", dest="to", required=True, choices=["researched", "escalated", "assumed"])
    sp.add_argument("--answer", default="", help="the resolved answer (researched/escalated)")
    sp.add_argument("--source", default="", help="provenance of the answer (researched)")
    sp.add_argument("--derivation", default="", help="how the answer follows from the source (researched)")
    sp.add_argument("--basis", default="", help="the ground the assumption rests on (assumed)")
    sp.add_argument("--risk", default="", help="what breaks if the assumption is wrong (assumed)")
    sp.add_argument("--plan", default=None,
                    help="stamp disposed_at_key against this plan instead of the session's "
                         "current plan_path (use when disposing against a CORRECTED plan)")

    sp = add("question-rebind"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="disposed question id to re-stamp against its stage's current key")
    sp.add_argument("--confirm-still-valid", dest="confirm_still_valid", required=True,
                    help="why the disposition still holds against the changed stage (non-empty)")
    sp.add_argument("--plan", default=None,
                    help="re-stamp disposed_at_key against this plan instead of the session's "
                         "current plan_path (use when preparing a CORRECTED plan for replan)")

    sp = add("question-retire"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id whose target stage no longer exists")
    sp.add_argument("--reason", required=True, help="why the question is retired")

    sp = add("question-list"); sp.add_argument("--session", required=True)
    sp.add_argument("--format", dest="format", default="", choices=["", "md"],
                    help="md renders the thinker's markdown table; default is a compact one-liner")

    sp = add("question-check"); sp.add_argument("--session", required=True)
    _retired = ("Retired — exits 2 and changes nothing; customer questions now come from "
                "plan-review --customer-question. The flags below describe the route as it ran.")
    sp = add("question-enumerate", help="Retired — exits 2", description=_retired)
    sp.add_argument("--session", required=True)
    sp.add_argument("--plan", default=None,
                    help="enumerate against this plan instead of the session's current "
                         "plan_path (use when preparing a CORRECTED plan for replan)")
    sp.add_argument("--reopen-dismissed", action="store_true",
                    help="re-raise a candidate whose text was previously carried forward as "
                         "dismissed (the old fresh-pass behaviour), instead of carrying it "
                         "forward again under its recorded reason")

    sp = add("question-enumerate-escape", help="Retired — exits 2", description=_retired)
    sp.add_argument("--session", required=True)
    sp.add_argument("--reason", required=True, choices=list(premise.ENUMERATION_ESCAPE_REASONS),
                    help="why the mandatory enumeration cross-check is being discharged "
                         "without a healthy run — a closed set, so the escapes aggregate")
    sp.add_argument("--note", required=True,
                    help="what actually happened, for the reader of one row (the reason "
                         "token is what counts across rows)")
    sp.add_argument("--plan", default=None,
                    help="escape against this plan instead of the session's current "
                         "plan_path (use when preparing a CORRECTED plan for replan)")

    sp = add("question-enumerate-worker", help="Retired — exits 2", description=_retired)
    sp.add_argument("--session", required=True)
    sp.add_argument("--plan", required=True,
                    help="plan file to enumerate — this detached worker never touches "
                         "session state, so the launcher names it explicitly")
    sp.add_argument("--digest", required=True,
                    help="plan content digest the launcher computed (plugins_premise."
                         "_plan_content_digest) — the sidecar write's key")
    sp.add_argument("--stages", default="",
                    help="comma-separated stage indices to read instead of the whole "
                         "plan, when only those stages moved; omit for the whole plan")

    sp = add("question-candidate-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True,
                    help="candidate id (qenum-<part>-N) to disposition")
    sp.add_argument("--as", dest="as_", required=True, choices=["recorded", "dismissed"])
    sp.add_argument("--reason", default="", help="required when --as dismissed")
    sp.add_argument("--question", default="",
                    help="question id this candidate resolves to, required when --as recorded")

    sp = add("order-raise"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True,
                    help="order-element id; re-raising an existing id resets it to undispositioned")
    sp.add_argument("--element", required=True,
                    help="one element of the order, enumerated FROM THE TEXT OF THE ORDER "
                         "and BEFORE submit-plan — elements read off an already-written "
                         "plan make coverage trivially total and prove nothing")

    sp = add("order-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="order-element id to disposition")
    sp.add_argument("--as", dest="as_", required=True, choices=["covered", "cut"])
    sp.add_argument("--stage", type=int, default=None,
                    help="the stage that covers this element, required when --as covered")
    sp.add_argument("--reason", default="", help="why the element is cut, required when --as cut")
    sp.add_argument("--plan", default=None,
                    help="stamp content_digest against this plan instead of the session's "
                         "current plan_path (use when re-covering against a CORRECTED plan)")

    sp = add("order-list"); sp.add_argument("--session", required=True)
    sp.add_argument("--format", dest="format", default="", choices=["", "md"],
                    help="md renders the scope-coverage block the essence must carry; "
                         "default is a compact one-liner")
