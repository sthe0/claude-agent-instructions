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

from . import plan, premise
from .plugins import Plugin, PluginDirective, register
from .state import WeightClass


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
       later replan.
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
        stage_keys = {}
        content_digest = None

    questions = premise.questions_from_dicts(bag.get("questions", []))
    candidates = premise.question_candidates_from_dicts(bag.get("candidates", []))
    blockers = premise.validate_questions(questions, stage_keys=stage_keys)
    blockers += premise.validate_question_candidates(candidates, questions)

    if not bag.get("enumerated"):
        blockers.append(_ENUMERATE_NOT_RUN)
    elif content_digest is not None and bag.get("enumerated_at") != content_digest:
        blockers.append(_ENUMERATE_STALE)

    return blockers


def _premise_gate(state, bag) -> list[str]:
    return premise_blockers(state, bag)


def _observe_approve(state, bag) -> list[PluginDirective]:
    blockers = _premise_gate(state, bag)
    if not blockers:
        return []
    return [PluginDirective(
        "premise", "close_questions",
        "dispose every open question and run the enumeration cross-check before "
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
            "enumerated": False,
            "enumerated_at": "",
            "enumerated_runner_ok": None,
            "enumerated_count": None,
        },
    )
)
