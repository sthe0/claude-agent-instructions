"""Difficulty declaration, investigation, critique and normalization subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

from . import gates, runtime_host
from .directive import Directive
from .dispatch import Runner
from .state import (
    Critique,
    Declaration,
    canonical_failure_address,
    canonical_normalization_destination,
    FAILURE_ADDRESS_ALIASES,
    FAILURE_ADDRESS_VALUES,
    Investigation,
    Node,
    Normalization,
    NORMALIZATION_DESTINATION_ALIASES,
    NORMALIZATION_DESTINATIONS,
    NORMALIZATION_LEVELS,
)
from .store import StateStore
from .cli_common import (
    NESTED_DIFFICULTY_ORIGINS,
    _open_difficulty,
    _difficulty_cap_directive,
    _plan_review_release_active,
    _sync_release_anchor,
    _log_gate,
    _attach_advisories,
    _require,
    _escalation_link,
    _require_diagnosing,
    _supersede_for_release,
    _snapshot_norm_targets,
    _normalize_record_fields,
    _log_normalize_event,
    _record_normalization_waiver,
    _cmd_normalize_outside_diagnosing,
    _resume_action,
)


def cmd_declare(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state, admit_release=True)
    if bad:
        return bad
    if _plan_review_release_active(state):
        _sync_release_anchor(state)
        _supersede_for_release(state)
    state.difficulty.declaration = Declaration(
        expected=args.expected, actual=args.actual, mismatch=args.mismatch
    )
    state.log("declare", expected=args.expected, actual=args.actual, mismatch=args.mismatch)
    store.save(state)
    return Directive(True, state.node, "investigate",
                     "declaration recorded; localize the divergence next (investigate)")


def cmd_investigate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state, admit_release=True)
    if bad:
        return bad
    if state.difficulty.declaration is None:
        return Directive(False, state.node, "declare",
                         "investigate is out of order: declare the divergence first")
    state.difficulty.investigation = Investigation(
        localized_expectation=args.localized_expectation,
        localized_actual=args.localized_actual,
        hypotheses=list(getattr(args, "hypotheses", None) or []),
    )
    state.log("investigate")
    store.save(state)
    return Directive(True, state.node, "critique",
                     "investigation recorded; state the functional ground + replanning task (critique)")


def cmd_critique(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state, admit_release=True)
    if bad:
        return bad
    if state.difficulty.declaration is None or state.difficulty.investigation is None:
        return Directive(False, state.node, "declare",
                         "critique is out of order: declaration and investigation must come first")
    # getattr-with-default: in-process Namespace callers (test_replan.py builds one by
    # hand with only the two required fields) must keep working — an absent field is None.
    failure_address = canonical_failure_address(getattr(args, "failure_address", None))
    # Reject a bogus value HERE (mirroring cmd_normalize's --level check) so an in-process
    # caller bypassing the argparse `choices` is caught before the Critique is built — this
    # (with argparse) is the ONLY validation of the value set. The closure gate
    # (failure_address_blockers) checks only non-None, so a bogus value must never reach a
    # persisted record; that also grandfathers a legacy OLD-value record on load. None is
    # allowed at critique time — the routing may be recorded now or left for a later replan,
    # where the closure gate demands it be present.
    if failure_address is not None and failure_address not in FAILURE_ADDRESS_VALUES:
        return Directive(False, state.node, "critique",
                         f"--failure-address must be one of {list(FAILURE_ADDRESS_VALUES)} "
                         f"or omitted, got {failure_address!r}")
    norm_targets = list(getattr(args, "norm_targets", None) or [])
    if failure_address == "normative" and not norm_targets:
        return Directive(False, state.node, "critique",
                         "a normative critique must name the norm that failed: pass "
                         "--norm-target stage:<n> or the path of the norm file "
                         "(repeatable), the edit the closing replan has to make")
    if norm_targets and failure_address != "normative":
        return Directive(False, state.node, "critique",
                         "--norm-target names the norm of a normative fault; it is "
                         f"refused with --failure-address {failure_address!r}")
    snapshots, refusal = _snapshot_norm_targets(state, norm_targets)
    if refusal:
        return Directive(False, state.node, "critique", refusal)
    inherited = [t for t in state.difficulty.norm_targets if t.get("source", "critique") != "critique"]
    held = {t.get("target") for t in inherited}
    # A re-critique naming a stage again keeps the baseline of the first snapshot: the
    # bar is the norm as it stood when the fault was first routed to it.
    first_baselines = {t.get("target"): t.get("baseline")
                       for t in state.difficulty.norm_targets
                       if t.get("kind") == "stage" and t.get("source", "critique") == "critique"}
    for t in snapshots:
        if t["kind"] == "stage" and t["target"] in first_baselines:
            t["baseline"] = first_baselines[t["target"]]
    state.difficulty.norm_targets = (
        [t for t in snapshots if t["target"] not in held] + inherited)
    state.difficulty.escalated_to = _escalation_link(state.difficulty, state.parent_link)
    state.difficulty.critique = Critique(
        functional_ground=args.functional_ground,
        replanning_task=args.replanning_task,
        invariants_to_preserve=list(getattr(args, "invariants_to_preserve", None) or []),
        differences_to_remove=list(getattr(args, "differences_to_remove", None) or []),
        failure_address=failure_address,
    )
    state.log(
        "critique",
        functional_ground=state.difficulty.critique.functional_ground,
        replanning_task=state.difficulty.critique.replanning_task,
        invariants_to_preserve=state.difficulty.critique.invariants_to_preserve,
        differences_to_remove=state.difficulty.critique.differences_to_remove,
        failure_address=state.difficulty.critique.failure_address,
    )
    store.save(state)
    # Consult (never fire) the same gate cmd_replan enforces: the record now has all
    # three sections, but the gate also shape-checks them (>=2 distinct hypotheses,
    # non-placeholder declaration fields). Announcing "replan unblocked" without
    # reading the gate drifts the moment either side changes shape.
    blockers = gates.difficulty_blockers(state)
    if blockers:
        action = "investigate" if any("investigation" in b for b in blockers) else "declare"
        return Directive(False, state.node, action, "; ".join(blockers), data={"blockers": blockers})
    if any(t.get("kind") == gates.NORM_TARGET_KIND_PARENT
           for t in state.difficulty.norm_targets):
        return Directive(
            True, state.node, "return_replan",
            "the fault is in the norm of the parent stage this session serves, which this "
            "session cannot edit: end the session with `REPLAN:` naming the mismatch -- the "
            "parent's dispatch verifies this record and opens its own difficulty; do not "
            "replan or normalize here",
            marker="REPLAN",
            data={"norm_target": gates.NORM_TARGET_PARENT,
                  "parent_session": (state.parent_link or {}).get("parent_session"),
                  "stage_index": (state.parent_link or {}).get("stage_index")},
        )
    if state.node != Node.DIAGNOSING.value:
        d = Directive(True, state.node, "normalize",
                      "reflection record complete; record the re-norming (normalize --factor) "
                      "-- an agent-authored review override is then admitted once, with "
                      "this record")
    else:
        d = Directive(True, state.node, "replan",
                      "difficulty cycle complete; replan is now unblocked")
    inv = state.difficulty.investigation
    decl = state.difficulty.declaration
    _attach_advisories(d, "hypothesis_distinctness", {
        "hypotheses": inv.hypotheses if inv else [],
        "declaration": {"expected": decl.expected, "actual": decl.actual, "mismatch": decl.mismatch}
        if decl else {},
    }, runner, weight_class=state.weight_class,
       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    return d


def cmd_normalize(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Phase 4 (closure): record the renorming act. Mandatory at DIAGNOSING closure —
    a reproducible factor left un-normed re-fails, so replan is blocked (see
    gates.normalization_blockers) until this records the factor, or the user takes the
    explicit --normalization-waiver escape for a genuinely one-off factor. The LEVEL
    (note/leaf/principle) is payoff-gated and may be omitted, and so may the DESTINATION —
    the functional place the act lands on. The two are ORTHOGONAL: `--level` says how
    generally the record is written down, `--destination` says what is being repaired, and
    every destination is recordable at every level.

    Also callable OUTSIDE DIAGNOSING (any node from CLASSIFIED up to, but not
    including, RESOLVED) as a plain record-only act -- a normalization worth writing
    down does not always arrive framed as a difficulty closure. That branch touches no
    gate and `state.difficulty` only under a plan-review release (the reflection exit's
    record takes the re-norming), and logs the SAME `normalize` event shape
    with `in_diagnosis=False` so every reader of the DIAGNOSING event still recognizes
    it. Dispatch happens FIRST, before any of this function's own validation, so a
    DIAGNOSING call reaches byte-identical code below."""
    state = _require(store, args.session)
    if state.node != Node.DIAGNOSING.value:
        return _cmd_normalize_outside_diagnosing(args, state, store)
    bad = _require_diagnosing(state)
    if bad:
        return bad
    d = state.difficulty
    if d.declaration is None or d.investigation is None or d.critique is None:
        return Directive(False, state.node, "declare",
                         "normalize is out of order: declaration, investigation, and "
                         "critique must come first")
    waiver = getattr(args, "normalization_waiver", None)
    if waiver is not None:
        return _record_normalization_waiver(args, state, store, waiver)
    error, factor = _normalize_record_fields(
        getattr(args, "factor", None), getattr(args, "level", None), getattr(args, "destination", None))
    if error:
        return Directive(False, state.node, "normalize", error)
    level = getattr(args, "level", None)
    destination = canonical_normalization_destination(getattr(args, "destination", None))
    d.normalization = Normalization(factor=factor, level=level, destination=destination)
    _log_normalize_event(state, factor=factor, level=level, destination=destination, in_diagnosis=True)
    store.save(state)
    return Directive(True, state.node, "replan",
                     "renorming recorded; replan is now unblocked")


