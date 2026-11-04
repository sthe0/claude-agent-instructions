"""Session lifecycle, plugin and claim-ledger subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

from pathlib import Path
from lib import config_root
from . import (
    advisor,
    delivery,
    gates,
    ledger,
    plugins,
    plugins_ledger,
    runtime_host,
    task_accumulator,
)
from .directive import Directive
from .dispatch import Runner
from .plan import PlanError, load_plan
from .render import build_site_prompt
from .state import CriterionType, Node, SessionState
from .store import StateStore
from .cli_common import (
    _flush_order_effort,
    _adopt_parent_difficulty,
    _adopt_norm_handoff,
    _require,
    _cost_rows,
    _refresh_spend,
    _utcnow,
)


def cmd_start(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    if getattr(args, "if_absent", False):
        existing = store.load(args.session)
        if existing is not None and existing.node != Node.RESOLVED.value:
            return Directive(
                True, existing.node, "continue",
                f"session live (task={existing.task_id}, node={existing.node}); start is a no-op",
            )
    state = SessionState(
        session_id=args.session,
        task_id=args.task,
        goal=getattr(args, "goal", "") or "",
        overall_done_criterion=getattr(args, "done_criterion", "") or "",
        overall_criterion_type=getattr(args, "criterion_type", CriterionType.MEASURABLE.value),
        recursion_depth=int(getattr(args, "recursion_depth", 0) or 0),
    )
    runtime_host.bind_runtime_host(state, getattr(args, "host", None), require=False)
    _adopt_parent_difficulty(state)
    _adopt_norm_handoff(state, store)
    state.log("start", task=state.task_id)
    store.save(state)
    return Directive(True, state.node, "classify", "session registered; run classify next")


def cmd_reset(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Re-arm a session for a NEW task once its prior task is closed. Refuses to
    discard a live prior task (not RESOLVED/ROUTED/BLOCKED) unless --force, so a
    new prompt cannot silently wipe in-flight work. Otherwise builds a fresh
    CLASSIFIED SessionState from the same args cmd_start uses.

    Second refusal, for the opposite case: re-entering a task that already reached
    RESOLVED. RESOLVED has no outgoing edge but `pop_subplan`, so this command is the
    only way back into a closed order — and the fresh SessionState it builds zeroes the
    effort baseline, the replan count and every round-release counter, so an unbounded
    reopen loop pays nothing and nobody is ever asked whether the order still stands.
    `gates.resolved_reentry_blockers` requires a recorded reason, and past
    `effort-replan-absolute` reopens a recorded user decision; the count it reads lives
    in the cross-session accumulator precisely because this command discards state."""
    prior = store.load(args.session)
    if (
        prior is not None
        and prior.node not in (Node.RESOLVED.value, Node.ROUTED.value, Node.BLOCKED.value)
        and not getattr(args, "force", False)
    ):
        return Directive(
            False, prior.node, "noop",
            f"prior task '{prior.task_id}' is live at node={prior.node}; "
            "resolve/block it or pass --force to discard",
        )
    reopen_reason = (getattr(args, "reopen_reason", None) or "").strip()
    reopen_decision = (getattr(args, "reopen_user_decision", None) or "").strip()
    reopening = prior is not None and prior.node == Node.RESOLVED.value and prior.task_id == args.task
    reentry_count = (
        int(task_accumulator.get(prior.task_id)["per_axis_totals"].get("resolved_reentry", 0) or 0)
        if reopening else 0
    )
    reentry_blockers = gates.resolved_reentry_blockers(
        prior.node if prior is not None else None,
        task_id=args.task,
        same_task=reopening,
        reopen_count=reentry_count,
        reason=reopen_reason,
        user_decision=reopen_decision,
    )
    if reentry_blockers:
        return Directive(
            False, prior.node, "noop", "; ".join(reentry_blockers),
            data={"blockers": reentry_blockers, "resolved_reentry_count": reentry_count},
        )
    if prior is not None:
        _refresh_spend(prior, store, _cost_rows(args), prior.plan_path)
        _flush_order_effort(prior, store=store)
    new = SessionState(
        session_id=args.session,
        task_id=args.task,
        goal=getattr(args, "goal", "") or "",
        overall_done_criterion=getattr(args, "done_criterion", "") or "",
        overall_criterion_type=getattr(args, "criterion_type", CriterionType.MEASURABLE.value),
        recursion_depth=int(getattr(args, "recursion_depth", 0) or 0),
    )
    runtime_host.bind_runtime_host(new, getattr(args, "host", None), require=False)
    _adopt_parent_difficulty(new)
    if (
        new.parent_difficulty is None and prior is not None and prior.parent_difficulty
        and prior.task_id == new.task_id
    ):
        # The session was spawned under that parent difficulty; a later `reset` of the
        # same task runs without the spawn's env and must not turn it into a parentless
        # root. A reset to a different task starts unrelated work, which must not stay
        # nested under a parent record that may long since have closed.
        new.parent_difficulty = dict(prior.parent_difficulty)
    if prior is not None and prior.parent_link is not None:
        # Spawn lineage belongs to the session, not to one task: a reset inside the same
        # child process stays linked to the stage that spawned it.
        new.parent_link = dict(prior.parent_link)
        new.recursion_depth = new.parent_link["depth"]
    _adopt_norm_handoff(new, store)
    new.log("reset", task=new.task_id, prior_task=(prior.task_id if prior else None))
    if reopening:
        # Counted in the accumulator, which survives this command; logged on the new
        # state, which is what a reader of THIS task's history will open. Neither
        # substitutes for the other: the log carries the reason, the accumulator
        # carries the count the ceiling compares.
        task_accumulator.add(
            new.task_id, "resolved_reentry", 1,
            session_id=new.session_id, now=_utcnow(),
        )
        new.log(
            "resolved_reentry", reason=reopen_reason,
            user_decision=reopen_decision or None, prior_reopens=reentry_count,
        )
    store.save(new)
    # Hygiene, not a security boundary (delivery.delete_stamp's own docstring):
    # a stale stamp cannot silently clear a later task's gate since the gate
    # binds on plan_sha256/rendering_sha256 and a reset task starts with no
    # plan, but leaving the sidecar around would confuse status/debugging
    # output with a stamp for a task that no longer exists.
    state_file = config_root.resolve_agentctl_state_file(args.session)
    if state_file is not None:
        delivery.delete_stamp(state_file)
    return Directive(
        True, new.node, "classify",
        (f"resolved task re-opened (reopen #{reentry_count + 1}); run classify"
         if reopening else "session re-armed for new task; run classify"),
        data={"resolved_reentry_count": reentry_count + 1} if reopening else {},
    )


