"""The question-provenance plugin: binds every question raised during substantive
plan construction to the content element that produced it, and blocks approval
while any raised question is still open, the independent enumeration cross-check
has not run against the CURRENT plan content, or it HAS run against that content
and its runner FAILED. The third condition is the one that used to discharge
itself: the flag flipped because the pass ran, whatever it returned. Its route out
is `agentctl question-enumerate-escape --reason <closed-set value>`, one countable
row naming why — never silence.

Gap-2 arming fix: `plugins_ledger`'s claim-provenance discipline arms only when
`deliverable_kind` is 'reasoning'/'mixed' (state.py defaults it to '' at classify),
so an ordinary engineering plan — the common case, and the one the arming gap was
actually about — never gets it. This plugin's `_auto_activate` is `weight_class ==
SUBSTANTIVE` alone, nothing else: every substantive session gets a premise bag,
regardless of what it delivers.

Division of labour, mirroring plugins_ledger: this module wires the pure
`premise.validate_questions` / `premise.validate_question_candidates` checks (F6's
per-question closure) to the `plan_approval` core gate — NOT `resolution`, because a
smuggled premise is a plan-construction-time defect, not a delivery-time one. It
never judges a question's content, only whether it has been closed against the
CURRENT plan bytes.

No terminal predicate (deliberately, unlike `dummy`): a terminal firing at `approve`
would archive the bag, and the gate would then never fire again on a replanned
plan — exactly the hole stage 3 closes in cmd_replan's plugin-gate composition.
Adding a terminal here would reopen, at the plugin layer, the hole being closed at
the CLI layer. This plugin is `scope='task'`, retired only at the task boundary."""
from __future__ import annotations

import os
import time

from . import advisor, gates, plan, premise
from .plugins import Plugin, PluginDirective, register
from .state import PLAN_PRESENTATION_KIND_ESSENCE, WeightClass


def _auto_activate(state) -> bool:
    """Arm for EVERY substantive session — weight_class alone, no deliverable_kind
    condition (the gap-2 fix). AGENTCTL_PREMISE is a test-seam that overrides in both
    directions ("1" forces on, "0" forces off), mirroring gates.plan_review_active's
    AGENTCTL_PLAN_REVIEW knob: it lets the suite at large default the gate off (the
    premise gate fail-closes `approve`, so every substantive-cycle e2e test would
    otherwise have to drive the discharge verbs — question-dispose, order-dispose,
    question-enumerate — to reach approve at all). Env-unset — every real session —
    resolves to the plain weight_class predicate."""
    env = os.environ.get("AGENTCTL_PREMISE")
    if env == "1":
        return True
    if env == "0":
        return False
    return getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value


_ENUMERATE_NOT_RUN = (
    "question enumeration cross-check not run — run `agentctl question-enumerate`"
)
_ENUMERATE_STALE = (
    "question enumeration cross-check ran against different plan content — "
    "re-run `agentctl question-enumerate`"
)
_ENUMERATE_RUNNER_FAILED = (
    "question enumeration cross-check ran but its runner FAILED — record a typed escape "
    "with `agentctl question-enumerate-escape --reason <reason> --note <text>`"
)


def _runner_failed_blocker(bag) -> str:
    """The runner-failure blocker, carrying the reason the ENGINE already knows from
    the failed run's own stderr. Pre-selecting it turns the escape into a
    confirmation rather than a free choice among five tokens — the operator reads
    back a value derived from the evidence instead of guessing which one fits."""
    reason = advisor.classify_runner_failure(bag.get("enumerated_runner_stderr", "") or "")
    return f"{_ENUMERATE_RUNNER_FAILED} — the stderr reads as `--reason {reason}`"


