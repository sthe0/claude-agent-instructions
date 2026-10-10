"""The question-provenance plugin: binds every question raised during substantive
plan construction to the content element that produced it, and blocks approval
while any raised question — or candidate, including the `qrev-` candidates a review
act records from its customer questions — is still open, or an element of the order
is neither covered nor cut. There is no enumeration cross-check any more: the
standalone enumerator, its detached worker and its runner-health blocker are retired
for new plans (amendments-2.md E3), and a review act returns the customer questions
the cross-check used to produce. Legacy bags still carry the enumeration fields
(`qenum-` candidates, `enumerated*`, escapes); they load and validate, and a raised
`qenum-` candidate still blocks.

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

from . import gates, plan, premise
from .plugins import Plugin, PluginDirective, register
from .state import PLAN_PRESENTATION_KIND_ESSENCE, WeightClass


def _auto_activate(state) -> bool:
    """Arm for EVERY substantive session — weight_class alone, no deliverable_kind
    condition (the gap-2 fix). AGENTCTL_PREMISE is a test-seam that overrides in both
    directions ("1" forces on, "0" forces off), mirroring gates.plan_review_active's
    AGENTCTL_PLAN_REVIEW knob: it lets the suite at large default the gate off (the
    premise gate fail-closes `approve`, so every substantive-cycle e2e test would
    otherwise have to drive the discharge verbs — question-dispose, order-dispose,
    question-candidate-dispose — to reach approve at all). Env-unset — every real session —
    resolves to the plain weight_class predicate."""
    env = os.environ.get("AGENTCTL_PREMISE")
    if env == "1":
        return True
    if env == "0":
        return False
    return getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value


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


def coverage_block(state, bag, *, doc=None) -> str | None:
    """The scope-coverage block the presented essence must carry — the plan's stage
    count, what it does with each element of the order, and every LIVE risk
    acceptance discharging a `revise` concern — or None when no plan is submitted
    yet (nothing to size, nothing to cover). `doc` is an already-loaded PlanDoc when
    the caller has one (premise_blockers does), so the block is derived from the same
    bytes its other checks used. premise.render_coverage_block generates the
    scope/order/risk lines; the risk-staleness filtering is done here because
    premise.py has no access to gates/state/plan. Picked up the same way
    coverage_block_missing_lines picks up every other line: mechanical containment
    of engine-generated text, never a semantic read."""
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
    return premise.render_coverage_block(elements, len(doc.stages), accepted_risks=accepted_risks)


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


def premise_blockers(state, bag, *, include_essence_coverage: bool = True) -> list[str]:
    """The full plan_approval-gate blocker set for a premise bag, so the read-only
    `question-check` command (stage 4) and the gate never diverge (the
    plugins_ledger.ledger_blockers precedent).

    `include_essence_coverage` defaults True, preserving `approve`'s own
    unfiltered gating exactly. `present-plan --kind essence` is the one caller
    that passes False: half (5) below compares the essence receipt against
    itself, which is circular before that receipt has been stamped.

    1. per-question closure (premise.validate_questions), keyed against the
       CURRENT plan's per-stage keys — loaded fresh from `state.plan_path` rather
       than trusting `state.stages`. `state.plan_path` is only ever set by
       cmd_submit_plan after a successful `load_plan`, so a set-but-unparseable
       path is not a state this gate needs to defend against.
    2. candidate disposition-completeness (premise.validate_question_candidates) —
       every raised candidate, whichever route recorded it: a `qrev-` candidate
       recorded from a review act's customer questions or a legacy `qenum-` one.
    3. (retired) the enumeration cross-check — run, current, runner-healthy or
       escaped. The numbering of the parts below is kept so the references to them
       in the engine's refusals and tests stay stable.
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
    Skips the stage-key binding checks when no plan has been submitted yet
    (`state.plan_path` empty) — there is nothing to key against, and
    premise.validate_questions already tolerates an empty `stage_keys` map for
    exactly this case.
    """
    plan_path = getattr(state, "plan_path", None)
    if plan_path:
        doc = plan.load_plan(plan_path)
        stage_keys = {s.index: plan.stage_norm_keys(s) for s in doc.stages}
        meta_keys = plan.plan_meta_element_keys(doc)
    else:
        doc = None
        stage_keys = {}
        meta_keys = {}

    questions = premise.questions_from_dicts(bag.get("questions", []))
    candidates = premise.question_candidates_from_dicts(bag.get("candidates", []))
    # `.get` with a default, not `bag["order_elements"]`: a premise bag minted before
    # the order-coverage half existed must load, not KeyError.
    order_elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    blockers = premise.validate_questions(
        questions, stage_keys=stage_keys, meta_keys=meta_keys
    )
    blockers += premise.validate_question_candidates(candidates, questions)
    blockers += premise.validate_order_elements(
        order_elements,
        stage_indices=set(stage_keys),
        plan_present=bool(plan_path),
        stage_keys=stage_keys,
    )

    if include_essence_coverage and doc is not None and gates.plan_presentation_active(state):
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
        "dispose every open question, cover or cut every element of the order "
        f"before approving — blockers: {'; '.join(blockers)} (use `agentctl "
        "question-raise ...`, `agentctl question-research ...`, `agentctl "
        "question-dispose ...`, `agentctl question-candidate-dispose ...`, then "
        "`agentctl question-check` to confirm closure)",
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
            # Content hashes (premise.dismissal_hash) of candidate statements a
            # COORDINATOR dismissed (cmd_question_candidate_dispose --as dismissed),
            # each mapped to {reason, from_id}. Populated only by a genuine
            # coordinator dismissal; read by the legacy `qenum-` upsert only.
            "dismissed_hashes": {},
            "order_elements": [],
            # The enumeration fields below are LEGACY: no engine path writes them for a
            # new plan (the standalone enumerator is retired); they stay so a bag minted
            # before the retirement loads, validates and reports unchanged.
            "enumerated": False,
            "enumerated_at": "",
            "enumerated_meta_at": "",
            "enumerated_stage_at": {},
            "enumerated_stage_elements": {},
            "enumerated_runner_ok": None,
            "enumerated_runner_stderr": "",
            "enumerated_count": None,
            "escapes": [],
            "enumerate_launch": 0,
            "enumerate_launch_digest": "",
            "enumerate_pass": 0,
            "enumerate_deadline": None,
        },
    )
)
