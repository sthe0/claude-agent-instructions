"""The question-provenance plugin: binds every question raised during substantive
plan construction to the content element that produced it, and blocks approval
while any raised question is still open or the independent enumeration
cross-check has not run against the CURRENT plan content.

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

import hashlib
import os

from . import advisor, gates, plan, premise
from .plugins import Plugin, PluginDirective, register
from .state import PLAN_PRESENTATION_KIND_ESSENCE, WeightClass


def _auto_activate(state) -> bool:
    """Arm for EVERY substantive session — weight_class alone, no deliverable_kind
    condition (the gap-2 fix). AGENTCTL_PREMISE is a test-seam that overrides in both
    directions ("1" forces on, "0" forces off), mirroring gates.plan_review_active's
    AGENTCTL_PLAN_REVIEW knob: it lets the suite at large default the gate off (the
    premise gate fail-closes `approve` and its discharge verbs land in a later stage,
    so every substantive-cycle e2e test would otherwise wedge at approve). Env-unset
    — every real session — resolves to the plain weight_class predicate."""
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
    """Whether an escape from `reasons` is on record against THIS plan content.

    Bound to the content digest for the same reason `enumerated_at` is: an escape
    is a statement about one plan's one failed pass, and letting it survive an edit
    of the plan would discharge the cross-check forever after a single infra blip —
    the precise fail-open shape this blocker exists to close. A bag with no
    `escapes` key at all (minted before this half existed) reads as no escape, not
    as a KeyError.

    No digest (no plan submitted) means no escape is admissible — which is not the
    liveness hole it looks like: `approve` is only reachable from PLAN_READY, and
    reaching PLAN_READY runs the launch that clears the enumeration record back to
    not-run, so an escapable blocker at the approve gate always has a plan to bind
    to."""
    if not content_digest:
        return False
    for record in bag.get("escapes", []) or []:
        if record.get("content_digest") == content_digest and record.get("reason") in reasons:
            return True
    return False


def _plan_content_digest(doc: "plan.PlanDoc") -> str:
    """A digest of the plan's PARSED content (post-tomllib), so a TOML comment-only
    edit — which tomllib never surfaces as a field — is already a no-op here
    without any extra comment-stripping logic. Reuses `stage_question_key` per
    stage rather than re-deriving a parallel notion of 'stage bytes'."""
    payload = repr((
        doc.meta.goal,
        doc.meta.done_criterion,
        doc.meta.criterion_type,
        doc.meta.weight_class,
        doc.meta.repo_root,
        tuple(sorted((s.index, plan.stage_question_key(s)) for s in doc.stages)),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def coverage_block(state, bag, *, doc=None) -> str | None:
    """The scope-coverage block the presented essence must carry — the plan's stage
    count plus what it does with each element of the order — or None when no plan is
    submitted yet (nothing to size, nothing to cover). `doc` is an already-loaded
    PlanDoc when the caller has one (premise_blockers does), so the block is derived
    from the same bytes its other checks used. premise.render_coverage_block is the
    single generator; this only supplies its two inputs."""
    plan_path = getattr(state, "plan_path", None)
    if not plan_path:
        return None
    if doc is None:
        doc = plan.load_plan(plan_path)
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    return premise.render_coverage_block(elements, len(doc.stages))


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
       has, that it ran against the plan content AS IT CURRENTLY STANDS
       (bag['enumerated_at'] == the live content digest) — otherwise one
       enumerate call would silently discharge the flag forever across every
       later replan — and that the run it recorded did not FAIL. The three are
       one if/elif chain, not three independent tests, because a relaunch clears
       `enumerated` back to not-run while leaving the SUPERSEDED pass's
       `enumerated_runner_ok` behind: firing the runner-failure blocker there
       would demand an escape for a failure that a currently-running child may
       be about to replace. So the failure branch speaks only for the pass that
       landed against the content now under evaluation; the relaunch window is
       _ENUMERATE_NOT_RUN's, with `enumeration_not_landed` as its route out.
       Both escapable branches clear on an escape bound to the LIVE digest;
       _ENUMERATE_STALE has no escape and needs none — re-running the check is
       always available and always cheaper than recording a reason.
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
        stage_keys = {s.index: plan.stage_question_key(s) for s in doc.stages}
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
    elif content_digest is not None and bag.get("enumerated_at") != content_digest:
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
            "enumerated_runner_ok": None,
            # The failed run's own stderr, carried from the pass that produced
            # enumerated_runner_ok so the blocker can pre-select the escape reason
            # instead of asking the operator to pick one blind.
            "enumerated_runner_stderr": "",
            "enumerated_count": None,
            # Typed escapes from the enumeration blockers, each bound to the plan
            # content digest it was recorded against (see escape_recorded).
            "escapes": [],
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