def escape_recorded(bag, content_digest, reasons) -> bool:
    """Whether an escape from `reasons` is on record against THIS plan content, THIS
    launch window and THIS pass count.

    Bound to the content digest for the same reason `enumerated_at` is: an escape
    is a statement about one plan's one failed pass, and letting it survive an edit
    of the plan would discharge the cross-check forever after a single infra blip —
    the precise fail-open shape this blocker exists to close. A bag with no
    `escapes` key at all (minted before this half existed) reads as no escape, not
    as a KeyError.

    The digest alone implements one plan's EVERY pass, which is the same fail-open
    one level in, so the row binds to two counters as well. `enumerate_launch` is
    bumped by every `_launch_enumeration`, so a resubmit or a replan that opens a
    NEW window over the same bytes no longer inherits the previous window's escape —
    a window with no enumeration, no wait and no escape row would otherwise approve
    for free. `enumerate_pass` is bumped by every applied enumeration result, so a
    SECOND failed pass at the same digest re-blocks and is counted; without it
    `escape_counts` — this work's own refutation instrument — undercounts, and a
    counter that undercounts cannot refute anything.

    An INTEGER counter, not `enumerate_deadline`: the deadline is restamped by every
    launch and by nothing else, so it would serve as an identity right up until two
    launches land in the same clock tick or the deadline becomes configurable. A row
    carrying NEITHER counter (minted before they existed) does not match — fail
    CLOSED, because this is a fail-open fix and ambiguity must not discharge. The
    cost is one extra escape recording on a session carried across the change; the
    cost of the other choice is the hole staying open for exactly the bags most
    likely to have one.

    No digest (no plan submitted) means no escape is admissible — which is not the
    liveness hole it looks like: `approve` is only reachable from PLAN_READY, and
    reaching PLAN_READY runs the launch that clears the enumeration record back to
    not-run, so an escapable blocker at the approve gate always has a plan to bind
    to."""
    if not content_digest:
        return False
    launch = int(bag.get("enumerate_launch") or 0)
    passes = int(bag.get("enumerate_pass") or 0)
    for record in bag.get("escapes", []) or []:
        if (record.get("content_digest") == content_digest
                and record.get("reason") in reasons
                and record.get("enumerate_launch") == launch
                and record.get("enumerate_pass") == passes):
            return True
    return False


def _tally(records) -> dict:
    """Three buckets, counted SEPARATELY, because they are three different facts about
    the fleet and each calls for a different fix. `runner_failure` — the pass landed and
    its runner broke — is an advisor-reliability work item. `not_landed` — no pass ever
    arrived — is a detachment-liveness one. `manual` — the pass failed AND a coordinator
    re-read the plan by hand — is neither: it is the gate working as designed, at cost.

    The last split is the one this stage's own refutation turns on ("refuted if the
    escape degrades into a click-through"): a fleet escaping via `manual` did the work
    the enumeration exists to do, a fleet escaping via `advisor_timeout` did not, and a
    single runner-failure total reports them as the same number. `manual` stays inside
    premise.ENUMERATION_RUNNER_FAILURE_REASONS — admissibility is unchanged, since it
    too speaks only for a run that actually failed; only the counting splits.

    The infra/work-was-done distinction is read off `premise.ENUMERATION_INFRA_FAILURE_
    REASONS` — the closed set that NAMES the infra subset — rather than re-derived here
    as "in the wider family and not manual". A two-clause condition tracks the CURRENT
    membership of the family by exclusion; a future infra reason added only to the wider
    tuple would land in this bucket correctly under either form, but a future WORK-WAS-
    DONE reason (a second `manual`-shaped token) would silently fall into `runner_failure`
    under the exclusion form and nowhere under this one — it would need its own bucket,
    which is the failure this split exists to avoid."""
    reasons = [r.get("reason") for r in records]
    return {
        "runner_failure": sum(
            1 for reason in reasons if reason in premise.ENUMERATION_INFRA_FAILURE_REASONS),
        "manual": sum(
            1 for reason in reasons if reason == premise.ESCAPE_MANUAL_ENUMERATION_DONE),
        "not_landed": sum(
            1 for reason in reasons if reason == premise.ESCAPE_ENUMERATION_NOT_LANDED),
    }