def cmd_plugin_activate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Attach a registered plugin to THIS session (the per-session counterpart of
    import-time registration). The owning skill runs this on invocation. Plugin-
    specific kwargs (e.g. --tracker-key) are stashed in the seeded bag. Idempotent
    — safe to re-run on resume; merges new kwargs into the existing bag."""
    state = _require(store, args.session)
    name = args.plugin
    if name not in plugins.REGISTRY:
        return Directive(
            False, state.node, "noop",
            f"unknown plugin {name!r}; registered: {sorted(plugins.REGISTRY)}",
        )
    seed = dict(getattr(args, "seed", None) or {})
    if getattr(args, "tracker_key", None):
        seed["tracker_key"] = args.tracker_key
    plugins.activate(state, name, seed)
    state.log("plugin_activate", plugin=name)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"plugin {name!r} activated for this session",
        data={"active": sorted(state.plugins)},
    )


def cmd_plugin_deactivate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Manual retire (escape hatch: 'stop touching the tracker'). Engine-driven
    auto-retire (terminal) is the normal path; this is for a lapsed trigger or a
    user change of mind. Archives the bag for audit."""
    state = _require(store, args.session)
    ok = plugins.deactivate(state, args.plugin)
    state.log("plugin_deactivate", plugin=args.plugin, was_active=ok)
    store.save(state)
    detail = (
        f"plugin {args.plugin!r} deactivated (archived)" if ok
        else f"plugin {args.plugin!r} was not active"
    )
    return Directive(ok, state.node, "continue", detail, data={"active": sorted(state.plugins)})