def cmd_difficulty_open(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Open a NESTED difficulty under the one being worked: the current record is
    suspended on `state.difficulty_stack` (never overwritten, never discarded) and a fresh
    one, one level deeper, becomes the open record the declare/investigate/critique/
    normalize commands act on. At the depth cap (`max-recursion-depth`) nothing is
    recorded and the user decides -- the state is left exactly as it was."""
    state = _require(store, args.session)
    origin = getattr(args, "origin", None) or "stage"
    if origin not in NESTED_DIFFICULTY_ORIGINS:
        return Directive(False, state.node, "noop",
                         f"difficulty-open --origin must be one of "
                         f"{list(NESTED_DIFFICULTY_ORIGINS)}, got {origin!r}")
    bad = _require_diagnosing(state)
    if bad:
        return bad
    parent = state.difficulty
    opened = _open_difficulty(state, origin, nested=True)
    if opened is None:
        return _difficulty_cap_directive(state, "a nested difficulty cannot be opened")
    state.log("difficulty_open", uid=opened.uid, parent_uid=opened.parent_uid,
              level=opened.level, origin=origin, suspended=parent.uid)
    store.save(state)
    return Directive(
        True, state.node, "declare",
        f"nested difficulty {opened.uid} opened at level {opened.level} under "
        f"{parent.uid}; work it through declare -> investigate -> critique -> normalize, "
        f"then difficulty-close",
        data={"uid": opened.uid, "parent_uid": opened.parent_uid, "level": opened.level,
              "origin": origin},
    )


def cmd_difficulty_close(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Close the open NESTED difficulty and resume its parent. The record must be as
    complete as one closing through `replan` (`gates.difficulty_close_blockers`). The
    norm targets it named -- including ones whose file or stage was already edited -- move
    to the parent, tagged with the closed record's uid, so the parent's closing replan
    still has to deliver every norm edit; the closed record joins the parent's `children`.
    The root difficulty has no parent to resume and closes with `replan`."""
    state = _require(store, args.session)
    blockers = gates.difficulty_close_blockers(state)
    _log_gate(state, "difficulty_close", blockers, passed=not blockers)
    if blockers:
        d = state.difficulty
        if state.node != Node.DIAGNOSING.value:
            action = "noop"
        elif not state.difficulty_stack:
            action = "replan"
        elif d is None or not d.complete():
            action = _resume_action(d) if d else "declare"
        elif d.critique.failure_address is None:
            action = "critique"
        else:
            action = "normalize"
        return Directive(False, state.node, action,
                         "difficulty-close refused: " + "; ".join(blockers),
                         data={"blockers": blockers})
    closed = state.difficulty
    parent = state.difficulty_stack.pop()
    held = {t.get("target"): t for t in parent.norm_targets}
    moved: list[str] = []
    for t in closed.norm_targets:
        entry = dict(t)
        entry["source"] = closed.uid
        if entry.get("target") in held:
            # Both records named the target: the closing child's entry (baseline
            # included) replaces the parent's -- the child's is the later snapshot, and
            # keeping the parent's stale digest would hold the file to an edit the
            # child's own cycle has already accounted for.
            held[entry["target"]].update(entry)
        else:
            parent.norm_targets.append(entry)
        moved.append(entry.get("target", ""))
    # A parent-kind target moved onto `parent` is the child's escalation, now carried by
    # the record the child session will return REPLAN from: it needs the typed link too.
    parent.escalated_to = _escalation_link(parent, state.parent_link)
    parent.children.append(closed)
    state.difficulty = parent
    state.log("difficulty_close", uid=closed.uid, parent_uid=parent.uid, level=closed.level,
              moved_norm_targets=moved)
    store.save(state)
    return Directive(
        True, state.node, _resume_action(parent),
        f"nested difficulty {closed.uid} closed; {parent.uid} (level {parent.level}) is the "
        f"open difficulty again" + (f"; norm targets moved to it: {moved}" if moved else ""),
        data={"closed_uid": closed.uid, "parent_uid": parent.uid, "level": parent.level,
              "moved_norm_targets": moved},
    )


def register(sub, add):
    sp = add("declare"); sp.add_argument("--session", required=True)
    sp.add_argument("--expected", required=True); sp.add_argument("--actual", required=True)
    sp.add_argument("--mismatch", required=True)
    sp = add("investigate"); sp.add_argument("--session", required=True)
    sp.add_argument("--localized-expectation", dest="localized_expectation", required=True)
    sp.add_argument("--localized-actual", dest="localized_actual", required=True)
    sp.add_argument("--hypothesis", dest="hypotheses", action="append", default=None,
                    help="a candidate hypothesis (repeatable; >=2 required for a complete record)")
    sp = add("critique"); sp.add_argument("--session", required=True)
    sp.add_argument("--functional-ground", dest="functional_ground", required=True)
    sp.add_argument("--replanning-task", dest="replanning_task", required=True)
    sp.add_argument("--invariant-to-preserve", dest="invariants_to_preserve",
                    action="append", default=None,
                    help="a similarity the corrected plan must PRESERVE as a condition/"
                         "invariant (repeatable); the engine verifies coverage on replan")
    sp.add_argument("--difference-to-remove", dest="differences_to_remove",
                    action="append", default=None,
                    help="a difference whose removal requires a CHANGED means/method "
                         "(repeatable); the engine verifies a means/method changed on replan")
    sp.add_argument("--failure-address", dest="failure_address", default=None,
                    choices=list(FAILURE_ADDRESS_VALUES) + list(FAILURE_ADDRESS_ALIASES),
                    metavar="{" + ",".join(FAILURE_ADDRESS_VALUES) + "}",
                    help="route the fault to the inadequate provision (R2): resource "
                         "(resource provision — material/tool) | normative "
                         "(normative provision — norm/method) | not_applicable (routing "
                         "does not apply); the original Russian spellings are accepted as aliases; "
                         "the closure gate demands it on a difficulty-closing replan")
    sp.add_argument("--norm-target", dest="norm_targets", action="append", default=None,
                    help="the norm a normative fault names, repeatable and required with "
                         "--failure-address normative: `stage:<n>` (a stage of the current "
                         "plan) or the absolute path of an existing norm file (a linked "
                         "worktree of the instructions repo, a memory leaf, or a file under "
                         "the agent home's skills/ or plans/); the closing replan must "
                         "change it. `parent` (alone, in a session linked to a parent "
                         "stage) names that stage's norm instead: the session hands the "
                         "fault back with REPLAN and the parent's dispatch verifies the "
                         "record")
    sp = add("normalize"); sp.add_argument("--session", required=True)
    sp.add_argument("--factor", default=None,
                    help="the reproducible cause the difficulty exposed, being re-normed "
                         "(the ACT is mandatory at closure); required unless "
                         "--normalization-waiver is given")
    sp.add_argument("--normalization-waiver", dest="normalization_waiver", default=None,
                    help="record the open difficulty as exposing a genuinely one-off factor "
                         "instead of a re-norming; a reason, stored on the record and what "
                         "lets a nested difficulty close without a factor (refused if empty, "
                         "alongside --factor, and for a record that names norm targets)")
    sp.add_argument("--level", dest="level", default=None, choices=list(NORMALIZATION_LEVELS),
                    help="recording level (payoff-gated by rediscovery-threshold-min); omit "
                         "for an in-head note below the leaf threshold")
    sp.add_argument("--destination", dest="destination", default=None,
                    choices=list(NORMALIZATION_DESTINATIONS) + list(NORMALIZATION_DESTINATION_ALIASES),
                    metavar="{" + ",".join(NORMALIZATION_DESTINATIONS) + "}",
                    help="the functional place the renorming lands on — material | tool "
                         "| norm | method | knowledge (the original Russian spellings are "
                         "accepted as aliases); ORTHOGONAL to --level (any destination "
                         "at any level), omit when the place is not being recorded")
    sp = add("difficulty-open"); sp.add_argument("--session", required=True)
    sp.add_argument("--origin", dest="origin", default="stage",
                    choices=list(NESTED_DIFFICULTY_ORIGINS),
                    help="why the nested difficulty exists: it arose while replanning "
                         "(replan), while re-norming (normalize), or from the stage's "
                         "own work (stage)")
    sp = add("difficulty-close"); sp.add_argument("--session", required=True)