def escape_counts(bag, content_digest) -> dict:
    """How often this gate has been escaped, on two axes — the single derivation both
    surfaces (`agentctl status` and the plan_approval directive) read, so neither can
    drift into its own idea of what an escape is. Always returns a dict — never None;
    only its `this_plan` entry can be (see below).

    This stage's own refutation is the escape rate itself: an escape nobody can count
    is the fail-open it replaced, one level up. So the numbers are the deliverable.

    `this_plan` is the number that means something AT THE GATE — a plan on its fourth
    escape is a different object from one on its first. `session` is informational and
    resets with `agentctl reset`, which is exactly why the per-digest count exists
    beside it rather than instead of it.

    Takes a real bag: whether the premise plugin is armed at all is decided by
    cli._enumeration_escape_counts, the only caller, which returns its own None before
    reaching here. `this_plan` is still None — 'not applicable', never 'measured zero'
    — when no plan is submitted, since there is no plan version for a per-version count
    to be about; its `session` half is a real zero and says so.

    `.get` with defaults throughout: a bag minted before `escapes` existed reads as
    zero, never KeyError."""
    records = bag.get("escapes", []) or []
    return {
        "this_plan": (
            _tally([r for r in records if r.get("content_digest") == content_digest])
            if content_digest else None
        ),
        "session": _tally(records),
    }


def _plan_content_digest(doc: "plan.PlanDoc") -> str:
    """A digest of the plan's PARSED content (post-tomllib), so a TOML comment-only
    edit — which tomllib never surfaces as a field — is already a no-op here
    without any extra comment-stripping logic. Reuses the per-stage whole-stage key
    rather than re-deriving a parallel notion of 'stage bytes', and
    `plan.order_place` for the order rather than re-deriving a notion of 'order
    bytes': it is the wider of the two order keys, so a re-wording, an added or
    renamed requirement id, and a coverage-key change all move the digest. A
    question is raised against the statement of what the plan is FOR; an order
    rewritten under a discharged enumeration is exactly the staleness this digest
    exists to catch.

    The composition lives in `plan.plan_content_digest` beside the per-part digests
    it recomposes; this name is what the escape rows, the launch window and every
    persisted `enumerated_at` were written against, so it stays."""
    return plan.plan_content_digest(doc)


def enumeration_baseline(bag) -> dict:
    """The per-part digests the recorded enumeration ran against, in the shape
    `plan.changed_parts` compares against."""
    return {
        "meta": bag.get("enumerated_meta_at") or "",
        "stages": bag.get("enumerated_stage_at") or {},
    }


def stale_enumeration_parts(bag, doc) -> tuple[bool, set[int]]:
    """Which parts of `doc` the recorded enumeration no longer speaks for.

    A bag carrying NEITHER part digest predates the per-part split and can only be
    judged whole: reading its empty maps as "no part enumerated" would flip every
    already-discharged live session to _ENUMERATE_STALE on its next call."""
    baseline = enumeration_baseline(bag)
    if not baseline["meta"] and not baseline["stages"]:
        if bag.get("enumerated_at") == plan.plan_content_digest(doc):
            return False, set()
        return True, set(plan.plan_stage_digests(doc))
    return plan.changed_parts(doc, baseline)


def enumeration_is_stale(bag, doc) -> bool:
    meta_stale, stale_stages = stale_enumeration_parts(bag, doc)
    return meta_stale or bool(stale_stages)


def enumeration_run_scope(bag, doc) -> tuple[bool, set[int]]:
    """The parts a re-run must cover — `(whole_plan, {stage indices})`.

    Narrowed to the stages that moved only when a pass has landed and the plan's
    meta is unchanged; every other case reads the whole plan, so a first pass and an
    explicitly re-requested one behave exactly as they did before the split. A moved
    goal / done criterion / order re-opens every stage's fit to it, which is why a
    meta move widens rather than adding a part."""
    meta_stale, stale_stages = stale_enumeration_parts(bag, doc)
    if stale_stages and not meta_stale and bag.get("enumerated"):
        return False, stale_stages
    return True, set(plan.plan_stage_digests(doc))