def cmd_plugin_record(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record that a plugin-side publication actually happened: marks
    bag['published_phases'][key]=True. The coordinator runs this AFTER the
    comment lands, so a publish gate (e.g. tracker's) reflects a real post rather
    than an intention. Generic — any publish-style plugin shares the convention;
    it does NOT fire a plugin event (recording a publish must not re-trigger
    observers). No-op-with-error if the plugin is not active.

    `--stage <n>` composes the bag key as f"{phase}:{stage}" instead of the bare
    phase name — the tracker plugin's per-stage journal gate (_journal_gate)
    tests membership of "progress:<index>" for each PASSED stage that declared
    output_artifacts, so a per-stage progress entry must be distinguishable from
    every other stage's. Omitting --stage keeps the plain phase key, unchanged
    for every other caller of this command (plan/result/status/searched/…)."""
    state = _require(store, args.session)
    bag = state.plugins.get(args.plugin)
    if bag is None:
        return Directive(False, state.node, "noop", f"plugin {args.plugin!r} is not active")
    phase = args.phase
    stage = getattr(args, "stage", None)
    key = f"{phase}:{stage}" if stage is not None else phase
    note = getattr(args, "note", None)
    skipped = bool(getattr(args, "skipped", False))
    comment = (getattr(args, "comment", None) or "").strip()
    if comment and (args.plugin != "tracker" or phase != "progress" or skipped):
        return Directive(False, state.node, "noop",
                         "--comment names the tracker's rolling status comment: it goes only "
                         "with --plugin tracker --phase progress, and never with --skipped")
    if skipped:
        # An HONEST degrade: the transport for this phase was unavailable (e.g. the
        # tracker backend defines no tracker_publish_plan). Store a SKIP MARKER under
        # the phase key — the gate tests MEMBERSHIP, so the marker discharges a
        # mandatory phase without wedging resolution, while the reason stays visible
        # in the bag and the returned directive. Never a silent `recorded`.
        if not (note and note.strip()):
            return Directive(False, state.node, "noop",
                             "a skipped publication must carry a reason: pass --note")
        published = bag.setdefault("published_phases", {})
        published[key] = {"skipped": note}
        state.log("plugin_record", plugin=args.plugin, phase=phase, stage=stage, skipped=True)
        store.save(state)
        return Directive(
            True, state.node, "continue",
            f"plugin {args.plugin!r}: phase {key!r} publication skipped: {note}",
            data={"published_phases": sorted(published)},
        )
    if phase == "skipped" and not (note and note.strip()):
        return Directive(False, state.node, "noop", "a skip must carry a reason: pass --note")
    attachment = (getattr(args, "attachment", None) or "").strip()
    needs_attachment = args.plugin == "tracker" and phase == "plan"
    if needs_attachment and not attachment:
        return Directive(False, state.node, "noop",
                         "the tracker plan phase needs the plan TOML attached to the ticket: "
                         "pass --attachment <id|url>, or --skipped --note <why> if the "
                         "backend cannot attach")
    published = bag.setdefault("published_phases", {})
    if needs_attachment:
        published[key] = {"attachment": attachment}
    elif comment:
        # newest ref wins: a fallback new comment becomes the rolling one
        published[key] = {"comment": comment}
        bag["rolling_comment"] = comment
    else:
        published[key] = True
    # top-level bool-flag convention: a plugin whose bag seeds a bool keyed by the
    # phase name (e.g. experience: searched/recorded/skipped) reads those flags in
    # its gate; flip it true. Tracker's bag has no such keys, so it is untouched.
    # (A composite --stage key never collides with this convention's bare names.)
    if isinstance(bag.get(key), bool):
        bag[key] = True
    if note:
        if phase == "skipped":
            bag["skip_reason"] = note
        elif phase == "searched":
            bag["decision"] = note
    state.log("plugin_record", plugin=args.plugin, phase=phase, stage=stage)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"plugin {args.plugin!r}: phase {key!r} recorded as published",
        data={"published_phases": sorted(published)},
    )


def cmd_ledger_add(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Declare (or re-declare) one claim in the active ledger bag. Permissive by
    design — an ungrounded axiom/derivation/assumption is stored as-is; only the
    resolution gate (ledger.validate_ledger, via plugins_ledger._ledger_gate)
    rejects it. Grounding a claim is re-adding the same --id with --source/
    --premise/--basis filled in: UPSERT, last write wins. Fires no plugin event —
    recording a claim must not re-trigger the resolve nudge."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    claims = bag.setdefault("claims", [])
    entry = {
        "id": args.id,
        "status": args.status,
        "statement": getattr(args, "statement", "") or "",
        "source": getattr(args, "source", "") or "",
        "premises": list(getattr(args, "premises", None) or []),
        "basis": getattr(args, "basis", "") or "",
        "load_bearing": bool(getattr(args, "load_bearing", True)),
    }
    for i, c in enumerate(claims):
        if c.get("id") == entry["id"]:
            claims[i] = entry
            break
    else:
        claims.append(entry)
    state.log("ledger_add", claim=entry["id"], status=entry["status"])
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"claim {entry['id']!r} recorded as {entry['status']!r}",
        data={"claims": [c["id"] for c in claims]},
    )


def cmd_ledger_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: report the active ledger's blockers via the SAME
    plugins_ledger.ledger_blockers the resolution gate uses, so check and gate never
    diverge — claim closure (validate_ledger) AND candidate disposition-completeness
    (validate_candidates) AND the enumeration cross-check having RUN (bag
    ['enumerated']). Green (ok=True, no blockers) iff every load-bearing claim is
    grounded/derived/marked, every candidate is recorded/dismissed, and
    ledger-enumerate has run. Does not mutate state."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    blockers = plugins_ledger.ledger_blockers(bag)
    detail = "ledger closed" if not blockers else f"ledger not closed: {'; '.join(blockers)}"
    return Directive(not blockers, state.node, "inspect", detail, data={"blockers": blockers})


def cmd_ledger_candidate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Declare (or re-declare) one enumeration candidate — a load-bearing
    decision/judgment the coordinator (or ledger-enumerate's independent advisor
    pass, stage 5) has surfaced. Permissive by design, mirroring ledger-add:
    stored as 'raised' (undispositioned); only the resolution gate (ledger.
    validate_candidates) rejects a candidate left raised. UPSERT, last write
    wins — re-raising the same --id resets any prior disposition, so grounding a
    candidate is done via ledger-dispose, not by re-raising it. Fires no plugin
    event."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    candidates = bag.setdefault("candidates", [])
    entry = {
        "id": args.id,
        "statement": getattr(args, "statement", "") or "",
        "disposition": "raised",
        "reason": "",
        "claim": "",
    }
    for i, c in enumerate(candidates):
        if c.get("id") == entry["id"]:
            candidates[i] = entry
            break
    else:
        candidates.append(entry)
    state.log("ledger_candidate", candidate=entry["id"])
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"candidate {entry['id']!r} raised",
        data={"candidates": [c["id"] for c in candidates]},
    )


def cmd_ledger_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition one enumeration candidate: 'recorded' (linked to an existing
    load-bearing claim via --claim) or 'dismissed' (with --reason). Refuses early
    when the flag its own --as needs is missing, rather than landing a
    disposition the resolution gate (ledger.validate_candidates) would reject
    anyway — the gate still owns whether the --claim id is real/load-bearing,
    this command only owns whether the required flag was passed at all."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    candidates = bag.setdefault("candidates", [])
    match = next((c for c in candidates if c.get("id") == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such candidate {args.id!r}")
    if args.as_ == "recorded" and not args.claim:
        return Directive(False, state.node, "noop", "--claim is required for --as recorded")
    if args.as_ == "dismissed" and not args.reason:
        return Directive(False, state.node, "noop", "--reason is required for --as dismissed")
    match["disposition"] = args.as_
    match["reason"] = args.reason or ""
    match["claim"] = args.claim or ""
    state.log("ledger_dispose", candidate=args.id, disposition=args.as_)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"candidate {args.id!r} dispositioned as {args.as_!r}",
        data={"candidate": args.id, "disposition": args.as_},
    )


def cmd_ledger_enumerate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Run the independent semantic enumeration cross-check over an outgoing
    deliverable: read --artifact, ask an independent advisor pass (advisor.
    enumerate_claims, `claude -p --model sonnet`, cost-bounded) to RAISE the
    load-bearing decisions/judgments/claims it detects, UPSERT each as a 'raised'
    candidate (last-wins by a deterministic `enum-N` id), then flip bag
    ['enumerated']=True so the mandatory-cross-check blocker is discharged.

    It only RAISES — disposition stays the coordinator's act (ledger-dispose), so
    it is the disposition + enumerated blockers, not this call, that make the
    cross-check advisory-BLOCKING. A recall-widener, never authoritative: an empty
    enumeration still flips the flag (the pass RAN), and the residual missed claim
    is Layer B discipline's to catch. Fires no plugin event."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    try:
        text = Path(args.artifact).read_text(encoding="utf-8")
    except OSError as exc:
        return Directive(False, state.node, "noop",
                         f"cannot read artifact {args.artifact!r}: {exc}")
    run = runner if runner is not None else advisor.enumerate_subprocess_runner
    host = state.runtime_host or runtime_host.HOST_CLAUDE
    try:
        plan_stages = [s.index for s in load_plan(args.artifact).stages]
    except PlanError:
        statements = advisor.enumerate_claims(text, run, runtime_host=host)
    else:
        statements = []
        for stage in (None, *plan_stages):
            node = build_site_prompt("enumerate-node", args.artifact, stage=stage)
            statements.extend(advisor.enumerate_claims(node.text, run, runtime_host=host))
    candidates = bag.setdefault("candidates", [])
    raised: list[str] = []
    for i, statement in enumerate(statements):
        cid = f"enum-{i + 1}"
        entry = {"id": cid, "statement": statement, "disposition": "raised",
                 "reason": "", "claim": ""}
        for j, c in enumerate(candidates):
            if c.get("id") == cid:
                candidates[j] = entry
                break
        else:
            candidates.append(entry)
        raised.append(cid)
    bag["enumerated"] = True
    state.log("ledger_enumerate", raised=len(raised))
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"enumeration cross-check ran; raised {len(raised)} candidate(s) — "
        "disposition each with `agentctl ledger-dispose`",
        data={"raised": raised, "enumerated": True},
    )


def register(sub, add):
    sp = add("start"); sp.add_argument("--session", required=True); sp.add_argument("--task", required=True)
    sp.add_argument("--goal", default=""); sp.add_argument("--done-criterion", dest="done_criterion", default="")
    sp.add_argument("--criterion-type", dest="criterion_type", default=CriterionType.MEASURABLE.value)
    sp.add_argument("--recursion-depth", dest="recursion_depth", type=int, default=0)
    sp.add_argument("--if-absent", dest="if_absent", action="store_true")
    sp.add_argument("--host", choices=runtime_host.HOSTS, default=None,
                    help="coordination host this session dispatches through (claude|cursor); auto-detected when omitted (best-effort; classify is the hard gate)")

    sp = add("reset"); sp.add_argument("--session", required=True); sp.add_argument("--task", required=True)
    sp.add_argument("--goal", default=""); sp.add_argument("--done-criterion", dest="done_criterion", default="")
    sp.add_argument("--criterion-type", dest="criterion_type", default=CriterionType.MEASURABLE.value)
    sp.add_argument("--recursion-depth", dest="recursion_depth", type=int, default=0)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--reopen-reason", dest="reopen_reason", default="",
                    help="why a task that already RESOLVED is being re-entered — required by "
                         "gates.resolved_reentry_blockers, because reset is the only way back "
                         "into a closed order and it discards the effort baseline, the replan "
                         "count and every round-release counter on the way in")
    sp.add_argument("--reopen-user-decision", dest="reopen_user_decision", default="",
                    help="the user's answer to whether this order still warrants continuing. "
                         "Required IN ADDITION to --reopen-reason once the task has been "
                         "re-opened `effort-replan-absolute` times: past that count a reason "
                         "the coordinator wrote for itself is no longer enough, but it is "
                         "still owed — the reason says what is being re-opened, the decision "
                         "says who authorized re-opening it again")
    sp.add_argument("--host", choices=runtime_host.HOSTS, default=None,
                    help="coordination host this session dispatches through (claude|cursor); auto-detected when omitted (best-effort; classify is the hard gate)")

    sp = add("plugin-activate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True)
    sp.add_argument("--tracker-key", dest="tracker_key", default=None)
    sp = add("plugin-deactivate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True)
    sp = add("plugin-record"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True); sp.add_argument("--phase", required=True)
    sp.add_argument("--stage", type=int, default=None,
                    help="compose the published_phases key as '<phase>:<stage>' instead "
                         "of the bare phase name — used by the tracker plugin's per-stage "
                         "progress journal gate, e.g. --phase progress --stage 4")
    sp.add_argument("--note", default=None)
    sp.add_argument("--attachment", default=None,
                    help="id or URL of the ticket attachment carrying the plan TOML; "
                         "required for --plugin tracker --phase plan (unless --skipped)")
    sp.add_argument("--comment", default=None,
                    help="id or URL of the tracker's rolling status comment; only for "
                         "--plugin tracker --phase progress (not with --skipped)")
    sp.add_argument("--skipped", action="store_true",
                    help="record the phase as an honest SKIP (transport unavailable): "
                         "stores a marker+reason under the phase key so the gate is "
                         "discharged without a real post. Requires --note.")

    sp = add("ledger-add"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="claim id; re-adding an existing id upserts it")
    sp.add_argument("--status", required=True, choices=sorted(ledger.VALID_STATUSES))
    sp.add_argument("--statement", default="", help="the claim text")
    sp.add_argument("--source", default="", help="axiom provenance (sensor+window / ticket / file:line)")
    sp.add_argument("--premise", dest="premises", action="append", default=None,
                    help="a premise claim id this derivation rests on (repeatable)")
    sp.add_argument("--basis", default="", help="assumption ground")
    sp.add_argument("--load-bearing", dest="load_bearing", action="store_true", default=True)
    sp.add_argument("--not-load-bearing", dest="load_bearing", action="store_false")
    sp = add("ledger-check"); sp.add_argument("--session", required=True)

    sp = add("ledger-candidate"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="candidate id; re-adding an existing id re-raises it")
    sp.add_argument("--statement", default="", help="the decision/judgment statement")

    sp = add("ledger-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="candidate id to disposition")
    sp.add_argument("--as", dest="as_", required=True, choices=["recorded", "dismissed"])
    sp.add_argument("--reason", default="", help="required when --as dismissed")
    sp.add_argument("--claim", default="", help="grounding claim id, required when --as recorded")

    sp = add("ledger-enumerate"); sp.add_argument("--session", required=True)
    sp.add_argument("--artifact", required=True,
                    help="path to the outgoing deliverable to cross-check for load-bearing "
                         "decisions/judgments/claims (raised as candidates)")