def _enumeration_in_flight(bag) -> bool:
    """Whether a background enumeration launch is outstanding right now: armed (a
    launch actually went out), not yet landed, and still inside its deadline. This
    is the disclosure half of #60's residual gap — cmd_approve's escape-deadline
    logic already blocks correctly on this exact window (_ENUMERATE_NOT_RUN /
    _ENUMERATE_STALE), so nothing here changes what `approve` allows; the gap was
    that a reader of the presented essence had no signal a background pass could
    still add premise questions before approval is reachable.

    Deliberately silent on a launch whose deadline has already elapsed: that
    window is `enumeration_not_landed`'s to name (via
    `question-enumerate-escape`), not this disclosure line's — conflating the two
    would claim a pass is "in flight" for one that has, in fact, gone missing."""
    launch = int(bag.get("enumerate_launch") or 0)
    if launch <= 0 or bag.get("enumerated"):
        return False
    deadline = bag.get("enumerate_deadline")
    if deadline is None:
        return False
    return time.time() < float(deadline)


def _enumeration_in_flight_line(bag) -> str:
    launch = int(bag.get("enumerate_launch") or 0)
    return (
        f"- enumeration in flight: background cross-check (launch {launch}) has "
        "not landed yet — approving now may miss questions it would still raise; "
        "wait for it to land, or run `agentctl question-enumerate` to run it "
        "synchronously"
    )


def coverage_block(state, bag, *, doc=None) -> str | None:
    """The scope-coverage block the presented essence must carry — the plan's stage
    count, what it does with each element of the order, every LIVE risk
    acceptance discharging a `revise` concern, and (when one is outstanding) the
    in-flight-enumeration disclosure line — or None when no plan is submitted yet
    (nothing to size, nothing to cover). `doc` is an already-loaded PlanDoc when the
    caller has one (premise_blockers does), so the block is derived from the same
    bytes its other checks used. premise.render_coverage_block generates the
    scope/order/risk lines; the in-flight line is appended here because it reads
    bag fields (enumerate_launch/enumerated/enumerate_deadline) render_coverage_block
    has no access to — same division of labour as the risk-staleness filtering
    below (premise.py cannot do this itself — it has no access to gates/state/plan).
    Appended the same way coverage_block_missing_lines already picks up every other
    line: mechanical containment of engine-generated text, never a semantic read."""
    plan_path = getattr(state, "plan_path", None)
    if not plan_path:
        return None
    if doc is None:
        doc = plan.load_plan(plan_path)
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    accepted_risks = [
        (ra.scope, ra.concern_id, ra.concern_text, ra.basis, ra.risk, ra.author,
         gates._risk_acceptance_superseded(ra, state))
        for ra in getattr(state, "risk_acceptances", [])
        if not gates._risk_acceptance_stale(ra, doc)
    ]
    block = premise.render_coverage_block(elements, len(doc.stages), accepted_risks=accepted_risks)
    if _enumeration_in_flight(bag):
        block += "\n" + _enumeration_in_flight_line(bag)
    return block


def coverage_block_missing_lines(block: str, rendering_text: str) -> list[str]:
    """Which of the block's lines a rendering does not carry. A MECHANICAL
    containment check over ENGINE-GENERATED text — the same form as
    cmd_present_plan's `--kind full` stage-anchor completeness check — never a
    semantic read of the essence's own prose. Lines are compared stripped, so the
    surrounding indentation or markdown context is free; the generated line's own
    text must appear intact."""
    present = {line.strip() for line in rendering_text.splitlines()}
    return [
        line for line in block.splitlines()
        if line.strip() and line.strip() not in present
    ]


def _essence_coverage_blocker(missing: list[str]) -> str:
    return (
        "the presented essence does not carry the current scope-coverage block "
        "(the order bag changed after it was presented) — missing: "
        + "; ".join(missing)
        + " — re-present with `agentctl present-plan --kind essence` (`agentctl "
        "order-list --format md` prints the block to paste)"
    )


def premise_blockers(state, bag) -> list[str]:
    """The full plan_approval-gate blocker set for a premise bag, so the read-only
    `question-check` command (stage 4) and the gate never diverge (the
    plugins_ledger.ledger_blockers precedent).

    1. per-question closure (premise.validate_questions), keyed against the
       CURRENT plan's per-stage keys — loaded fresh from `state.plan_path` rather
       than trusting `state.stages`, because the enumeration-staleness check (3)
       below needs the same freshly-parsed doc to compute its content digest, and
       a single load keeps both checks against identical bytes. `state.plan_path`
       is only ever set by cmd_submit_plan after a successful `load_plan`, so a
       set-but-unparseable path is not a state this gate needs to defend against.
    2. candidate disposition-completeness (premise.validate_question_candidates);
    3. the enumeration cross-check has RUN at all (bag['enumerated']) and, if it
       has, that no PART of the plan has moved since the pass that covered it
       (stale_enumeration_parts) — otherwise one enumerate call would silently
       discharge the flag forever across every later replan — and that the run it
       recorded did not FAIL. The three are
       one if/elif chain, not three independent tests, because a relaunch clears
       `enumerated` back to not-run while leaving the SUPERSEDED pass's
       `enumerated_runner_ok` behind: firing the runner-failure blocker there
       would demand an escape for a failure that a currently-running child may
       be about to replace. So the failure branch speaks only for the pass that
       landed against the content now under evaluation; the relaunch window is
       _ENUMERATE_NOT_RUN's, with `enumeration_not_landed` as its route out.
       Both escapable branches clear on an escape bound to the LIVE digest;
       _ENUMERATE_STALE has no escape below the round budget — re-running the
       check is available and cheaper than recording a reason. At or above it
       (`gates.plan_enumerate_round_release_active`) the branch routes to the
       round-release message instead, and `enumerate_rounds_exhausted` is its
       escape: re-running clears staleness per step but re-arms it over the loop.
    4. order coverage (premise.validate_order_elements): every element of the order
       is covered by a stage the CURRENT plan contains, or cut with a reason. Unlike
       (1) an EMPTY bag blocks here, but only once a plan exists — before
       submit-plan there is nothing to check coverage of.
    5. the essence ACTUALLY PRESENTED to the user carries the CURRENT coverage
       block (coverage_block_missing_lines against the receipt's rendering_text).
       Surfacing the cut list and satisfying the gate are two different acts: a
       block that was in the rendering when the receipt was stamped says nothing
       about an element cut afterwards, which is why the block is re-derived here
       from live state rather than trusted from the receipt's plan_sha256 binding.
    Skips both the stage-key binding checks and the staleness check when no plan
    has been submitted yet (`state.plan_path` empty) — there is nothing to key
    against, and premise.validate_questions already tolerates an empty
    `stage_keys` map for exactly this case.
    """
    plan_path = getattr(state, "plan_path", None)
    if plan_path:
        doc = plan.load_plan(plan_path)
        stage_keys = {s.index: plan.stage_element_keys(s) for s in doc.stages}
        content_digest = _plan_content_digest(doc)
    else:
        doc = None
        stage_keys = {}
        content_digest = None

    questions = premise.questions_from_dicts(bag.get("questions", []))
    candidates = premise.question_candidates_from_dicts(bag.get("candidates", []))
    # `.get` with a default, not `bag["order_elements"]`: a premise bag minted before
    # the order-coverage half existed must load, not KeyError.
    order_elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    blockers = premise.validate_questions(questions, stage_keys=stage_keys)
    blockers += premise.validate_question_candidates(candidates, questions)
    blockers += premise.validate_order_elements(
        order_elements, stage_indices=set(stage_keys), plan_present=bool(plan_path)
    )

    if not bag.get("enumerated"):
        if not escape_recorded(bag, content_digest,
                               (premise.ESCAPE_ENUMERATION_NOT_LANDED,)):
            blockers.append(_ENUMERATE_NOT_RUN)
    elif content_digest is not None and enumeration_is_stale(bag, doc):
        if gates.plan_enumerate_round_release_active(bag):
            if not escape_recorded(bag, content_digest,
                                   (premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED,)):
                blockers.append(
                    gates.PLAN_ENUMERATE_ROUND_RELEASE_MESSAGE.format(
                        passes=int(bag.get("enumerate_pass") or 0)
                    )
                )
        else:
            blockers.append(_ENUMERATE_STALE)
    elif bag.get("enumerated_runner_ok") is False:
        # `is False`, never `is not True`: None means the advisor was ABSENT (also what
        # `.get` yields for a bag minted before this field existed, and what the suite's
        # injected stubs leave behind), and folding that into the failure branch would
        # newly block sessions whose runner never failed.
        if not escape_recorded(bag, content_digest,
                               premise.ENUMERATION_RUNNER_FAILURE_REASONS):
            blockers.append(_runner_failed_blocker(bag))

    if doc is not None and gates.plan_presentation_active(state):
        receipt = gates._plan_presentation_for(state, PLAN_PRESENTATION_KIND_ESSENCE)
        # Silent when NO essence receipt exists, and when the one that exists
        # presents another plan: both are already gates.plan_presentation_blockers'
        # own refusals, each carrying its own route out, and a second blocker here
        # would leave a refusal whose route belongs to another gate. What this half
        # DOES catch is the window that gate structurally cannot see — an element
        # cut (or covered, or raised) AFTER the essence was presented leaves the
        # receipt's plan_sha256 valid, because the order bag is not plan bytes.
        if receipt is not None and receipt.plan_path == plan_path:
            missing = coverage_block_missing_lines(
                coverage_block(state, bag, doc=doc), receipt.rendering_text)
            if missing:
                blockers.append(_essence_coverage_blocker(missing))

    return blockers


def _premise_gate(state, bag) -> list[str]:
    return premise_blockers(state, bag)


def _observe_approve(state, bag) -> list[PluginDirective]:
    blockers = _premise_gate(state, bag)
    if not blockers:
        return []
    return [PluginDirective(
        "premise", "close_questions",
        "dispose every open question, cover or cut every element of the order, and "
        "run the enumeration cross-check before "
        f"approving — blockers: {'; '.join(blockers)} (use `agentctl question-raise "
        "...`, `agentctl question-research ...`, `agentctl question-dispose ...`, "
        "`agentctl question-enumerate`, then `agentctl question-check` to confirm "
        "closure)",
        blocking=True,
    )]


register(
    Plugin(
        name="premise",
        scope="task",
        auto_activate=_auto_activate,
        observers={"approve": _observe_approve},
        gates={"plan_approval": _premise_gate},
        state_factory=lambda: {
            "questions": [],
            "candidates": [],
            "order_elements": [],
            "enumerated": False,
            "enumerated_at": "",
            # The per-part digests the recorded pass covered: the plan's meta/order,
            # and one entry per stage index (as a string — these round-trip through
            # JSON). Both empty means "no part enumerated" for a bag minted since the
            # split and "judge by enumerated_at alone" for one minted before it; the
            # two are told apart in stale_enumeration_parts, which is the only reader.
            "enumerated_meta_at": "",
            "enumerated_stage_at": {},
            "enumerated_runner_ok": None,
            # The failed run's own stderr, carried from the pass that produced
            # enumerated_runner_ok so the blocker can pre-select the escape reason
            # instead of asking the operator to pick one blind.
            "enumerated_runner_stderr": "",
            "enumerated_count": None,
            # Typed escapes from the enumeration blockers, each bound to the plan
            # content digest, the launch window and the pass count it was recorded
            # against (see escape_recorded).
            "escapes": [],
            # Monotonic count of detached-worker launches, bumped by cli.py's
            # _launch_enumeration, and the digest the latest one was launched for.
            # Together they identify ONE launch window: the counter is what an escape
            # binds to, the digest is what tells a retried `replan` that a window over
            # these exact bytes is already outstanding and must not be reopened.
            "enumerate_launch": 0,
            "enumerate_launch_digest": "",
            # Monotonic count of enumeration results APPLIED to this bag (both the
            # synchronous command and the sidecar fold go through
            # cli._apply_enumeration_result). An escape binds to it so a second failed
            # pass at the same digest re-blocks instead of riding the first's escape.
            "enumerate_pass": 0,
            # Absolute epoch (launch instant + advisor.ENUMERATE_TIMEOUT_S), stamped by
            # cli.py's _launch_enumeration on every detached-worker launch. None until
            # the first launch. Read by cmd_question_enumerate_escape to decide whether
            # an outstanding _ENUMERATE_NOT_RUN blocker has aged past the deadline into
            # its `enumeration_not_landed` escape; premise_blockers itself does not
            # consult it — the deadline decides whether the ESCAPE is admissible, not
            # whether the blocker fires.
            "enumerate_deadline": None,
        },
    )
)
